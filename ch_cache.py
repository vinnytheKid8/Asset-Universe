"""ClickHouse-backed frame cache: the stage handoff, with no local disk.

`run_screen.py` produces seven DataFrames that `ch_load.py` and `build_report.py`
read back whole. That handoff used to be `data/*.parquet`, which pinned the cron to
one machine - the box that ran the fetch was the only box that could load or report
on it. Here it lives in ClickHouse instead, so any host that can reach the server
can run any stage.

    put_frame(df, "deep_daily", run_date)      -> stores it
    get_frame("deep_daily")                    -> latest cached copy
    get_frame("deep_daily", date(2026, 8, 11)) -> that run

Why parquet bytes in a String column rather than seven typed tables: nothing
SELECTs a column out of these frames - every consumer reads the whole thing into
pandas - so typed tables would buy nothing and cost a migration every time
`run_screen` gains a column. Parquet also round-trips dtypes exactly, which
matters here: several of these frames carry nullable ints and all-NaN float
columns that a naive columnar round-trip silently changes. What you *do* want in
SQL is the curated output, and that is `symbol_stats` (see ch_schema.py).

The metadata columns (rows/cols/nbytes) are there so `SELECT run_date, frame,
rows, nbytes FROM asset_universe_cache.frames ORDER BY run_date DESC` tells you
what is cached and whether a run is complete, without pulling any payloads.
"""
from __future__ import annotations

import base64
import io
import logging
import os
from datetime import date, datetime, timezone

import pandas as pd

log = logging.getLogger(__name__)

CACHE_DB = os.environ.get("AU_CACHE_DB", "asset_universe_cache")

# Retention. 4-6 MB per run, so 180 days is well under a gigabyte. The only frame
# with a hard reason to persist is deep_daily (--reuse-deep reads it back); the
# rest are kept so a past run's report can be rebuilt without a refetch.
TTL_DAYS = int(os.environ.get("AU_CACHE_TTL_DAYS", "180"))

# The full handoff. shortlist and asset_scores are deliberately absent: both were
# written to data/ and read by nobody (asset_scores is build_report's own output,
# and screen_runs in ClickHouse already holds it).
FRAMES = ("instrument_ref", "internal_map", "snapshot", "dup_tickers",
          "deep_daily", "asset_metrics", "venue_metrics")

# What ch_load.py cannot run without - the completeness test for "is this run
# loadable". build_report needs snapshot too, but a missing snapshot should not
# block the load.
REQUIRED = ("instrument_ref", "deep_daily", "asset_metrics", "venue_metrics")


def client():
    """clickhouse-connect client, host/port parsed off ch_schema.CH_URL.

    Native client rather than the HTTP helpers used elsewhere in this repo because
    payloads are megabyte-scale binary and this handles them without a text
    round-trip on insert.
    """
    import clickhouse_connect

    import ch_schema
    url = ch_schema.CH_URL.rstrip("/")
    host = url.split("//")[1].split(":")[0]
    port = int(url.split(":")[-1])
    return clickhouse_connect.get_client(
        host=host, port=port, username=ch_schema.CH_USER,
        password=ch_schema.CH_PASS, connect_timeout=30, send_receive_timeout=600)


def create_all(ch=None) -> None:
    ch = ch or client()
    ch.command(f"CREATE DATABASE IF NOT EXISTS {CACHE_DB}")
    # ReplacingMergeTree on (run_date, frame): re-running a day overwrites that
    # day's frames instead of stacking copies, so --reuse-deep and a re-run are
    # both idempotent. Reads still order by ingest_ts and take one row rather than
    # trusting FINAL, since a merge may not have happened yet.
    #
    # payload is CODEC(NONE) because parquet arrives already compressed - LZ4 over
    # it burns CPU on every insert to save low single-digit percent.
    ch.command(f"""
    CREATE TABLE IF NOT EXISTS {CACHE_DB}.frames (
        run_date    Date,
        frame       LowCardinality(String),
        rows        UInt32,
        cols        UInt16,
        nbytes      UInt32,
        pandas_ver  LowCardinality(String),
        payload     String CODEC(NONE),
        ingest_ts   DateTime
    )
    ENGINE = ReplacingMergeTree(ingest_ts)
    PARTITION BY toYYYYMM(run_date)
    ORDER BY (run_date, frame)
    TTL run_date + INTERVAL {TTL_DAYS} DAY
    SETTINGS index_granularity = 8192""")


# --------------------------------------------------------------------------- #
# write / read
# --------------------------------------------------------------------------- #

def put_frame(df: pd.DataFrame, frame: str, run_date: date, ch=None) -> int:
    """Store one frame. Returns payload size in bytes."""
    ch = ch or client()
    buf = io.BytesIO()
    # index=False to match what data/*.parquet did; every one of these frames
    # carries a RangeIndex that no consumer reads.
    df.to_parquet(buf, index=False, compression="snappy")
    payload = buf.getvalue()
    ch.insert(
        f"{CACHE_DB}.frames",
        [[run_date, frame, len(df), df.shape[1], len(payload),
          pd.__version__, payload,
          datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)]],
        column_names=["run_date", "frame", "rows", "cols", "nbytes",
                      "pandas_ver", "payload", "ingest_ts"])
    log.debug("cached %s run_date=%s  %d rows x %d cols  %.1f KB",
              frame, run_date, len(df), df.shape[1], len(payload) / 1024)
    return len(payload)


def get_frame(frame: str, run_date: date | None = None, ch=None) -> pd.DataFrame:
    """Read one frame back. `run_date=None` takes the most recent copy.

    base64Encode server-side: the payload is binary and the driver decodes String
    columns as UTF-8 text, which mangles it. Encoding costs 33% on the wire for
    one frame and removes the whole class of bug.
    """
    ch = ch or client()
    where = f"frame = '{frame}'"
    if run_date is not None:
        where += f" AND run_date = '{run_date}'"
    rows = ch.query(
        f"SELECT base64Encode(payload) FROM {CACHE_DB}.frames WHERE {where} "
        f"ORDER BY run_date DESC, ingest_ts DESC LIMIT 1").result_rows
    if not rows:
        raise KeyError(f"{frame} not cached in {CACHE_DB}"
                       + (f" for run_date={run_date}" if run_date else ""))
    return pd.read_parquet(io.BytesIO(base64.b64decode(rows[0][0])))


def manifest(run_date: date | None = None, ch=None) -> pd.DataFrame:
    """What is cached, without pulling payloads."""
    ch = ch or client()
    where = f"WHERE run_date = '{run_date}'" if run_date else ""
    return ch.query_df(
        f"SELECT run_date, frame, rows, cols, nbytes, ingest_ts "
        f"FROM {CACHE_DB}.frames {where} ORDER BY run_date DESC, frame")


def latest_run_date(required: tuple[str, ...] = REQUIRED, ch=None) -> date | None:
    """Most recent run_date holding every one of `required`.

    Guards against loading a half-written run: if the fetch died after
    instrument_ref but before deep_daily, that run_date is skipped rather than
    loaded as though it were whole. This is the cache equivalent of the
    missing-artifact check ch_load did against data/.
    """
    ch = ch or client()
    # Built by hand rather than from tuple(): a 1-element Python tuple renders as
    # ('x',) and the trailing comma is a syntax error in SQL.
    in_list = ", ".join(f"'{f}'" for f in required)
    rows = ch.query(
        f"SELECT run_date FROM {CACHE_DB}.frames "
        f"WHERE frame IN ({in_list}) GROUP BY run_date "
        f"HAVING uniqExact(frame) = {len(required)} "
        f"ORDER BY run_date DESC LIMIT 1").result_rows
    return rows[0][0] if rows else None


def missing(run_date: date, required: tuple[str, ...] = REQUIRED, ch=None) -> list[str]:
    ch = ch or client()
    have = {r[0] for r in ch.query(
        f"SELECT DISTINCT frame FROM {CACHE_DB}.frames "
        f"WHERE run_date = '{run_date}'").result_rows}
    return [f for f in required if f not in have]


# --------------------------------------------------------------------------- #

def main() -> int:
    """CLI: inspect or initialise the cache.

        python ch_cache.py --create
        python ch_cache.py --list
        python ch_cache.py --check
    """
    import argparse
    ap = argparse.ArgumentParser(description=main.__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--create", action="store_true", help="create database + table")
    ap.add_argument("--list", action="store_true", help="what is cached")
    ap.add_argument("--check", action="store_true", help="latest complete run")
    ap.add_argument("--run-date", default=None)
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    ch = client()

    if a.create:
        create_all(ch)
        print(f"{CACHE_DB}.frames ready (TTL {TTL_DAYS}d)")

    if a.list:
        rd = date.fromisoformat(a.run_date) if a.run_date else None
        m = manifest(rd, ch)
        if not len(m):
            print("cache empty")
        else:
            m["MB"] = (m["nbytes"] / 1e6).round(2)
            print(m[["run_date", "frame", "rows", "cols", "MB", "ingest_ts"]]
                  .to_string(index=False))
            print(f"\ntotal {m['nbytes'].sum() / 1e6:.1f} MB across "
                  f"{m['run_date'].nunique()} runs")

    if a.check:
        rd = latest_run_date(ch=ch)
        print(f"latest complete run: {rd}")
        if rd:
            gaps = [f for f in FRAMES if f in missing(rd, FRAMES, ch)]
            print(f"frames present: {len(FRAMES) - len(gaps)}/{len(FRAMES)}"
                  + (f"  missing {gaps}" if gaps else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
