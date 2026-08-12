"""CLI:  python -m tardis_stats.cli <command> [...]

Commands
    check                       verify key + plan + exportedUntil per feed
    universe                    dump the full symbol universe to CSV
    day     --feed --date       fetch+aggregate a single feed/day
    backfill --days N           fetch+aggregate all feeds for the last N days
    daily                       roll hourly bars up to the daily table
    dashboard                   build the wide per-asset snapshot CSV
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from .client import TardisConfig, TardisDatasets, TardisError, TardisMetadata
from .config import ALL_FEED_IDS, FEEDS
from .pipeline import backfill, fetch_feed_day, symbol_universe

DEFAULT_OUT = Path("./data")


def _cfg(a) -> TardisConfig:
    key = a.api_key or os.environ.get("TARDIS_API_KEY", "")
    return TardisConfig(api_key=key, cache_dir=Path(a.cache_dir), keep_raw=a.keep_raw)


def cmd_check(a) -> int:
    cfg = _cfg(a)
    meta = TardisMetadata(cfg)
    print(f"api_key set: {bool(cfg.api_key)}")
    plan: dict[str, dict] = {}
    try:
        info = meta.api_key_info()
        plan = {e["exchange"]: e for e in info}
        kinds = sorted({e["accessType"] for e in info})
        until = sorted({e["to"][:10] for e in info})
        print(f"plan: {','.join(kinds)} | {len(info)} exchanges | access to {','.join(until)}")
    except TardisError as e:
        print("api-key-info FAILED:", e)
    ds = TardisDatasets(cfg)
    probe_day = date.today() - timedelta(days=2)
    for fid in (a.feeds or ALL_FEED_IDS):
        f = FEEDS[fid]
        try:
            until = meta.exported_until(fid)
        except TardisError as e:
            until = f"ERR {e}"
        dtype = "derivative_ticker" if f.has_derivative_ticker else "trades"
        group = f.grouped.get(dtype, "?")
        code, has_data = ds.probe(fid, dtype, probe_day, group) if group != "?" else (0, False)
        p = plan.get(fid)
        entitled = f"{p['accessType']} to {p['to'][:10]}" if p else "NOT IN PLAN"
        status = "OK" if has_data else ("EMPTY" if code == 200 else f"HTTP {code}")
        print(f"{fid:18s} since={f.available_since} exportedUntil={str(until)[:10]} "
              f"{entitled:16s} {dtype}/{group} @ {probe_day}: {status}")
    return 0


def cmd_universe(a) -> int:
    cfg = _cfg(a)
    rows = []
    for fid in (a.feeds or ALL_FEED_IDS):
        try:
            rows += symbol_universe(cfg, fid, data_type=a.data_type)
        except TardisError as e:
            print(f"{fid}: {e}", file=sys.stderr)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} instruments -> {out}")
    return 0


def cmd_day(a) -> int:
    res = fetch_feed_day(_cfg(a), a.feed, date.fromisoformat(a.date), Path(a.out),
                         data_types=tuple(a.types) if a.types else None,
                         bucket_us=a.bucket_minutes * 60_000_000, fmt=a.format)
    for r in res:
        print(r)
    return 1 if any(r.error for r in res) else 0


def cmd_backfill(a) -> int:
    end = date.fromisoformat(a.end) if a.end else date.today() - timedelta(days=1)
    start = date.fromisoformat(a.start) if a.start else end - timedelta(days=a.days - 1)
    res = backfill(_cfg(a), a.feeds or ALL_FEED_IDS, start, end, Path(a.out),
                   data_types=tuple(a.types) if a.types else None,
                   bucket_us=a.bucket_minutes * 60_000_000,
                   workers=a.workers, fmt=a.format)
    bad = [r for r in res if r.error]
    print(f"{len(res) - len(bad)}/{len(res)} tasks ok")
    for r in bad:
        print("FAIL", r.feed, r.day, r.data_type, r.error)
    return 1 if bad else 0


def cmd_contracts(a) -> int:
    from .contracts import fetch_contract_specs, save_contract_specs
    recs = fetch_contract_specs(_cfg(a), a.feeds or None)
    dest = save_contract_specs(recs, Path(a.out) / "contracts.csv")
    print(f"{len(recs)} instruments -> {dest}")
    return 0


def cmd_daily(a) -> int:
    import pandas as pd
    from .contracts import apply_units
    from .rollup import daily_table
    df = daily_table(Path(a.out))
    spec_path = Path(a.out) / "contracts.csv"
    if spec_path.exists() and not df.empty:
        df = apply_units(df, pd.read_csv(spec_path))
    dest = Path(a.out) / "daily.csv"
    df.to_csv(dest, index=False)
    print(f"{len(df)} rows -> {dest}")
    return 0


def cmd_dashboard(a) -> int:
    import pandas as pd
    from .rollup import dashboard_frame
    src = Path(a.out) / "daily.csv"
    if a.rebuild or not src.exists():
        cmd_daily(a)
    df = dashboard_frame(pd.read_csv(src))
    dest = Path(a.out) / "dashboard.csv"
    df.to_csv(dest, index=False)
    print(f"{len(df)} instruments -> {dest}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tardis_stats")
    p.add_argument("--api-key", default="")
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--cache-dir", default="./tardis_cache")
    p.add_argument("--keep-raw", action="store_true",
                   help="persist raw .csv.gz instead of stream-only")
    p.add_argument("--format", default="auto", choices=["auto", "parquet", "csv"])
    p.add_argument("--bucket-minutes", type=int, default=60)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("check"); s.add_argument("--feeds", nargs="*"); s.set_defaults(fn=cmd_check)
    s = sub.add_parser("universe")
    s.add_argument("--feeds", nargs="*"); s.add_argument("--data-type", default="trades")
    s.add_argument("--out-file", dest="out", default="./data/universe.csv")
    s.set_defaults(fn=cmd_universe)
    s = sub.add_parser("day")
    s.add_argument("--feed", required=True); s.add_argument("--date", required=True)
    s.add_argument("--types", nargs="*"); s.set_defaults(fn=cmd_day)
    s = sub.add_parser("backfill")
    s.add_argument("--feeds", nargs="*"); s.add_argument("--days", type=int, default=14)
    s.add_argument("--start"); s.add_argument("--end")
    s.add_argument("--types", nargs="*"); s.add_argument("--workers", type=int, default=4)
    s.set_defaults(fn=cmd_backfill)
    s = sub.add_parser("contracts")
    s.add_argument("--feeds", nargs="*")
    s.set_defaults(fn=cmd_contracts)
    sub.add_parser("daily").set_defaults(fn=cmd_daily)
    s = sub.add_parser("dashboard")
    s.add_argument("--no-rebuild", dest="rebuild", action="store_false",
                   help="reuse an existing daily.csv instead of re-rolling bars")
    s.set_defaults(fn=cmd_dashboard, rebuild=True)

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
