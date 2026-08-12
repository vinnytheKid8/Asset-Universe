"""Static registry of Tardis exchange ids, segments and dataset coverage.

Everything here is verified against https://api.tardis.dev/v1/exchanges and
https://docs.tardis.dev/historical-data-details/<exchange>.

Design note: the *authoritative* per-symbol coverage lives in
``GET /v1/exchanges/{exchange}`` -> ``datasets.symbols[]``.  This module only
holds facts that the API does not return (OI units, inverse/linear split rules,
grouped-file availability), plus sane defaults for bootstrapping.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

Segment = Literal["spot", "linear_perp", "inverse_perp", "futures"]

DATASETS_ENDPOINT = "https://datasets.tardis.dev/v1"
API_ENDPOINT = "https://api.tardis.dev/v1"

# Grouped (one file per exchange per day) symbol ids.
GROUPED_SYMBOLS = {"SPOT", "FUTURES", "PERPETUALS", "COMBOS", "OPTIONS", "PREDICTIONS"}


@dataclass(frozen=True)
class ExchangeFeed:
    """One Tardis exchange id = one download namespace."""

    tardis_id: str
    venue: str                      # binance | okx | bitget | gate | kucoin
    segments: tuple[Segment, ...]
    available_since: date
    has_derivative_ticker: bool
    # grouped symbol usable for bulk pulls, per data type
    grouped: dict[str, str] = field(default_factory=dict)
    # how to tell linear vs inverse perps apart inside this feed
    inverse_rule: str | None = None
    # unit that `open_interest` in derivative_ticker is denominated in
    oi_unit: str = "n/a"
    # approx gzip bytes per grouped file per day (measured 2026-07-01)
    daily_gz_bytes: dict[str, int] = field(default_factory=dict)
    notes: str = ""


_D = date

FEEDS: dict[str, ExchangeFeed] = {
    # ---------------- Binance ----------------
    "binance": ExchangeFeed(
        tardis_id="binance",
        venue="binance",
        segments=("spot",),
        available_since=_D(2019, 3, 30),
        has_derivative_ticker=False,
        grouped={"trades": "SPOT"},
        daily_gz_bytes={"trades": 427_035_112},
        notes="Spot only. book_ticker available per-symbol (not grouped). All pairs since 2021-03-05.",
    ),
    "binance-futures": ExchangeFeed(
        tardis_id="binance-futures",
        venue="binance",
        segments=("linear_perp", "futures"),
        available_since=_D(2019, 11, 17),
        has_derivative_ticker=True,
        grouped={"trades": "PERPETUALS", "derivative_ticker": "PERPETUALS", "liquidations": "PERPETUALS"},
        inverse_rule="none - all linear",
        oi_unit="base asset (contracts == base for USDT-M)",
        daily_gz_bytes={"trades": 1_929_748_475, "derivative_ticker": 1_118_418_616},
        notes="USDT/USDC-margined. open_interest sourced from REST poll since 2020-05-13. "
              "Dated futures use symbol suffix _YYMMDD and live under grouped symbol FUTURES.",
    ),
    "binance-delivery": ExchangeFeed(
        tardis_id="binance-delivery",
        venue="binance",
        segments=("inverse_perp", "futures"),
        available_since=_D(2020, 6, 16),
        has_derivative_ticker=True,
        grouped={"trades": "PERPETUALS", "derivative_ticker": "PERPETUALS", "liquidations": "PERPETUALS"},
        inverse_rule="all inverse (COIN-M)",
        oi_unit="contracts (USD notional per contract: 100 USD for BTC, 10 USD for alts)",
        daily_gz_bytes={"derivative_ticker": 24_854_964},
        notes="Symbols like BTCUSD_PERP (perp) and BTCUSD_260327 (dated).",
    ),
    # ---------------- OKX (tardis id is 'okex') ----------------
    "okex": ExchangeFeed(
        tardis_id="okex",
        venue="okx",
        segments=("spot",),
        available_since=_D(2019, 3, 30),
        has_derivative_ticker=False,
        grouped={"trades": "SPOT"},
        daily_gz_bytes={"trades": 87_251_768},
        notes="Legacy id 'okex' (not 'okx'). Symbols BTC-USDT.",
    ),
    "okex-swap": ExchangeFeed(
        tardis_id="okex-swap",
        venue="okx",
        segments=("linear_perp", "inverse_perp"),
        available_since=_D(2019, 3, 30),
        has_derivative_ticker=True,
        grouped={"trades": "PERPETUALS", "derivative_ticker": "PERPETUALS", "liquidations": "PERPETUALS"},
        inverse_rule="symbol endswith '-USD-SWAP' => inverse; '-USDT-SWAP'/'-USDC-SWAP' => linear",
        oi_unit="contracts (ctVal from instruments metadata / OKX /public/instruments)",
        daily_gz_bytes={"derivative_ticker": 554_435_936},
        notes="Linear and inverse perps share one feed.",
    ),
    "okex-futures": ExchangeFeed(
        tardis_id="okex-futures",
        venue="okx",
        segments=("futures",),
        available_since=_D(2019, 3, 30),
        has_derivative_ticker=True,
        grouped={"trades": "FUTURES", "derivative_ticker": "FUTURES", "liquidations": "FUTURES"},
        inverse_rule="symbol contains '-USD-' => inverse dated future",
        oi_unit="contracts",
    ),
    # ---------------- Bitget ----------------
    "bitget": ExchangeFeed(
        tardis_id="bitget",
        venue="bitget",
        segments=("spot",),
        available_since=_D(2024, 11, 8),
        has_derivative_ticker=False,
        grouped={"trades": "SPOT"},
        daily_gz_bytes={"trades": 74_117_990},
        notes="book_ticker since 2025-01-01. Bitget API migrated v2->v3 on 2026-04-28.",
    ),
    "bitget-futures": ExchangeFeed(
        tardis_id="bitget-futures",
        venue="bitget",
        segments=("linear_perp", "inverse_perp", "futures"),
        available_since=_D(2024, 11, 8),
        has_derivative_ticker=True,
        grouped={"trades": "PERPETUALS", "derivative_ticker": "PERPETUALS", "liquidations": "PERPETUALS"},
        inverse_rule="COIN-FUTURES symbols quote in USD (e.g. BTCUSD); USDT/USDC suffix => linear",
        oi_unit="base asset",
        daily_gz_bytes={"derivative_ticker": 1_098_015_019},
        notes="liquidations only since 2026-05-01.",
    ),
    # ---------------- Gate.io ----------------
    "gate-io": ExchangeFeed(
        tardis_id="gate-io",
        venue="gate",
        segments=("spot",),
        available_since=_D(2020, 7, 1),
        has_derivative_ticker=False,
        grouped={"trades": "SPOT"},
        daily_gz_bytes={"trades": 122_174_651},
        notes="All pairs since 2022-06-09. Symbols BTC_USDT.",
    ),
    "gate-io-futures": ExchangeFeed(
        tardis_id="gate-io-futures",
        venue="gate",
        segments=("linear_perp", "inverse_perp", "futures"),
        available_since=_D(2020, 7, 1),
        has_derivative_ticker=True,
        grouped={"trades": "PERPETUALS", "derivative_ticker": "PERPETUALS"},
        inverse_rule="settle=btc contracts quote in USD (BTC_USD); BTC_USDT => linear",
        oi_unit="contracts (quanto_multiplier from Gate /futures/{settle}/contracts)",
        daily_gz_bytes={"derivative_ticker": 154_416_901},
    ),
    # ---------------- KuCoin ----------------
    "kucoin": ExchangeFeed(
        tardis_id="kucoin",
        venue="kucoin",
        segments=("spot",),
        available_since=_D(2022, 8, 16),
        has_derivative_ticker=False,
        grouped={"trades": "SPOT"},
        daily_gz_bytes={"trades": 70_022_258},
    ),
    "kucoin-futures": ExchangeFeed(
        tardis_id="kucoin-futures",
        venue="kucoin",
        segments=("linear_perp", "inverse_perp", "futures"),
        available_since=_D(2024, 1, 25),
        has_derivative_ticker=True,
        grouped={"trades": "PERPETUALS", "derivative_ticker": "PERPETUALS"},
        inverse_rule="symbol endswith 'M' after USDT => linear (XBTUSDTM); 'USDM' => inverse (XBTUSDM)",
        oi_unit="contracts (multiplier from KuCoin /api/v1/contracts/active)",
        daily_gz_bytes={"derivative_ticker": 132_326_621},
        notes="XBT is used instead of BTC.",
    ),
}

PERP_FEEDS = [f for f in FEEDS.values() if f.has_derivative_ticker]
SPOT_FEEDS = [f for f in FEEDS.values() if "spot" in f.segments]
ALL_FEED_IDS = list(FEEDS)


def classify_symbol(feed_id: str, symbol: str) -> Segment:
    """Best-effort linear/inverse/spot classification from the dataset symbol id."""
    feed = FEEDS[feed_id]
    if feed.segments == ("spot",):
        return "spot"
    s = symbol.upper()
    if feed_id == "binance-delivery":
        return "inverse_perp" if s.endswith("_PERP") else "futures"
    if feed_id == "binance-futures":
        return "linear_perp" if "_" not in s else "futures"
    if feed_id == "okex-swap":
        return "inverse_perp" if s.endswith("-USD-SWAP") else "linear_perp"
    if feed_id == "okex-futures":
        return "futures"
    if feed_id == "bitget-futures":
        return "linear_perp" if ("USDT" in s or "USDC" in s) else "inverse_perp"
    if feed_id == "gate-io-futures":
        return "linear_perp" if s.endswith("_USDT") or s.endswith("_USDC") else "inverse_perp"
    if feed_id == "kucoin-futures":
        return "linear_perp" if s.endswith("USDTM") or s.endswith("USDCM") else "inverse_perp"
    return "futures"
