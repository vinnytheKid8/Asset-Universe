"""Day-level orchestration: one grouped file -> one streaming pass -> bars.

Public entry points:
    fetch_feed_day(...)   one exchange feed, one UTC day
    backfill(...)         N days x many feeds, thread-parallel
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import requests

from .aggregate import AGGREGATORS, US_PER_HOUR
from .client import TardisAuthError, TardisConfig, TardisDatasets, TardisError, TardisMetadata
from .config import FEEDS, ExchangeFeed

log = logging.getLogger(__name__)

# data types we pull per feed, in priority order
DEFAULT_TYPES_PERP = ("derivative_ticker", "trades")
DEFAULT_TYPES_SPOT = ("trades",)


@dataclass
class DayResult:
    feed: str
    day: date
    data_type: str
    symbol_group: str
    rows_in: int = 0
    records_out: int = 0
    seconds: float = 0.0
    path: str | None = None
    error: str | None = None


def types_for(feed: ExchangeFeed, requested: tuple[str, ...] | None = None) -> tuple[str, ...]:
    base = DEFAULT_TYPES_PERP if feed.has_derivative_ticker else DEFAULT_TYPES_SPOT
    if requested:
        base = tuple(t for t in requested if t in feed.grouped or t in ("trades",))
    return tuple(t for t in base if t in feed.grouped)


def fetch_feed_day(cfg: TardisConfig, feed_id: str, day: date, out_dir: Path,
                   data_types: tuple[str, ...] | None = None,
                   bucket_us: int = US_PER_HOUR,
                   fmt: str = "auto",
                   session: requests.Session | None = None) -> list[DayResult]:
    """Stream every requested grouped dataset for one feed/day and write bars.

    Uses the grouped symbol (PERPETUALS / SPOT / FUTURES) so a whole exchange
    segment costs exactly one HTTP request per data type per day.
    """
    from . import store

    feed = FEEDS[feed_id]
    ds = TardisDatasets(cfg, session=session)
    results: list[DayResult] = []

    for dtype in types_for(feed, data_types):
        group = feed.grouped[dtype]
        res = DayResult(feed=feed_id, day=day, data_type=dtype, symbol_group=group)
        t0 = time.time()
        try:
            if day < feed.available_since:
                raise TardisError(f"{feed_id} has no data before {feed.available_since}")
            agg = AGGREGATORS[dtype](bucket_us=bucket_us)
            for row in ds.stream_rows(feed_id, dtype, day, group):
                agg.update(row)
            recs = agg.records(feed_id, day.isoformat())
            table = {"derivative_ticker": "deriv_bars",
                     "trades": "trade_bars",
                     "liquidations": "liq_bars"}[dtype]
            path = store.write_table(recs, out_dir, table, feed_id, day.isoformat(), fmt=fmt)
            res.rows_in, res.records_out = agg.rows_seen, len(recs)
            res.path = str(path) if path else None
        except TardisAuthError as e:
            res.error = f"AUTH: {e}"
        except Exception as e:                       # noqa: BLE001
            res.error = f"{type(e).__name__}: {e}"
        res.seconds = round(time.time() - t0, 1)
        log.info("%s %s %s %s rows=%s out=%s %.0fs %s", feed_id, day, dtype, group,
                 res.rows_in, res.records_out, res.seconds, res.error or "")
        results.append(res)
    return results


def backfill(cfg: TardisConfig, feed_ids: list[str], start: date, end: date,
             out_dir: Path, data_types: tuple[str, ...] | None = None,
             bucket_us: int = US_PER_HOUR, workers: int = 4,
             fmt: str = "auto") -> list[DayResult]:
    """Inclusive [start, end] backfill. One task per (feed, day).

    Keep `workers` modest: each task streams a 0.1-2 GB gzip file, so the
    bottleneck is bandwidth/CPU (gzip decode), not request count.
    """
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    tasks = [(f, d) for f in feed_ids for d in days
             if d >= FEEDS[f].available_since]
    out: list[DayResult] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        sessions = {}
        futs = {}
        for f, d in tasks:
            s = sessions.setdefault(f, requests.Session())
            futs[pool.submit(fetch_feed_day, cfg, f, d, out_dir,
                             data_types, bucket_us, fmt, s)] = (f, d)
        for fut in as_completed(futs):
            f, d = futs[fut]
            try:
                out.extend(fut.result())
            except Exception as e:                   # noqa: BLE001
                out.append(DayResult(feed=f, day=d, data_type="?", symbol_group="?",
                                     error=f"{type(e).__name__}: {e}"))
    return out


def latest_exported_day(cfg: TardisConfig, feed_id: str) -> date | None:
    """datasets.exportedUntil — CSVs for a UTC day land by ~06:00 UTC next day."""
    v = TardisMetadata(cfg).exported_until(feed_id)
    return date.fromisoformat(v[:10]) if v else None


def symbol_universe(cfg: TardisConfig, feed_id: str, data_type: str = "trades") -> list[dict]:
    """Per-symbol coverage straight from GET /v1/exchanges/{id} (no auth needed)."""
    from .config import classify_symbol
    meta = TardisMetadata(cfg)
    rows = []
    for s in meta.dataset_symbols(feed_id, data_type=data_type):
        rows.append({
            "exchange": feed_id,
            "venue": FEEDS[feed_id].venue,
            "symbol": s["id"],
            "tardis_type": s.get("type"),
            "segment": classify_symbol(feed_id, s["id"]),
            "available_since": s.get("availableSince"),
            "available_to": s.get("availableTo"),
            "data_types": ",".join(s.get("dataTypes", [])),
        })
    return rows
