"""Universe calibration dashboard - a local web UI over symbol_stats.

    python dashboard/app.py                 # http://127.0.0.1:8815
    python dashboard/app.py --host 0.0.0.0  # to serve it from the ClickHouse box

Everything is read live from ClickHouse; nothing is cached to disk. The one piece
of real logic here is /api/screen, which re-runs `build_report.score()` - the same
function the nightly loader and the HTML report use - against whatever gate and
weight parameters the UI sends. That is deliberate: the point of this tool is to
answer "what happens to the universe if the ADV gate moves to $20M", and an
answer computed by a reimplementation in JS would drift from the real screen
within a week.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ch_schema                                    # noqa: E402
from build_report import DEFAULTS, score            # noqa: E402

HERE = Path(__file__).parent
STATIC = HERE / "static"
DB = ch_schema.DB

app = FastAPI(title="Universe calibration")


# --------------------------------------------------------------------------- #
# ClickHouse
# --------------------------------------------------------------------------- #

def q(sql: str) -> pd.DataFrame:
    """Query -> DataFrame.

    `na_values=["\\\\N"]` is load-bearing: ClickHouse writes NULL as a literal
    \\N in TSV, which pandas otherwise reads as the *string* "\\N" - and one NULL
    anywhere turns the whole column to object dtype, so the JSON ships numbers as
    text and every chart silently plots nothing.
    """
    import io
    import requests
    r = requests.post(ch_schema.CH_URL, data=(sql + " FORMAT TSVWithNames").encode(),
                      auth=(ch_schema.CH_USER, ch_schema.CH_PASS), timeout=120)
    if r.status_code != 200:
        raise HTTPException(500, f"ClickHouse: {r.text[:400]}")
    if not r.text.strip():
        return pd.DataFrame()
    return pd.read_csv(io.StringIO(r.text), sep="\t", na_values=["\\N"],
                       keep_default_na=True)


def jsonable(df: pd.DataFrame) -> list[dict]:
    """NaN/NaT -> None so the JSON is valid and the UI can show an em-dash."""
    return df.astype(object).where(pd.notna(df), None).to_dict("records")


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

@app.get("/api/meta")
def meta():
    runs = q(f"SELECT DISTINCT run_date FROM {DB}.screen_runs "
             f"ORDER BY run_date DESC")
    venues = q(f"SELECT DISTINCT venue FROM {DB}.instrument_daily ORDER BY venue")
    classes = q(f"SELECT asset_class, count() AS n FROM {DB}.screen_runs "
                f"WHERE run_date = (SELECT max(run_date) FROM {DB}.screen_runs) "
                f"GROUP BY asset_class ORDER BY n DESC")
    dates = q(f"SELECT min(date) AS d0, max(date) AS d1, "
              f"count() AS rows FROM {DB}.instrument_daily")
    return {
        "run_dates": runs["run_date"].tolist() if len(runs) else [],
        "venues": venues["venue"].tolist() if len(venues) else [],
        "classes": jsonable(classes),
        "daily": jsonable(dates)[0] if len(dates) else {},
        "defaults": DEFAULTS,
    }


def _fast_change() -> pd.DataFrame:
    """Fast, *directional* volume change per asset, straight off the daily series.

    The screen's vol_trend is 7d-median / 30d-median. That is a level-vs-baseline
    ratio, not a change: after a pump the 30d median lags for weeks, so the ratio
    stays enormous and RISING while the asset collapses. BLESS on 2026-08-12 read
    38.8x while volume had fallen 10x from its peak over six days. These metrics
    can actually go down:
      d1       last day / previous day        - fastest; read it next to the weekday
      dow7     last day / same weekday a week ago - calendar-neutral 1-step change
      off_peak last day / max of the last 14d  - the "rolling over" signal
      peak_age days since that 14d peak       - is it 2 days off the top or 13
      slope7   OLS slope of ln(volume) over 7d - the week's shape, %/day
      r37      3d median / 7d median           - what decay_fast reads

    Two traps make the obvious choices wrong here:

    1. WINDOW. Any window that still contains a pump reports "up" for weeks after the
       pump ends. TUT on 2026-08-12 was halving daily yet read +97 %/day on a 7d slope,
       +74 %/day on a 5d compound rate, and +14,287% week-over-week - all correct, all
       useless for "is it rolling over". A 5d average of daily changes cannot fix this:
       the geometric mean of n daily changes collapses to the endpoint-to-endpoint rate.
       Only off_peak/peak_age answer the question, because they are measured from the
       high rather than over a window.

    2. CALENDAR. Volume has a large weekday cycle, and it is brutal on RWA: MU trades
       $1.2-3.0B Mon-Fri and $96-433M Sat-Sun, a 20x swing. BTC is 2-3x. So on a Monday
       every RWA name shows a +1000% "1d change" that is pure calendar. dow7 compares
       like weekday with like weekday; the 7d windows in slope7/r37 each hold one of
       every weekday, so they are neutral by construction. d1 is NOT - hence the
       weekday shown beside it in the UI.
    """
    return q(f"""
        WITH s AS (
            SELECT asset_key,
                   -- assumeNotNull: vol_usd is Nullable in the view and array
                   -- aggregates refuse Nullable; the WHERE already excludes nulls
                   arraySort(x -> -toUInt32(x.1),
                             groupArray((date, assumeNotNull(vol_usd)))) AS a,
                   arrayMap(x -> x.2, a) AS v,
                   -- reversed: v is newest-first, a regression needs chronological
                   -- order or the sign comes out backwards
                   arrayReverse(arraySlice(v, 1, 7)) AS v7
            FROM {DB}.asset_daily
            WHERE date >= today() - 21 AND vol_usd > 0
            GROUP BY asset_key)
        SELECT asset_key,
               a[1].1                                       AS chg_through,
               v[1]                                         AS vol_last,
               if(length(v) > 1, v[2], NULL)                 AS vol_prev,
               if(length(v) > 1 AND v[2] > 0, v[1] / v[2], NULL) AS vol_d1,
               if(length(v) >= 8 AND v[8] > 0, v[1] / v[8], NULL) AS vol_dow7,
               indexOf(arraySlice(v, 1, 14),
                       arrayMax(arraySlice(v, 1, 14))) - 1 AS vol_peak_age,
               if(length(v7) >= 4,
                  (exp(arrayReduce('simpleLinearRegression',
                       arrayMap(x -> toFloat64(x), arrayEnumerate(v7)),
                       arrayMap(x -> log(x), v7)).1) - 1) * 100, NULL)
                                                            AS vol_slope7_pct_day,
               if(length(v) >= 7 AND arrayAvg(arraySlice(v, 1, 7)) > 0,
                  arrayReduce('median', arraySlice(v, 1, 3))
                  / arrayReduce('median', arraySlice(v, 1, 7)), NULL) AS vol_r37,
               if(arrayMax(arraySlice(v, 1, 14)) > 0,
                  v[1] / arrayMax(arraySlice(v, 1, 14)), NULL) AS vol_off_peak
        FROM s""")


def _metrics(run_date: str | None) -> pd.DataFrame:
    where = (f"run_date = '{run_date}'" if run_date
             else f"run_date = (SELECT max(run_date) FROM {DB}.screen_runs)")
    d = q(f"SELECT * FROM {DB}.screen_runs WHERE {where}")
    if d.empty:
        raise HTTPException(404, "no screen_runs rows - has the loader run?")
    d["traded"] = d["traded"].astype(bool)
    # Computed live rather than read from screen_runs so it is right today instead of
    # after the next nightly run, and so it can never disagree with the detail charts.
    #
    # screen_runs now HAS these columns too (added for the nightly path), but they stay
    # zero until run_screen next writes them. Drop them before the merge: pandas would
    # otherwise suffix both sides _x/_y, score() would find no `vol_off_peak`, and the
    # fast-decay rule would silently no-op instead of erroring.
    live = _fast_change()
    dup = [c for c in live.columns if c != "asset_key" and c in d.columns]
    return d.drop(columns=dup).merge(live, on="asset_key", how="left")


@app.get("/api/screen")
def screen_impl(run_date: str | None = None,
                g_adv: float = DEFAULTS["g_adv"],
                g_persist: float = DEFAULTS["g_persist"],
                g_venues: float = DEFAULTS["g_venues"],
                g_oi: float = DEFAULTS["g_oi"],
                lane_days: int = DEFAULTS["lane_days"],
                new_listing_days: int = DEFAULTS["new_listing_days"],
                tick_pinned_ratio: float = DEFAULTS["tick_pinned_ratio"],
                decay_trend: float = DEFAULTS["decay_trend"],
                decay_slope: float = DEFAULTS["decay_slope"],
                decay_off_peak: float = DEFAULTS["decay_off_peak"],
                decay_r37: float = DEFAULTS["decay_r37"],
                n_fail_drop: int = DEFAULTS["n_fail_drop"],
                w_flow: float = DEFAULTS["w_flow"],
                w_structure: float = DEFAULTS["w_structure"],
                w_carry: float = DEFAULTS["w_carry"],
                w_friction: float = DEFAULTS["w_friction"]):
    """Re-score a run under caller-supplied parameters.

    Returns both the recomputed verdicts and the baseline (DEFAULTS) verdicts, so
    the UI can show what *moved* rather than just what is.
    """
    m = _metrics(run_date)
    params = dict(g_adv=g_adv, g_persist=g_persist, g_venues=g_venues, g_oi=g_oi,
                  lane_days=lane_days, new_listing_days=new_listing_days,
                  tick_pinned_ratio=tick_pinned_ratio, decay_trend=decay_trend,
                  decay_slope=decay_slope, decay_off_peak=decay_off_peak,
                  decay_r37=decay_r37, n_fail_drop=n_fail_drop,
                  w_flow=w_flow, w_structure=w_structure, w_carry=w_carry,
                  w_friction=w_friction)
    cur = score(m, params)
    base = score(m)[["asset_key", "verdict", "composite", "n_fail"]].rename(
        columns={"verdict": "base_verdict", "composite": "base_composite",
                 "n_fail": "base_n_fail"})
    out = cur.merge(base, on="asset_key", how="left")
    out["moved"] = out["verdict"] != out["base_verdict"]
    for c in ("gate_adv", "gate_persist", "gate_venues", "gate_oi",
              "new_listing", "tick_pinned", "decaying", "decay_slow",
              "decay_fast", "traded"):
        out[c] = out[c].astype(bool)
    return JSONResponse({
        "params": params,
        "add_bar": float(out["add_bar"].iloc[0]),
        "counts": out["verdict"].value_counts().to_dict(),
        "base_counts": out["base_verdict"].value_counts().to_dict(),
        "rows": jsonable(out),
    })


@app.get("/api/venues")
def venues(run_date: str | None = None):
    where = (f"run_date = '{run_date}'" if run_date
             else f"run_date = (SELECT max(run_date) FROM {DB}.screen_runs_venue)")
    d = q(f"SELECT * FROM {DB}.screen_runs_venue WHERE {where} "
          f"ORDER BY vol_usd_med30 DESC")
    return JSONResponse(jsonable(d))


@app.get("/api/series")
def series(asset: str = Query(...), days: int = 400):
    """Per-venue daily series for one asset, plus the asset-level roll-up."""
    a = asset.replace("'", "")
    byvenue = q(f"""
        SELECT date, venue, market_type,
               sum(vol_usd) AS vol_usd, sum(oi_usd) AS oi_usd,
               avg(px_close / scale_factor) AS px, avg(funding_apr_pct) AS funding_apr_pct,
               sum(trades) AS trades
        FROM {DB}.instrument_daily
        WHERE asset_key = '{a}' AND date >= today() - {int(days)}
        GROUP BY date, venue, market_type ORDER BY date, venue""")
    total = q(f"""
        SELECT date, vol_usd, oi_usd, n_venues,
               px_spread_bps, usdc_basis_bps, funding_apr_spread_pct
        FROM {DB}.asset_daily
        WHERE asset_key = '{a}' AND date >= today() - {int(days)} ORDER BY date""")
    hist = q(f"""
        SELECT run_date, verdict, composite, add_bar,
               n_fail, vol_usd_med30, oi_usd_med30, spread_bps_med, tick_bps_med
        FROM {DB}.screen_runs WHERE asset_key = '{a}' ORDER BY run_date""")
    # Leg table leads with what is happening NOW, not the 30d median. vol24h_usd is the
    # rolling 24h from the snapshot taken during the run; the medians are the screen's
    # scoring inputs and are deliberately slow. `through` dates each leg's last daily row
    # so a leg that stopped updating is visible instead of just looking quiet.
    legs = q(f"""
        WITH d AS (
            SELECT venue, symbol, market_type, max(date) AS through,
                   argMax(vol_usd, date) AS vol_usd_last,
                   argMax(oi_usd, date)  AS oi_usd_last,
                   arrayMap(x -> x.2, arraySort(x -> -toUInt32(x.1),
                            groupArray((date, assumeNotNull(vol_usd))))) AS v,
                   arrayReverse(arraySlice(v, 1, 7)) AS v7,
                   if(length(v) > 1 AND v[2] > 0, v[1] / v[2], NULL) AS vol_d1,
                   if(length(v) >= 8 AND v[8] > 0, v[1] / v[8], NULL) AS vol_dow7,
                   indexOf(arraySlice(v, 1, 14),
                           arrayMax(arraySlice(v, 1, 14))) - 1 AS vol_peak_age,
                   if(length(v7) >= 4,
                      (exp(arrayReduce('simpleLinearRegression',
                           arrayMap(x -> toFloat64(x), arrayEnumerate(v7)),
                           arrayMap(x -> log(x), v7)).1) - 1) * 100, NULL)
                                                        AS vol_slope7_pct_day,
                   if(arrayMax(arraySlice(v, 1, 14)) > 0,
                      v[1] / arrayMax(arraySlice(v, 1, 14)), NULL) AS vol_off_peak
            FROM {DB}.instrument_daily
            WHERE asset_key = '{a}' AND vol_usd > 0
            GROUP BY venue, symbol, market_type)
        SELECT l.venue AS venue, l.market_type AS market_type, l.symbol AS symbol,
               l.quote AS quote, l.we_quote AS we_quote,
               l.vol24h_usd AS vol24h_usd,
               d.vol_usd_last AS vol_usd_last,
               d.vol_d1 AS vol_d1, d.vol_dow7 AS vol_dow7,
               d.vol_off_peak AS vol_off_peak, d.vol_peak_age AS vol_peak_age,
               d.vol_slope7_pct_day AS vol_slope7_pct_day,
               l.vol_usd_med7 AS vol_usd_med7, l.vol_usd_med30 AS vol_usd_med30,
               if(l.vol_usd_med30 > 0, l.vol24h_usd / l.vol_usd_med30, NULL) AS vol_vs_med30,
               l.vol_share AS vol_share,
               -- BIN publishes no live OI in our snapshot (0 of 158 legs), so fall back to
               -- the last daily close rather than render a real market as zero.
               if(l.oi_usd_live > 0, l.oi_usd_live, d.oi_usd_last) AS oi_usd_latest,
               l.oi_usd_med30 AS oi_usd_med30,
               l.spread_bps AS spread_bps, l.tick_bps AS tick_bps,
               l.fund_bps_med AS fund_bps_med, l.fund_iv_h AS fund_iv_h,
               l.maker_fee AS maker_fee, l.min_notional AS min_notional,
               l.px_close AS px_close, d.through AS through,
               l.listed_since AS listed_since, l.days AS days
        FROM {DB}.screen_runs_venue AS l
        LEFT JOIN d ON d.venue = l.venue AND d.symbol = l.symbol
                   AND d.market_type = l.market_type
        WHERE l.asset_key = '{a}'
          AND l.run_date = (SELECT max(run_date) FROM {DB}.screen_runs_venue)
        ORDER BY l.vol24h_usd DESC""")
    return JSONResponse({"by_venue": jsonable(byvenue), "total": jsonable(total),
                         "history": jsonable(hist), "legs": jsonable(legs)})


@app.get("/api/health")
def health():
    loads = q(f"""SELECT run_ts, table, rows_in, rows_loaded,
                         rows_rejected, status, round(duration_s,2) AS duration_s, notes
                  FROM {DB}.load_runs ORDER BY run_ts DESC, table LIMIT 60""")
    rejects = q(f"""SELECT run_ts, table, reason, venue, symbol, detail
                    FROM {DB}.load_rejects ORDER BY run_ts DESC LIMIT 100""")
    dups = q(f"""SELECT asset_a, asset_b, class, px_a, px_b, diff_bps, vol_a, vol_b,
                        venues_a, venues_b
                 FROM {DB}.screen_dup_tickers
                 WHERE run_date = (SELECT max(run_date) FROM {DB}.screen_dup_tickers)
                 ORDER BY abs(diff_bps)""")
    # cross-venue price agreement over time for the majors (validation gate 1)
    agree = q(f"""SELECT date, asset_key, px_spread_bps, usdc_basis_bps
                  FROM {DB}.asset_daily WHERE asset_key IN ('BTC','ETH','SOL')
                  ORDER BY date""")
    # anything disagreeing badly: a ticker collision or a missing scale_factor
    worst = q(f"""SELECT asset_key, round(max(px_spread_bps)) AS worst_bps,
                         round(median(px_spread_bps),1) AS median_bps, count() AS days
                  FROM {DB}.asset_daily GROUP BY asset_key
                  HAVING worst_bps > 100 ORDER BY worst_bps DESC LIMIT 20""")
    coverage = q(f"""SELECT venue, date, uniqExact(symbol) AS instruments,
                            sum(vol_usd) AS vol_usd
                     FROM {DB}.instrument_daily GROUP BY venue, date ORDER BY date, venue""")
    unmapped = q(f"""SELECT name, venue, kind, symbol, fills, match_rule
                     FROM {DB}.internal_map
                     WHERE run_date = (SELECT max(run_date) FROM {DB}.internal_map)
                       AND (match_rule = 'UNMATCHED' OR asset_key = '')
                     ORDER BY fills DESC""")
    return JSONResponse({"loads": jsonable(loads), "rejects": jsonable(rejects),
                         "dups": jsonable(dups), "agreement": jsonable(agree),
                         "worst": jsonable(worst), "coverage": jsonable(coverage),
                         "unmapped": jsonable(unmapped)})


@app.get("/api/history")
def history(new_days: int = 30):
    """What we stopped trading, and what is newly listed."""
    dropped = q(f"""
        SELECT name, server, venue, kind, symbol, asset_key, asset_class,
               first_seen, last_seen, cfg_date, config_days, days_since_drop,
               fills_30d, last_fill, match_rule
        FROM {DB}.traded_history
        WHERE run_date = (SELECT max(run_date) FROM {DB}.traded_history)
          AND status = 'dropped'
        ORDER BY days_since_drop, fills_30d DESC""")
    active = q(f"""
        SELECT name, server, venue, kind, symbol, asset_key, asset_class,
               first_seen, last_seen, config_days, fills_30d, last_fill
        FROM {DB}.traded_history
        WHERE run_date = (SELECT max(run_date) FROM {DB}.traded_history)
          AND status = 'active'
        ORDER BY fills_30d DESC""")
    # newly listed anywhere, with the per-venue listed/not matrix
    new = q(f"""
        SELECT asset_key, asset_class, first_listed, age_days, we_trade,
               venues_perp, venues_spot, BIN, OKX, BGT, GAT, KCN,
               BIN_since, OKX_since, BGT_since, GAT_since, KCN_since
        FROM {DB}.asset_listing_matrix
        WHERE age_days <= {int(new_days)} AND age_days >= 0
        ORDER BY first_listed DESC, venues_perp DESC""")
    upcoming = q(f"""
        SELECT asset_key, asset_class, first_listed, age_days, venues_perp,
               BIN, OKX, BGT, GAT, KCN
        FROM {DB}.asset_listing_matrix WHERE age_days < 0
        ORDER BY first_listed""")
    # a venue adding an asset we already trade elsewhere = a leg we could add
    gaps = q(f"""
        SELECT asset_key, asset_class, first_listed, venues_perp, venues_spot,
               BIN, OKX, BGT, GAT, KCN
        FROM {DB}.asset_listing_matrix
        WHERE we_trade = 1 AND (BIN = 0 OR OKX = 0 OR BGT = 0 OR GAT = 0 OR KCN = 0)
        ORDER BY venues_perp DESC LIMIT 200""")
    return JSONResponse({"dropped": jsonable(dropped), "active": jsonable(active),
                         "new": jsonable(new), "upcoming": jsonable(upcoming),
                         "gaps": jsonable(gaps)})


@app.get("/api/query")
def raw_query(sql: str = Query(..., description="read-only SELECT")):
    """Escape hatch for anything the tabs don't cover. SELECT only."""
    s = sql.strip().rstrip(";")
    low = s.lower()
    if not (low.startswith("select") or low.startswith("with")):
        raise HTTPException(400, "SELECT/WITH only")
    for bad in ("insert", "alter", "drop", "truncate", "delete", "create", "attach",
                "detach", "optimize", "rename", "grant", "system"):
        if f" {bad} " in f" {low} ":
            raise HTTPException(400, f"'{bad}' is not allowed")
    d = q(f"SELECT * FROM ({s}) LIMIT 5000")
    return JSONResponse({"columns": list(d.columns), "rows": jsonable(d)})


# --------------------------------------------------------------------------- #

@app.middleware("http")
async def no_cache(request, call_next):
    """Local dev tool: never let the browser hold a stale app.js.

    A cached bundle is indistinguishable from a broken feature - the tab is there,
    the click does nothing, and the server logs look fine.
    """
    resp = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def main():
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8815)
    ap.add_argument("--reload", action="store_true")
    a = ap.parse_args()
    print(f"universe dashboard -> http://{a.host}:{a.port}   (ClickHouse "
          f"{ch_schema.CH_URL}{DB})")
    uvicorn.run("app:app" if a.reload else app, host=a.host, port=a.port,
                reload=a.reload, log_level="warning")


if __name__ == "__main__":
    main()
