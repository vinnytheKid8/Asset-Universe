"""Per-venue REST adapters producing ONE normalised daily row per (venue, symbol, date).

Design notes
------------
* Daily grain only - the venue candle APIs top out there for anything with useful history.
* Volume is taken in the venue's own quote units and converted to USD via `fx.py`
  (USDT/USD, USDC/USD), never assumed 1:1.
* Contract multipliers are needed for **KuCoin volume** and for OI on the
  contract-denominated venues; every other venue reports quote volume directly.
* `trades` (count) and `taker_buy_quote` exist only where the venue publishes them
  (Binance for both, Gate for taker split). They are NULL elsewhere, not faked.
* Open interest history: Binance 30d, OKX/Gate paginate, **Bitget and KuCoin publish
  current only** -> those two accumulate forward from a daily snapshot.

Everything here returns plain dicts; no ClickHouse, no disk.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

CFG_PATH = Path(__file__).parent / "general_config.json"
_CFG = json.load(open(CFG_PATH)) if CFG_PATH.exists() else {}
PROXIES = ({"http": _CFG["proxy"], "https": _CFG["proxy"]} if _CFG.get("proxy") else None)

DAY_MS = 86_400_000
RETRY_STATUS = {429, 418, 500, 502, 503, 504}


def _get(url: str, params: dict | None = None, attempts: int = 5):
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, proxies=PROXIES, timeout=45)
        except requests.RequestException as e:
            log.warning("%s: %s (retry %d)", url, e, i)
            time.sleep(min(2 ** i, 20))
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in RETRY_STATUS:
            wait = float(r.headers.get("Retry-After", min(2 ** i, 20)))
            log.warning("%s -> %s, sleeping %.1fs", url, r.status_code, wait)
            time.sleep(wait)
            continue
        raise RuntimeError(f"{url} -> HTTP {r.status_code}: {r.text[:200]}")
    raise RuntimeError(f"{url}: exhausted retries")


def _d(ms) -> date:
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).date()


def _ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _f(x):
    try:
        return None if x in (None, "", "null") else float(x)
    except (TypeError, ValueError):
        return None


@dataclass
class Instrument:
    """What we need to know about one instrument to normalise its daily bars."""
    venue: str                       # BIN | OKX | BGT | GAT | KCN
    market_type: str                 # linear_perp | inverse_perp | spot
    symbol: str                      # venue-native symbol
    base_ccy: str
    quote_ccy: str
    contract_mult: float = 1.0       # base per contract (linear) / USD per contract (inverse)
    inverse: bool = False
    extra: dict = field(default_factory=dict)


# =========================================================================== BIN
class Binance:
    FAPI, SAPI = "https://fapi.binance.com", "https://api.binance.com"

    @staticmethod
    def klines(ins: Instrument, start: date, end: date) -> dict[date, dict]:
        base = Binance.SAPI + "/api/v3/klines" if ins.market_type == "spot" \
            else Binance.FAPI + "/fapi/v1/klines"
        rows, out, cur = [], {}, _ms(start)
        while cur <= _ms(end):
            page = _get(base, {"symbol": ins.symbol, "interval": "1d",
                               "startTime": cur, "limit": 1000})
            if not page:
                break
            rows += page
            cur = page[-1][0] + DAY_MS
            if len(page) < 1000:
                break
        for k in rows:
            d = _d(k[0])
            if d < start or d > end:
                continue
            out[d] = {"px_open": _f(k[1]), "px_high": _f(k[2]), "px_low": _f(k[3]),
                      "px_close": _f(k[4]), "vol_base": _f(k[5]), "vol_quote": _f(k[7]),
                      "trades": int(k[8]), "taker_buy_base": _f(k[9]),
                      "taker_buy_quote": _f(k[10])}
        return out

    @staticmethod
    def funding(ins: Instrument, start: date, end: date) -> list[dict]:
        if ins.market_type == "spot":
            return []
        out, cur = [], _ms(start)
        while cur <= _ms(end):
            page = _get(Binance.FAPI + "/fapi/v1/fundingRate",
                        {"symbol": ins.symbol, "startTime": cur, "limit": 1000})
            if not page:
                break
            out += [{"funding_ts": r["fundingTime"], "rate": _f(r["fundingRate"])}
                    for r in page]
            cur = page[-1]["fundingTime"] + 1
            if len(page) < 1000:
                break
        return out

    @staticmethod
    def oi(ins: Instrument, start: date, end: date) -> dict[date, dict]:
        """30-day lookback ONLY (documented + verified)."""
        if ins.market_type == "spot":
            return {}
        page = _get(Binance.FAPI + "/futures/data/openInterestHist",
                    {"symbol": ins.symbol, "period": "1d", "limit": 500})
        return {_d(r["timestamp"]): {"oi_native": _f(r["sumOpenInterest"]),
                                     "oi_usd": _f(r["sumOpenInterestValue"]),
                                     "oi_source": "rest_hist"}
                for r in (page or []) if start <= _d(r["timestamp"]) <= end}


# =========================================================================== OKX
class OKX:
    B = "https://www.okx.com"

    @staticmethod
    def klines(ins: Instrument, start: date, end: date) -> dict[date, dict]:
        out, after = {}, _ms(end + timedelta(days=2))
        for _ in range(40):                     # 100/page
            # bar=1Dutc, NOT 1D: OKX's default daily candle closes at 16:00 UTC
            # (a UTC+8 day). Verified 2026-08-06 - using 1D silently shifts every
            # volume figure by 8h and breaks cross-venue comparison.
            page = (_get(OKX.B + "/api/v5/market/history-candles",
                         {"instId": ins.symbol, "bar": "1Dutc", "limit": "100",
                          "after": str(after)}) or {}).get("data") or []
            if not page:
                break
            for k in page:
                d = _d(k[0])
                if start <= d <= end:
                    # [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
                    # vol/volCcy change meaning with instrument type, and the base
                    # column is the one that moves (verified against the API 2026-08-12):
                    #   SPOT: vol=BASE,      volCcy=quote, volCcyQuote=quote
                    #   SWAP: vol=contracts, volCcy=BASE,  volCcyQuote=quote
                    # Reading k[6] for both put quote volume in vol_base on every spot
                    # row. volCcyQuote (k[7]) is quote in both, so vol_usd was never
                    # affected - only vol_base, silently, on 156 spot instruments.
                    out[d] = {"px_open": _f(k[1]), "px_high": _f(k[2]), "px_low": _f(k[3]),
                              "px_close": _f(k[4]),
                              "vol_base": _f(k[5] if ins.market_type == "spot" else k[6]),
                              "vol_quote": _f(k[7]), "trades": None,
                              "taker_buy_base": None, "taker_buy_quote": None}
            after = int(page[-1][0])
            if _d(after) < start:
                break
        return out

    @staticmethod
    def funding(ins: Instrument, start: date, end: date) -> list[dict]:
        if ins.market_type == "spot":
            return []
        out, after = [], _ms(end + timedelta(days=2))
        for _ in range(40):
            page = (_get(OKX.B + "/api/v5/public/funding-rate-history",
                         {"instId": ins.symbol, "limit": "100",
                          "after": str(after)}) or {}).get("data") or []
            if not page:
                break
            out += [{"funding_ts": int(r["fundingTime"]), "rate": _f(r["realizedRate"])}
                    for r in page]
            after = int(page[-1]["fundingTime"])
            if _d(after) < start:
                break
        return out

    @staticmethod
    def oi(ins: Instrument, start: date, end: date) -> dict[date, dict]:
        if ins.market_type == "spot":
            return {}
        out, after = {}, _ms(end + timedelta(days=2))
        for _ in range(40):
            page = (_get(OKX.B + "/api/v5/rubik/stat/contracts/open-interest-history",
                         {"instId": ins.symbol, "period": "1D", "limit": "100",
                          "after": str(after)}) or {}).get("data") or []
            if not page:
                break
            for r in page:
                d = _d(r[0])
                if start <= d <= end:
                    # [ts, oi(contracts), oi_ccy(base), oi_usd]
                    out[d] = {"oi_native": _f(r[1]), "oi_usd": _f(r[3]),
                              "oi_source": "rest_hist"}
            after = int(page[-1][0])
            if _d(after) < start:
                break
        return out


# =========================================================================== BGT
class Bitget:
    B = "https://api.bitget.com"

    @staticmethod
    def _pt(ins: Instrument) -> str:
        if ins.market_type == "inverse_perp":
            return "COIN-FUTURES"
        return "USDC-FUTURES" if ins.quote_ccy == "USDC" else "USDT-FUTURES"

    @staticmethod
    def klines(ins: Instrument, start: date, end: date) -> dict[date, dict]:
        out = {}
        if ins.market_type == "spot":
            endt = _ms(end + timedelta(days=1))
            for _ in range(20):                 # 200/page
                # "1Dutc", NOT "1dutc" - Bitget rejects the lowercase d with
                # 400171, and "1day" would silently give UTC+8 days (see 2.1)
                page = (_get(Bitget.B + "/api/v2/spot/market/candles",
                             {"symbol": ins.symbol, "granularity": "1Dutc",
                              "limit": "200", "endTime": str(endt)}) or {}).get("data") or []
                if not page:
                    break
                for k in page:
                    d = _d(k[0])
                    if start <= d <= end:
                        out[d] = {"px_open": _f(k[1]), "px_high": _f(k[2]),
                                  "px_low": _f(k[3]), "px_close": _f(k[4]),
                                  "vol_base": _f(k[5]), "vol_quote": _f(k[6]),
                                  "trades": None, "taker_buy_base": None,
                                  "taker_buy_quote": None}
                endt = int(page[0][0])
                if _d(endt) < start:
                    break
            return out
        endt = _ms(end + timedelta(days=1))
        for _ in range(20):                     # 90/page, endTime pagination verified
            # granularity=1Dutc, NOT 1D - same UTC+8 boundary trap as OKX
            page = (_get(Bitget.B + "/api/v2/mix/market/history-candles",
                         {"symbol": ins.symbol, "productType": Bitget._pt(ins),
                          "granularity": "1Dutc", "limit": "200",
                          "endTime": str(endt)}) or {}).get("data") or []
            if not page:
                break
            for k in page:
                d = _d(k[0])
                if start <= d <= end:
                    out[d] = {"px_open": _f(k[1]), "px_high": _f(k[2]), "px_low": _f(k[3]),
                              "px_close": _f(k[4]), "vol_base": _f(k[5]),
                              "vol_quote": _f(k[6]), "trades": None,
                              "taker_buy_base": None, "taker_buy_quote": None}
            endt = int(page[0][0])
            if _d(endt) < start:
                break
        return out

    @staticmethod
    def funding(ins: Instrument, start: date, end: date) -> list[dict]:
        if ins.market_type == "spot":
            return []
        out = []
        for pg in range(1, 21):
            page = (_get(Bitget.B + "/api/v2/mix/market/history-fund-rate",
                         {"symbol": ins.symbol, "productType": Bitget._pt(ins),
                          "pageSize": "100", "pageNo": str(pg)}) or {}).get("data") or []
            if not page:
                break
            out += [{"funding_ts": int(r["fundingTime"]), "rate": _f(r["fundingRate"])}
                    for r in page]
            if _d(min(int(r["fundingTime"]) for r in page)) < start:
                break
        return out

    @staticmethod
    def oi(ins: Instrument, start: date, end: date) -> dict[date, dict]:
        """CURRENT ONLY - Bitget publishes no historical OI. Snapshot for today."""
        if ins.market_type == "spot":
            return {}
        d = (_get(Bitget.B + "/api/v2/mix/market/open-interest",
                  {"symbol": ins.symbol, "productType": Bitget._pt(ins)}) or {}).get("data")
        lst = (d or {}).get("openInterestList") or []
        if not lst:
            return {}
        today = datetime.now(timezone.utc).date()
        return {today: {"oi_native": _f(lst[0].get("size")), "oi_usd": None,
                        "oi_source": "rest_snapshot"}}


# =========================================================================== GAT
class Gate:
    B = "https://api.gateio.ws/api/v4"

    @staticmethod
    def _settle(ins: Instrument) -> str:
        return "btc" if ins.market_type == "inverse_perp" else ins.quote_ccy.lower()

    @staticmethod
    def klines(ins: Instrument, start: date, end: date) -> dict[date, dict]:
        out = {}
        if ins.market_type == "spot":
            page = _get(Gate.B + "/spot/candlesticks",
                        {"currency_pair": ins.symbol, "interval": "1d",
                         "from": _ms(start) // 1000, "to": _ms(end) // 1000}) or []
            for k in page:
                # [ts, quote_vol, close, high, low, open, base_vol, closed]
                d = _d(int(k[0]) * 1000)
                if start <= d <= end:
                    out[d] = {"px_open": _f(k[5]), "px_high": _f(k[3]), "px_low": _f(k[4]),
                              "px_close": _f(k[2]), "vol_base": _f(k[6]),
                              "vol_quote": _f(k[1]), "trades": None,
                              "taker_buy_base": None, "taker_buy_quote": None}
            return out
        page = _get(f"{Gate.B}/futures/{Gate._settle(ins)}/candlesticks",
                    {"contract": ins.symbol, "interval": "1d",
                     "from": _ms(start) // 1000, "to": _ms(end) // 1000}) or []
        for k in page:
            d = _d(int(k["t"]) * 1000)
            if start <= d <= end:
                # v = contracts, sum = quote volume (verified against price)
                vb = _f(k["v"])
                out[d] = {"px_open": _f(k["o"]), "px_high": _f(k["h"]), "px_low": _f(k["l"]),
                          "px_close": _f(k["c"]),
                          "vol_base": (vb * ins.contract_mult) if vb is not None else None,
                          "vol_quote": _f(k["sum"]), "trades": None,
                          "taker_buy_base": None, "taker_buy_quote": None}
        return out

    @staticmethod
    def funding(ins: Instrument, start: date, end: date) -> list[dict]:
        if ins.market_type == "spot":
            return []
        page = _get(f"{Gate.B}/futures/{Gate._settle(ins)}/funding_rate",
                    {"contract": ins.symbol, "limit": 1000}) or []
        return [{"funding_ts": int(r["t"]) * 1000, "rate": _f(r["r"])} for r in page]

    @staticmethod
    def stats(ins: Instrument, start: date, end: date) -> dict[date, dict]:
        """contract_stats: OI history (with USD precomputed) + taker split + liquidations
        + long/short account ratios. Richest daily endpoint of the five venues."""
        if ins.market_type == "spot":
            return {}
        out = {}
        to = _ms(end + timedelta(days=1)) // 1000
        for _ in range(20):                     # 100/page
            page = _get(f"{Gate.B}/futures/{Gate._settle(ins)}/contract_stats",
                        {"contract": ins.symbol, "interval": "1d", "limit": 100,
                         "to": to}) or []
            if not page:
                break
            for r in page:
                d = _d(int(r["time"]) * 1000)
                if start <= d <= end:
                    lt, st_ = _f(r.get("long_taker_size")), _f(r.get("short_taker_size"))
                    imb = ((lt - st_) / (lt + st_)) if (lt is not None and st_ is not None
                                                        and (lt + st_)) else None
                    out[d] = {"oi_native": _f(r.get("open_interest")),
                              "oi_usd": _f(r.get("open_interest_usd")),
                              "oi_source": "rest_hist",
                              "mark_close": _f(r.get("mark_price")),
                              "lsr_account": _f(r.get("lsr_account")),
                              "lsr_taker": _f(r.get("lsr_taker")),
                              "taker_imbalance": imb,
                              "liq_long_usd": _f(r.get("long_liq_usd")),
                              "liq_short_usd": _f(r.get("short_liq_usd"))}
            to = int(page[0]["time"])
            if _d(to * 1000) < start:
                break
        return out

    oi = stats


# =========================================================================== KCN
class KuCoin:
    FB, SB = "https://api-futures.kucoin.com", "https://api.kucoin.com"

    @staticmethod
    def klines(ins: Instrument, start: date, end: date) -> dict[date, dict]:
        out = {}
        if ins.market_type == "spot":
            page = (_get(KuCoin.SB + "/api/v1/market/candles",
                         {"symbol": ins.symbol, "type": "1day",
                          "startAt": _ms(start) // 1000,
                          "endAt": _ms(end + timedelta(days=1)) // 1000})
                    or {}).get("data") or []
            for k in page:
                # [ts, open, close, high, low, base_vol, quote_vol]
                d = _d(int(k[0]) * 1000)
                if start <= d <= end:
                    out[d] = {"px_open": _f(k[1]), "px_high": _f(k[3]), "px_low": _f(k[4]),
                              "px_close": _f(k[2]), "vol_base": _f(k[5]),
                              "vol_quote": _f(k[6]), "trades": None,
                              "taker_buy_base": None, "taker_buy_quote": None}
            return out
        page = (_get(KuCoin.FB + "/api/v1/kline/query",
                     {"symbol": ins.symbol, "granularity": 1440,
                      "from": _ms(start), "to": _ms(end + timedelta(days=1))})
                or {}).get("data") or []
        for k in page:
            d = _d(k[0])
            if start <= d <= end:
                # [ts, o, h, l, c, volume] - volume is in CONTRACTS, no quote volume
                vc = _f(k[5])
                vb = vc * ins.contract_mult if vc is not None else None
                px = _f(k[4])
                out[d] = {"px_open": _f(k[1]), "px_high": _f(k[2]), "px_low": _f(k[3]),
                          "px_close": px, "vol_base": vb,
                          # no quote volume published -> reconstruct from typical price
                          "vol_quote": (vb * px) if (vb is not None and px) else None,
                          "vol_quote_is_estimate": True,
                          "trades": None, "taker_buy_base": None, "taker_buy_quote": None}
        return out

    @staticmethod
    def funding(ins: Instrument, start: date, end: date) -> list[dict]:
        if ins.market_type == "spot":
            return []
        page = (_get(KuCoin.FB + "/api/v1/contract/funding-rates",
                     {"symbol": ins.symbol, "from": _ms(start),
                      "to": _ms(end + timedelta(days=1))}) or {}).get("data") or []
        return [{"funding_ts": int(r["timepoint"]), "rate": _f(r["fundingRate"])}
                for r in page]

    @staticmethod
    def oi(ins: Instrument, start: date, end: date) -> dict[date, dict]:
        """Historical daily OI: 70-day retention, data starts 2025-12-29.

        The endpoint lives on the SPOT host (api.kucoin.com), not api-futures - that is
        why probing it under api-futures 404s. Verified 2026-08-06: 70 rows, `ts` on true
        UTC midnight boundaries, `openInterest` in contracts.
        Retention by interval: 1day = 70d; 5min/15min/30min/1hour/4hour = 7d.
        """
        if ins.market_type == "spot":
            return {}
        out = {}
        rows = (_get(f"{KuCoin.SB}/api/ua/v1/market/open-interest",
                     {"symbol": ins.symbol, "interval": "1day",
                      "startAt": _ms(start), "endAt": _ms(end + timedelta(days=1)),
                      "pageSize": 100}) or {}).get("data") or []
        for r in rows:
            d = _d(r["ts"])
            oi = _f(r.get("openInterest"))
            if oi is None or not (start <= d <= end):
                continue
            out[d] = {"oi_native": oi * ins.contract_mult, "oi_usd": None,
                      "oi_source": "rest_hist"}
        if out:
            return out
        # fall back to a live snapshot outside the 70-day window
        d0 = (_get(f"{KuCoin.FB}/api/v1/contracts/{ins.symbol}") or {}).get("data") or {}
        oi = _f(d0.get("openInterest"))
        if oi is None:
            return {}
        today = datetime.now(timezone.utc).date()
        return {today: {"oi_native": oi * ins.contract_mult, "oi_usd": None,
                        "oi_source": "rest_snapshot"}}


ADAPTERS = {"BIN": Binance, "OKX": OKX, "BGT": Bitget, "GAT": Gate, "KCN": KuCoin}

# what each venue can and cannot give at daily grain (verified 2026-08-06)
CAPABILITIES = {
    "BIN": {"trades": True,  "taker_split": True,  "oi_hist_days": 30,
            "quote_vol": "native"},
    "OKX": {"trades": False, "taker_split": False, "oi_hist_days": None,
            "quote_vol": "native"},
    "BGT": {"trades": False, "taker_split": False, "oi_hist_days": 0,
            "quote_vol": "native"},
    "GAT": {"trades": False, "taker_split": True,  "oi_hist_days": None,
            "quote_vol": "native"},
    "KCN": {"trades": False, "taker_split": False, "oi_hist_days": 0,
            "quote_vol": "estimated"},
}
