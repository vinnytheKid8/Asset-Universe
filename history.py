"""Universe change history: what we dropped, and what is newly listed.

    python history.py            # rebuild both tables from source
    python history.py --dry-run

Two questions the point-in-time screen cannot answer:

1. **What did we stop trading?**  `strat__{tk,hk}_alex.symbols_record` is written
   whenever a strategy publishes its symbol list (on restart, not daily). An
   instrument present in the most recent publish is currently configured; one that
   appears earlier but not in the latest publish was dropped, and its `last_seen`
   dates the drop. 176 of 520 instruments on tk are in that state.

2. **What is newly listed?**  Venues publish a listing date, so perp history goes
   back to 2018 without us having recorded anything. **Spot does not**: only OKX
   returns `listed_since` for spot pairs, so for the other four venues a spot
   listing is only datable from when our own daily `instrument_ref` snapshot first
   saw it (2026-08-07 onward). Both are exposed, and which one you are looking at
   is stated per row in `listed_src` rather than blended.

A listing date in the future is not an error - venues announce ahead of open, and
those rows are the most actionable thing in the table.
"""
from __future__ import annotations

import argparse
import io
from datetime import datetime, timezone

import pandas as pd
import requests

import ch_schema
from exchange_api_fetcher.symbology import map_internal, parse_internal_names

CH, AUTH = ch_schema.CH_URL, (ch_schema.CH_USER, ch_schema.CH_PASS)
DB = ch_schema.DB
SERVERS = {"tk": "strat__tk_alex", "hk": "strat__hk_alex"}


def q(sql: str) -> pd.DataFrame:
    r = requests.post(CH, params={"query": sql + " FORMAT TSVWithNames"}, auth=AUTH,
                      timeout=300)
    r.raise_for_status()
    if not r.text.strip():
        return pd.DataFrame()
    return pd.read_csv(io.StringIO(r.text), sep="\t", na_values=["\\N"])


# --------------------------------------------------------------------------- #
# 1. what we trade, and what we stopped trading
# --------------------------------------------------------------------------- #

def traded_history(spec: pd.DataFrame) -> pd.DataFrame:
    frames = []
    for server, db in SERVERS.items():
        d = q(f"""
            WITH s AS (
                SELECT symbol_id,
                       argMax(replaceAll(toString(name),'\\0',''), local_nanos) AS name,
                       min(toDate(local_nanos)) AS first_seen,
                       max(toDate(local_nanos)) AS last_seen,
                       uniqExact(toDate(local_nanos)) AS publish_days
                FROM {db}.symbols_record GROUP BY symbol_id),
            f AS (
                SELECT CAST(symbol_id AS UInt64) AS symbol_id, count() AS fills_30d,
                       max(toDate(local_nanos)) AS last_fill
                FROM {db}.dop_exec_record
                WHERE local_nanos >= now() - INTERVAL 30 DAY
                GROUP BY symbol_id)
            SELECT s.name, s.first_seen, s.last_seen, s.publish_days,
                   ifNull(f.fills_30d, 0) AS fills_30d, f.last_fill,
                   (SELECT max(toDate(local_nanos)) FROM {db}.symbols_record) AS cfg_date
            FROM s LEFT JOIN f ON s.symbol_id = f.symbol_id
            WHERE match(s.name, '^[A-Z]{{3}}-(S|P|IP|F|IF)-')""")
        if d.empty:
            continue
        d["server"] = server
        frames.append(d)
    t = pd.concat(frames, ignore_index=True)

    # An instrument is CURRENT if it appears in that server's most recent publish.
    # Anything older was dropped from the config, and last_seen dates the drop.
    t["status"] = ["active" if ls >= cd else "dropped"
                   for ls, cd in zip(t["last_seen"], t["cfg_date"])]
    today = datetime.now(timezone.utc).date()
    t["days_since_drop"] = [
        (today - pd.to_datetime(ls).date()).days if st == "dropped" else 0
        for ls, st in zip(t["last_seen"], t["status"])]
    t["config_days"] = (pd.to_datetime(t["last_seen"]) - pd.to_datetime(t["first_seen"])).dt.days

    # attach identity via the same mapper the screen uses
    mp = map_internal(parse_internal_names(t["name"]), spec)
    t = t.merge(mp[["name", "venue", "kind", "symbol", "asset_key", "asset_class",
                    "match_rule"]], on="name", how="left")
    return t


# --------------------------------------------------------------------------- #
# 2. what is listed where, and when it first appeared
# --------------------------------------------------------------------------- #

def listing_history(spec: pd.DataFrame, traded_assets: set[str]) -> pd.DataFrame:
    s = spec[(spec["is_excluded"] == 0) & (spec["asset_key"].notna())
             & (spec["asset_key"] != "")].copy()
    s["listed_since"] = pd.to_datetime(s["listed_since"], errors="coerce")
    s["market_type"] = s["kind"].where(s["kind"] == "spot", s["kind"])

    # Where a venue publishes no listing date - every venue but OKX on spot - fall
    # back to the first date our own instrument_ref snapshot saw the row. Tagged,
    # never blended: "venue" is real history, "first_snapshot" starts 2026-08-07.
    seen = q(f"""SELECT venue, symbol, kind, min(toDate(ingest_ts)) AS first_snapshot
                 FROM {DB}.instrument_ref GROUP BY venue, symbol, kind""")
    if not seen.empty:
        s = s.merge(seen, on=["venue", "symbol", "kind"], how="left")
    else:
        s["first_snapshot"] = pd.NaT
    s["first_snapshot"] = pd.to_datetime(s["first_snapshot"], errors="coerce")
    s["listed_src"] = ["venue" if pd.notna(d) else
                       ("first_snapshot" if pd.notna(f) else "unknown")
                       for d, f in zip(s["listed_since"], s["first_snapshot"])]
    s["listed_date"] = s["listed_since"].fillna(s["first_snapshot"])
    s["we_trade_asset"] = s["asset_key"].isin(traded_assets).astype(int)
    return s[["asset_key", "asset_class", "venue", "kind", "symbol", "quote",
              "listed_since", "first_snapshot", "listed_date", "listed_src",
              "active", "we_trade_asset"]]


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    from pathlib import Path
    spec = pd.read_parquet(Path(__file__).parent / "data" / "instrument_ref.parquet")

    th = traded_history(spec)
    print(f"traded_history: {len(th)} instruments  "
          f"{(th.status == 'active').sum()} active / {(th.status == 'dropped').sum()} dropped")
    drops = th[th.status == "dropped"].nlargest(8, "fills_30d")
    if len(drops):
        print("  most recently traded of the dropped:")
        for r in drops.itertuples():
            print(f"    {r.name:<24} {r.server}  last_cfg {r.last_seen}  "
                  f"{r.days_since_drop:>3}d ago  fills30d {r.fills_30d}")

    traded_assets = set(th.loc[th.status == "active", "asset_key"].dropna())
    lh = listing_history(spec, traded_assets)
    print(f"listing_history: {len(lh)} instrument listings, "
          f"{lh.asset_key.nunique()} assets; source "
          f"{lh.listed_src.value_counts().to_dict()}")
    fut = lh[lh.listed_date > pd.Timestamp.utcnow().tz_localize(None)]
    if len(fut):
        print(f"  {len(fut)} listings announced for the future "
              f"({fut.asset_key.nunique()} assets)")

    if a.dry_run:
        print("dry run - nothing written")
        return 0

    import clickhouse_connect
    from ch_load import client, conform
    ch = client()
    run_ts = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    for df, table in ((th, "traded_history"), (lh, "asset_listings")):
        df = df.copy()
        df.insert(0, "run_date", run_ts.date())
        ch.command(f"ALTER TABLE {table} DELETE WHERE run_date = '{run_ts.date()}' "
                   f"SETTINGS mutations_sync = 2")
        ch.insert_df(table, conform(df, table)[0])
        print(f"  loaded {table}: {len(df)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
