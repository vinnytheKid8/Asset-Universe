"""ClickHouse schema for the `symbol_stats` database.

Idempotent — every statement is CREATE ... IF NOT EXISTS, so this runs on every
load and only does something the first time (or after a table is added here).

    python ch_schema.py --create     # create anything missing
    python ch_schema.py --show       # what exists now, with row counts
    python ch_schema.py --drop-all   # tear down (asks first)

A note on the sorting keys, because getting one wrong loses rows silently:
ReplacingMergeTree collapses any two rows sharing the full ORDER BY tuple, with
no error. Each key below is the real uniqueness key of its table, and each has a
non-obvious member that was found by loading real data and counting:

  instrument_ref      needs `kind`        - spot and perp share the symbol string
                                            (BIN BTCUSDT is both). 1,381 collisions.
  screen_runs_venue   needs `symbol` AND `market_type` - one asset has several
                                            instruments per venue (AAVEUSDT and
                                            AAVEUSDC on BIN), and BIN lists BTCUSDT
                                            as both spot and perp under one string.
  instrument_daily    needs `market_type` - no collision while the deep pull is
                                            perps only; latent the moment spot lands.

`ch_load.check_keys()` asserts stored == distinct == input on every load. Do not
change an ORDER BY without re-running it.
"""
from __future__ import annotations

import argparse
import os

import requests

CH_URL = os.environ.get("CH_URL", "http://192.168.50.39:8123/")
CH_USER = os.environ.get("CH_USER", "vinny")
CH_PASS = os.environ.get("CH_PASS", "888")
DB = os.environ.get("CH_DB", "symbol_stats")


def execute(sql: str, timeout: int = 120) -> str:
    r = requests.post(CH_URL, data=sql.encode(), auth=(CH_USER, CH_PASS), timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"ClickHouse error:\n{r.text[:2000]}\n--- query ---\n{sql[:800]}")
    return r.text


# --------------------------------------------------------------------------- #
# Facts
# --------------------------------------------------------------------------- #

INSTRUMENT_DAILY = f"""
CREATE TABLE IF NOT EXISTS {DB}.instrument_daily
(
    date            Date,
    venue           LowCardinality(String),
    symbol          String,
    market_type     LowCardinality(String),
    asset_key       LowCardinality(String),
    asset_class     LowCardinality(String),
    scale_factor    Float64,              -- 1000 for 1000SHIBUSDT. Prices are NOT
                                          -- comparable until divided by this.
    base_ccy        LowCardinality(String),
    quote_ccy       LowCardinality(String),
    px_open         Float64,
    px_high         Float64,
    px_low          Float64,
    px_close        Float64,
    vol_base        Float64,
    vol_quote       Nullable(Float64),
    vol_usd         Nullable(Float64),
    fx_rate         Float64,
    fx_source       LowCardinality(String),
    vol_quote_is_estimate UInt8,
    trades          Nullable(UInt32),
    taker_buy_quote Nullable(Float64),
    taker_imbalance Nullable(Float64),
    oi_native       Nullable(Float64),
    oi_usd          Nullable(Float64),
    oi_source       LowCardinality(String),
    mark_close      Nullable(Float64),
    lsr_account     Nullable(Float64),
    lsr_taker       Nullable(Float64),
    liq_long_usd    Nullable(Float64),
    liq_short_usd   Nullable(Float64),
    contract_mult   Float64,
    funding_sum     Nullable(Float64),
    funding_events  Nullable(UInt16),
    funding_interval_h Nullable(Float64),
    funding_apr_pct Nullable(Float64),
    caps_trades     UInt8,
    caps_oi_hist_days Float64,
    ingest_ts       DateTime('UTC')
)
ENGINE = ReplacingMergeTree(ingest_ts)
PARTITION BY (venue, toYYYYMM(date))
ORDER BY (venue, symbol, market_type, date)
"""

# --------------------------------------------------------------------------- #
# Dimension
# --------------------------------------------------------------------------- #

INSTRUMENT_REF = f"""
CREATE TABLE IF NOT EXISTS {DB}.instrument_ref
(
    venue           LowCardinality(String),
    symbol          String,
    kind            LowCardinality(String),
    base_raw        String,
    quote           LowCardinality(String),
    asset_key       LowCardinality(String),
    asset_class     LowCardinality(String),
    equity_region   LowCardinality(String),
    issuer          LowCardinality(String),
    contract_mult   Float64,
    scale_factor    Float64,
    inverse         UInt8,
    tick            Float64,
    lot             Float64,
    min_notional    Float64,
    maker_fee       Float64,
    taker_fee       Float64,
    underlying_type LowCardinality(String),
    is_rwa          UInt8,
    funding_interval_h Nullable(Float64),
    listed_since    Nullable(Date),
    active          UInt8,
    id_source       LowCardinality(String),
    id_confidence   LowCardinality(String),
    is_excluded     UInt8,
    exclude_reason  String,
    ingest_ts       DateTime('UTC')
)
ENGINE = ReplacingMergeTree(ingest_ts)
ORDER BY (venue, symbol, kind)
"""

# --------------------------------------------------------------------------- #
# Screen snapshots - one row set per run. This is the only source of verdict
# history; a run that isn't persisted is a data point that cannot be recovered.
# --------------------------------------------------------------------------- #

SCREEN_RUNS = f"""
CREATE TABLE IF NOT EXISTS {DB}.screen_runs
(
    run_date        Date,
    run_ts          DateTime('UTC'),
    asset_key       LowCardinality(String),
    asset_class     LowCardinality(String),
    equity_region   LowCardinality(String),
    lane            LowCardinality(String),
    verdict         LowCardinality(String),
    traded          UInt8,
    -- active | dropped | never. `traded` is (traded_state = 'active'); the third
    -- state exists because "we took this off" is not the same as "we never had it",
    -- and collapsing them put already-dropped assets back on the drop list.
    traded_state    LowCardinality(String),
    dropped_on      Nullable(Date),
    composite       Float64,
    add_bar         Float64,
    c_flow          Float64,
    c_structure     Float64,
    c_carry         Float64,
    c_friction      Float64,
    gate_adv        UInt8,
    gate_persist    UInt8,
    gate_venues     UInt8,
    gate_oi         UInt8,
    n_fail          UInt8,
    new_listing     UInt8,
    tick_pinned     UInt8,
    decaying        UInt8,
    -- WHICH decay path fired, not just that one did. decay_slow is the 7d/30d level
    -- ratio + 30d slope rule; decay_fast is the off-peak rule (FRAMEWORK.md 3.0),
    -- which ships disabled. Without these, an asset reads "decaying" with no way to
    -- tell whether it faded slowly or fell off a cliff - and they were being dropped
    -- at load with a warning on every single run.
    decay_slow      UInt8,
    decay_fast      UInt8,
    days            Float64,
    vol_usd_med30   Float64,
    vol_usd_mean30  Float64,
    vol_usd_med7    Float64,
    -- Weekday-only twins of the level / persistence / off-peak readings. Weekend
    -- volume is 11% of weekday on equity and 96% on crypto (measured 2026-08-23),
    -- so every window holding a Saturday marks RWA down for the calendar alone.
    -- score() takes the better of each pair: a weekend can help, never penalise.
    vol_usd_med7_wd  Float64,
    vol_usd_med30_wd Float64,
    days_above_5m_wd Float64,
    vol_off_peak_wd  Float64,
    wknd_ratio       Float64,   -- weekend median / weekday median; <1 = goes quiet
    -- Per-venue depth over the 30d history. vol_venue_med is the MEDIAN venue's
    -- daily volume: an asset doing $16M with 82% on one venue has a median venue of
    -- ~$1M, and summed volume cannot tell that from $16M spread evenly.
    vol_venue_med    Float64,
    vol_venue_mean   Float64,
    vol_hhi_30d      Float64,   -- HHI from 30d volume; stabler than the 24h one
    vol_top_share    Float64,   -- largest venue's share, hhi's readable twin
    spot_vol_venue_med Float64,
    -- What the gates and the flow score actually read once the calendar adjustment
    -- is applied. Stored so a verdict can be explained without recomputing it.
    adv7_cal         Float64,
    persist_cal      Float64,
    off_peak_cal     Float64,
    conc_hhi         Float64,
    -- Per-venue 30d median daily volume, largest first. Stored as a vector rather
    -- than a count so the liquidity floor stays a dashboard knob.
    vol_venue_1      Float64,
    vol_venue_2      Float64,
    vol_venue_3      Float64,
    vol_venue_4      Float64,
    vol_venue_5      Float64,
    -- Venues clearing g_venue_adv. n_venues counts LISTINGS: 96% of the universe
    -- cleared a 3-venue gate on it while the median asset's top venue carried 75%
    -- of its volume. This is the number the gate actually reads.
    n_venues_liq     UInt8,
    thin_venues      UInt8,   -- fewer real venues than hard_venue_min -> drop
    -- How much of this book is US. The volume we score is the venue TOTAL, which
    -- includes our own flow - circular on a thin book, where we quote, volume
    -- appears, and the screen reads a liquid venue. Measured over 7d on both sides.
    -- our_share_max_leg is the one to watch: the single worst leg, where FARTCOIN
    -- on Bitget perp reads 0.68 and LTC on Bitget spot reads 1.10.
    our_vol_7d       Float64,
    mkt_vol_7d       Float64,
    our_vol_share    Float64,
    our_share_max_leg Float64,
    our_fills_7d     Float64,
    -- Our share of OPEN INTEREST. Position = session + outside - invested, valued
    -- at the last traded price x contract_mult. Both sides are restricted to legs
    -- the venue reports OI for: Bitget publishes none, so counting our BGT position
    -- against an OI total that excludes Bitget inflated the ratio above 100% of the
    -- worst leg, which is impossible for a share.
    our_oi_usd       Float64,
    venue_oi_usd     Float64,
    our_oi_share     Float64,
    our_oi_share_max_leg Float64,
    vol_trend       Float64,
    vol_burstiness  Float64,
    vol_slope_pct_day Float64,
    -- fast, directional change: vol_trend is a level ratio and cannot fall while the
    -- 30d median lags, so these are what the decay rule should read (see build_report)
    vol_d1          Float64,
    vol_r37         Float64,
    vol_off_peak    Float64,
    vol_slope7_pct_day Float64,
    vol_dow7        Float64,
    vol_peak_age    Int16,
    vol_logmad      Float64,
    days_above_5m   Float64,
    days_above_5m_live Float64,
    days_since_onset Float64,
    live_days       Float64,
    oi_usd_med30    Float64,
    oi_usd_mean30   Float64,
    oi_usd_live     Float64,
    oi_usd_live_bgt Float64,
    oi_bgt_share    Float64,
    oi_trend        Float64,
    oi_to_adv       Float64,
    oi_days         Float64,
    oi_venues       UInt16,
    trades_med30    Float64,
    n_venues        Float64,
    venues_perp     UInt16,
    venues_spot     UInt16,          -- pairs LISTED on spot
    spot_venues_live UInt16,         -- spot venues with actual 30d flow
    spot_days       UInt16,
    spot_vol_usd_med30 Float64,
    spot_vol_usd_mean30 Float64,
    spot_vol_usd_med7 Float64,
    perp_spot_ratio Float64,         -- perp ADV / spot ADV; high = thin deliverable
    spot_share      Float64,
    venue_hhi       Float64,
    rv_bps_med      Float64,
    px_spread_bps_med Float64,
    spread_bps_med  Float64,
    spread_bps_min  Float64,
    tick_bps_med    Float64,
    edge_headroom_bps Float64,
    fund_bps_med    Float64,
    fund_spread_bps_med Float64,
    maker_fee_min   Float64,
    min_notional_max Float64,
    listed_since    Nullable(Date),
    age_days        Int32
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(run_date)
ORDER BY (asset_key, run_date)
"""

SCREEN_RUNS_VENUE = f"""
CREATE TABLE IF NOT EXISTS {DB}.screen_runs_venue
(
    run_date        Date,
    asset_key       LowCardinality(String),
    venue           LowCardinality(String),
    symbol          String,
    market_type     LowCardinality(String),   -- linear_perp | inverse_perp | spot
    quote           LowCardinality(String),
    we_quote        UInt8,
    vol_usd_mean30  Float64,
    vol_usd_med30   Float64,
    vol_usd_med7    Float64,
    vol_share       Float64,
    vol_trend       Float64,
    vol24h_usd      Float64,
    trades_med30    Float64,
    oi_usd_med30    Float64,
    oi_usd_live     Float64,
    oi_days         UInt16,
    spread_bps      Float64,
    tick_bps        Float64,
    fund_bps_med    Float64,
    fund_iv_h       Float64,
    maker_fee       Float64,
    taker_fee       Float64,
    min_notional    Float64,
    contract_mult   Float64,
    px_close        Float64,
    listed_since    Nullable(Date),
    days            UInt16
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(run_date)
ORDER BY (asset_key, venue, symbol, market_type, run_date)
"""

# --------------------------------------------------------------------------- #
# Data quality
# --------------------------------------------------------------------------- #

SCREEN_DUP_TICKERS = f"""
CREATE TABLE IF NOT EXISTS {DB}.screen_dup_tickers
(
    run_date        Date,
    asset_a         LowCardinality(String),
    asset_b         LowCardinality(String),
    class           LowCardinality(String),
    px_a            Float64,
    px_b            Float64,
    diff_bps        Float64,
    vol_a           Float64,
    vol_b           Float64,
    venues_a        UInt16,
    venues_b        UInt16
)
ENGINE = ReplacingMergeTree
ORDER BY (run_date, asset_a, asset_b)
"""

INTERNAL_MAP = f"""
CREATE TABLE IF NOT EXISTS {DB}.internal_map
(
    run_date        Date,
    name            String,             -- our internal symbology, e.g. BIN-P-BTCUSDT
    venue           LowCardinality(String),
    kind            LowCardinality(String),
    base            String,
    quote           LowCardinality(String),
    symbol          String,             -- venue-native
    asset_key       LowCardinality(String),
    asset_class     LowCardinality(String),
    match_rule      LowCardinality(String),
    -- 1 = present in the latest publish of a LIVE strat server. A server that
    -- stopped publishing (hk, 2026-08-03) is not evidence of anything current.
    in_config       UInt8,
    fills           UInt64,             -- last 10 days
    fills_prev      UInt64,             -- last 90 days
    dropped_on      Nullable(Date),     -- last config date, if no longer in it
    first_day       Nullable(Date),
    last_day        Nullable(Date)
)
ENGINE = ReplacingMergeTree
ORDER BY (run_date, name)
"""


TRADED_HISTORY = f"""
CREATE TABLE IF NOT EXISTS {DB}.traded_history
(
    run_date        Date,
    name            String,                   -- internal, e.g. BIN-P-BTCUSDT
    server          LowCardinality(String),   -- tk | hk
    venue           LowCardinality(String),
    kind            LowCardinality(String),
    symbol          String,
    asset_key       LowCardinality(String),
    asset_class     LowCardinality(String),
    match_rule      LowCardinality(String),
    first_seen      Date,                     -- first appearance in symbols_record
    last_seen       Date,                     -- last publish that still had it
    cfg_date        Date,                     -- that server's most recent publish
    publish_days    UInt16,
    config_days     UInt16,
    status          LowCardinality(String),   -- active | dropped
    days_since_drop UInt16,
    fills_30d       UInt64,
    last_fill       Nullable(Date)
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(run_date)
ORDER BY (run_date, server, name)
"""

ASSET_LISTINGS = f"""
CREATE TABLE IF NOT EXISTS {DB}.asset_listings
(
    run_date        Date,
    asset_key       LowCardinality(String),
    asset_class     LowCardinality(String),
    venue           LowCardinality(String),
    kind            LowCardinality(String),
    symbol          String,
    quote           LowCardinality(String),
    listed_since    Nullable(Date),           -- venue-declared; NULL on most spot
    first_snapshot  Nullable(Date),           -- first time OUR ref snapshot saw it
    listed_date     Nullable(Date),           -- coalesce of the two
    listed_src      LowCardinality(String),   -- venue | first_snapshot | unknown
    active          UInt8,
    we_trade_asset  UInt8
)
ENGINE = ReplacingMergeTree
PARTITION BY toYYYYMM(run_date)
ORDER BY (run_date, asset_key, venue, kind, symbol)
"""

# Per asset: when it first appeared anywhere, and a listed/not column per venue.
# Restricted to the latest run so it reads as "the picture now".
ASSET_LISTING_MATRIX = f"""
CREATE VIEW IF NOT EXISTS {DB}.asset_listing_matrix AS
SELECT
    asset_key,
    any(asset_class)                                    AS asset_class,
    -- "New" must mean venue-declared new. Dating a listing from when OUR snapshot
    -- first saw it would make every spot pair on BIN/BGT/GAT/KCN look days old,
    -- because those venues publish no spot listing date (only OKX does).
    minIf(listed_date, listed_src = 'venue')            AS first_listed,
    min(listed_date)                                    AS first_seen_any,
    countIf(listed_src = 'venue') > 0                   AS date_is_declared,
    dateDiff('day', minIf(listed_date, listed_src = 'venue'), today()) AS age_days,
    max(we_trade_asset)                                 AS we_trade,
    uniqExactIf(venue, kind != 'spot')                  AS venues_perp,
    uniqExactIf(venue, kind  = 'spot')                  AS venues_spot,
    -- per venue: 0 not listed | 1 perp only | 2 spot only | 3 both
    maxIf(kind != 'spot', venue='BIN') + 2 * maxIf(kind = 'spot', venue='BIN') AS BIN,
    maxIf(kind != 'spot', venue='OKX') + 2 * maxIf(kind = 'spot', venue='OKX') AS OKX,
    maxIf(kind != 'spot', venue='BGT') + 2 * maxIf(kind = 'spot', venue='BGT') AS BGT,
    maxIf(kind != 'spot', venue='GAT') + 2 * maxIf(kind = 'spot', venue='GAT') AS GAT,
    maxIf(kind != 'spot', venue='KCN') + 2 * maxIf(kind = 'spot', venue='KCN') AS KCN,
    minIf(listed_date, venue = 'BIN')                   AS BIN_since,
    minIf(listed_date, venue = 'OKX')                   AS OKX_since,
    minIf(listed_date, venue = 'BGT')                   AS BGT_since,
    minIf(listed_date, venue = 'GAT')                   AS GAT_since,
    minIf(listed_date, venue = 'KCN')                   AS KCN_since,
    groupUniqArray(listed_src)                          AS srcs
FROM {DB}.asset_listings
WHERE run_date = (SELECT max(run_date) FROM {DB}.asset_listings)
GROUP BY asset_key
HAVING date_is_declared
"""

# --------------------------------------------------------------------------- #
# Load audit - answers "did last night's job run, and was it clean?"
# --------------------------------------------------------------------------- #

LOAD_RUNS = f"""
CREATE TABLE IF NOT EXISTS {DB}.load_runs
(
    run_ts          DateTime('UTC'),
    table           LowCardinality(String),
    rows_in         UInt64,
    rows_loaded     UInt64,
    rows_rejected   UInt64,
    status          LowCardinality(String),   -- ok | warn | fail
    duration_s      Float32,
    notes           String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(run_ts)
ORDER BY (run_ts, table)
"""

LOAD_REJECTS = f"""
CREATE TABLE IF NOT EXISTS {DB}.load_rejects
(
    run_ts          DateTime('UTC'),
    table           LowCardinality(String),
    reason          LowCardinality(String),
    venue           LowCardinality(String),
    symbol          String,
    detail          String
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(run_ts)
ORDER BY (run_ts, table, venue, symbol)
"""

# --------------------------------------------------------------------------- #
# Derived view. Cheap, recomputable, always in sync - no MV to backfill.
# --------------------------------------------------------------------------- #

ASSET_DAILY = f"""
CREATE VIEW IF NOT EXISTS {DB}.asset_daily AS
SELECT
    date,
    asset_key,
    any(asset_class)                                    AS asset_class,
    sum(vol_usd)                                        AS vol_usd,
    sum(oi_usd)                                         AS oi_usd,
    sum(trades)                                         AS trades,
    uniqExact(venue)                                    AS n_venues,
    avgIf(px_close / scale_factor, quote_ccy = 'USDT')  AS px_avg,
    -- validation gate 1, live: cross-venue price agreement.
    -- USDT-to-USDT and scale-normalised, or it measures the wrong thing entirely:
    -- mixing quote currencies made ETH read 11,004 bps (ETHBTC), and skipping
    -- scale_factor made SHIB read 49,751 bps (1000SHIBUSDT).
    if(countIf(quote_ccy = 'USDT') > 1,
       (maxIf(px_close / scale_factor, quote_ccy = 'USDT')
          - minIf(px_close / scale_factor, quote_ccy = 'USDT'))
         / avgIf(px_close / scale_factor, quote_ccy = 'USDT') * 10000, 0) AS px_spread_bps,
    -- not an error: the stablecoin basis, a real number (~-5 to -9 bps on majors)
    if(countIf(quote_ccy = 'USDC') > 0 AND countIf(quote_ccy = 'USDT') > 0,
       (avgIf(px_close / scale_factor, quote_ccy = 'USDC')
          / avgIf(px_close / scale_factor, quote_ccy = 'USDT') - 1) * 10000,
       NULL)                                            AS usdc_basis_bps,
    avgIf(funding_apr_pct, isFinite(funding_apr_pct))    AS funding_apr_pct_avg,
    maxIf(funding_apr_pct, isFinite(funding_apr_pct))
      - minIf(funding_apr_pct, isFinite(funding_apr_pct)) AS funding_apr_spread_pct
FROM {DB}.instrument_daily
WHERE market_type != 'spot'
  AND quote_ccy IN ('USDT', 'USDC', 'USD')
GROUP BY date, asset_key
"""

VENUE_SNAPSHOT = f"""
CREATE TABLE IF NOT EXISTS {DB}.venue_snapshot
(
    run_date        Date,
    venue           LowCardinality(String),
    symbol          String,
    kind            LowCardinality(String),   -- linear_perp | inverse_perp | spot
    quote           LowCardinality(String),
    asset_key       LowCardinality(String),
    asset_class     LowCardinality(String),
    equity_region   LowCardinality(String),
    is_rwa          UInt8,
    inverse         UInt8,
    active          UInt8,
    is_excluded     UInt8,
    last            Nullable(Float64),
    vol24h_usd      Float64,
    oi_usd          Nullable(Float64),
    trades24h       Nullable(Float64),
    funding_rate    Nullable(Float64),
    funding_interval_h Nullable(Float64),
    spread_bps      Nullable(Float64),
    tick_bps        Nullable(Float64),
    ingest_ts       DateTime('UTC')
)
ENGINE = ReplacingMergeTree(ingest_ts)
PARTITION BY toYYYYMM(run_date)
-- kind is load-bearing: BIN lists BTCUSDT as both spot and perp, and 2,578 rows
-- collide on (venue, symbol) alone.
ORDER BY (run_date, venue, symbol, kind)
"""

TABLES = {
    "instrument_ref": INSTRUMENT_REF,
    "venue_snapshot": VENUE_SNAPSHOT,
    "instrument_daily": INSTRUMENT_DAILY,
    "screen_runs": SCREEN_RUNS,
    "screen_runs_venue": SCREEN_RUNS_VENUE,
    "screen_dup_tickers": SCREEN_DUP_TICKERS,
    "internal_map": INTERNAL_MAP,
    "traded_history": TRADED_HISTORY,
    "asset_listings": ASSET_LISTINGS,
    "load_runs": LOAD_RUNS,
    "load_rejects": LOAD_REJECTS,
}
VIEWS = {"asset_daily": ASSET_DAILY, "asset_listing_matrix": ASSET_LISTING_MATRIX}

# (table, key columns) - checked after every load. See the module docstring.
DEDUP_KEYS = {
    "instrument_ref": ["venue", "symbol", "kind"],
    "venue_snapshot": ["run_date", "venue", "symbol", "kind"],
    "instrument_daily": ["venue", "symbol", "market_type", "date"],
    "screen_runs": ["asset_key", "run_date"],
    "screen_runs_venue": ["asset_key", "venue", "symbol", "market_type", "run_date"],
    "traded_history": ["run_date", "server", "name"],
    "asset_listings": ["run_date", "asset_key", "venue", "kind", "symbol"],
}


def _desired(name: str, ddl: str) -> tuple[dict[str, str], str]:
    """Columns and sorting key this DDL *would* produce.

    Built by creating the table under a scratch name and reading it back, so
    ClickHouse itself is the DDL parser rather than a regex that will drift.
    """
    tmp = f"__schemachk_{name}"
    execute(f"DROP TABLE IF EXISTS {DB}.{tmp}")
    execute(ddl.replace(f"{DB}.{name}", f"{DB}.{tmp}", 1)
               .replace("IF NOT EXISTS", "", 1))
    cols = dict(line.split("\t") for line in execute(
        f"SELECT name, type FROM system.columns WHERE database='{DB}' AND table='{tmp}' "
        f"ORDER BY position FORMAT TSV").strip().split("\n") if line)
    key = execute(f"SELECT sorting_key FROM system.tables WHERE database='{DB}' "
                  f"AND name='{tmp}' FORMAT TSV").strip()
    execute(f"DROP TABLE IF EXISTS {DB}.{tmp}")
    return cols, key


def migrate(recreate: bool = False, verbose: bool = True) -> None:
    """Bring existing tables up to the DDL above.

    New columns are added in place. A changed SORTING KEY cannot be altered - it
    is the physical layout - so those tables are listed and only recreated when
    `recreate` is set, because recreating drops their history.
    """
    existing = set(execute(
        f"SELECT name FROM system.tables WHERE database='{DB}' FORMAT TSV").split())
    for name, ddl in TABLES.items():
        if name not in existing:
            continue
        want_cols, want_key = _desired(name, ddl)
        have_cols = columns(name)
        have_key = execute(f"SELECT sorting_key FROM system.tables WHERE database='{DB}' "
                           f"AND name='{name}' FORMAT TSV").strip()
        add = [(c, t) for c, t in want_cols.items() if c not in have_cols]
        if add:
            for c, t in add:
                execute(f"ALTER TABLE {DB}.{name} ADD COLUMN IF NOT EXISTS `{c}` {t}")
            if verbose:
                print(f"  {name}: +{len(add)} column(s): {', '.join(c for c, _ in add)}")
        if want_key != have_key:
            n = execute(f"SELECT count() FROM {DB}.{name} FORMAT TSV").strip()
            if recreate:
                execute(f"DROP TABLE {DB}.{name}")
                execute(ddl)
                print(f"  {name}: RECREATED (sorting key changed, {n} rows dropped)")
            else:
                print(f"  {name}: sorting key must change and cannot be ALTERed\n"
                      f"      have: {have_key}\n      want: {want_key}\n"
                      f"      {n} rows would be lost - rerun with --recreate to apply")


def create_all(verbose: bool = True, recreate: bool = False) -> list[str]:
    """Create the database, tables and views that don't exist yet. Idempotent."""
    execute(f"CREATE DATABASE IF NOT EXISTS {DB}")
    existing = set(execute(
        f"SELECT name FROM system.tables WHERE database='{DB}' FORMAT TSV").split())
    made = []
    for name, ddl in list(TABLES.items()) + list(VIEWS.items()):
        if name not in existing:
            made.append(name)
        execute(ddl)
    if verbose and made:
        print(f"created: {', '.join(made)}")
    migrate(recreate=recreate, verbose=verbose)
    # views are cheap and must track the tables they read
    for name, ddl in VIEWS.items():
        execute(f"DROP VIEW IF EXISTS {DB}.{name}")
        execute(ddl)
    return made


def columns(table: str) -> dict[str, str]:
    rows = execute(f"SELECT name, type FROM system.columns "
                   f"WHERE database='{DB}' AND table='{table}' ORDER BY position FORMAT TSV")
    return dict(line.split("\t") for line in rows.strip().split("\n") if line)


def show() -> None:
    out = execute(f"""
        SELECT name, engine, formatReadableQuantity(total_rows), formatReadableSize(total_bytes)
        FROM system.tables WHERE database='{DB}' ORDER BY name FORMAT TSV""")
    print(f"{'table':<22} {'engine':<22} {'rows':>14}  size")
    for line in out.strip().split("\n"):
        if not line:
            continue
        n, e, r, s = line.split("\t")
        print(f"{n:<22} {e:<22} {r:>14}  {s}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--drop-all", action="store_true")
    ap.add_argument("--recreate", action="store_true",
                    help="rebuild tables whose sorting key changed (drops their rows)")
    a = ap.parse_args()

    if a.drop_all:
        print(f"This drops every table in {DB} at {CH_URL}. Data is not recoverable.")
        if input("type the database name to confirm: ").strip() != DB:
            print("aborted")
            return
        for name in list(VIEWS) + list(TABLES):
            execute(f"DROP TABLE IF EXISTS {DB}.{name}")
        print("dropped")
        return

    if a.create or a.recreate or not a.show:
        create_all(recreate=a.recreate)
    show()


if __name__ == "__main__":
    main()
