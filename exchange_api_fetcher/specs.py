"""Instrument reference data from the five venues' own specs endpoints.

Base/quote resolution rule, per venue (verified 2026-08-06):
  BIN  declared  baseAsset / quoteAsset
  BGT  declared  baseCoin / quoteCoin
  KCN  declared  baseCurrency / quoteCurrency        (XBT -> BTC)
  OKX  PARSED from instId - `baseCcy`/`quoteCcy` come back EMPTY for swaps.
       Safe because instId is delimited: BTC-USDT-SWAP.
  GAT  PARSED from name - there are no base/quote fields at all.
       Safe because name is delimited: BTC_USDT.

The dangerous case is a concatenated symbol with no delimiter (UBUSDT vs UBERUSDT,
MUSDT vs MUUSDT vs MUUUSDT) - and those venues are exactly the ones that declare
base/quote. So we never guess.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from .venues import _f, _get

log = logging.getLogger(__name__)

BIN_F, BIN_S = "https://fapi.binance.com", "https://api.binance.com"
BIN_D = "https://dapi.binance.com"          # COIN-M inverse perps, separate host
OKX_B, BGT_B = "https://www.okx.com", "https://api.bitget.com"
GAT_B = "https://api.gateio.ws/api/v4"
KCN_F, KCN_S = "https://api-futures.kucoin.com", "https://api.kucoin.com"


def _ts(ms, unit="ms"):
    if ms in (None, "", 0, "0"):
        return None
    try:
        v = float(ms)
    except (TypeError, ValueError):
        return None
    if unit == "s":
        v *= 1000
    return datetime.fromtimestamp(v / 1000, timezone.utc).date()


def _row(**kw):
    base = dict(venue=None, symbol=None, kind=None, base_raw=None, quote=None,
                contract_mult=1.0, inverse=0, tick=None, lot=None, min_notional=None,
                maker_fee=None, taker_fee=None, underlying_type=None, is_rwa=None,
                funding_interval_h=None, listed_since=None, active=1, id_source=None)
    base.update(kw)
    return base


# ------------------------------------------------------------------ Binance
def binance() -> list[dict]:
    out = []
    for st in _get(f"{BIN_F}/fapi/v1/exchangeInfo")["symbols"]:
        if st.get("contractType") not in ("PERPETUAL", "TRADIFI_PERPETUAL"):
            continue                                  # skip dated futures
        flt = {f["filterType"]: f for f in st.get("filters", [])}
        out.append(_row(
            venue="BIN", symbol=st["symbol"], kind="linear_perp",
            base_raw=st["baseAsset"], quote=st["quoteAsset"],
            tick=_f(flt.get("PRICE_FILTER", {}).get("tickSize")),
            lot=_f(flt.get("LOT_SIZE", {}).get("stepSize")),
            min_notional=_f(flt.get("MIN_NOTIONAL", {}).get("notional")),
            underlying_type=st.get("underlyingType"),
            is_rwa=1 if st.get("underlyingType") == "EQUITY" else 0,
            listed_since=_ts(st.get("onboardDate")),
            active=1 if st.get("status") == "TRADING" else 0, id_source="declared"))
    # COIN-M inverse perps live on a SEPARATE host (dapi), not fapi
    for st in _get(f"{BIN_D}/dapi/v1/exchangeInfo")["symbols"]:
        if st.get("contractType") != "PERPETUAL":
            continue
        flt = {f["filterType"]: f for f in st.get("filters", [])}
        out.append(_row(
            venue="BIN", symbol=st["symbol"], kind="inverse_perp",
            base_raw=st["baseAsset"], quote=st["quoteAsset"],
            contract_mult=_f(st.get("contractSize")) or 1.0, inverse=1,
            tick=_f(flt.get("PRICE_FILTER", {}).get("tickSize")),
            lot=_f(flt.get("LOT_SIZE", {}).get("stepSize")),
            underlying_type=st.get("underlyingType"),
            is_rwa=1 if st.get("underlyingType") == "EQUITY" else 0,
            listed_since=_ts(st.get("onboardDate")),
            active=1 if st.get("contractStatus") == "TRADING" else 0,
            id_source="declared"))
    for st in _get(f"{BIN_S}/api/v3/exchangeInfo")["symbols"]:
        flt = {f["filterType"]: f for f in st.get("filters", [])}
        out.append(_row(
            venue="BIN", symbol=st["symbol"], kind="spot",
            base_raw=st["baseAsset"], quote=st["quoteAsset"],
            tick=_f(flt.get("PRICE_FILTER", {}).get("tickSize")),
            lot=_f(flt.get("LOT_SIZE", {}).get("stepSize")),
            min_notional=_f(flt.get("NOTIONAL", {}).get("minNotional")),
            active=1 if st.get("status") == "TRADING" else 0, id_source="declared"))
    return out


# ------------------------------------------------------------------ OKX
def okx() -> list[dict]:
    out = []
    for st in _get(f"{OKX_B}/api/v5/public/instruments", {"instType": "SWAP"})["data"]:
        p = st["instId"].split("-")
        if len(p) != 3:
            continue
        inv = st.get("ctType") == "inverse"
        out.append(_row(
            venue="OKX", symbol=st["instId"],
            kind="inverse_perp" if inv else "linear_perp",
            base_raw=p[0], quote=p[1],
            contract_mult=_f(st.get("ctVal")) or 1.0, inverse=int(inv),
            tick=_f(st.get("tickSz")), lot=_f(st.get("lotSz")),
            listed_since=_ts(st.get("listTime")),
            active=1 if st.get("state") == "live" else 0,
            id_source="parsed_delimited"))
    for st in _get(f"{OKX_B}/api/v5/public/instruments", {"instType": "SPOT"})["data"]:
        out.append(_row(
            venue="OKX", symbol=st["instId"], kind="spot",
            base_raw=st.get("baseCcy") or st["instId"].split("-")[0],
            quote=st.get("quoteCcy") or st["instId"].split("-")[-1],
            tick=_f(st.get("tickSz")), lot=_f(st.get("lotSz")),
            listed_since=_ts(st.get("listTime")),
            active=1 if st.get("state") == "live" else 0, id_source="declared"))
    return out


# ------------------------------------------------------------------ Bitget
def bitget() -> list[dict]:
    out = []
    for pt in ("USDT-FUTURES", "USDC-FUTURES", "COIN-FUTURES"):
        try:
            rows = _get(f"{BGT_B}/api/v2/mix/market/contracts", {"productType": pt})["data"]
        except Exception as e:                                   # noqa: BLE001
            log.warning("bitget %s: %s", pt, e)
            continue
        for st in rows:
            if st.get("symbolType") and st["symbolType"] != "perpetual":
                continue                       # dated futures share this endpoint
            inv = pt == "COIN-FUTURES"
            place = _f(st.get("pricePlace"))
            step = _f(st.get("priceEndStep")) or 1.0
            out.append(_row(
                venue="BGT", symbol=st["symbol"],
                kind="inverse_perp" if inv else "linear_perp",
                base_raw=st["baseCoin"], quote=st["quoteCoin"],
                # NOT sizeMultiplier - that is a lot-size step. Bitget quotes OI and
                # volume in BASE units, so the contract multiplier is 1.
                contract_mult=1.0, inverse=int(inv),
                tick=(10 ** -place) * step if place is not None else None,
                lot=_f(st.get("minTradeNum")),
                min_notional=_f(st.get("minTradeUSDT")),
                maker_fee=_f(st.get("makerFeeRate")), taker_fee=_f(st.get("takerFeeRate")),
                is_rwa=1 if str(st.get("isRwa")).upper() == "YES" else 0,
                underlying_type=("RWA" if str(st.get("isRwa")).upper() == "YES" else "COIN"),
                funding_interval_h=_f(st.get("fundInterval")),
                listed_since=_ts(st.get("launchTime") or st.get("openTime")),
                active=1 if st.get("symbolStatus") == "normal" else 0,
                id_source="declared"))
    for st in _get(f"{BGT_B}/api/v2/spot/public/symbols")["data"]:
        out.append(_row(
            venue="BGT", symbol=st["symbol"], kind="spot",
            base_raw=st["baseCoin"], quote=st["quoteCoin"],
            min_notional=_f(st.get("minTradeUSDT")),
            maker_fee=_f(st.get("makerFeeRate")), taker_fee=_f(st.get("takerFeeRate")),
            active=1 if st.get("status") == "online" else 0, id_source="declared"))
    return out


# ------------------------------------------------------------------ Gate
def gate() -> list[dict]:
    out = []
    for settle, kind in (("usdt", "linear_perp"), ("usdc", "linear_perp"),
                         ("btc", "inverse_perp")):
        try:
            rows = _get(f"{GAT_B}/futures/{settle}/contracts")
        except Exception as e:                                   # noqa: BLE001
            log.warning("gate %s: %s", settle, e)
            continue
        for st in rows:
            p = st["name"].split("_")
            if len(p) != 2:
                continue
            fi = _f(st.get("funding_interval"))
            out.append(_row(
                venue="GAT", symbol=st["name"], kind=kind,
                base_raw=p[0], quote=p[1],
                contract_mult=_f(st.get("quanto_multiplier")) or 1.0,
                inverse=int(kind == "inverse_perp"),
                tick=_f(st.get("order_price_round")), lot=_f(st.get("order_size_min")),
                maker_fee=_f(st.get("maker_fee_rate")), taker_fee=_f(st.get("taker_fee_rate")),
                funding_interval_h=(fi / 3600) if fi else None,
                listed_since=_ts(st.get("create_time"), "s"),
                active=0 if st.get("in_delisting") else 1,
                id_source="parsed_delimited"))
    for st in _get(f"{GAT_B}/spot/currency_pairs"):
        out.append(_row(
            venue="GAT", symbol=st["id"], kind="spot",
            base_raw=st.get("base"), quote=st.get("quote"),
            min_notional=_f(st.get("min_quote_amount")),
            maker_fee=_f(st.get("fee")),
            active=1 if st.get("trade_status") == "tradable" else 0,
            id_source="declared"))
    return out


# ------------------------------------------------------------------ KuCoin
def kucoin() -> list[dict]:
    out = []
    for st in _get(f"{KCN_F}/api/v1/contracts/active")["data"]:
        # FFWCSX = perpetual swap, FFICSX = dated future
        if st.get("type") != "FFWCSX" or st.get("expireDate"):
            continue
        b = st.get("baseCurrency")
        b = "BTC" if b == "XBT" else b
        inv = bool(st.get("isInverse"))
        out.append(_row(
            venue="KCN", symbol=st["symbol"],
            kind="inverse_perp" if inv else "linear_perp",
            base_raw=b, quote=st.get("quoteCurrency"),
            # KuCoin reports a NEGATIVE multiplier on inverse contracts (XBTUSDM
            # = -1.0), which flipped OI negative until this abs()
            contract_mult=abs(_f(st.get("multiplier")) or 1.0), inverse=int(inv),
            tick=_f(st.get("tickSize")), lot=_f(st.get("lotSize")),
            maker_fee=_f(st.get("makerFeeRate")), taker_fee=_f(st.get("takerFeeRate")),
            funding_interval_h=(_f(st.get("fundingRateGranularity")) or 0) / 3_600_000 or None,
            listed_since=_ts(st.get("firstOpenDate")),
            active=1 if st.get("status") == "Open" else 0, id_source="declared"))
    for st in _get(f"{KCN_S}/api/v2/symbols")["data"]:
        b = st.get("baseCurrency")
        out.append(_row(
            venue="KCN", symbol=st["symbol"], kind="spot",
            base_raw="BTC" if b == "XBT" else b, quote=st.get("quoteCurrency"),
            tick=_f(st.get("priceIncrement")), lot=_f(st.get("baseIncrement")),
            min_notional=_f(st.get("quoteMinSize")),
            active=1 if st.get("enableTrading") else 0, id_source="declared"))
    return out


FETCHERS = {"BIN": binance, "OKX": okx, "BGT": bitget, "GAT": gate, "KCN": kucoin}


def fetch_all(venues=None) -> pd.DataFrame:
    rows = []
    for v in (venues or list(FETCHERS)):
        try:
            r = FETCHERS[v]()
            rows += r
            n_perp = sum(1 for x in r if x["kind"] != "spot")
            print(f"  {v}: {len(r):5d} instruments ({n_perp} perp, {len(r)-n_perp} spot)")
        except Exception as e:                                   # noqa: BLE001
            log.error("specs %s failed: %s", v, e)
    df = pd.DataFrame(rows)
    # Keep the venue's own casing before folding to upper. Bitget encodes its
    # tokenised-stock product in the CASE of baseCoin - rSPY, rNVDA, rV - and
    # uppercasing first destroys the only exact signal for it, leaving symbology to
    # guess from string length (which missed rA/rB/rC/rD/rF/rO/rT/rU/rV, ten symbols
    # reporting $10.5B/day between them). derive_asset_keys reads this and drops it.
    df["base_native"] = df["base_raw"].astype(str)
    df["base_raw"] = df["base_raw"].astype(str).str.upper()
    df["quote"] = df["quote"].astype(str).str.upper()
    df["symbol"] = df["symbol"].astype(str)
    return df
