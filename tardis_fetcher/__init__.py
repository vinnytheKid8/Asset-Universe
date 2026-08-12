"""tardis_stats — Tardis-only fetch layer for a multi-exchange perp/spot stats dashboard.

Covers Binance (spot / USDT-M / COIN-M), OKX (spot / swap / futures),
Bitget (spot / futures), Gate.io (spot / futures), KuCoin (spot / futures).

    from tardis_stats import TardisConfig, fetch_feed_day, backfill

Quick start:
    export TARDIS_API_KEY=...
    python -m tardis_stats.cli check
    python -m tardis_stats.cli universe --out-file data/universe.csv
    python -m tardis_stats.cli backfill --days 14 --workers 4
    python -m tardis_stats.cli dashboard
"""

from .aggregate import (DerivativeTickerAggregator, LiquidationsAggregator,
                        TradesAggregator)
from .client import (TardisAuthError, TardisConfig, TardisDatasets, TardisError,
                     TardisMetadata, TardisNotSubscribed)
from .config import FEEDS, PERP_FEEDS, SPOT_FEEDS, classify_symbol
from .pipeline import backfill, fetch_feed_day, symbol_universe

__all__ = [
    "TardisConfig", "TardisDatasets", "TardisMetadata",
    "TardisError", "TardisAuthError", "TardisNotSubscribed",
    "FEEDS", "PERP_FEEDS", "SPOT_FEEDS", "classify_symbol",
    "fetch_feed_day", "backfill", "symbol_universe",
    "DerivativeTickerAggregator", "TradesAggregator", "LiquidationsAggregator",
]
__version__ = "0.1.0"
