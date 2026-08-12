"""Probe: what daily-grain data can we get straight from the venue REST APIs?

One symbol (BTC linear perp + spot) across BIN/OKX/BGT/GAT/KCN. For each endpoint
we report: reachable, row count, oldest timestamp reachable, and the fields it gives.
The point is to find the lookback limits and the gaps, not to fetch data.

    python probe_daily.py [--no-proxy]
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

CFG = json.load(open(Path(__file__).parent / "general_config.json"))
PROXY = None if "--no-proxy" in sys.argv else {"http": CFG["proxy"], "https": CFG["proxy"]}
NOW_MS = int(time.time() * 1000)
DAY_MS = 86_400_000

results = []


def get(url, params=None, tag="", headers=None):
    t0 = time.time()
    try:
        r = requests.get(url, params=params, headers=headers, proxies=PROXY, timeout=45)
        dt = time.time() - t0
        if r.status_code != 200:
            return None, f"HTTP {r.status_code} {r.text[:120]}", dt
        return r.json(), None, dt
    except Exception as e:                                    # noqa: BLE001
        return None, f"{type(e).__name__}: {str(e)[:120]}", time.time() - t0


def ts_str(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%d")
    except Exception:                                         # noqa: BLE001
        return str(ms)[:19]


def rec(venue, what, endpoint, data, err, dt, n=None, oldest=None, note=""):
    results.append({"venue": venue, "what": what, "endpoint": endpoint,
                    "ok": err is None, "n": n, "oldest": oldest,
                    "secs": round(dt, 2), "note": note or (err or "")})
    status = "ok " if err is None else "FAIL"
    print(f"  [{status}] {what:22s} n={str(n):>6s} oldest={str(oldest):>10s} "
          f"{dt:4.1f}s  {note or (err or '')}"[:160])


# ---------------------------------------------------------------- Binance
def binance():
    print("\n=== BINANCE ===")
    F, S = "https://fapi.binance.com", "https://api.binance.com"

    d, e, dt = get(f"{F}/fapi/v1/klines", {"symbol": "BTCUSDT", "interval": "1d", "limit": 1500})
    rec("BIN", "perp klines 1d", "/fapi/v1/klines", d, e, dt,
        len(d) if d else None, ts_str(d[0][0]) if d else None,
        "fields: o,h,l,c,vol_base,quote_vol,n_trades,taker_buy_base,taker_buy_quote" if d else "")

    d, e, dt = get(f"{S}/api/v3/klines", {"symbol": "BTCUSDT", "interval": "1d", "limit": 1000})
    rec("BIN", "spot klines 1d", "/api/v3/klines", d, e, dt,
        len(d) if d else None, ts_str(d[0][0]) if d else None)

    d, e, dt = get(f"{F}/fapi/v1/fundingRate", {"symbol": "BTCUSDT", "limit": 1000})
    rec("BIN", "funding history", "/fapi/v1/fundingRate", d, e, dt,
        len(d) if d else None, ts_str(d[0]["fundingTime"]) if d else None,
        "settled rates + markPrice" if d else "")

    for lim, per in ((500, "1d"), (500, "1h")):
        d, e, dt = get(f"{F}/futures/data/openInterestHist",
                       {"symbol": "BTCUSDT", "period": per, "limit": lim})
        rec("BIN", f"OI history {per}", "/futures/data/openInterestHist", d, e, dt,
            len(d) if d else None, ts_str(d[0]["timestamp"]) if d else None,
            "sumOpenInterest + sumOpenInterestValue" if d else "")

    d, e, dt = get(f"{F}/fapi/v1/fundingInfo")
    rec("BIN", "funding info (cap/iv)", "/fapi/v1/fundingInfo", d, e, dt,
        len(d) if d else None, None,
        "fundingIntervalHours + clamp per symbol" if d else "")

    d, e, dt = get(f"{F}/fapi/v1/premiumIndexKlines",
                   {"symbol": "BTCUSDT", "interval": "1d", "limit": 500})
    rec("BIN", "premium index 1d", "/fapi/v1/premiumIndexKlines", d, e, dt,
        len(d) if d else None, ts_str(d[0][0]) if d else None)

    d, e, dt = get(f"{F}/fapi/v1/exchangeInfo")
    n = len(d.get("symbols", [])) if d else None
    rec("BIN", "contract specs", "/fapi/v1/exchangeInfo", d, e, dt, n, None,
        "tick/lot/minNotional/underlyingType" if d else "")


# ---------------------------------------------------------------- OKX
def okx():
    print("\n=== OKX ===")
    B = "https://www.okx.com"

    d, e, dt = get(f"{B}/api/v5/market/candles",
                   {"instId": "BTC-USDT-SWAP", "bar": "1D", "limit": 300})
    rows = d.get("data") if d else None
    rec("OKX", "perp candles 1D", "/api/v5/market/candles", d, e, dt,
        len(rows) if rows else None, ts_str(rows[-1][0]) if rows else None,
        "o,h,l,c,vol_contracts,volCcy,volCcyQuote" if rows else (d or {}).get("msg", ""))

    d, e, dt = get(f"{B}/api/v5/market/history-candles",
                   {"instId": "BTC-USDT-SWAP", "bar": "1D", "limit": 100})
    rows = d.get("data") if d else None
    rec("OKX", "perp history-candles", "/api/v5/market/history-candles", d, e, dt,
        len(rows) if rows else None, ts_str(rows[-1][0]) if rows else None,
        "paginate with 'after' for deep history" if rows else (d or {}).get("msg", ""))

    d, e, dt = get(f"{B}/api/v5/public/funding-rate-history",
                   {"instId": "BTC-USDT-SWAP", "limit": 100})
    rows = d.get("data") if d else None
    rec("OKX", "funding history", "/api/v5/public/funding-rate-history", d, e, dt,
        len(rows) if rows else None, ts_str(rows[-1]["fundingTime"]) if rows else None,
        "realisedRate + formulaType" if rows else (d or {}).get("msg", ""))

    # OI history: several candidate endpoints, only some exist
    for path, params, label in (
        ("/api/v5/rubik/stat/contracts/open-interest-history",
         {"instId": "BTC-USDT-SWAP", "period": "1D", "limit": 100}, "OI hist (per-instId)"),
        ("/api/v5/rubik/stat/contracts/open-interest-volume",
         {"ccy": "BTC", "period": "1D"}, "OI+vol (per-ccy)"),
        ("/api/v5/public/open-interest",
         {"instType": "SWAP", "instId": "BTC-USDT-SWAP"}, "OI current"),
    ):
        d, e, dt = get(f"{B}{path}", params)
        rows = d.get("data") if d else None
        old = None
        if rows:
            first = rows[-1]
            old = ts_str(first[0] if isinstance(first, list) else first.get("ts"))
        rec("OKX", label, path, d, e, dt, len(rows) if rows else None, old,
            "" if rows else (d or {}).get("msg", ""))

    d, e, dt = get(f"{B}/api/v5/public/instruments", {"instType": "SWAP"})
    rows = d.get("data") if d else None
    rec("OKX", "contract specs", "/api/v5/public/instruments", d, e, dt,
        len(rows) if rows else None, None, "ctVal/ctMult/tickSz/lotSz" if rows else "")


# ---------------------------------------------------------------- Bitget
def bitget():
    print("\n=== BITGET ===")
    B = "https://api.bitget.com"
    PT = "USDT-FUTURES"

    for path, label in (("/api/v2/mix/market/candles", "perp candles 1D"),
                        ("/api/v2/mix/market/history-candles", "perp history-candles")):
        d, e, dt = get(f"{B}{path}",
                       {"symbol": "BTCUSDT", "productType": PT, "granularity": "1D",
                        "limit": "200"})
        rows = d.get("data") if d else None
        rec("BGT", label, path, d, e, dt, len(rows) if rows else None,
            ts_str(rows[0][0]) if rows else None,
            "o,h,l,c,vol_base,vol_quote (NO trade count)" if rows else (d or {}).get("msg", ""))

    d, e, dt = get(f"{B}/api/v2/mix/market/history-fund-rate",
                   {"symbol": "BTCUSDT", "productType": PT, "pageSize": "100"})
    rows = d.get("data") if d else None
    rec("BGT", "funding history", "/api/v2/mix/market/history-fund-rate", d, e, dt,
        len(rows) if rows else None,
        ts_str(rows[-1]["fundingTime"]) if rows else None,
        "" if rows else (d or {}).get("msg", ""))

    d, e, dt = get(f"{B}/api/v2/mix/market/open-interest",
                   {"symbol": "BTCUSDT", "productType": PT})
    rec("BGT", "OI current", "/api/v2/mix/market/open-interest", d, e, dt,
        1 if d and d.get("data") else None, "now",
        "CURRENT ONLY - no public OI history" if d else (d or {}).get("msg", ""))

    d, e, dt = get(f"{B}/api/v2/mix/market/contracts", {"productType": PT})
    rows = d.get("data") if d else None
    rec("BGT", "contract specs", "/api/v2/mix/market/contracts", d, e, dt,
        len(rows) if rows else None, None, "" if rows else (d or {}).get("msg", ""))

    d, e, dt = get(f"{B}/api/v2/spot/market/candles",
                   {"symbol": "BTCUSDT", "granularity": "1day", "limit": "200"})
    rows = d.get("data") if d else None
    rec("BGT", "spot candles 1d", "/api/v2/spot/market/candles", d, e, dt,
        len(rows) if rows else None, ts_str(rows[0][0]) if rows else None,
        "" if rows else (d or {}).get("msg", ""))


# ---------------------------------------------------------------- Gate
def gate():
    print("\n=== GATE ===")
    B = "https://api.gateio.ws/api/v4"

    d, e, dt = get(f"{B}/futures/usdt/candlesticks",
                   {"contract": "BTC_USDT", "interval": "1d", "limit": 1000})
    rec("GAT", "perp candles 1d", "/futures/usdt/candlesticks", d, e, dt,
        len(d) if d else None, ts_str(d[0]["t"] * 1000) if d else None,
        "t,o,h,l,c,v(contracts),sum(quote)" if d else "")

    d, e, dt = get(f"{B}/futures/usdt/funding_rate", {"contract": "BTC_USDT", "limit": 1000})
    rec("GAT", "funding history", "/futures/usdt/funding_rate", d, e, dt,
        len(d) if d else None, ts_str(d[-1]["t"] * 1000) if d else None)

    d, e, dt = get(f"{B}/futures/usdt/contract_stats",
                   {"contract": "BTC_USDT", "interval": "1d", "limit": 100})
    rec("GAT", "contract_stats 1d", "/futures/usdt/contract_stats", d, e, dt,
        len(d) if d else None, ts_str(d[0]["time"] * 1000) if d else None,
        f"OI hist + LSR + taker vol; keys={sorted(d[0].keys())}" if d else "")

    d, e, dt = get(f"{B}/futures/usdt/contracts")
    rec("GAT", "contract specs", "/futures/usdt/contracts", d, e, dt,
        len(d) if d else None, None, "quanto_multiplier/order_price_round" if d else "")

    d, e, dt = get(f"{B}/spot/candlesticks",
                   {"currency_pair": "BTC_USDT", "interval": "1d", "limit": 1000})
    rec("GAT", "spot candles 1d", "/spot/candlesticks", d, e, dt,
        len(d) if d else None, ts_str(int(d[0][0]) * 1000) if d else None)


# ---------------------------------------------------------------- KuCoin
def kucoin():
    print("\n=== KUCOIN ===")
    FB, SB = "https://api-futures.kucoin.com", "https://api.kucoin.com"

    frm = NOW_MS - 400 * DAY_MS
    d, e, dt = get(f"{FB}/api/v1/kline/query",
                   {"symbol": "XBTUSDTM", "granularity": 1440, "from": frm, "to": NOW_MS})
    rows = d.get("data") if d else None
    rec("KCN", "perp kline 1440m", "/api/v1/kline/query", d, e, dt,
        len(rows) if rows else None, ts_str(rows[0][0]) if rows else None,
        "t,o,h,l,c,v (NO quote vol, NO trade count)" if rows else (d or {}).get("msg", ""))

    d, e, dt = get(f"{FB}/api/v1/contract/funding-rates",
                   {"symbol": "XBTUSDTM", "from": NOW_MS - 90 * DAY_MS, "to": NOW_MS})
    rows = d.get("data") if d else None
    rec("KCN", "funding history", "/api/v1/contract/funding-rates", d, e, dt,
        len(rows) if rows else None,
        ts_str(rows[-1]["timepoint"]) if rows else None,
        "" if rows else (d or {}).get("msg", ""))

    for path, params, label in (
        ("/api/ua/v1/market/open-interest",
         {"symbol": "XBTUSDTM", "interval": "1day"}, "OI hist (ua, 1day)"),
        ("/api/v1/contracts/XBTUSDTM", None, "contract spec + current OI"),
    ):
        d, e, dt = get(f"{FB}{path}", params)
        rows = d.get("data") if d else None
        n = len(rows) if isinstance(rows, list) else (1 if rows else None)
        rec("KCN", label, path, d, e, dt, n, None,
            (f"keys={sorted(rows[0].keys())[:8]}" if isinstance(rows, list) and rows
             else ("" if rows else (d or {}).get("msg", ""))))

    d, e, dt = get(f"{SB}/api/v1/market/candles",
                   {"symbol": "BTC-USDT", "type": "1day"})
    rows = d.get("data") if d else None
    rec("KCN", "spot candles 1day", "/api/v1/market/candles", d, e, dt,
        len(rows) if rows else None, ts_str(int(rows[-1][0]) * 1000) if rows else None,
        "has quote volume" if rows else (d or {}).get("msg", ""))


# ---------------------------------------------------------------- stable FX
def stable_fx():
    """USDT/USD and USDC/USD daily - needed to express notional in real USD."""
    print("\n=== STABLECOIN FX -> USD ===")
    for pair, label in (("USDTZUSD", "USDT/USD"), ("USDCUSD", "USDC/USD")):
        d, e, dt = get("https://api.kraken.com/0/public/OHLC",
                       {"pair": pair, "interval": 1440})
        res = (d or {}).get("result", {})
        rows = next((v for k, v in res.items() if k != "last"), None)
        rec("FX", f"kraken {label}", "/0/public/OHLC", d, e, dt,
            len(rows) if rows else None,
            ts_str(rows[0][0] * 1000) if rows else None,
            "o,h,l,c,vwap,vol" if rows else str((d or {}).get("error", e))[:80])

    d, e, dt = get("https://api.exchange.coinbase.com/products/USDT-USD/candles",
                   {"granularity": 86400})
    rec("FX", "coinbase USDT/USD", "/products/USDT-USD/candles", d, e, dt,
        len(d) if d else None, ts_str(d[-1][0] * 1000) if d else None,
        "max 300 candles/request" if d else "")


if __name__ == "__main__":
    print(f"proxy: {PROXY}")
    for fn in (binance, okx, bitget, gate, kucoin, stable_fx):
        try:
            fn()
        except Exception as ex:                                # noqa: BLE001
            print(f"  !! {fn.__name__} crashed: {type(ex).__name__}: {ex}")

    import pandas as pd
    df = pd.DataFrame(results)
    out = Path(__file__).parent / "probe_results.csv"
    df.to_csv(out, index=False)
    print(f"\n{df['ok'].sum()}/{len(df)} endpoints ok -> {out}")
    bad = df[~df["ok"]]
    if len(bad):
        print("\nFAILURES:")
        print(bad[["venue", "what", "note"]].to_string(index=False))
