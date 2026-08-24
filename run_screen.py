"""End-to-end universe screen. Caches its frames to ClickHouse for the later stages.

    python run_screen.py [--days 30] [--top 90] [--no-deep]
    python run_screen.py --reuse-deep              # re-score the last cached history
    python run_screen.py --data-dir /tmp/screen    # + a local parquet mirror

Stages: specs -> asset keys -> internal mapping -> venue-wide snapshot -> shortlist
-> deep 30d daily history -> per-asset metrics.

Output goes to `asset_universe_cache.frames` keyed by run_date (see ch_cache.py),
not to local disk, so ch_load.py and build_report.py can run on a different host
than this fetch.
"""
from __future__ import annotations

import argparse
import io
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

import ch_cache
import ch_schema
from exchange_api_fetcher.daily import resolve
from exchange_api_fetcher.fx import FxTable
from exchange_api_fetcher.screen import build_shortlist, fetch_deep, fetch_snapshot
from exchange_api_fetcher.specs import fetch_all
from exchange_api_fetcher.symbology import (derive_asset_keys, map_internal,
                                            parse_internal_names)

logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(message)s")

# Via ch_schema so CH_URL / CH_USER / CH_PASS override every stage at once. This
# used to be a second hardcoded copy of the host, which meant setting CH_URL sent
# ch_load.py to one server while this stage kept reading strat__*_alex off the old
# one - a split-brain run that looks clean in the logs.
CH, AUTH = ch_schema.CH_URL, (ch_schema.CH_USER, ch_schema.CH_PASS)


def ch(sql: str) -> pd.DataFrame:
    r = requests.post(CH, params={"query": sql + " FORMAT TSVWithNames"}, auth=AUTH,
                      timeout=300)
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text), sep="\t")


def traded_instruments(days: int = 10) -> pd.DataFrame:
    frames = []
    for db in ("strat__tk_alex", "strat__hk_alex"):
        frames.append(ch(f"""
        WITH nm AS (SELECT symbol_id,
                     argMax(replaceAll(toString(name),'\\0',''), local_nanos) AS name
                    FROM {db}.symbols_record GROUP BY symbol_id)
        SELECT nm.name AS name, count() AS fills,
               min(toDate(e.local_nanos)) AS first_day,
               max(toDate(e.local_nanos)) AS last_day
        FROM {db}.dop_exec_record e
        LEFT JOIN nm ON CAST(e.symbol_id AS UInt64) = nm.symbol_id
        WHERE e.local_nanos >= now() - INTERVAL {days} DAY
        GROUP BY name"""))
    t = pd.concat(frames)
    t = t.groupby("name", as_index=False).agg(fills=("fills", "sum"),
                                             first_day=("first_day", "min"),
                                             last_day=("last_day", "max"))
    return t[t["name"].str.match(r"^[A-Z]{3}-(S|P|IP|F|IF)-")].copy()


def our_participation(mp: pd.DataFrame, spec: pd.DataFrame,
                      days: int = 7) -> pd.DataFrame:
    """Our own traded notional per (venue, symbol, mkt), in USD.

    The denominator problem: an asset's venue count is evidence that OTHER people
    trade it, but the volume we score is the venue's total - which includes us. On a
    thin book that is circular: we quote there, volume appears, the screen reads a
    liquid venue, we keep quoting. Measured 2026-08-24, our share of the book runs a
    0.4% median above $20M/day and a 4.4% median below $0.5M/day - with a p90 of 36%
    and a max of 110% (LTC on Bitget spot, where we ARE the book).

    notional = price x qty x contract_mult, joined through the internal map because
    the internal name carries no native symbol. INVERSE contracts are excluded: their
    qty is already denominated in USD, so the same formula overstates them by the
    price - OKX BTC-USD-SWAP read $1.04 TRILLION over 7 days before they were cut.
    """
    f = ch(f"""
    WITH nm AS (SELECT symbol_id,
                 argMax(replaceAll(toString(name),'\\0',''), local_nanos) AS name
                FROM strat__tk_alex.symbols_record GROUP BY symbol_id),
    ex AS (
        SELECT nm.name AS name, sum(e.price * e.qty) AS raw_notional,
               count() AS our_fills, argMax(e.price, e.local_nanos) AS last_px
        FROM strat__tk_alex.dop_exec_record e
        LEFT JOIN nm ON CAST(e.symbol_id AS UInt64) = nm.symbol_id
        WHERE e.local_nanos >= now() - INTERVAL {int(days)} DAY
        GROUP BY nm.name),
    po AS (
        -- our position = session + outside - invested (confirmed 2026-08-24).
        -- invested_position is NOT always zero - 14,143 rows carry it - so dropping
        -- it overstates the position wherever it is set.
        SELECT nm.name AS name,
               argMax(p.session_position, p.local_nanos)
             + argMax(p.outside_position, p.local_nanos)
             - argMax(p.invested_position, p.local_nanos) AS our_pos
        FROM strat__tk_alex.positions_record p
        LEFT JOIN nm ON CAST(p.symbol_id AS UInt64) = nm.symbol_id
        WHERE p.local_nanos >= now() - INTERVAL 12 HOUR
        GROUP BY nm.name)
    SELECT ex.name AS name, ex.raw_notional AS raw_notional, ex.our_fills AS our_fills,
           ex.last_px AS last_px, ifNull(po.our_pos, 0) AS our_pos
    FROM ex LEFT JOIN po ON po.name = ex.name""")
    cols = ["venue", "symbol", "mkt", "our_notional", "our_fills", "our_oi_usd"]
    if f.empty or mp.empty:
        return pd.DataFrame(columns=cols)
    link = mp.loc[mp["match_rule"] != "UNMATCHED",
                  ["name", "venue", "kind", "symbol"]].drop_duplicates("name")
    d = f.merge(link, on="name", how="inner")
    sp = spec.drop_duplicates(["venue", "symbol", "kind"])[
        ["venue", "symbol", "kind", "contract_mult", "inverse"]]
    d = d.merge(sp, on=["venue", "symbol", "kind"], how="left")
    d = d[d["inverse"] != 1]
    mult = d["contract_mult"].fillna(1.0)
    d["our_notional"] = d["raw_notional"] * mult
    # Position -> USD at the last traded price. Spot legs carry no OI, so this is
    # only meaningful on perps and is dropped for spot below.
    d["our_oi_usd"] = d["our_pos"].abs() * d["last_px"] * mult
    d["mkt"] = np.where(d["kind"] == "spot", "spot", "perp")
    d.loc[d["mkt"] == "spot", "our_oi_usd"] = np.nan
    return (d.groupby(["venue", "symbol", "mkt"], as_index=False)
             .agg(our_notional=("our_notional", "sum"),
                  our_fills=("our_fills", "sum"),
                  our_oi_usd=("our_oi_usd", "sum")))[cols]


def _med(s: pd.Series) -> float:
    """median() that returns NaN for an all-NaN group without a numpy warning.

    All-NaN is the NORMAL case for two of these, not an anomaly: trade counts are
    Binance-only (DATA_COVERAGE.md) and spot rows carry no funding, so most groups
    have nothing to take a median of. numpy's "Mean of empty slice" on every one of
    them buries the warnings that do mean something. NaN is the correct answer here
    and this returns it quietly.
    """
    s = s.dropna()
    return float(s.median()) if len(s) else np.nan


def find_duplicate_tickers(snap: pd.DataFrame, tol: float = 0.004) -> pd.DataFrame:
    """Same underlying listed under different tickers = a symbology collision we would
    otherwise count as two assets with one venue each.

    Detection: two asset_keys of the same class whose median price agrees within `tol`
    and whose tickers share a >=3-char prefix. Reported, never auto-merged.
    """
    # last > 0, not just notna: a venue reporting 0 makes the ratio below a divide
    # by zero, and a zero-priced "match" is not evidence of a ticker collision.
    a = (snap[(snap["is_excluded"] == 0) & (snap["last"] > 0)]
         .groupby(["asset_key", "asset_class"], as_index=False)
         .agg(px=("last", "median"), vol=("vol24h_usd", "sum"),
              venues=("venue", "nunique")))
    hits = []
    for cls, g in a.groupby("asset_class"):
        g = g.sort_values("px").reset_index(drop=True)
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                if g.px[j] > g.px[i] * (1 + tol):
                    break
                k1, k2 = g.asset_key[i], g.asset_key[j]
                pre = len({k1[:3], k2[:3]}) == 1
                if pre and k1 != k2:
                    hits.append({"asset_a": k1, "asset_b": k2, "class": cls,
                                 "px_a": g.px[i], "px_b": g.px[j],
                                 "diff_bps": (g.px[j] / g.px[i] - 1) * 1e4,
                                 "vol_a": g.vol[i], "vol_b": g.vol[j],
                                 "venues_a": g.venues[i], "venues_b": g.venues[j]})
    return pd.DataFrame(hits).sort_values("diff_bps") if hits else pd.DataFrame()


def asset_metrics(deep: pd.DataFrame, snap: pd.DataFrame, spec: pd.DataFrame,
                  part: pd.DataFrame | None = None) -> pd.DataFrame:
    """Per-asset level / trend / stability / structure, from the 30d daily history."""
    d = deep.copy()
    d["date"] = pd.to_datetime(d["date"])
    spec = spec.copy()
    spec["listed_since"] = pd.to_datetime(spec["listed_since"], errors="coerce")
    spec["mkt"] = np.where(spec["kind"] == "spot", "spot", "perp")
    key = spec[["venue", "symbol", "mkt", "asset_key", "asset_class", "listed_since",
                "tick", "maker_fee", "is_excluded"]] \
        .drop_duplicates(["venue", "symbol", "mkt"])
    d["mkt"] = np.where(d["market_type"] == "spot", "spot", "perp")
    d = d.merge(key, on=["venue", "symbol", "mkt"], how="left")
    d = d[d["is_excluded"] != 1]

    # Spot is measured but kept OUT of the perp aggregates below. The ADV/OI gates
    # describe the market we quote on; folding spot turnover into them would move
    # every asset's number and silently invalidate the calibrated thresholds.
    # Spot arrives as its own columns further down.
    spot = d[d["mkt"] == "spot"].copy()
    d = d[d["mkt"] == "perp"]

    # ---- per asset per day, summed across venues -----------------------------
    ad = d.groupby(["asset_key", "date"], as_index=False).agg(
        vol_usd=("vol_usd", "sum"),
        oi_usd=("oi_usd", "sum"),
        trades=("trades", "sum"),
        n_venues=("venue", "nunique"),
        px=("px_close", "median"),
        px_min=("px_close", "min"), px_max=("px_close", "max"),
        fund_min=("funding_sum", "min"), fund_max=("funding_sum", "max"),
        fund_med=("funding_sum", "median"),
        hl=("px_high", lambda s: np.nan), )
    # intraday range needs the per-venue rows, recompute properly
    rng = d.assign(hl_bps=(d["px_high"] - d["px_low"]) / d["px_close"] * 1e4) \
        .groupby(["asset_key", "date"], as_index=False)["hl_bps"].median()
    ad = ad.drop(columns=["hl"]).merge(rng, on=["asset_key", "date"], how="left")
    ad["px_spread_bps"] = (ad["px_max"] / ad["px_min"] - 1) * 1e4
    ad["fund_spread_bps"] = (ad["fund_max"] - ad["fund_min"]) * 1e4

    def agg(g: pd.DataFrame) -> pd.Series:
        g = g.sort_values("date")
        v = g["vol_usd"].replace(0, np.nan)
        v7, v30 = v.tail(7).median(), v.median()
        # ---- calendar-clean twins -------------------------------------------
        # Volume has a weekday cycle that is mild on crypto (weekend/weekday 0.96)
        # and brutal on RWA (equity 0.11, commodity 0.20 - measured 2026-08-23 over
        # the 30d panel). Every level and persistence metric below therefore reads
        # low for an RWA purely because two of every seven days are a weekend, and
        # the gates cannot tell that from a genuine decline.
        #
        # These are the SAME metrics over weekdays only. score() takes the better of
        # the two, so a weekend can help an asset but never penalise it - which is
        # the asymmetry we actually want: a name that trades through the weekend has
        # earned the higher number, a name that cannot should not be marked down for
        # it twice (once here, once in every window that contains a Saturday).
        # The window is held FIXED and weekend days are dropped from inside it -
        # NOT "the last 7 weekday observations", which reaches ~10 calendar days back
        # and measures a longer window rather than a cleaner one. Getting that wrong
        # made a rising crypto name read a 0.60x "calendar lift" that was pure window
        # length (RATS: 13.49 -> 10.76 by pulling in three older, quieter days).
        wd = g[g["date"].dt.dayofweek < 5]
        we = g[g["date"].dt.dayofweek >= 5]
        vwd = wd["vol_usd"].replace(0, np.nan)
        g7 = g.tail(7)
        v7_wd = (g7[g7["date"].dt.dayofweek < 5]["vol_usd"]
                 .replace(0, np.nan).median())
        v30_wd = vwd.median()
        # off-peak twin: latest weekday vs the weekday max inside the same trailing
        # 14 calendar days, so both sides of the ratio are weekdays.
        g14 = g.tail(14)
        w14 = g14[g14["date"].dt.dayofweek < 5].sort_values("date", ascending=False)
        dwd = w14["vol_usd"].to_numpy(dtype=float)
        dwd = dwd[dwd > 0]
        # --- onset detection: a name that listed mid-window must not be judged on
        # 30-day persistence. "Live" = a day with >10% of the asset's own recent
        # median volume; onset = the first such day.
        thresh = (v7 or v30 or 0) * 0.10
        live = (g["vol_usd"] >= thresh) & (g["vol_usd"] > 0)
        live_days = int(live.sum())
        onset_idx = int(np.argmax(live.values)) if live.any() else len(g)
        days_since_onset = len(g) - onset_idx
        post = g.iloc[onset_idx:]
        # newest-first, zeros dropped: the fast change metrics below index off this
        vd = g.sort_values("date", ascending=False)["vol_usd"]
        vd = vd[vd > 0].to_numpy(dtype=float)
        lv = np.log(v.dropna())
        slope = np.nan
        if len(lv) >= 8:
            x = np.arange(len(lv))
            slope = np.polyfit(x, lv.values, 1)[0]
            slope = (np.exp(slope) - 1) * 100          # %/day
        # same regression over the trailing 7 days only. vd is newest-first, so it is
        # reversed back to chronological order or the sign comes out backwards.
        # w7, NOT v7: v7 is the scalar 7d median set above and still owed to
        # vol_usd_med7 / vol_trend below. Rebinding it here put a 7-element array in
        # both of those for every asset, which crashed the load and made the lane-A
        # ADV gate (vol_usd_med7 >= g_adv) an array comparison.
        slope7 = np.nan
        w7 = vd[:7][::-1]
        if len(w7) >= 4:
            slope7 = np.polyfit(np.arange(len(w7)), np.log(w7), 1)[0]
            slope7 = (np.exp(slope7) - 1) * 100
        return pd.Series({
            "days": g["date"].nunique(),
            "vol_usd_med30": v30,
            "vol_usd_mean30": float(g["vol_usd"].mean()),
            "vol_usd_med7": v7,
            # mean/median > ~1.5 means the flow is concentrated in a few days
            "vol_burstiness": (float(g["vol_usd"].mean()) / v30) if v30 else np.nan,
            "vol_trend": (v7 / v30) if v30 else np.nan,
            # vol_trend is a LEVEL ratio, not a change: after a pump the 30d median
            # lags for weeks, so it stays huge and rising while the asset collapses
            # (BLESS 2026-08-12 read 38.8 with volume down 10x over six days). These
            # three can actually fall, so they are what a decay rule should read.
            "vol_d1": (vd[0] / vd[1]) if len(vd) > 1 and vd[1] > 0 else np.nan,
            # same weekday a week ago: volume has a big weekday cycle (MU trades 20x
            # more Mon-Fri than Sat-Sun), so a raw 1d change on a Monday is calendar,
            # not flow. This comparison cancels it.
            "vol_dow7": (vd[0] / vd[7]) if len(vd) > 7 and vd[7] > 0 else np.nan,
            # days since the 14d peak - with vol_off_peak this is the only pair that
            # says "rolling over" while a pump is still inside every window
            "vol_peak_age": (int(np.argmax(vd[:14])) if len(vd) else -1),
            # 7d version of vol_slope_pct_day. The 30d regression stays positive for
            # weeks after a pump ends (BLESS: +19.6 %/day over 30d while the 7d slope
            # read -35.6), so the short window is the one that reflects direction.
            "vol_slope7_pct_day": slope7,
            "vol_r37": (float(np.median(vd[:3])) / float(np.median(vd[:7]))
                        if len(vd) >= 7 and np.median(vd[:7]) > 0 else np.nan),
            "vol_off_peak": (vd[0] / vd[:14].max()
                             if len(vd) and vd[:14].max() > 0 else np.nan),
            # weekday-only twins. Same formulas, Mon-Fri only. See the note above.
            "vol_usd_med7_wd": v7_wd,
            "vol_usd_med30_wd": v30_wd,
            "days_above_5m_wd": (float((wd["vol_usd"] >= 5e6).mean())
                                 if len(wd) else np.nan),
            "vol_off_peak_wd": (dwd[0] / dwd.max()
                                if len(dwd) and dwd.max() > 0 else np.nan),
            # <1 means the asset goes quiet at weekends; ~1 means it does not. The
            # diagnostic behind the adjustment, so the correction is never invisible.
            "wknd_ratio": (float(we["vol_usd"].replace(0, np.nan).median()
                                 / vwd.median())
                           if len(we) and len(wd) and vwd.median() else np.nan),
            "vol_slope_pct_day": slope,
            "vol_logmad": (lv - lv.median()).abs().median() if len(lv) > 3 else np.nan,
            "days_above_5m": float((g["vol_usd"] >= 5e6).mean()),
            # persistence measured only over days the asset was actually live
            "days_above_5m_live": (float((post["vol_usd"] >= 5e6).mean())
                                   if len(post) else np.nan),
            "days_since_onset": days_since_onset,
            "live_days": live_days,
            "oi_usd_med30": _med(g["oi_usd"].replace(0, np.nan)),
            "oi_usd_mean30": g["oi_usd"].replace(0, np.nan).dropna().mean()
                             if g["oi_usd"].replace(0, np.nan).notna().any() else np.nan,
            "oi_days": int(g["oi_usd"].replace(0, np.nan).notna().sum()),
            "oi_trend": (g["oi_usd"].tail(7).median()
                         / g["oi_usd"].median()) if g["oi_usd"].median() else np.nan,
            "trades_med30": _med(g["trades"].replace(0, np.nan)),
            "n_venues": int(g["n_venues"].max()),
            "rv_bps_med": g["hl_bps"].median(),
            "px_spread_bps_med": g["px_spread_bps"].median(),
            "fund_bps_med": g["fund_med"].median() * 1e4,
            "fund_spread_bps_med": g["fund_spread_bps"].median(),
        })

    m = ad.groupby("asset_key").apply(agg, include_groups=False).reset_index()

    # ---- structure from the live snapshot ------------------------------------
    # perp only: spread/tick/venue-count/OI all describe the derivative market.
    # Before spot was fetched this filter was implicit; now it has to be explicit
    # or venues_perp and venue_hhi silently double-count.
    snap_all = snap
    snap = snap[snap.get("mkt", "perp") == "perp"] if "mkt" in snap.columns else snap
    s = snap[snap["is_excluded"] == 0].copy()
    s["listed_since"] = pd.to_datetime(s["listed_since"], errors="coerce")
    st = s.groupby("asset_key").agg(
        spread_bps_min=("spread_bps", "min"),
        spread_bps_med=("spread_bps", "median"),
        tick_bps_med=("tick_bps", "median"),
        venues_perp=("venue", "nunique"),
        maker_fee_min=("maker_fee", "min"),
        asset_class=("asset_class", "first"),
        equity_region=("equity_region", lambda x: next((v for v in x if v), "")),
        listed_since=("listed_since", "min"),
        min_notional_max=("min_notional", "max")).reset_index()
    m = m.merge(st, on="asset_key", how="left")
    # venue concentration
    vs = s.groupby(["asset_key", "venue"])["vol24h_usd"].sum().reset_index()
    tot = vs.groupby("asset_key")["vol24h_usd"].transform("sum")
    vs["sh"] = vs["vol24h_usd"] / tot.replace(0, np.nan)
    hhi = vs.groupby("asset_key")["sh"].apply(lambda x: float((x ** 2).sum()))
    m = m.merge(hhi.rename("venue_hhi"), on="asset_key", how="left")

    # ---- per-venue DEPTH, from the 30d history -------------------------------
    # venue_hhi above is one 24h reading off the live snapshot: fine as a live
    # diagnostic, too noisy to score on. These are the same shape over 30 days.
    #
    # vol_venue_med is the point: the MEDIAN venue's daily volume, not the sum. An
    # asset doing $16M across five venues where one carries 82% has a median venue
    # of ~$1M, and that is what quoting it actually feels like. Summed volume cannot
    # tell that apart from $16M spread evenly, which is a completely different book.
    pv = d.groupby(["asset_key", "venue"])["vol_usd"].median()
    m = m.merge(pv.groupby("asset_key").median().rename("vol_venue_med"),
                on="asset_key", how="left")
    m = m.merge(pv.groupby("asset_key").mean().rename("vol_venue_mean"),
                on="asset_key", how="left")
    tv = d.groupby(["asset_key", "venue"])["vol_usd"].sum()
    tsh = tv / tv.groupby("asset_key").transform("sum").replace(0, np.nan)
    m = m.merge(tsh.pow(2).groupby("asset_key").sum().rename("vol_hhi_30d"),
                on="asset_key", how="left")
    # top_share is hhi's readable twin: "Binance is 82% of this" lands where 0.694
    # does not, and it is the number to put in front of a human.
    m = m.merge(tsh.groupby("asset_key").max().rename("vol_top_share"),
                on="asset_key", how="left")
    # ---- the per-venue volume VECTOR, largest first --------------------------
    # Stored as columns rather than collapsed to a count so the liquidity floor stays
    # a dashboard knob: score() counts how many of these clear g_venue_adv, and the
    # threshold can be swept without re-running the pipeline. Five venues, so five
    # columns; absent venues stay NaN and never count.
    #
    # This is what n_venues could not say. Across the universe the median asset's
    # top venue carries 75% of its volume and its 3rd carries 6.6% ($1.1M) - so a
    # plain venue COUNT passes 96% of assets through a 3-venue gate, which is not a
    # gate. RATS: BIN $6.8M, then $0.98M, $0.96M, $0.19M.
    ranked = (d.groupby(["asset_key", "venue"])["vol_usd"].median()
              .rename("v").reset_index()
              .sort_values(["asset_key", "v"], ascending=[True, False]))
    ranked["rk"] = ranked.groupby("asset_key").cumcount() + 1
    wide = (ranked[ranked["rk"] <= 5]
            .pivot(index="asset_key", columns="rk", values="v")
            .rename(columns=lambda i: f"vol_venue_{i}"))
    for i in range(1, 6):
        if f"vol_venue_{i}" not in wide.columns:
            wide[f"vol_venue_{i}"] = np.nan
    m = m.merge(wide[[f"vol_venue_{i}" for i in range(1, 6)]], on="asset_key", how="left")

    # ---- how much of this book is US -----------------------------------------
    # Matched to the participation window (7d) on both sides, or the ratio compares
    # a week of our flow against a month of the venue's.
    if part is not None and len(part):
        recent = d[d["date"] >= d["date"].max() - pd.Timedelta(days=6)]
        mv = (recent.groupby(["asset_key", "venue", "symbol"], as_index=False)
                    .agg(mkt7=("vol_usd", "sum")))
        pp = part[part["mkt"] == "perp"]
        mv = mv.merge(pp[["venue", "symbol", "our_notional", "our_fills"]],
                      on=["venue", "symbol"], how="left")
        mv["our_notional"] = mv["our_notional"].fillna(0.0)
        mv["leg_share"] = mv["our_notional"] / mv["mkt7"].replace(0, np.nan)
        ag = mv.groupby("asset_key").agg(
            our_vol_7d=("our_notional", "sum"), mkt_vol_7d=("mkt7", "sum"),
            our_share_max_leg=("leg_share", "max"),
            our_fills_7d=("our_fills", "sum"))
        ag["our_vol_share"] = ag["our_vol_7d"] / ag["mkt_vol_7d"].replace(0, np.nan)
        # OI share, latest day, perp only
        last = d[d["date"] == d["date"].max()]
        oi = (last.groupby(["asset_key", "venue", "symbol"], as_index=False)
                  .agg(v_oi=("oi_usd", "sum"))
                  .merge(pp[["venue", "symbol", "our_oi_usd"]],
                         on=["venue", "symbol"], how="left"))
        oi["our_oi_usd"] = oi["our_oi_usd"].fillna(0.0)
        # Both sides restricted to legs the venue actually reports OI for. Bitget
        # publishes no OI history, so counting our BGT position in the numerator
        # while its OI is missing from the denominator inflates the ratio - RATS read
        # 1.38% aggregate against a 0.84% worst leg, which is arithmetically
        # impossible for a share.
        oi = oi[oi["v_oi"] > 0]
        oi["leg_oi_share"] = oi["our_oi_usd"] / oi["v_oi"]
        oa = oi.groupby("asset_key").agg(our_oi_usd=("our_oi_usd", "sum"),
                                         venue_oi_usd=("v_oi", "sum"),
                                         our_oi_share_max_leg=("leg_oi_share", "max"))
        oa["our_oi_share"] = (oa["our_oi_usd"]
                              / oa["venue_oi_usd"].replace(0, np.nan))
        ag = ag.join(oa)
        m = m.merge(ag.reset_index(), on="asset_key", how="left")
    else:
        for c in ("our_vol_7d", "mkt_vol_7d", "our_share_max_leg", "our_fills_7d",
                  "our_vol_share", "our_oi_usd", "venue_oi_usd", "our_oi_share",
                  "our_oi_share_max_leg"):
            m[c] = np.nan

    if len(spot):
        spv = spot.groupby(["asset_key", "venue"])["vol_usd"].median()
        m = m.merge(spv.groupby("asset_key").median().rename("spot_vol_venue_med"),
                    on="asset_key", how="left")
    else:
        m["spot_vol_venue_med"] = np.nan
    # Bitget publishes no OI HISTORY, so the 30d OI series omits it. Quantify the
    # understatement from the live snapshot, where Bitget's OI *is* available.
    oi_live = s.groupby("asset_key")["oi_usd"].sum().rename("oi_usd_live")
    oi_bgt = (s[s["venue"] == "BGT"].groupby("asset_key")["oi_usd"].sum()
              .rename("oi_usd_live_bgt"))
    m = m.merge(oi_live, on="asset_key", how="left").merge(oi_bgt, on="asset_key",
                                                           how="left")
    m["oi_usd_live_bgt"] = m["oi_usd_live_bgt"].fillna(0.0)
    m["oi_bgt_share"] = (m["oi_usd_live_bgt"] / m["oi_usd_live"].replace(0, np.nan))
    # venues whose OI history we actually have (BGT excluded by construction)
    oi_venues = (s[s["venue"] != "BGT"].groupby("asset_key")["venue"].nunique()
                 .rename("oi_venues"))
    m = m.merge(oi_venues, on="asset_key", how="left")
    m["oi_venues"] = m["oi_venues"].fillna(0).astype(int)
    # ---- spot ---------------------------------------------------------------
    # venues_spot counts LISTED pairs; a listing with no flow is not a hedge. The
    # _live columns come from the 30d spot history and are the ones to trust.
    sp = spec[(spec["kind"] == "spot") & (spec["active"] == 1) & (spec["is_excluded"] == 0)]
    m = m.merge(sp.groupby("asset_key")["venue"].nunique().rename("venues_spot"),
                on="asset_key", how="left")
    m["venues_spot"] = m["venues_spot"].fillna(0).astype(int)

    if len(spot):
        sd = spot.groupby(["asset_key", "date"], as_index=False).agg(
            vol_usd=("vol_usd", "sum"), n_venues=("venue", "nunique"))
        sm = sd.groupby("asset_key").agg(
            spot_vol_usd_med30=("vol_usd", "median"),
            spot_vol_usd_mean30=("vol_usd", "mean"),
            spot_days=("date", "nunique"),
            spot_venues_live=("n_venues", "max")).reset_index()
        sm["spot_vol_usd_med7"] = (spot.groupby(["asset_key", "date"])["vol_usd"].sum()
                                   .groupby("asset_key").apply(lambda x: x.tail(7).median())
                                   .values)
        m = m.merge(sm, on="asset_key", how="left")
    for c in ("spot_vol_usd_med30", "spot_vol_usd_mean30", "spot_vol_usd_med7"):
        if c not in m:
            m[c] = np.nan
    for c in ("spot_days", "spot_venues_live"):
        if c not in m:
            m[c] = 0
        m[c] = m[c].fillna(0).astype(int)
    # perp/spot ratio: a very high number means the derivative floats free of any
    # deliverable market, which is where hedging gets expensive
    m["perp_spot_ratio"] = m["vol_usd_med30"] / m["spot_vol_usd_med30"].replace(0, np.nan)
    m["spot_share"] = (m["spot_vol_usd_med30"]
                       / (m["spot_vol_usd_med30"].fillna(0) + m["vol_usd_med30"]))
    m["oi_to_adv"] = m["oi_usd_med30"] / m["vol_usd_med30"]
    m["age_days"] = (pd.Timestamp.utcnow().tz_localize(None)
                     - pd.to_datetime(m["listed_since"])).dt.days
    return m


def venue_metrics(deep: pd.DataFrame, snap: pd.DataFrame, spec: pd.DataFrame,
                  imap: pd.DataFrame) -> pd.DataFrame:
    """One row per (asset, venue): the per-exchange breakdown behind each asset."""
    d = deep.copy()
    d["date"] = pd.to_datetime(d["date"])
    spec = spec.copy()
    spec["mkt"] = np.where(spec["kind"] == "spot", "spot", "perp")
    key = spec[["venue", "symbol", "mkt", "asset_key", "asset_class", "is_excluded"]] \
        .drop_duplicates(["venue", "symbol", "mkt"])
    d["mkt"] = np.where(d["market_type"] == "spot", "spot", "perp")
    d = d.merge(key, on=["venue", "symbol", "mkt"], how="left")
    d = d[d["is_excluded"] != 1]

    # market_type is part of the key: BIN lists BTCUSDT as both spot and perp, so
    # (asset, venue, symbol) alone merges the two into one row and loses whichever
    # sorts last.
    g = d.groupby(["asset_key", "venue", "symbol", "market_type"], as_index=False).agg(
        vol_usd_mean30=("vol_usd", "mean"),
        vol_usd_med30=("vol_usd", "median"),
        oi_usd_med30=("oi_usd", "median"),
        oi_days=("oi_usd", lambda x: int(x.notna().sum())),
        trades_med30=("trades", "median"),
        fund_bps_med=("funding_sum", lambda x: _med(x) * 1e4),
        fund_iv_h=("funding_interval_h", "median"),
        px_close=("px_close", "last"),
        days=("date", "nunique"))
    g["vol_usd_med7"] = d.sort_values("date").groupby(
        ["asset_key", "venue", "symbol", "market_type"])["vol_usd"].apply(
        lambda x: x.tail(7).median()).values
    g["vol_trend"] = g["vol_usd_med7"] / g["vol_usd_med30"].replace(0, np.nan)

    # live structure per instrument
    sl = snap[["venue", "symbol", "mkt", "spread_bps", "tick_bps", "oi_usd",
               "vol24h_usd", "maker_fee", "taker_fee", "min_notional", "listed_since",
               "quote", "contract_mult"]].rename(columns={"oi_usd": "oi_usd_live"}) \
        .drop_duplicates(["venue", "symbol", "mkt"])
    g["mkt"] = np.where(g["market_type"] == "spot", "spot", "perp")
    g = g.merge(sl, on=["venue", "symbol", "mkt"], how="left")
    # share is within market type - a spot leg's share of the perp market is not a
    # meaningful number, and mixing them makes both wrong
    tot = g.groupby(["asset_key", "mkt"])["vol_usd_mean30"].transform("sum")
    g["vol_share"] = g["vol_usd_mean30"] / tot.replace(0, np.nan)
    # do we quote this exact instrument?
    ours = set(zip(imap["venue"].fillna(""), imap["symbol"].fillna("")))
    g["we_quote"] = [int((v, s_) in ours) for v, s_ in zip(g["venue"], g["symbol"])]
    g = g.sort_values(["asset_key", "mkt", "vol_usd_mean30"],
                      ascending=[True, True, False])
    return g.drop(columns=["mkt"])          # helper only; market_type is the real column


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top", type=int, default=90)
    ap.add_argument("--no-deep", action="store_true")
    ap.add_argument("--no-spot", action="store_true",
                    help="skip spot legs (perp-only, the pre-2026-08-10 behaviour)")
    ap.add_argument("--reuse-deep", action="store_true",
                    help="reuse the last cached deep history instead of refetching")
    ap.add_argument("--run-date", default=None,
                    help="cache key, YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--data-dir", default=None, metavar="DIR",
                    help="also mirror frames to local parquet (debugging only)")
    ap.add_argument("--no-cache", action="store_true",
                    help="skip ClickHouse; requires --data-dir")
    a = ap.parse_args()
    # UTC, not date.today(): every bar boundary in this pipeline is a true UTC day
    # (PIPELINE.md 2.1), and the box is America/New_York. A cron firing at 23:20
    # local is already 03:20 the next UTC day, so a local date silently fetches one
    # day less than it should.
    today = datetime.now(timezone.utc).date()
    end = today - timedelta(days=1)
    start = end - timedelta(days=a.days - 1)
    run_date = date.fromisoformat(a.run_date) if a.run_date else today

    if a.no_cache and not a.data_dir:
        ap.error("--no-cache leaves nowhere to put the frames; pass --data-dir too")

    # The stage handoff lives in ClickHouse so ch_load / build_report can run on a
    # different box than the fetch. --data-dir is a debugging mirror, not the path.
    mirror = Path(a.data_dir) if a.data_dir else None
    if mirror:
        mirror.mkdir(parents=True, exist_ok=True)
    ch = None
    if not a.no_cache:
        ch = ch_cache.client()
        ch_cache.create_all(ch)

    def emit(df: pd.DataFrame, name: str) -> None:
        if ch is not None:
            kb = ch_cache.put_frame(df, name, run_date, ch) / 1024
            print(f"   -> cached {name} ({len(df)} rows, {kb:.0f} KB)")
        if mirror is not None:
            df.to_parquet(mirror / f"{name}.parquet", index=False)

    print("== 1. instrument specs ==")
    spec = derive_asset_keys(fetch_all())
    emit(spec, "instrument_ref")
    print(f"   {len(spec)} instruments, {spec.active.sum()} active, "
          f"{spec.is_excluded.sum()} excluded")

    print("== 2. internal symbology mapping ==")
    trd = traded_instruments()
    mp = map_internal(parse_internal_names(trd["name"]), spec).merge(trd, on="name")
    emit(mp, "internal_map")
    n_ok = (mp.match_rule != "UNMATCHED").sum()
    print(f"   {n_ok}/{len(mp)} internal instruments mapped; "
          f"rules={mp.match_rule.value_counts().to_dict()}")

    print("== 3. venue-wide snapshot ==")
    fx = FxTable(["USDT", "USDC", "USD"], start, today)
    snap = fetch_snapshot(spec, fx)
    emit(snap, "snapshot")
    print(f"   {len(snap)} perp instruments, ${snap.vol24h_usd.sum()/1e9:.1f}B 24h")

    dup = find_duplicate_tickers(snap)
    emit(dup, "dup_tickers")
    print(f"   {len(dup)} possible duplicate-ticker pairs")

    traded_assets = set(mp["asset_key"].dropna())
    # Not persisted: the shortlist is consumed in this process to pick the deep-fetch
    # targets, and nothing downstream ever read data/shortlist.parquet.
    sl = build_shortlist(snap, traded_assets, top_n=a.top)
    print(f"   shortlist {len(sl)} assets "
          f"({int(sl.traded.sum())} traded / {int((~sl.traded).sum())} candidates)")

    if a.no_deep:
        return
    reused = None
    if a.reuse_deep:
        # Whatever the last run cached, from any box - the point of moving this off
        # local disk. Refetch rather than die if there is nothing cached yet.
        try:
            reused = (pd.read_parquet(mirror / "deep_daily.parquet")
                      if a.no_cache else ch_cache.get_frame("deep_daily", ch=ch))
        except (KeyError, FileNotFoundError, OSError) as e:
            print(f"   --reuse-deep: no cached deep history ({e}); refetching")
    if reused is not None:
        deep = reused
        # the spec fixes only REMOVE instruments, so the cached pull is a superset
        mk = np.where(snap["kind"] == "spot", "spot", "perp")
        live = set(zip(snap.venue, snap.symbol, mk))
        before = len(deep)
        dmk = np.where(deep["market_type"] == "spot", "spot", "perp")
        deep = deep[[(v, s_, m) in live
                     for v, s_, m in zip(deep.venue, deep.symbol, dmk)]]
        print(f"== 4. reusing cached deep history: {before} -> {len(deep)} rows "
              f"after re-applying the spec filters ==")
    else:
        print(f"== 4. deep history {start} .. {end} ==")
        kinds = ["linear_perp"] + ([] if a.no_spot else ["spot"])
        tgt = snap[snap["asset_key"].isin(sl["asset_key"]) & (snap["is_excluded"] == 0)
                   & (snap["active"] == 1) & (snap["kind"].isin(kinds))]
        # exact_symbol instruments (OKX XPERP) carry an instId no formula can rebuild,
        # so their symbol is passed through and the round-trip check below would be
        # comparing a value against itself. They are exempted rather than silently
        # "passing": the check still has to mean something for the other 1,400.
        exact = (tgt["exact_symbol"] == 1) if "exact_symbol" in tgt.columns \
            else pd.Series(False, index=tgt.index)
        inst = [resolve(r.venue, r.kind, r.base_raw, r.quote,
                        symbol=r.symbol if ex else None)
                for r, ex in zip(tgt.itertuples(), exact)]
        ok = [i for i, r, ex in zip(inst, tgt.itertuples(), exact)
              if ex or i.symbol == r.symbol]
        nsp = sum(i.market_type == "spot" for i in ok)
        n_exact = int(exact.sum())
        print(f"   {len(ok)}/{len(inst)} round-tripped to a native symbol "
              f"({len(ok) - nsp} perp, {nsp} spot"
              + (f", {n_exact} passed through verbatim" if n_exact else "") + ")")
        deep = fetch_deep(ok, start, end, fx, workers=8)
        print(f"   {len(deep)} daily rows")

    # Emitted on the reuse path too, so every run_date is self-contained. Without
    # this a --reuse-deep run would leave its own run_date missing deep_daily,
    # ch_load would judge it incomplete, and fall back to loading the *previous*
    # run's metrics under a silent one-day lag.
    emit(deep, "deep_daily")

    print("== 5. asset metrics ==")
    part = our_participation(mp, spec)
    if len(part):
        print(f"   our own flow: {len(part)} legs, "
              f"${part.our_notional.sum()/1e6:,.0f}M over 7d")
    m = asset_metrics(deep, snap, spec, part=part)
    m["traded"] = m["asset_key"].isin(traded_assets)
    emit(m, "asset_metrics")
    vm = venue_metrics(deep, snap, spec, mp)
    emit(vm, "venue_metrics")
    print(f"   {len(vm)} asset x venue rows, {len(m)} assets scored")
    print(f"   run_date {run_date} cached to {ch_cache.CACHE_DB}"
          + (f", mirrored to {mirror}" if mirror else ""))


if __name__ == "__main__":
    main()
