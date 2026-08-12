"""Contract specs from the Tardis Instruments Metadata API (pro/business plan).

Needed because `open_interest` and trade `amount` are denominated in *contracts*
on binance-delivery, okex-swap/futures, gate-io-futures and kucoin-futures, so
nothing is comparable across venues until you multiply by the contract size.

Verified multipliers (2026-07-30):
    okex-swap  BTC-USDT-SWAP  linear   mult 0.01    (BTC per contract)
    okex-swap  BTC-USD-SWAP   inverse  mult 100     (USD per contract)
    gate-io-futures BTC_USDT  linear   mult 0.0001
    gate-io-futures BTC_USD   inverse  mult 1
    kucoin-futures XBTUSDTM   linear   mult 0.001
    kucoin-futures XBTUSDM    inverse  mult 1
    binance-delivery BTCUSD_PERP inverse mult 100
    binance-futures / bitget-futures    linear   mult 1  (already base units)

Unit rules:
    inverse : usd = qty_contracts * multiplier                (multiplier = USD/contract)
    linear  : usd = qty_contracts * multiplier * price        (multiplier = base/contract)

GOTCHA: instrument ids are returned **lowercase** for some feeds
(`btcusd_perp`) while dataset symbols are uppercase (`BTCUSD_PERP`) — always
join case-insensitively.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .client import TardisConfig, TardisError, TardisMetadata
from .config import FEEDS

log = logging.getLogger(__name__)

# quote legs treated as 1:1 with USD when converting notionals
USD_QUOTES = {"USD", "USDT", "USDC", "BUSD", "FDUSD", "TUSD", "DAI", "USDE", "USD1"}

FIELDS = ("id", "type", "inverse", "contractMultiplier", "baseCurrency",
          "quoteCurrency", "settlementCurrency", "active", "availableSince",
          "availableTo", "priceIncrement", "amountIncrement", "fundingRateInterval")


def fetch_contract_specs(cfg: TardisConfig, feed_ids: list[str] | None = None,
                         active_only: bool = False) -> list[dict]:
    """One request per feed. Include delisted instruments by default so that
    historical bars still join."""
    meta = TardisMetadata(cfg)
    out: list[dict] = []
    for fid in (feed_ids or list(FEEDS)):
        try:
            rows = meta.instruments(fid, {"active": True} if active_only else None)
        except TardisError as e:
            log.warning("instruments %s: %s", fid, e)
            continue
        for r in rows:
            rec = {"exchange": fid, "symbol_key": str(r.get("id", "")).lower()}
            rec.update({f: r.get(f) for f in FIELDS})
            out.append(rec)
        log.info("instruments %s: %d", fid, len(rows))
    return out


def save_contract_specs(records: list[dict], path: Path) -> Path:
    import pandas as pd
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame.from_records(records).to_csv(path, index=False)
    return path


def apply_units(daily, specs):
    """Attach contract specs to the daily table and add USD-normalised columns.

    daily : DataFrame from rollup.daily_table()
    specs : DataFrame from fetch_contract_specs()
    """
    import numpy as np
    import pandas as pd

    if daily.empty:
        return daily
    d = daily.copy()
    d["symbol_key"] = d["symbol"].str.lower()
    cols = ["exchange", "symbol_key", "inverse", "contractMultiplier",
            "baseCurrency", "quoteCurrency", "type"]
    s = specs[[c for c in cols if c in specs.columns]].drop_duplicates(
        ["exchange", "symbol_key"])
    d = d.merge(s, on=["exchange", "symbol_key"], how="left")

    mult = pd.to_numeric(d.get("contractMultiplier"), errors="coerce").fillna(1.0)
    inv = d.get("inverse").fillna(False).astype(bool) if "inverse" in d else False
    px = d["mark_close"].where(d["mark_close"].notna(), d.get("close")) \
        if "mark_close" in d else d.get("close")

    if "oi_close" in d:
        d["oi_notional_usd"] = np.where(inv, d["oi_close"] * mult,
                                        d["oi_close"] * mult * px)
        d["oi_base"] = np.where(inv, d["oi_close"] * mult / px, d["oi_close"] * mult)
    if "volume_base" in d:
        # `amount` is contracts on contract-denominated venues
        vol = np.where(inv, d["volume_base"] * mult, d["volume_quote"] * mult)
        # a USD figure is only meaningful when the quote leg is a dollar stable
        usd_quote = d.get("quoteCurrency", pd.Series(index=d.index, dtype=object)) \
            .astype(str).str.upper().isin(USD_QUOTES)
        d["volume_usd"] = np.where(inv | usd_quote, vol, np.nan)
    return d.drop(columns=["symbol_key"])
