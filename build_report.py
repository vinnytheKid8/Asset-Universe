"""Build the universe rotation HTML report from the run_screen.py artifacts.

    python build_report.py            # -> reports/universe_review.html
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
DATA, REPORTS = HERE / "data", HERE / "reports"

# ---- gates (see FRAMEWORK.md §4.1) -----------------------------------------
G_ADV, G_PERSIST, G_VENUES, G_OI = 5e6, 0.60, 3, 3e6
NEW_LISTING_DAYS = 21
TICK_PINNED_RATIO = 1.15          # spread <= tick * this  => pinned at one tick

# ---- palette (validated: all-pairs PASS, light & dark) ----------------------
C = dict(s1="#2a78d6", s2="#eb6834", s3="#1baf7a",
         s1d="#3987e5", s2d="#d95926", s3d="#199e70")


# ============================================================ scoring
# Every tunable in one place so the dashboard can sweep them against this exact
# code path rather than reimplementing the scoring and drifting from it.
DEFAULTS = dict(
    g_adv=G_ADV,                # $/day, 30d median (7d for lane A)
    g_persist=G_PERSIST,        # share of days above $5M
    g_venues=G_VENUES,          # perp venues carrying the asset
    g_oi=G_OI,                  # $ open interest, 30d median
    lane_days=21,               # days since volume onset below which lane A applies
    new_listing_days=NEW_LISTING_DAYS,
    tick_pinned_ratio=TICK_PINNED_RATIO,
    decay_trend=0.6,            # 7d/30d volume ratio below this...
    decay_slope=-2.0,           # ...and %/day slope below this => decaying
    # Fast decay path. The rule above reads vol_trend, which is a LEVEL ratio: it
    # cannot fall while the 30d median lags, so a post-pump collapse - the case you
    # most want out of - never trips it. BLESS on 2026-08-12 was 90% off its peak
    # with vol_trend at 38.8, nowhere near the 0.6 threshold.
    # decay_off_peak fires when volume sits below this share of its trailing 14d max
    # AND the 3d/7d ratio confirms the direction. DEFAULT 0.0 = OFF: this changes
    # which assets get dropped, so it is opt-in rather than a silent policy change.
    decay_off_peak=0.0,         # e.g. 0.25 => "trading under a quarter of its 14d peak"
    decay_r37=1.0,              # ...and 3d median / 7d median below this
    n_fail_drop=2,              # gates failed before an asset we trade is dropped
    w_flow=0.40, w_structure=0.25, w_carry=0.15, w_friction=0.20,
)


def score(m: pd.DataFrame, p: dict | None = None) -> pd.DataFrame:
    p = {**DEFAULTS, **(p or {})}
    d = m.copy()
    # A name that listed mid-window has no 30-day history to be persistent over.
    # Lane A (FRAMEWORK.md s2): judge it on the days it was actually live.
    d["lane"] = np.where(d["days_since_onset"] < p["lane_days"], "new", "established")
    d["gate_adv"] = np.where(d["lane"] == "new",
                             d["vol_usd_med7"] >= p["g_adv"],
                             d["vol_usd_med30"] >= p["g_adv"])
    d["gate_persist"] = np.where(d["lane"] == "new",
                                 d["days_above_5m_live"].fillna(0) >= p["g_persist"],
                                 d["days_above_5m"] >= p["g_persist"])
    d["gate_venues"] = d["n_venues"] >= p["g_venues"]
    d["gate_oi"] = d["oi_usd_med30"] >= p["g_oi"]
    gates = ["gate_adv", "gate_persist", "gate_venues", "gate_oi"]
    d["n_fail"] = (~d[gates]).sum(axis=1)
    d["new_listing"] = d["age_days"] < p["new_listing_days"]
    d["tick_pinned"] = d["spread_bps_med"] <= d["tick_bps_med"] * p["tick_pinned_ratio"]
    d["edge_headroom_bps"] = d["spread_bps_med"] - d["tick_bps_med"]
    # decay only applies to established names - a new listing's 7d/30d ratio is
    # mechanically huge and its slope mechanically positive
    d["decay_slow"] = ((d["vol_trend"] < p["decay_trend"])
                       & (d["vol_slope_pct_day"] < p["decay_slope"])
                       & (d["lane"] == "established"))
    # off-peak path: applies to every lane, because a new listing that spiked and died
    # is exactly the thing the established-only rule was written to avoid flagging by
    # accident - here it is the thing we are trying to catch.
    if p["decay_off_peak"] > 0 and "vol_off_peak" in d.columns:
        d["decay_fast"] = ((d["vol_off_peak"] < p["decay_off_peak"])
                           & (d["vol_r37"].fillna(1.0) < p["decay_r37"]))
    else:
        d["decay_fast"] = False
    d["decay_fast"] = d["decay_fast"].fillna(False).astype(bool)
    d["decaying"] = d["decay_slow"] | d["decay_fast"]

    def pct(s, asc=True):
        return s.rank(pct=True, ascending=asc, na_option="bottom") * 100

    d["c_flow"] = (pct(np.log10(d["vol_usd_med30"].clip(lower=1)))
                   + pct(d["days_above_5m"]) + pct(np.log10(d["trades_med30"]
                                                            .fillna(1).clip(lower=1)))) / 3
    d["c_structure"] = (pct(d["n_venues"]) + pct(1 - d["venue_hhi"])
                        + pct(d["venues_spot"])) / 3
    d["c_carry"] = pct(d["fund_spread_bps_med"].abs())
    d["c_friction"] = (pct(d["edge_headroom_bps"])
                       + pct(d["min_notional_max"].fillna(0), asc=False)) / 2
    wsum = p["w_flow"] + p["w_structure"] + p["w_carry"] + p["w_friction"] or 1.0
    d["composite"] = (p["w_flow"] * d["c_flow"] + p["w_structure"] * d["c_structure"]
                      + p["w_carry"] * d["c_carry"]
                      + p["w_friction"] * d["c_friction"]) / wsum

    # Add-priority bar: at least as attractive as the MEDIAN asset we already keep.
    # Self-calibrating, so it cannot be gamed by how the shortlist was built - and
    # the raw gates alone are near-tautological here because the shortlist was
    # selected by volume in the first place.
    nf = p["n_fail_drop"]
    kept = d[d.traded & (d.n_fail < nf)]
    bar = float(kept["composite"].median()) if len(kept) else 50.0
    d["add_bar"] = bar
    d["verdict"] = "hold"
    d.loc[d.traded & ((d.n_fail >= nf) | d.decaying), "verdict"] = "drop"
    d.loc[d.traded & (d.n_fail < nf) & ~d.decaying, "verdict"] = "keep"
    d.loc[(~d.traded) & (d.n_fail == 0) & (d.composite >= bar), "verdict"] = "add"
    d.loc[(~d.traded) & ((d.n_fail > 0) | (d.composite < bar)), "verdict"] = "watch"
    return d.sort_values("composite", ascending=False)


def reasons(r) -> str:
    out = []
    if not r.gate_adv:
        out.append(f"ADV ${r.vol_usd_med30/1e6:.1f}M &lt; $5M")
    if not r.gate_persist:
        out.append(f"only {r.days_above_5m*100:.0f}% of days above $5M")
    if not r.gate_venues:
        out.append(f"{int(r.n_venues)} venue(s)")
    if not r.gate_oi:
        out.append(f"OI ${r.oi_usd_med30/1e6:.1f}M &lt; $3M")
    if r.decaying:
        out.append(f"decaying: 7d/30d {r.vol_trend:.2f}, {r.vol_slope_pct_day:+.1f}%/day")
    return "; ".join(out) or "&mdash;"


def strengths(r) -> str:
    out = [f"${r.vol_usd_med30/1e6:,.0f}M/day on {int(r.n_venues)} venues"]
    if r.venues_spot:
        out.append(f"{int(r.venues_spot)} spot venues (hedgeable)")
    if not r.tick_pinned:
        out.append(f"{r.edge_headroom_bps:.1f}bps above tick")
    else:
        out.append("tick-pinned")
    if abs(r.fund_spread_bps_med) >= 3:
        out.append(f"funding spread {r.fund_spread_bps_med:.1f}bps")
    if r.new_listing:
        out.append(f"NEW ({int(r.age_days)}d)")
    return "; ".join(out)


# ============================================================ svg helpers
def _sc(v, lo, hi, a, b, log=False):
    if log:
        v, lo, hi = (np.log10(max(v, 1e-9)), np.log10(max(lo, 1e-9)),
                     np.log10(max(hi, 1e-9)))
    if hi == lo:
        return a
    return a + (b - a) * (v - lo) / (hi - lo)


def scatter(df, xcol, ycol, W=880, H=460, xlog=True, ylog=True,
            xlab="", ylab="", groups=None, hline=None, diag=False,
            label_top=8, label_by=None):
    """Scatter with hover tooltips, a legend, and selective direct labels."""
    P = dict(l=76, r=26, t=18, b=54)
    iw, ih = W - P["l"] - P["r"], H - P["t"] - P["b"]
    x, y = df[xcol].astype(float), df[ycol].astype(float)
    xlo, xhi = np.nanmin(x), np.nanmax(x)
    ylo, yhi = np.nanmin(y), np.nanmax(y)
    if xlog:
        xlo, xhi = max(xlo, 1e-9) * 0.7, xhi * 1.4
    else:
        pad = (xhi - xlo) * .06 or 1; xlo, xhi = xlo - pad, xhi + pad
    if ylog:
        ylo, yhi = max(ylo, 1e-9) * 0.7, yhi * 1.4
    else:
        pad = (yhi - ylo) * .06 or 1; ylo, yhi = ylo - pad, yhi + pad

    def X(v): return P["l"] + _sc(v, xlo, xhi, 0, iw, xlog)
    def Y(v): return P["t"] + ih - _sc(v, ylo, yhi, 0, ih, ylog)

    def ticks(lo, hi, log):
        if log:
            a, b = int(np.floor(np.log10(lo))), int(np.ceil(np.log10(hi)))
            return [10 ** k for k in range(a, b + 1) if lo <= 10 ** k <= hi]
        n = 5
        step = (hi - lo) / n
        return [lo + i * step for i in range(n + 1)]

    def fmt(v):
        if v >= 1e9: return f"{v/1e9:g}B"
        if v >= 1e6: return f"{v/1e6:g}M"
        if v >= 1e3: return f"{v/1e3:g}k"
        return f"{v:g}" if v >= 1 else f"{v:.3g}"

    s = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" '
         f'aria-label="{html.escape(xlab)} versus {html.escape(ylab)}">']
    # grid + axes
    for t in ticks(ylo, yhi, ylog):
        yy = Y(t)
        s.append(f'<line class="grid" x1="{P["l"]}" x2="{P["l"]+iw}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        s.append(f'<text class="tick" x="{P["l"]-9}" y="{yy+4:.1f}" text-anchor="end">{fmt(t)}</text>')
    for t in ticks(xlo, xhi, xlog):
        xx = X(t)
        s.append(f'<line class="grid" y1="{P["t"]}" y2="{P["t"]+ih}" x1="{xx:.1f}" x2="{xx:.1f}"/>')
        s.append(f'<text class="tick" x="{xx:.1f}" y="{P["t"]+ih+20}" text-anchor="middle">{fmt(t)}</text>')
    s.append(f'<line class="axis" x1="{P["l"]}" x2="{P["l"]+iw}" y1="{P["t"]+ih}" y2="{P["t"]+ih}"/>')
    s.append(f'<line class="axis" x1="{P["l"]}" x2="{P["l"]}" y1="{P["t"]}" y2="{P["t"]+ih}"/>')
    if hline is not None and ylo <= hline <= yhi:
        yy = Y(hline)
        s.append(f'<line class="ref" x1="{P["l"]}" x2="{P["l"]+iw}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
        s.append(f'<text class="reflab" x="{P["l"]+iw-4}" y="{yy-7:.1f}" text-anchor="end">flat (7d = 30d)</text>')
    if diag:
        lo, hi = max(xlo, ylo), min(xhi, yhi)
        s.append(f'<line class="ref" x1="{X(lo):.1f}" y1="{Y(lo):.1f}" '
                 f'x2="{X(hi):.1f}" y2="{Y(hi):.1f}"/>')
        s.append(f'<text class="reflab" x="{X(hi)-6:.1f}" y="{Y(hi)+18:.1f}" '
                 f'text-anchor="end">spread = 1 tick</text>')
    # marks
    for gname, gdf, col in groups:
        for r in gdf.itertuples():
            xv, yv = getattr(r, xcol), getattr(r, ycol)
            if not np.isfinite(xv) or not np.isfinite(yv):
                continue
            tip = ("\n".join([
                f"{r.asset_key}  ({r.asset_class}) - {gname}",
                f"ADV mean   ${r.vol_usd_mean30/1e6:,.0f}M",
                f"ADV median ${r.vol_usd_med30/1e6:,.0f}M   (7d/30d {r.vol_trend:.2f})",
                f"OI median  ${r.oi_usd_med30/1e6:,.0f}M   OI/ADV {r.oi_to_adv:.2f}",
                f"venues     {int(r.n_venues)} perp / {int(r.venues_spot)} spot",
                f"spread     {r.spread_bps_med:.2f} bps   tick {r.tick_bps_med:.2f} bps",
                f"funding    {r.fund_bps_med:.2f} bps   spread {r.fund_spread_bps_med:.1f} bps",
            ]))
            s.append(f'<circle class="pt" cx="{X(xv):.1f}" cy="{Y(yv):.1f}" r="5" '
                     f'fill="{col}"><title>{html.escape(tip)}</title></circle>')
    # selective direct labels
    if label_by is not None:
        for r in df.nlargest(label_top, label_by).itertuples():
            xv, yv = getattr(r, xcol), getattr(r, ycol)
            if np.isfinite(xv) and np.isfinite(yv):
                s.append(f'<text class="ptlab" x="{X(xv)+8:.1f}" y="{Y(yv)+4:.1f}">'
                         f'{html.escape(str(r.asset_key))}</text>')
    s.append(f'<text class="axlab" x="{P["l"]+iw/2:.0f}" y="{H-8}" text-anchor="middle">'
             f'{html.escape(xlab)}</text>')
    s.append(f'<text class="axlab" transform="translate(16,{P["t"]+ih/2:.0f}) rotate(-90)" '
             f'text-anchor="middle">{html.escape(ylab)}</text>')
    s.append("</svg>")
    leg = " ".join(f'<span class="lg"><i style="background:{col}"></i>{html.escape(n)}</span>'
                   for n, _, col in groups)
    return f'<div class="figure">{"".join(s)}<div class="legend">{leg}</div></div>'


def hbar(rows, W=880, rowh=26, maxv=None, lab="", fmtv=None):
    """Simple horizontal bar chart: rows = [(label, value, color)]."""
    P = dict(l=132, r=90, t=10, b=32)
    H = P["t"] + P["b"] + rowh * len(rows)
    iw = W - P["l"] - P["r"]
    mx = maxv or max((v for _, v, _ in rows), default=1) or 1
    fmtv = fmtv or (lambda v: f"{v:,.0f}")
    s = [f'<svg viewBox="0 0 {W} {H}" class="chart" role="img" aria-label="{html.escape(lab)}">']
    for i, (name, v, col) in enumerate(rows):
        y = P["t"] + i * rowh
        w = max(2.0, iw * (v / mx))
        s.append(f'<text class="tick" x="{P["l"]-10}" y="{y+rowh/2+4:.0f}" '
                 f'text-anchor="end">{html.escape(str(name))}</text>')
        s.append(f'<rect class="bar" x="{P["l"]}" y="{y+4:.0f}" width="{w:.1f}" '
                 f'height="{rowh-10}" rx="4" fill="{col}"><title>'
                 f'{html.escape(str(name))}: {fmtv(v)}</title></rect>')
        s.append(f'<text class="barval" x="{P["l"]+w+8:.1f}" y="{y+rowh/2+4:.0f}">'
                 f'{fmtv(v)}</text>')
    s.append("</svg>")
    return f'<div class="figure">{"".join(s)}</div>'


# ============================================================ tables
def table(df, cols, cls="") -> str:
    th = "".join(f"<th{' class=num' if n else ''}>{h}</th>" for h, _, n in cols)
    body = []
    for r in df.itertuples():
        tds = []
        for _, f, n in cols:
            v = f(r)
            tds.append(f"<td{' class=num' if n else ''}>{v}</td>")
        body.append("<tr>" + "".join(tds) + "</tr>")
    return (f'<div class="tablewrap"><table class="{cls}"><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


RAW_COLS = [
    ("asset", "Asset", "t", lambda r: r.asset_key),
    ("class", "Class", "t", lambda r: r.asset_class),
    ("region", "Region", "t", lambda r: r.equity_region or "\u2014"),
    ("rwa", "RWA", "t", lambda r: "yes" if r.asset_class != "crypto" else "no"),
    ("verdict", "Verdict", "t", lambda r: r.verdict),
    ("lane", "Lane", "t", lambda r: r.lane),
    ("adv_mean", "ADV mean", "n", lambda r: r.vol_usd_mean30),
    ("adv_med", "ADV med", "n", lambda r: r.vol_usd_med30),
    ("burst", "Burstiness", "n", lambda r: r.vol_burstiness),
    ("trend", "7d/30d", "n", lambda r: r.vol_trend),
    ("slope", "%/day", "n", lambda r: r.vol_slope_pct_day),
    ("persist", "Days>$5M", "n", lambda r: r.days_above_5m * 100),
    ("trades", "Trades/day (BIN)", "n", lambda r: r.trades_med30),
    ("oi_med", "OI median $", "n", lambda r: r.oi_usd_med30),
    ("oi_mean", "OI mean $", "n", lambda r: r.oi_usd_mean30),
    ("oi_adv", "OI / ADV", "n", lambda r: r.oi_to_adv),
    ("oi_trend", "OI 7d/30d", "n", lambda r: r.oi_trend),
    ("oi_venues", "OI venues", "n", lambda r: r.oi_venues),
    ("oi_bgt", "BGT OI share", "n", lambda r: r.oi_bgt_share * 100),
    ("v_perp", "Perp venues", "n", lambda r: r.n_venues),
    ("v_spot", "Spot venues", "n", lambda r: r.venues_spot),
    ("hhi", "Venue HHI", "n", lambda r: r.venue_hhi),
    ("spread", "Spread bps", "n", lambda r: r.spread_bps_med),
    ("tick", "Tick bps", "n", lambda r: r.tick_bps_med),
    ("head", "Headroom bps", "n", lambda r: r.edge_headroom_bps),
    ("pinned", "Tick-pinned", "t", lambda r: "yes" if r.tick_pinned else "no"),
    ("rv", "Daily range bps", "n", lambda r: r.rv_bps_med),
    ("fund", "Funding bps", "n", lambda r: r.fund_bps_med),
    ("fundsp", "Funding spread bps", "n", lambda r: r.fund_spread_bps_med),
    ("pxsp", "Px spread bps", "n", lambda r: r.px_spread_bps_med),
    ("age", "Age days", "n", lambda r: r.age_days),
    ("score", "Score", "n", lambda r: r.composite),
]


VENUE_COLS = [
    ("asset", "Asset", "t", lambda r: r.asset_key),
    ("venue", "Venue", "t", lambda r: r.venue),
    ("symbol", "Native symbol", "t", lambda r: r.symbol),
    ("quote", "Quote", "t", lambda r: r.quote),
    ("ours", "We quote", "t", lambda r: "yes" if r.we_quote else "no"),
    ("adv_mean", "ADV mean", "n", lambda r: r.vol_usd_mean30),
    ("adv_med", "ADV med", "n", lambda r: r.vol_usd_med30),
    ("share", "Venue share %", "n", lambda r: r.vol_share * 100),
    ("trend", "7d/30d", "n", lambda r: r.vol_trend),
    ("trades", "Trades/day", "n", lambda r: r.trades_med30),
    ("oi_med", "OI med", "n", lambda r: r.oi_usd_med30),
    ("oi_live", "OI live", "n", lambda r: r.oi_usd_live),
    ("oi_days", "OI days", "n", lambda r: r.oi_days),
    ("spread", "Spread bps", "n", lambda r: r.spread_bps),
    ("tick", "Tick bps", "n", lambda r: r.tick_bps),
    ("fund", "Funding bps", "n", lambda r: r.fund_bps_med),
    ("fund_iv", "Fund iv h", "n", lambda r: r.fund_iv_h),
    ("maker", "Maker fee bps", "n", lambda r: (r.maker_fee * 1e4)
     if r.maker_fee is not None and np.isfinite(r.maker_fee) else np.nan),
    ("minnot", "Min notional", "n", lambda r: r.min_notional),
    ("mult", "Contract mult", "n", lambda r: r.contract_mult),
    ("px", "Last px", "n", lambda r: r.px_close),
    ("listed", "Listed", "t", lambda r: (str(r.listed_since)[:10]
                                         if pd.notna(r.listed_since) else "\u2014")),
]


def _num(v):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "\u2014", ""
    if abs(v) >= 1e9:
        t = f"{v/1e9:,.2f}B"
    elif abs(v) >= 1e6:
        t = f"{v/1e6:,.1f}M"
    elif abs(v) >= 1e4:
        t = f"{v:,.0f}"
    elif abs(v) >= 100:
        t = f"{v:,.1f}"
    else:
        t = f"{v:,.2f}"
    return t, f"{v:.6g}"


def raw_table(df) -> str:
    th = "".join(
        f'<th class="{"num" if k=="n" else ""}" data-col="{cid}" data-type="{k}">'
        f'{lab}<span class="sortcue"></span></th>' for cid, lab, k, _ in RAW_COLS)
    rows = []
    for r in df.itertuples():
        tds = []
        for cid, _, k, f in RAW_COLS:
            v = f(r)
            if k == "n":
                txt, sv = _num(float(v) if v is not None else np.nan)
                tds.append(f'<td class="num" data-v="{sv}">{txt}</td>')
            else:
                txt = html.escape(str(v))
                cls = f" class=\"v-{v}\"" if cid == "verdict" else ""
                tds.append(f'<td data-v="{txt}"{cls}>{txt}</td>')
        rows.append(f'<tr data-asset="{html.escape(str(r.asset_key)).lower()} '
                    f'{html.escape(str(r.asset_class))} {r.verdict}">'
                    + "".join(tds) + "</tr>")
    return (f'<div class="fullbleed"><div class="rawtools">'
            f'<input id="rawq" type="search" placeholder="filter by asset, class or verdict…" '
            f'aria-label="filter rows">'
            f'<span class="muted" id="rawcount"></span>'
            f'<button id="rawcsv" type="button">download CSV</button></div>'
            f'<div class="tablewrap raw"><table id="rawtable"><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></div>')


def venue_table(df) -> str:
    th = "".join(
        f'<th class="{"num" if k=="n" else ""}" data-col="{cid}" data-type="{k}">'
        f'{lab}<span class="sortcue"></span></th>' for cid, lab, k, _ in VENUE_COLS)
    rows = []
    for r in df.itertuples():
        tds = []
        for cid, _, k, f in VENUE_COLS:
            v = f(r)
            if k == "n":
                txt, sv = _num(float(v) if v is not None else np.nan)
                tds.append(f'<td class="num" data-v="{sv}">{txt}</td>')
            else:
                txt = html.escape(str(v))
                cls = " class=\"ours\"" if (cid == "ours" and v == "yes") else ""
                tds.append(f'<td data-v="{txt}"{cls}>{txt}</td>')
        rows.append(f'<tr data-asset="{html.escape(str(r.asset_key)).lower()} '
                    f'{html.escape(str(r.venue)).lower()} '
                    f'{html.escape(str(r.symbol)).lower()}">' + "".join(tds) + "</tr>")
    return (f'<div class="fullbleed"><div class="rawtools">'
            f'<input id="venq" type="search" placeholder="filter by asset, venue or symbol…" '
            f'aria-label="filter rows">'
            f'<span class="muted" id="vencount"></span>'
            f'<button id="vencsv" type="button">download CSV</button></div>'
            f'<div class="tablewrap raw"><table id="ventable"><thead><tr>{th}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></div>')


def money(v, unit="M"):
    if not np.isfinite(v):
        return "&mdash;"
    return f"${v/1e6:,.0f}M" if unit == "M" else f"${v/1e9:,.2f}B"


# ============================================================ main
def main():
    REPORTS.mkdir(exist_ok=True)
    m = score(pd.read_parquet(DATA / "asset_metrics.parquet"))
    snap = pd.read_parquet(DATA / "snapshot.parquet")
    spec = pd.read_parquet(DATA / "instrument_ref.parquet")
    imap = pd.read_parquet(DATA / "internal_map.parquet")
    dup = pd.read_parquet(DATA / "dup_tickers.parquet")
    m.to_parquet(DATA / "asset_scores.parquet", index=False)

    drops = m[m.verdict == "drop"].sort_values("vol_usd_med30")
    adds = m[m.verdict == "add"].sort_values("composite", ascending=False)
    keeps = m[m.verdict == "keep"]
    watch = m[m.verdict == "watch"].sort_values("composite", ascending=False)

    # venue-coverage gap for assets we trade
    ours = (imap[imap.asset_key.notna()]
            .groupby("asset_key")["venue"].nunique().rename("ours"))
    avail = (snap[snap.is_excluded == 0]
             .groupby("asset_key")["venue"].nunique().rename("listed"))
    gap = pd.concat([ours, avail], axis=1).dropna()
    gap = gap[(gap.ours < gap.listed)].assign(missing=lambda d: d.listed - d.ours)
    gap = gap.join(m.set_index("asset_key")["vol_usd_med30"]).dropna()
    gap = gap.sort_values(["missing", "vol_usd_med30"], ascending=False).head(14)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    n_active = int(spec.active.sum())
    n_perp = int(len(snap))
    tot_vol = snap.vol24h_usd.sum()

    # ---------- charts ----------
    ch_screen = scatter(
        m, "vol_usd_med30", "vol_trend", xlog=True, ylog=True,
        xlab="30-day MEDIAN daily volume, USD, summed across venues (log scale)",
        ylab="momentum: 7-day / 30-day median volume (log)",
        groups=[("we trade it", m[m.traded], C["s2"]),
                ("candidate", m[~m.traded], C["s1"])],
        hline=1.0, label_by="vol_usd_med30", label_top=9)

    ch_tick = scatter(
        m, "tick_bps_med", "spread_bps_med", xlog=True, ylog=True,
        xlab="tick size, bps of price (log)",
        ylab="quoted spread, bps (log)",
        groups=[("tick-pinned: spread = 1 tick", m[m.tick_pinned], C["s2"]),
                ("headroom above tick", m[~m.tick_pinned], C["s1"])],
        diag=True, label_by="vol_usd_med30", label_top=7)

    gap_rows = [(f"{i}  ({int(r.ours)}/{int(r.listed)})", r.missing, C["s1"])
                for i, r in gap.iterrows()]
    ch_gap = hbar(gap_rows, lab="venues not yet quoted per asset",
                  fmtv=lambda v: f"{int(v)} missing")

    cls_rows = []
    for k, g in m.groupby("asset_class"):
        cls_rows.append((f"{k}  (n={len(g)})", g.vol_usd_med30.sum(),
                         C["s3"] if k in ("commodity", "index") else
                         (C["s2"] if k in ("equity", "rwa") else C["s1"])))
    cls_rows.sort(key=lambda t: -t[1])
    ch_cls = hbar(cls_rows, lab="30-day median volume by asset class",
                  fmtv=lambda v: money(v, "B"))

    # ---------- tables ----------
    t_drop = table(drops, [
        ("Asset", lambda r: f"<b>{r.asset_key}</b>", False),
        ("Class", lambda r: r.asset_class, False),
        ("ADV mean", lambda r: money(r.vol_usd_mean30), True),
        ("ADV med", lambda r: money(r.vol_usd_med30), True),
        ("7d/30d", lambda r: f"{r.vol_trend:.2f}", True),
        ("%/day", lambda r: f"{r.vol_slope_pct_day:+.1f}", True),
        ("Days&gt;$5M", lambda r: f"{r.days_above_5m*100:.0f}%", True),
        ("Venues", lambda r: f"{int(r.n_venues)}", True),
        ("OI", lambda r: money(r.oi_usd_med30), True),
        ("Why it fails", lambda r: reasons(r), False)], "drop")

    adds_ranked = adds.reset_index(drop=True)
    adds_ranked.index += 1
    adds_ranked = adds_ranked.reset_index().rename(columns={"index": "rank"})
    ADD_COLS = [
        ("#", lambda r: f"{r.rank}", True),
        ("Asset", lambda r: f"<b>{r.asset_key}</b>", False),
        ("Class", lambda r: r.asset_class, False),
        ("ADV mean", lambda r: money(r.vol_usd_mean30), True),
        ("ADV med", lambda r: money(r.vol_usd_med30), True),
        ("7d/30d", lambda r: f"{r.vol_trend:.2f}", True),
        ("Venues", lambda r: f"{int(r.n_venues)}", True),
        ("Spot", lambda r: f"{int(r.venues_spot)}", True),
        ("OI med", lambda r: money(r.oi_usd_med30), True),
        ("OI/ADV", lambda r: f"{r.oi_to_adv:.2f}", True),
        ("Spread bps", lambda r: f"{r.spread_bps_med:.2f}", True),
        ("Tick bps", lambda r: f"{r.tick_bps_med:.2f}", True),
        ("Fund sprd", lambda r: f"{r.fund_spread_bps_med:.1f}", True),
        ("Score", lambda r: f"{r.composite:.0f}", True),
        ("Notes", lambda r: strengths(r), False)]
    t_add = table(adds_ranked.head(15), ADD_COLS)
    t_add_rest = (table(adds_ranked.iloc[15:], ADD_COLS)
                  if len(adds_ranked) > 15 else "")

    t_keep = table(keeps.sort_values("composite", ascending=False), [
        ("Asset", lambda r: f"<b>{r.asset_key}</b>", False),
        ("Class", lambda r: r.asset_class, False),
        ("ADV mean", lambda r: money(r.vol_usd_mean30), True),
        ("ADV med", lambda r: money(r.vol_usd_med30), True),
        ("7d/30d", lambda r: f"{r.vol_trend:.2f}", True),
        ("Venues", lambda r: f"{int(r.n_venues)}", True),
        ("Spot", lambda r: f"{int(r.venues_spot)}", True),
        ("OI med", lambda r: money(r.oi_usd_med30), True),
        ("OI/ADV", lambda r: f"{r.oi_to_adv:.2f}", True),
        ("Spread bps", lambda r: f"{r.spread_bps_med:.2f}", True),
        ("Tick bps", lambda r: f"{r.tick_bps_med:.2f}", True),
        ("Pinned", lambda r: "yes" if r.tick_pinned else "no", False),
        ("Score", lambda r: f"{r.composite:.0f}", True)])

    t_watch = table(watch.head(16), [
        ("Asset", lambda r: f"<b>{r.asset_key}</b>", False),
        ("Class", lambda r: r.asset_class, False),
        ("ADV mean", lambda r: money(r.vol_usd_mean30), True),
        ("ADV med", lambda r: money(r.vol_usd_med30), True),
        ("Venues", lambda r: f"{int(r.n_venues)}", True),
        ("Score", lambda r: f"{r.composite:.0f}", True),
        ("Blocking gate", lambda r: reasons(r), False)])

    unmatched = imap[imap.match_rule == "UNMATCHED"]
    t_unmatched = table(unmatched, [
        ("Internal name", lambda r: f"<code>{r.name}</code>", False),
        ("Venue", lambda r: r.venue, False),
        ("Kind", lambda r: r.kind, False),
        ("Fills 10d", lambda r: f"{int(r.fills):,}", True),
        ("Problem", lambda r: "no live venue instrument with this base/quote", False)])

    dup_show = dup.head(12) if len(dup) else dup
    t_dup = (table(dup_show, [
        ("Ticker A", lambda r: f"<b>{r.asset_a}</b>", False),
        ("Ticker B", lambda r: f"<b>{r.asset_b}</b>", False),
        ("Price gap", lambda r: f"{r.diff_bps:.1f} bps", True),
        ("Venues A", lambda r: f"{int(r.venues_a)}", True),
        ("Venues B", lambda r: f"{int(r.venues_b)}", True),
        ("ADV A", lambda r: money(r.vol_a), True),
        ("ADV B", lambda r: money(r.vol_b), True)]) if len(dup_show)
        else "<p class='muted'>none detected</p>")

    t_raw = raw_table(m.sort_values("vol_usd_mean30", ascending=False))
    vm = pd.read_parquet(DATA / "venue_metrics.parquet")
    order = m.set_index("asset_key")["vol_usd_mean30"].to_dict()
    vm["_o"] = vm["asset_key"].map(order).fillna(0)
    vm = vm.sort_values(["_o", "vol_usd_mean30"], ascending=[False, False])
    t_venue = venue_table(vm)
    n_legs_missing = int(((vm.we_quote == 0) & vm.asset_key.isin(
        m[m.traded].asset_key)).sum())
    bgt_hit = m[(m.oi_bgt_share > 0.2)]
    n_pinned = int(m.tick_pinned.sum())
    pinned_traded = int(m[m.traded].tick_pinned.sum())
    html_doc = TEMPLATE.format(
        now=now, n_active=f"{n_active:,}", n_perp=f"{n_perp:,}",
        tot_vol=f"${tot_vol/1e9:.0f}B", n_assets=len(m),
        n_traded=int(m.traded.sum()), n_drop=len(drops), n_add=len(adds),
        n_watch=len(watch), n_keep=len(keeps),
        add_bar=f"{m.add_bar.iloc[0]:.0f}", n_add_rest=max(0, len(adds) - 15),
        t_add_rest=t_add_rest, n_new=int((m.lane == 'new').sum()),
        n_pinned=n_pinned, pct_pinned=f"{n_pinned/len(m)*100:.0f}",
        pinned_traded=pinned_traded,
        ch_screen=ch_screen, ch_tick=ch_tick, ch_gap=ch_gap, ch_cls=ch_cls,
        t_drop=t_drop, t_add=t_add, t_keep=t_keep, t_watch=t_watch,
        t_unmatched=t_unmatched, t_dup=t_dup,
        t_raw=t_raw, t_venue=t_venue, n_venue_rows=len(vm),
        n_legs_missing=n_legs_missing, n_bgt_oi=len(bgt_hit),
        bgt_worst=", ".join(f"{r.asset_key} {r.oi_bgt_share*100:.0f}%"
                            for r in bgt_hit.nlargest(4, "oi_bgt_share").itertuples()),
        n_equity=int((m.asset_class == "equity").sum()),
        n_rwa=int((m.asset_class != "crypto").sum()),
        n_unclass=int((m.asset_class == "rwa_unclassified").sum()),
        med_burst=f"{m.vol_burstiness.median():.2f}",
        n_bursty=int((m.vol_burstiness > 1.5).sum()),
        n_unmatched=len(unmatched), n_dup=len(dup),
        n_mapped=int((imap.match_rule != "UNMATCHED").sum()), n_internal=len(imap),
        n_gate_issuer=int(imap.match_rule.str.startswith("gate_issuer").sum()),
        n_nospot=int(((m.venues_spot == 0) & m.asset_class.isin(
            ["commodity", "index"])).sum()),
        g_adv=f"${G_ADV/1e6:.0f}M", g_persist=f"{G_PERSIST*100:.0f}%",
        g_venues=G_VENUES, g_oi=f"${G_OI/1e6:.0f}M")
    out = REPORTS / "universe_review.html"
    out.write_text(html_doc, encoding="utf-8")
    print(f"-> {out}  ({out.stat().st_size/1024:.0f} KB)")
    print(f"   drop={len(drops)} add={len(adds)} keep={len(keeps)} watch={len(watch)}")
    print(f"   tick-pinned {n_pinned}/{len(m)} assets ({pinned_traded} of ours)")


TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trading universe review</title>
<style>
:root{{
  --surface:#fcfcfb; --plane:#f9f9f7; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781;
  --grid:#e1e0d9; --axis:#c3c2b7; --ring:rgba(11,11,11,.10);
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a;
  --good:#0ca30c; --warn:#fab219; --crit:#d03b3b;
  --mono:ui-monospace,SFMono-Regular,Menlo,monospace;
}}
@media (prefers-color-scheme:dark){{
  :root:where(:not([data-theme="light"])){{
    --surface:#1a1a19; --plane:#0d0d0d; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
    --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
    --s1:#3987e5; --s2:#d95926; --s3:#199e70;
  }}
}}
:root[data-theme="dark"]{{
  --surface:#1a1a19; --plane:#0d0d0d; --ink:#fff; --ink2:#c3c2b7; --muted:#898781;
  --grid:#2c2c2a; --axis:#383835; --ring:rgba(255,255,255,.10);
  --s1:#3987e5; --s2:#d95926; --s3:#199e70;
}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--plane);color:var(--ink);
  font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;}}
.wrap{{max-width:1180px;margin:0 auto;padding:40px 22px 90px}}
h1{{font-size:30px;line-height:1.2;margin:0 0 6px;letter-spacing:-.02em}}
h2{{font-size:21px;margin:52px 0 6px;letter-spacing:-.01em}}
h3{{font-size:16px;margin:30px 0 6px}}
.sub{{color:var(--ink2);margin:0 0 30px}}
p{{margin:10px 0;color:var(--ink2);max-width:74ch}}
p.tight{{margin:6px 0 14px}}
b,strong{{color:var(--ink);font-weight:650}}
code{{font-family:var(--mono);font-size:.88em;background:var(--surface);
  padding:1px 5px;border-radius:4px;border:1px solid var(--ring)}}
.muted{{color:var(--muted)}}
.stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  gap:12px;margin:26px 0 8px}}
.stat{{background:var(--surface);border:1px solid var(--ring);border-radius:10px;
  padding:14px 16px}}
.stat .v{{font-size:25px;font-weight:640;color:var(--ink);line-height:1.15}}
.stat .k{{font-size:12px;color:var(--muted);text-transform:uppercase;
  letter-spacing:.05em;margin-top:3px}}
.card{{background:var(--surface);border:1px solid var(--ring);border-radius:12px;
  padding:20px;margin:18px 0}}
.figure{{background:var(--surface);border:1px solid var(--ring);border-radius:12px;
  padding:14px 12px 6px;margin:18px 0;overflow-x:auto}}
svg.chart{{display:block;width:100%;height:auto;min-width:620px}}
.grid{{stroke:var(--grid);stroke-width:1}}
.axis{{stroke:var(--axis);stroke-width:1}}
.ref{{stroke:var(--muted);stroke-width:1.5;opacity:.55}}
.reflab{{fill:var(--muted);font-size:11px}}
.tick{{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}}
.axlab{{fill:var(--ink2);font-size:12px}}
.ptlab{{fill:var(--ink);font-size:11px;font-weight:600;paint-order:stroke;
  stroke:var(--surface);stroke-width:3px}}
circle.pt{{stroke:var(--surface);stroke-width:2;cursor:pointer}}
circle.pt:hover{{stroke:var(--ink);stroke-width:2}}
rect.bar{{cursor:pointer}}
.barval{{fill:var(--ink2);font-size:11px;font-variant-numeric:tabular-nums}}
.legend{{display:flex;flex-wrap:wrap;gap:18px;padding:6px 10px 10px;
  font-size:12px;color:var(--ink2)}}
.lg{{display:inline-flex;align-items:center;gap:7px}}
.lg i{{width:11px;height:11px;border-radius:3px;display:inline-block}}
.tablewrap{{overflow-x:auto;background:var(--surface);border:1px solid var(--ring);
  border-radius:12px;margin:16px 0}}
table{{border-collapse:collapse;width:100%;font-size:13.5px;min-width:640px}}
th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid var(--grid);
  vertical-align:top}}
th{{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);
  font-weight:600;white-space:nowrap}}
td{{color:var(--ink2)}}
td.num,th.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}}
tbody tr:last-child td{{border-bottom:none}}
tbody tr:hover td{{background:var(--plane)}}
.pill{{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:650;
  padding:2px 9px;border-radius:99px;border:1px solid var(--ring);
  text-transform:uppercase;letter-spacing:.04em}}
.pill.drop{{color:var(--crit)}} .pill.add{{color:var(--good)}}
.pill.warn{{color:var(--warn)}}
.note{{border-left:3px solid var(--s1);padding:2px 0 2px 15px;margin:16px 0;
  color:var(--ink2)}}
.note.warn{{border-left-color:var(--warn)}}
ul{{color:var(--ink2);max-width:74ch}} li{{margin:5px 0}}
details{{margin:14px 0}}
details summary{{cursor:pointer;color:var(--s1);font-size:13.5px;font-weight:600;
  padding:6px 0}}
details summary:hover{{text-decoration:underline}}
.fullbleed{{position:relative;left:50%;transform:translateX(-50%);
  width:calc(100vw - 36px);max-width:2280px}}
.rawtools{{display:flex;gap:12px;align-items:center;margin:16px 0 8px;flex-wrap:wrap}}
.rawtools input{{flex:1;min-width:220px;padding:8px 12px;border-radius:8px;
  border:1px solid var(--ring);background:var(--surface);color:var(--ink);
  font:inherit;font-size:13.5px}}
.rawtools button{{padding:8px 14px;border-radius:8px;border:1px solid var(--ring);
  background:var(--surface);color:var(--s1);font:inherit;font-size:13px;
  font-weight:600;cursor:pointer}}
.rawtools button:hover{{background:var(--plane)}}
.tablewrap.raw{{max-height:640px;overflow:auto}}
.raw table{{min-width:100%;font-size:12px}}
.raw th,.raw td{{padding:6px 9px;white-space:nowrap}}
td.ours{{color:var(--good);font-weight:650}}
.raw thead th{{position:sticky;top:0;background:var(--surface);z-index:2;
  cursor:pointer;user-select:none;box-shadow:0 1px 0 var(--grid)}}
.raw thead th:hover{{color:var(--ink)}}
.raw tbody td:first-child{{position:sticky;left:0;background:var(--surface);
  font-weight:650;color:var(--ink)}}
.raw tbody tr:hover td{{background:var(--plane)}}
.sortcue{{display:inline-block;width:11px;color:var(--muted);font-size:10px}}
td.v-drop{{color:var(--crit);font-weight:650}}
td.v-add{{color:var(--good);font-weight:650}}
td.v-keep{{color:var(--ink2)}}
td.v-watch{{color:var(--warn);font-weight:650}}
.foot{{margin-top:56px;padding-top:20px;border-top:1px solid var(--grid);
  font-size:12.5px;color:var(--muted)}}
</style></head><body><div class="wrap">

<h1>Trading universe review</h1>
<p class="sub">Perpetuals across Binance, OKX, Bitget, Gate and KuCoin &middot;
30-day window &middot; generated {now}</p>

<div class="stats">
  <div class="stat"><div class="v">{n_active}</div><div class="k">active instruments</div></div>
  <div class="stat"><div class="v">{n_perp}</div><div class="k">perps screened</div></div>
  <div class="stat"><div class="v">{tot_vol}</div><div class="k">24h volume</div></div>
  <div class="stat"><div class="v">{n_traded}</div><div class="k">assets we trade</div></div>
  <div class="stat"><div class="v">{n_drop}</div><div class="k">drop candidates</div></div>
  <div class="stat"><div class="v">{n_add}</div><div class="k">add candidates</div></div>
</div>

<p>Every number here comes from the venues' own REST APIs &mdash; no third-party
data. Volume and open interest are converted to real USD via USDT/USD and
USDC/USD, not assumed 1:1. The screen covers <b>{n_assets} assets</b>: everything
we currently quote, plus the highest-volume names we don't.</p>

<div class="note">
<b>How to read the verdicts.</b> An asset must clear four hard gates to be in the
universe: ADV &ge; {g_adv}, at least {g_persist} of days above $5M,
&ge; {g_venues} venues listing it, and OI &ge; {g_oi}.
<b>Drop</b> = we trade it and it fails two or more gates, or its volume is
decaying (7d/30d &lt; 0.6 with a negative slope). <b>Add</b> = we don't trade it
and it clears every gate. Ranking among survivors is a weighted blend of flow
(40%), cross-venue structure (25%), funding dispersion (15%) and friction (20%).
</div>

<h2>The screen</h2>
<p class="tight">Volume level against volume momentum. Anything left of the
$5M gridline fails the ADV gate; below the flat line is shrinking. The names worth
arguing about are bottom-left (ours, fading) and top-right (theirs, growing).</p>
{ch_screen}

<h2><span class="pill drop">drop</span> &nbsp;Candidates to remove &mdash; {n_drop} assets</h2>
<p class="tight">Assets we quoted in the last 10 days that fail the gates on current
market structure. The binding reason is stated for each. Nothing here judges our own
execution &mdash; the screen deliberately does not use our P&amp;L.</p>
<p class="tight"><b>Newly listed names are excluded from the persistence test.</b>
{n_new} assets in this screen listed inside the 30-day window, so a
&ldquo;% of days above $5M&rdquo; measured over the full window would be near zero for
them by construction. They are judged on the days they were actually live instead
&mdash; without that, RATS and BLESS (two of our strongest names, both up more than
tenfold on a 7d/30d basis) would have been flagged for removal.</p>
{t_drop}

<h2><span class="pill add">add</span> &nbsp;Candidates to add &mdash; top 15 of {n_add}</h2>
<p class="tight">Not currently quoted, clearing all four gates, <b>and scoring at or
above the median asset we already keep</b> (score {add_bar}). That bar is
self-calibrating: it says &ldquo;at least as attractive as the middle of our current
book&rdquo;, so it does not depend on how the candidate list was built. 
<b>Spread</b> and <b>Tick</b> are both bps of price &mdash; where they are equal the
market is pinned at one tick, the achievable edge is capped at half a tick, and the
game is queue priority rather than width.</p>
{t_add}
<details><summary>Remaining {n_add_rest} candidates above the bar, ranked</summary>
{t_add_rest}</details>

<h2>Where the edge ceiling actually is</h2>
<p class="tight">Quoted spread against tick size, both in bps. Points on the
diagonal are trading at exactly one tick wide &mdash; <b>{n_pinned} of {n_assets}
assets ({pct_pinned}%), including {pinned_traded} we already quote</b>. For those the
spread is not ours to set and volume alone badly overstates how attractive the name
is. Points well above the line are where width is a real decision.</p>
{ch_tick}

<h2>Venues we list but don't quote</h2>
<p class="tight">For assets already in our universe, the number of venues carrying
the asset that we are <em>not</em> on. Each one is extra liquidity to quote against
and an extra hedge leg, at no new symbology or research cost.</p>
{ch_gap}

<h2>Asset classes &mdash; and what &ldquo;RWA&rdquo; means here</h2>
<p class="tight"><b>RWA is a superset, not a category alongside equity.</b> Every
tokenised equity, commodity and index perp is a real-world asset, so the report
carries two independent fields: an <b>RWA flag</b> (true for anything that is not
native crypto &mdash; {n_rwa} of {n_assets} assets here) and a <b>class</b>
(crypto / equity / commodity / index / premarket).</p>
<p class="tight">The venues disagree on how much they tell you. Binance declares a
specific type &mdash; <code>EQUITY</code>, <code>KR_EQUITY</code>,
<code>HK_EQUITY</code>, <code>COMMODITY</code>, <code>INDEX</code>,
<code>PREMARKET</code>. Bitget declares only a binary <code>isRwa</code> with no
sub-type. OKX, Gate and KuCoin declare nothing at all. So class is resolved
<b>once per asset, taking the most specific declaration across all five venues</b>:
Binance's tag always beats Bitget's generic RWA, and the resolved class then applies
to every venue's listing of that asset. {n_unclass} asset(s) remain unclassified &mdash;
flagged RWA by Bitget, with no Binance listing to say what kind.</p>

<h2>What the universe is made of</h2>
<p class="tight">30-day median volume by asset class. The tokenised-equity and
commodity blocks are large and we are only in part of them &mdash; note that all
<b>{n_nospot} commodity/index assets in this screen have zero spot venues</b> on these
five exchanges (gold, silver, WTI, Brent), so a perp position there has no spot leg to
hedge against and the only offset is another venue's perp.</p>
{ch_cls}

<div class="note warn">
<b>Open interest is understated, and not evenly.</b> Bitget publishes no OI
<em>history</em> at all, so the 30-day OI series is summed over the other four venues
only. From the live snapshot &mdash; where Bitget's OI <em>is</em> readable &mdash;
Bitget holds more than 20% of total OI for <b>{n_bgt_oi} of {n_assets} assets</b>,
worst cases {bgt_worst}. Read every OI figure here as a lower bound, and read
<code>OI venues</code> and <code>BGT OI share</code> in the full table to know by how
much. A daily snapshot job closes this within about a month; the history cannot be
backfilled from Bitget.
</div>

<h2>Assets we hold and keep &mdash; {n_keep}</h2>
{t_keep}

<h2><span class="pill warn">watch</span> &nbsp;Near misses &mdash; {n_watch}</h2>
<p class="tight">Not quoted, and failing at least one gate. Worth re-checking as
they season; the blocking gate is named.</p>
{t_watch}

<h2>Data quality and symbology flags</h2>
<p class="tight">Our internal ClickHouse symbology
(<code>{{VENUE}}-{{KIND}}-{{BASE}}{{QUOTE}}</code>) is not the venues' symbology, and
the mapping is not one-to-one. <b>{n_mapped} of {n_internal}</b> internal instruments
resolve to a live venue instrument ({n_gate_issuer} of them only after searching Gate's
issuer-suffix variants &mdash; our perp names drop the suffix that Gate puts in the base
currency). The remaining {n_unmatched} do not exist on the venue at all:</p>
{t_unmatched}
<div class="note warn">
<b>OKX has no USDC-margined perps.</b> A direct lookup of
<code>BTC-USDC-SWAP</code> returns error 51001 &ldquo;Instrument ID doesn't
exist&rdquo;, and there are zero USDC swaps in OKX's instrument list &mdash; yet
these two internal symbols took over 10,000 fills in ten days. Either the fills are
booked against the wrong instrument definition or the symbols are stale. Worth
resolving before either is used in sizing or P&amp;L attribution.
</div>

<h3>Possible duplicate tickers &mdash; {n_dup} pairs</h3>
<p class="tight">Two tickers whose prices agree to within a few bps are usually the
same underlying listed under different names, which splits one asset's venue count
in two and makes both look thinner than they are. These are <b>reported, never
auto-merged</b> &mdash; <code>USD1</code>/<code>USDC</code> below is a genuine false
positive (two different dollar stablecoins), which is exactly why a human confirms.</p>
{t_dup}

<h2>All assets &mdash; full metric table</h2>
<p class="tight">Every asset in the screen with every metric behind it. Click a header
to sort, type to filter, or export the whole thing to CSV. Both <b>mean</b> and
<b>median</b> daily volume are shown: the mean is proportional to the total flow
available to capture over the window, the median says what a typical day looks like,
and their ratio (<b>burstiness</b>) says how concentrated the flow is. Median across
the screen is {med_burst}; {n_bursty} assets are above 1.5, meaning their volume is
carried by a minority of days.</p>
{t_raw}

<h2>Every asset on every exchange &mdash; {n_venue_rows} instruments</h2>
<p class="tight">The same universe broken out per exchange: one row per asset per venue,
with that venue's own volume, share, open interest, spread, tick, funding and fees.
This is the table for leg-level decisions &mdash; <b>{n_legs_missing} instruments belong
to assets we already trade but are not currently quoted by us</b> (the
<b>We quote</b> column). Sorted by asset size, then by venue size within each asset.</p>
{t_venue}

<h2>Method and caveats</h2>
<ul>
<li><b>Mean and median, because they answer different questions.</b> Expected
maker revenue scales with <em>total</em> flow, so the mean is the honest level for
"how much is there to capture". The median is the reliability read &mdash; it cannot
be moved by one listing day or one liquidation cascade &mdash; so the <em>gates</em>
are set on the median and the <em>ranking</em> uses both. Where the two diverge
(burstiness &gt; 1.5) the flow is concentrated in a few days and the asset needs a
closer look either way. Momentum is 7-day median over 30-day median; the slope is a
log-linear fit in %/day.</li>
<li><b>Open interest</b> is a 30-day median of the daily cross-venue sum, excluding
Bitget (no history published). <code>OI venues</code> gives the number of venues
actually contributing, and <code>BGT OI share</code> the size of the hole. Binance
contributes only its trailing 30 days &mdash; the cap on its history endpoint.</li>
<li><b>Asset, not symbol.</b> Volume is summed across venues per underlying. Asset
identity comes from each venue's declared base currency, never from parsing the
symbol string &mdash; <code>UBUSDT</code> (crypto UB) and <code>UBERUSDT</code>
(Uber, an RWA perp) are different assets, and no string rule separates
<code>MUSDT</code> / <code>MUUSDT</code> / <code>MUUUSDT</code> correctly.</li>
<li><b>Leveraged tokens and dated futures are excluded</b> by rule, not by eye
(<code>CRCL3L</code> is a 3&times; product, not CRCL).</li>
<li><b>Spread is a live snapshot</b>, one observation per instrument, taken from the
venue-wide ticker endpoints. It is indicative only: a proper spread metric needs the
5-minute sampled series accumulating over time. Treat the tick-pinned/not-pinned
split as reliable and the exact bps as approximate.</li>
<li><b>Trade counts are Binance-only</b> &mdash; no other venue publishes them, so
the flow component leans on Binance as the asset-level proxy.</li>
<li><b>Our own P&amp;L is deliberately not an input.</b> These verdicts describe the
market, not our execution. An asset that looks weak here but earns well should be
kept, and vice versa &mdash; that comparison is a separate exercise.</li>
</ul>

<div class="foot">
Sources: Binance <code>fapi</code>/<code>dapi</code>, OKX v5, Bitget v2, Gate v4,
KuCoin futures + spot hosts, Kraken (USDT/USDC&rarr;USD). Reproduce with
<code>run_screen.py</code> then <code>build_report.py</code>; endpoint coverage and
gaps are documented in <code>DATA_COVERAGE.md</code>.
</div>
</div>
<script>
function wireTable(tblId, qId, cntId, csvId, fname){{
  var tbl=document.getElementById(tblId); if(!tbl) return;
  var tb=tbl.tBodies[0], rows=[].slice.call(tb.rows), dir={{}}, cur=null;
  var q=document.getElementById(qId), cnt=document.getElementById(cntId);
  function shown(){{ return rows.filter(function(r){{return r.style.display!=='none';}}); }}
  function count(){{ cnt.textContent = shown().length+' of '+rows.length+' rows'; }}
  [].forEach.call(tbl.tHead.rows[0].cells, function(th,i){{
    th.addEventListener('click', function(){{
      var num = th.dataset.type==='n';
      dir[i] = cur===i ? -(dir[i]||1) : (num?-1:1); cur=i;
      [].forEach.call(tbl.tHead.rows[0].cells, function(o){{
        o.querySelector('.sortcue').textContent=''; }});
      th.querySelector('.sortcue').textContent = dir[i]>0?' \u25B2':' \u25BC';
      var s=rows.slice().sort(function(a,b){{
        var x=a.cells[i].dataset.v, y=b.cells[i].dataset.v;
        if(num){{ var nx=parseFloat(x), ny=parseFloat(y);
          if(isNaN(nx)&&isNaN(ny))return 0; if(isNaN(nx))return 1; if(isNaN(ny))return -1;
          return (nx-ny)*dir[i]; }}
        return x.localeCompare(y)*dir[i]; }});
      s.forEach(function(r){{ tb.appendChild(r); }});
    }});
  }});
  q.addEventListener('input', function(){{
    var v=q.value.trim().toLowerCase();
    rows.forEach(function(r){{
      r.style.display = (!v || r.dataset.asset.indexOf(v)>-1) ? '' : 'none'; }});
    count();
  }});
  document.getElementById(csvId).addEventListener('click', function(){{
    var head=[].map.call(tbl.tHead.rows[0].cells,function(th){{
      return '"'+th.textContent.replace(/[\u25B2\u25BC]/g,'').trim()+'"'; }}).join(',');
    var body=shown().map(function(r){{
      return [].map.call(r.cells,function(td){{
        var v=td.dataset.v||''; return /^-?[\d.eE+]+$/.test(v)?v:'"'+v+'"'; }}).join(','); }});
    var blob=new Blob([head+'\n'+body.join('\n')],{{type:'text/csv'}});
    var a=document.createElement('a');
    a.href=URL.createObjectURL(blob); a.download=fname; a.click();
  }});
  count();
}}
wireTable('rawtable','rawq','rawcount','rawcsv','universe_by_asset.csv');
wireTable('ventable','venq','vencount','vencsv','universe_by_asset_venue.csv');
</script>
</body></html>
"""

if __name__ == "__main__":
    main()
