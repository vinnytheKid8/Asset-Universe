"""Load the universe-screen frames from the ClickHouse cache into symbol_stats.

    python ch_load.py                      # latest complete cached run
    python ch_load.py --run-date 2026-08-06
    python ch_load.py --dry-run            # conform + validate, insert nothing
    python ch_load.py --data-dir data/     # read local parquet instead

Creates any missing tables first (ch_schema is idempotent), so a fresh machine
needs no separate init step.

Design notes, all of them earned by loading the real files:

* Schema drift never breaks the job. A column in the parquet that isn't in the
  table is dropped with a WARNING; a column in the table that isn't in the
  parquet is left at its default with a WARNING. Both land in load_runs.notes.
  A nightly job that dies because someone added a debug column is worse than one
  that tells you about it.
* Data corruption always breaks the job. Row counts that don't survive the
  sorting key, an identity-resolution reject rate over the threshold, or a
  cross-venue price disagreement past the gate all exit non-zero.
* Instruments that don't resolve to an asset_key are quarantined to
  load_rejects, never fillna'd. A filled key puts real volume under an empty
  asset where it silently vanishes from every downstream query.
* Re-running the same day is safe and leaves no residue. Fact tables are
  ReplacingMergeTree ordered on their true uniqueness key and are OPTIMIZEd at
  the end of each load, so raw row counts stay honest and Grafana panels need
  neither FINAL nor argMax. Snapshot tables clear their run_date first, so an
  asset that dropped out of the screen doesn't linger.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import clickhouse_connect
import numpy as np
import pandas as pd

import ch_cache
import ch_schema
from ch_schema import DB, DEDUP_KEYS

log = logging.getLogger("ch_load")

# Fail the load if more than this share of rows can't be identity-resolved.
MAX_REJECT_RATE = 0.02
# Fail if a major disagrees across venues by more than this (USDT-only,
# scale-normalised). Healthy is 1-3 bps; PIPELINE.md measured 1.59.
PX_AGREEMENT_GATE_BPS = 25.0
PX_AGREEMENT_ASSETS = ("BTC", "ETH", "SOL")


def client():
    return clickhouse_connect.get_client(
        host=ch_schema.CH_URL.split("//")[1].split(":")[0],
        port=int(ch_schema.CH_URL.rstrip("/").split(":")[-1]),
        username=ch_schema.CH_USER, password=ch_schema.CH_PASS,
        database=DB, connect_timeout=15, send_receive_timeout=600)


# --------------------------------------------------------------------------- #
# Conforming
# --------------------------------------------------------------------------- #

def conform(df: pd.DataFrame, table: str) -> tuple[pd.DataFrame, list[str]]:
    """Align a DataFrame to a table's schema. Returns (frame, notes).

    clickhouse_connect will not coerce for you: None in a non-Nullable
    LowCardinality(String) is a hard DataError, and a column absent from the
    table is dropped without a word.
    """
    sc = ch_schema.columns(table)
    notes = []

    extra = [c for c in df.columns if c not in sc]
    if extra:
        notes.append(f"dropped {len(extra)} column(s) not in schema: {sorted(extra)}")
        log.warning("%s: dropping columns not in schema: %s", table, sorted(extra))

    absent = [c for c in sc if c not in df.columns]
    if absent:
        notes.append(f"{len(absent)} column(s) left at default: {sorted(absent)}")
        log.warning("%s: not in parquet, left at default: %s", table, sorted(absent))

    out = df[[c for c in sc if c in df.columns]].copy()
    for col, ty in sc.items():
        if col not in out:
            continue
        nullable = ty.startswith("Nullable")
        if ty.endswith("Date") or "Date)" in ty:
            out[col] = pd.to_datetime(out[col], errors="coerce", utc=True).dt.date
            if nullable:
                # NaT must reach the driver as None. Left as NaT it lands as the
                # epoch, so a never-traded instrument reads "1970-01-01" instead of
                # blank - which looks like data rather than absence.
                out[col] = out[col].astype(object).where(out[col].notna(), None)
            else:
                out[col] = out[col].fillna(date(1970, 1, 1))
        elif "DateTime" in ty:
            out[col] = pd.to_datetime(out[col], errors="coerce", utc=True).dt.tz_localize(None)
        elif out[col].dtype == bool:
            out[col] = out[col].astype("uint8")
        elif "String" in ty:
            out[col] = out[col].astype(object).where(out[col].notna(), "").astype(str)
        elif "Int" in ty or "Float" in ty:
            out[col] = pd.to_numeric(out[col], errors="coerce")
            if not nullable:
                out[col] = out[col].fillna(0)
            if "Int" in ty:
                # Nullable ints must stay object-with-None; NaN is not a valid Int
                out[col] = (out[col].astype("Int64") if nullable
                            else out[col].round().astype("int64"))
    return out, notes


def check_keys(frame: pd.DataFrame, table: str) -> None:
    """Assert the sorting key can tell this batch's rows apart, BEFORE inserting.

    Checked on the frame rather than on the table: ReplacingMergeTree physically
    keeps both copies of a re-inserted row until a background merge, so
    `count() == uniqExact(key)` is false on any legitimate same-day re-run and
    would fail for the wrong reason. What actually matters is whether two rows
    the source considers distinct share a sorting key - that is unrecoverable
    data loss, and it is what caught screen_runs_venue collapsing 645 -> 589.
    """
    keys = DEDUP_KEYS.get(table)
    if not keys or frame.empty:
        return
    dup = frame.duplicated(subset=keys, keep=False)
    if dup.any():
        sample = frame.loc[dup, keys].head(6).to_string(index=False)
        raise SystemExit(
            f"FATAL {table}: {int(dup.sum())} rows share a sorting key "
            f"({', '.join(keys)}) and would be silently collapsed to "
            f"{frame[keys].drop_duplicates().shape[0]} of {len(frame)}. "
            f"Widen ORDER BY in ch_schema.py.\n{sample}")
    log.debug("  %s: %d rows, sorting key distinguishes all of them", table, len(frame))


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

class Loader:
    def __init__(self, ch, run_ts: datetime, dry_run: bool = False):
        self.ch, self.run_ts, self.dry_run = ch, run_ts, dry_run
        self.audit: list[dict] = []
        self.rejects: list[dict] = []
        self.failed = False

    def load(self, df: pd.DataFrame, table: str, replace_key: str | None = None,
             notes: list[str] | None = None) -> None:
        t0 = time.time()
        notes = list(notes or [])
        n_in = len(df)
        frame, cnotes = conform(df, table)
        notes += cnotes
        check_keys(frame, table)

        if not self.dry_run:
            # Snapshot tables: clear this run_date first. ReplacingMergeTree would
            # supersede a row whose key repeats, but an asset that dropped out of
            # the screen since the last attempt has no new row to supersede it and
            # would linger. These tables are hundreds of rows; the mutation is free.
            if replace_key:
                self.ch.command(f"ALTER TABLE {table} DELETE WHERE {replace_key} = "
                                f"'{self.run_ts.date()}' SETTINGS mutations_sync = 2")
            self.ch.insert_df(table, frame)

        dt = time.time() - t0
        status = "warn" if cnotes else "ok"
        log.info("%-20s %6d rows  %5.1fs  %s", table, len(frame), dt, status)
        self.audit.append(dict(run_ts=self.run_ts, table=table, rows_in=n_in,
                               rows_loaded=len(frame), rows_rejected=0,
                               status=status, duration_s=dt, notes="; ".join(notes)))

    def reject(self, table: str, reason: str, rows: pd.DataFrame) -> None:
        for r in rows.itertuples():
            self.rejects.append(dict(
                run_ts=self.run_ts, table=table, reason=reason,
                venue=getattr(r, "venue", ""), symbol=getattr(r, "symbol", ""),
                detail=getattr(r, "detail", "")))

    def optimize(self, tables: list[str], max_rows: int = 5_000_000) -> None:
        """Collapse ReplacingMergeTree duplicates now instead of waiting for a
        background merge.

        The nightly job re-fetches a rolling 30-day window, so ~97% of each load
        is rows that already exist. Left alone, physical row count grows by a
        full copy per night until merges catch up, and every panel has to carry
        FINAL or argMax to read a correct number. These tables are small - tens
        of thousands of rows - so forcing the merge is cheap and buys honest raw
        counts. The guard is there because this stops being true at scale;
        ticker_samples (~3.7M rows/day) must never be optimized this way.
        """
        if self.dry_run:
            return
        for t in tables:
            n = self.ch.query(f"SELECT count() FROM {t}").result_rows[0][0]
            if n > max_rows:
                log.warning("  %s has %d rows, skipping OPTIMIZE - queries must "
                            "use FINAL or argMax", t, n)
                continue
            t0 = time.time()
            self.ch.command(f"OPTIMIZE TABLE {t} FINAL")
            after = self.ch.query(f"SELECT count() FROM {t}").result_rows[0][0]
            log.info("  optimized %-18s %d -> %d rows (%.1fs)", t, n, after,
                     time.time() - t0)

    def flush_audit(self) -> None:
        if self.dry_run:
            return
        if self.rejects:
            self.ch.insert_df("load_rejects", pd.DataFrame(self.rejects))
        for a in self.audit:
            if a["table"] in {r["table"] for r in self.rejects}:
                a["rows_rejected"] = sum(r["table"] == a["table"] for r in self.rejects)
        if self.audit:
            self.ch.insert_df("load_runs", pd.DataFrame(self.audit))


# --------------------------------------------------------------------------- #
# Post-load validation (PIPELINE.md section 8)
# --------------------------------------------------------------------------- #

def validate(ch) -> bool:
    """Gates that run on every load. Returns False if any hard gate fails."""
    ok = True

    # Gate 1: cross-venue price agreement on the liquid names.
    q = ch.query(f"""
        SELECT asset_key, round(px_spread_bps, 2)
        FROM {DB}.asset_daily
        WHERE asset_key IN {PX_AGREEMENT_ASSETS}
          AND date = (SELECT max(date) FROM {DB}.instrument_daily)
        ORDER BY asset_key""").result_rows
    for asset, bps in q:
        if bps > PX_AGREEMENT_GATE_BPS:
            log.error("GATE FAIL price agreement %s = %.1f bps (limit %.0f) - "
                      "suspect a day-boundary or unit bug, see PIPELINE.md 2.1",
                      asset, bps, PX_AGREEMENT_GATE_BPS)
            ok = False
        else:
            log.info("  price agreement %-5s %5.2f bps", asset, bps)
    if not q:
        log.warning("  price agreement: no majors in the latest day")

    # Advisory: the worst disagreement anywhere. Not a gate - listing days and
    # stale venues legitimately spike it - but it is how ON (two different assets
    # sharing one asset_key, 39,860 bps) surfaced.
    worst = ch.query(f"""
        SELECT asset_key, round(max(px_spread_bps), 0) w
        FROM {DB}.asset_daily
        WHERE date >= today() - 7 GROUP BY asset_key
        HAVING w > 200 ORDER BY w DESC LIMIT 5""").result_rows
    for asset, bps in worst:
        log.warning("  price disagreement %s = %.0f bps over 7d - check for a "
                    "ticker collision or a missing scale_factor", asset, bps)

    # Gate 6: volume self-consistency per venue x market (PIPELINE.md s8.6).
    # vol_quote, vol_base and px_close all come from the same candle, so
    # vol_usd / (vol_base * px_close * fx_rate) is just VWAP/close and must sit near 1.
    # A venue-wide median far from 1 means we read the wrong field or missed a contract
    # multiplier - which is exactly how OKX spot vol_base (volCcy is QUOTE for spot but
    # BASE for swaps) was caught. scale_factor deliberately does not appear: it is a
    # price-comparison device and cancels between vol_base and px_close.
    vol = ch.query(f"""
        SELECT venue, market_type, count() n, round(quantile(0.5)(r), 3) p50,
               countIf(r > 3 OR r < 0.33) wild
        FROM (SELECT venue, market_type,
                     vol_usd / (vol_base * px_close * fx_rate) AS r
              FROM {DB}.instrument_daily
              WHERE date >= today() - 7 AND vol_base > 0 AND px_close > 0
                AND vol_usd > 0 AND fx_rate > 0)
        GROUP BY venue, market_type HAVING n >= 20 AND (p50 > 1.5 OR p50 < 0.67)
        ORDER BY venue, market_type""").result_rows
    for venue, mt, n, p50, wild in vol:
        log.error("GATE 6 FAIL volume consistency %s %s: median vol_usd/(vol_base*px*fx) "
                  "= %.3f over %d rows (%d wild) - wrong volume field or a missing "
                  "contract multiplier", venue, mt, p50, n, wild)
        ok = False
    if not vol:
        log.info("  volume consistency: all venue x market medians within 1.5x")

    # Gate 5: per-venue instrument count vs the previous load.
    drift = ch.query(f"""
        WITH d AS (
            SELECT venue, date, uniqExact(symbol) n FROM {DB}.instrument_daily
            WHERE date >= today() - 3 GROUP BY venue, date)
        SELECT venue, argMax(n, date) AS latest, argMin(n, date) AS prior
        FROM d GROUP BY venue HAVING latest < prior * 0.8 ORDER BY venue""").result_rows
    for venue, latest, prior in drift:
        log.warning("  %s instrument count %d -> %d (>20%% drop) - venue may be "
                    "silently returning fewer symbols", venue, prior, latest)
    return ok


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=None, metavar="DIR",
                    help="read local parquet instead of the ClickHouse frame cache")
    ap.add_argument("--run-date", default=None,
                    help="YYYY-MM-DD (default: latest complete cached run)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    # Input is the ClickHouse frame cache; --data-dir is the local-parquet escape
    # hatch. Either way `read(name)` hands back the frame run_screen produced.
    data = Path(a.data_dir) if a.data_dir else None
    if data is not None:
        missing = [f for f in ch_cache.REQUIRED if not (data / f"{f}.parquet").exists()]
        if missing:
            log.error("missing required artifacts in %s: %s - run run_screen.py first",
                      data, missing)
            return 2
        run_date = (date.fromisoformat(a.run_date) if a.run_date
                    else datetime.now(timezone.utc).date())
        source = str(data)

        def read(name: str) -> pd.DataFrame:
            return pd.read_parquet(data / f"{name}.parquet")
    else:
        cache = ch_cache.client()
        # latest_run_date only returns a date whose every required frame is present,
        # so a fetch that died halfway is skipped rather than loaded as if whole.
        run_date = (date.fromisoformat(a.run_date) if a.run_date
                    else ch_cache.latest_run_date(ch=cache))
        if run_date is None:
            log.error("no complete run in %s - run run_screen.py first",
                      ch_cache.CACHE_DB)
            return 2
        gaps = ch_cache.missing(run_date, ch=cache)
        if gaps:
            log.error("cached run %s is missing %s - run run_screen.py first",
                      run_date, gaps)
            return 2
        source = f"{ch_cache.CACHE_DB} run_date={run_date}"

        def read(name: str) -> pd.DataFrame:
            return ch_cache.get_frame(name, run_date, ch=cache)

    # run_ts stamps the load, run_date identifies the screen being loaded. They were
    # the same value when both came from "now"; with a cached run they are not, and
    # conflating them would date a backfill as though it were fetched today.
    run_ts = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)

    log.info("loading %s -> %s%s  (run_date %s)", source, ch_schema.CH_URL, DB, run_date)
    ch_schema.create_all()
    ch = client()
    ldr = Loader(ch, run_ts, dry_run=a.dry_run)

    # ---- dimension --------------------------------------------------------- #
    ref = read("instrument_ref")
    ref["ingest_ts"] = run_ts
    ldr.load(ref, "instrument_ref")

    # ---- daily facts ------------------------------------------------------- #
    # Identity is denormalised onto the facts so panels never join. scale_factor
    # is not optional: without it 1000SHIBUSDT sits 1000x above every other SHIB.
    # Keyed on market type, not just (venue, symbol): a venue lists the same symbol
    # string as both spot and perp - BIN BTCUSDT is both - so a two-part key silently
    # gives spot rows the perp's identity, and excluding spot from the key (as this
    # did before spot was fetched) rejects every spot row instead.
    ref = ref.copy()
    ref["mkt"] = np.where(ref["kind"] == "spot", "spot", "perp")
    key = (ref.drop_duplicates(subset=["venue", "symbol", "mkt"])
              .set_index(["venue", "symbol", "mkt"])
              [["asset_key", "asset_class", "scale_factor"]])
    deep = read("deep_daily")
    deep["ingest_ts"] = run_ts
    deep["mkt"] = np.where(deep["market_type"] == "spot", "spot", "perp")
    deep = deep.drop(columns=[c for c in ("asset_key", "asset_class", "scale_factor")
                              if c in deep.columns]).join(key, on=["venue", "symbol", "mkt"])
    deep = deep.drop(columns=["mkt"])

    unresolved = deep[deep["asset_key"].isna()]
    if len(unresolved):
        bad = unresolved[["venue", "symbol"]].drop_duplicates().assign(
            detail="no instrument_ref row for this (venue, symbol)")
        rate = len(unresolved) / len(deep)
        log.warning("%d rows / %d instruments have no asset_key (%.2f%%): %s",
                    len(unresolved), len(bad), rate * 100,
                    ", ".join(f"{r.venue}:{r.symbol}" for r in bad.head(8).itertuples()))
        ldr.reject("instrument_daily", "unresolved_asset_key", bad)
        if rate > MAX_REJECT_RATE:
            log.error("reject rate %.2f%% over the %.0f%% limit - not loading",
                      rate * 100, MAX_REJECT_RATE * 100)
            ldr.flush_audit()
            return 3
        deep = deep[deep["asset_key"].notna()]

    ldr.load(deep, "instrument_daily")

    # ---- screen snapshots -------------------------------------------------- #
    # score() lives in build_report; import it so the load never depends on the
    # HTML having been regenerated first.
    from build_report import score
    scores = score(read("asset_metrics"))
    scores.insert(0, "run_date", run_date)
    scores.insert(1, "run_ts", run_ts)
    ldr.load(scores, "screen_runs", replace_key="run_date")

    vm = read("venue_metrics")
    vm.insert(0, "run_date", run_date)
    ldr.load(vm, "screen_runs_venue", replace_key="run_date")

    # ---- venue-wide snapshot ------------------------------------------------ #
    # The whole tradeable landscape, not just the shortlist: ~9,800 instruments with
    # 24h volume, OI, funding and spread. The pipeline already fetches this every
    # night to BUILD the shortlist and used to throw it away, which left inverse
    # perps and the ~9,700 unshortlisted instruments invisible to every query.
    # instrument_daily stays the source for history; this is the breadth.
    try:
        snap = read("snapshot")
        snap.insert(0, "run_date", run_date)
        snap["kind"] = snap["kind"].fillna("")
        snap["ingest_ts"] = run_ts
        ldr.load(snap, "venue_snapshot", replace_key="run_date")
    except (KeyError, FileNotFoundError, OSError):
        log.warning("snapshot absent, skipping venue_snapshot")

    for fname, table in (("dup_tickers", "screen_dup_tickers"),
                         ("internal_map", "internal_map")):
        try:
            df = read(fname)
        except (KeyError, FileNotFoundError, OSError):
            log.warning("%s absent, skipping %s", fname, table)
            continue
        df.insert(0, "run_date", run_date)
        ldr.load(df, table, replace_key="run_date")

    ldr.optimize(["instrument_ref", "instrument_daily", "screen_runs",
                  "screen_runs_venue", "screen_dup_tickers", "internal_map",
                  "venue_snapshot"])
    ldr.flush_audit()

    if a.dry_run:
        log.info("dry run - nothing written")
        return 0

    log.info("validating")
    if not validate(ch):
        log.error("load completed but a validation gate FAILED - treat the data "
                  "as suspect")
        return 4

    n = ch.query(f"SELECT count() FROM {DB}.screen_runs WHERE run_date = "
                 f"'{run_date}'").result_rows[0][0]
    log.info("done - %d assets scored for %s", n, run_date)
    return 0


if __name__ == "__main__":
    sys.exit(main())
