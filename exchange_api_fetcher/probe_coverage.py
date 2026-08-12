"""Coverage probe: for every metric the universe framework needs, test every candidate
venue REST endpoint and record what actually works.

Re-run this whenever a venue changes its API. It writes `coverage.csv` (machine-readable)
and `COVERAGE_TABLE.md` (the matrix pasted into DATA_COVERAGE.md), so the documentation
regenerates from measurement rather than from memory.

    python probe_coverage.py [--no-proxy]
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import requests

CFG = json.load(open(Path(__file__).parent / "general_config.json"))
PROXY = None if "--no-proxy" in sys.argv else {"http": CFG["proxy"], "https": CFG["proxy"]}
NOW = int(time.time() * 1000)
DAY = 86_400_000

BIN_F, BIN_S = "https://fapi.binance.com", "https://api.binance.com"
OKX_B = "https://www.okx.com"
BGT_B = "https://api.bitget.com"
GAT_B = "https://api.gateio.ws/api/v4"
KCN_F, KCN_S = "https://api-futures.kucoin.com", "https://api.kucoin.com"


@dataclass
class Probe:
    metric: str                  # what the framework wants
    venue: str
    endpoint: str
    params: dict = field(default_factory=dict)
    grain: str = "1d"
    # how to count rows in the response
    rows: str = "auto"
    note: str = ""


# metric -> one probe per venue that could serve it. Absent = no candidate endpoint.
PROBES: list[Probe] = [
    # ---------------------------------------------------------------- OHLCV
    Probe("ohlcv_daily", "BIN", f"{BIN_F}/fapi/v1/klines",
          {"symbol": "BTCUSDT", "interval": "1d", "limit": 1500},
          note="UTC native; incl n_trades + taker split"),
    Probe("ohlcv_daily", "OKX", f"{OKX_B}/api/v5/market/history-candles",
          {"instId": "BTC-USDT-SWAP", "bar": "1Dutc", "limit": "100"},
          note="MUST use 1Dutc; bare 1D closes 16:00 UTC"),
    Probe("ohlcv_daily", "BGT", f"{BGT_B}/api/v2/mix/market/history-candles",
          {"symbol": "BTCUSDT", "productType": "USDT-FUTURES", "granularity": "1Dutc",
           "limit": "200"}, note="MUST use 1Dutc; bare 1D closes 16:00 UTC"),
    Probe("ohlcv_daily", "GAT", f"{GAT_B}/futures/usdt/candlesticks",
          {"contract": "BTC_USDT", "interval": "1d", "limit": 1000},
          note="UTC native; v=contracts, sum=quote"),
    Probe("ohlcv_daily", "KCN", f"{KCN_F}/api/v1/kline/query",
          {"symbol": "XBTUSDTM", "granularity": 1440, "from": NOW - 200 * DAY, "to": NOW},
          note="UTC native; contracts only, NO quote volume"),
    # ---------------------------------------------------------------- OHLCV hourly
    Probe("ohlcv_hourly", "BIN", f"{BIN_F}/fapi/v1/klines",
          {"symbol": "BTCUSDT", "interval": "1h", "limit": 1500}),
    Probe("ohlcv_hourly", "OKX", f"{OKX_B}/api/v5/market/history-candles",
          {"instId": "BTC-USDT-SWAP", "bar": "1H", "limit": "300"}),
    Probe("ohlcv_hourly", "BGT", f"{BGT_B}/api/v2/mix/market/history-candles",
          {"symbol": "BTCUSDT", "productType": "USDT-FUTURES", "granularity": "1H",
           "limit": "200"}),
    Probe("ohlcv_hourly", "GAT", f"{GAT_B}/futures/usdt/candlesticks",
          {"contract": "BTC_USDT", "interval": "1h", "limit": 1000}),
    Probe("ohlcv_hourly", "KCN", f"{KCN_F}/api/v1/kline/query",
          {"symbol": "XBTUSDTM", "granularity": 60, "from": NOW - 30 * DAY, "to": NOW}),
    # ---------------------------------------------------------------- open interest
    Probe("oi_history", "BIN", f"{BIN_F}/futures/data/openInterestHist",
          {"symbol": "BTCUSDT", "period": "1d", "limit": 500},
          note="30-DAY LOOKBACK CAP; incl sumOpenInterestValue (USD)"),
    Probe("oi_history", "OKX",
          f"{OKX_B}/api/v5/rubik/stat/contracts/open-interest-history",
          {"instId": "BTC-USDT-SWAP", "period": "1D", "limit": "100"},
          note="paginate via after; incl USD"),
    Probe("oi_history", "GAT", f"{GAT_B}/futures/usdt/contract_stats",
          {"contract": "BTC_USDT", "interval": "1d", "limit": 100},
          note="open_interest + open_interest_usd precomputed"),
    Probe("oi_history", "KCN", f"{KCN_S}/api/ua/v1/market/open-interest",
          {"symbol": "XBTUSDTM", "interval": "1day", "startAt": NOW - 70 * DAY,
           "endAt": NOW, "pageSize": 100},
          note="SPOT host not api-futures; 70d cap; from 2025-12-29; contracts only"),
    Probe("oi_current", "BGT", f"{BGT_B}/api/v2/mix/market/open-interest",
          {"symbol": "BTCUSDT", "productType": "USDT-FUTURES"},
          grain="snapshot", note="NO HISTORY - accumulate forward"),
    # ---------------------------------------------------------------- funding
    Probe("funding_settled", "BIN", f"{BIN_F}/fapi/v1/fundingRate",
          {"symbol": "BTCUSDT", "limit": 1000}, grain="event"),
    Probe("funding_settled", "OKX", f"{OKX_B}/api/v5/public/funding-rate-history",
          {"instId": "BTC-USDT-SWAP", "limit": "100"}, grain="event",
          note="realizedRate + formulaType"),
    Probe("funding_settled", "BGT", f"{BGT_B}/api/v2/mix/market/history-fund-rate",
          {"symbol": "BTCUSDT", "productType": "USDT-FUTURES", "pageSize": "100"},
          grain="event"),
    Probe("funding_settled", "GAT", f"{GAT_B}/futures/usdt/funding_rate",
          {"contract": "BTC_USDT", "limit": 1000}, grain="event"),
    Probe("funding_settled", "KCN", f"{KCN_F}/api/v1/contract/funding-rates",
          {"symbol": "XBTUSDTM", "from": NOW - 90 * DAY, "to": NOW}, grain="event"),
    Probe("funding_interval_cap", "BIN", f"{BIN_F}/fapi/v1/fundingInfo", {},
          grain="static", note="fundingIntervalHours + clamp, per symbol"),
    Probe("funding_interval_cap", "BGT", f"{BGT_B}/api/v2/mix/market/funding-time",
          {"symbol": "BTCUSDT", "productType": "USDT-FUTURES"}, grain="static"),
    # ---------------------------------------------------------------- mark / index
    Probe("mark_klines", "BIN", f"{BIN_F}/fapi/v1/markPriceKlines",
          {"symbol": "BTCUSDT", "interval": "1d", "limit": 500}),
    Probe("mark_klines", "OKX", f"{OKX_B}/api/v5/market/mark-price-candles",
          {"instId": "BTC-USDT-SWAP", "bar": "1Dutc", "limit": "100"}),
    Probe("mark_klines", "BGT", f"{BGT_B}/api/v2/mix/market/history-mark-candles",
          {"symbol": "BTCUSDT", "productType": "USDT-FUTURES", "granularity": "1Dutc",
           "limit": "200"}),
    Probe("mark_klines", "GAT", f"{GAT_B}/futures/usdt/candlesticks",
          {"contract": "mark_BTC_USDT", "interval": "1d", "limit": 100},
          note="mark_ / index_ symbol prefix"),
    Probe("index_klines", "BIN", f"{BIN_F}/fapi/v1/indexPriceKlines",
          {"pair": "BTCUSDT", "interval": "1d", "limit": 500}),
    Probe("index_klines", "OKX", f"{OKX_B}/api/v5/market/index-candles",
          {"instId": "BTC-USDT", "bar": "1Dutc", "limit": "100"}),
    Probe("index_klines", "BGT", f"{BGT_B}/api/v2/mix/market/history-index-candles",
          {"symbol": "BTCUSDT", "productType": "USDT-FUTURES", "granularity": "1Dutc",
           "limit": "200"}),
    Probe("index_klines", "GAT", f"{GAT_B}/futures/usdt/candlesticks",
          {"contract": "index_BTC_USDT", "interval": "1d", "limit": 100}),
    Probe("premium_klines", "BIN", f"{BIN_F}/fapi/v1/premiumIndexKlines",
          {"symbol": "BTCUSDT", "interval": "1d", "limit": 500}),
    Probe("basis_history", "BIN", f"{BIN_F}/futures/data/basis",
          {"pair": "BTCUSDT", "contractType": "PERPETUAL", "period": "1d",
           "limit": 500}, note="30-day cap"),
    # ---------------------------------------------------------------- flow / positioning
    Probe("taker_ratio", "BIN", f"{BIN_F}/futures/data/takerlongshortRatio",
          {"symbol": "BTCUSDT", "period": "1d", "limit": 500}, note="30-day cap"),
    Probe("taker_ratio", "OKX", f"{OKX_B}/api/v5/rubik/stat/taker-volume",
          {"ccy": "BTC", "instType": "SWAP", "period": "1D"},
          note="per-CCY not per-instrument"),
    Probe("taker_ratio", "GAT", f"{GAT_B}/futures/usdt/contract_stats",
          {"contract": "BTC_USDT", "interval": "1d", "limit": 100},
          note="long_taker_size / short_taker_size"),
    Probe("lsr_account", "BIN",
          f"{BIN_F}/futures/data/globalLongShortAccountRatio",
          {"symbol": "BTCUSDT", "period": "1d", "limit": 500}, note="30-day cap"),
    Probe("lsr_account", "OKX",
          f"{OKX_B}/api/v5/rubik/stat/contracts/long-short-account-ratio",
          {"ccy": "BTC", "period": "1D"}, note="per-CCY not per-instrument"),
    Probe("lsr_account", "BGT", f"{BGT_B}/api/v2/mix/market/account-long-short",
          {"symbol": "BTCUSDT", "productType": "USDT-FUTURES", "period": "4H"},
          grain="4h", note="NO daily period; 1H/4H only"),
    Probe("lsr_account", "GAT", f"{GAT_B}/futures/usdt/contract_stats",
          {"contract": "BTC_USDT", "interval": "1d", "limit": 100},
          note="lsr_account / lsr_taker / top_*"),
    Probe("liquidations", "GAT", f"{GAT_B}/futures/usdt/contract_stats",
          {"contract": "BTC_USDT", "interval": "1d", "limit": 100},
          note="long_liq_usd / short_liq_usd"),
    # ------------------------------------------------- BBO: venue-wide single request
    Probe("bbo_all_symbols", "BIN", f"{BIN_F}/fapi/v1/ticker/bookTicker", {},
          grain="snapshot", note="ALL perps in 1 request"),
    Probe("bbo_all_symbols", "OKX", f"{OKX_B}/api/v5/market/tickers",
          {"instType": "SWAP"}, grain="snapshot", note="ALL swaps in 1 request"),
    Probe("bbo_all_symbols", "BGT", f"{BGT_B}/api/v2/mix/market/tickers",
          {"productType": "USDT-FUTURES"}, grain="snapshot",
          note="ALL perps in 1 request; incl OI + funding"),
    Probe("bbo_all_symbols", "GAT", f"{GAT_B}/futures/usdt/tickers", {},
          grain="snapshot", note="ALL perps in 1 request; incl total_size (OI)"),
    Probe("bbo_all_symbols", "KCN", f"{KCN_F}/api/v1/allTickers", {},
          grain="snapshot", note="ALL perps in 1 request"),
    Probe("bbo_all_symbols_spot", "BIN", f"{BIN_S}/api/v3/ticker/bookTicker", {},
          grain="snapshot"),
    Probe("bbo_all_symbols_spot", "OKX", f"{OKX_B}/api/v5/market/tickers",
          {"instType": "SPOT"}, grain="snapshot"),
    Probe("bbo_all_symbols_spot", "BGT", f"{BGT_B}/api/v2/spot/market/tickers", {},
          grain="snapshot"),
    Probe("bbo_all_symbols_spot", "GAT", f"{GAT_B}/spot/tickers", {},
          grain="snapshot"),
    Probe("bbo_all_symbols_spot", "KCN", f"{KCN_S}/api/v1/market/allTickers", {},
          grain="snapshot"),
    # ---------------------------------------------------------------- book depth
    Probe("book_depth", "BIN", f"{BIN_F}/fapi/v1/depth",
          {"symbol": "BTCUSDT", "limit": 20}, grain="snapshot", note="per-symbol"),
    Probe("book_depth", "OKX", f"{OKX_B}/api/v5/market/books",
          {"instId": "BTC-USDT-SWAP", "sz": "20"}, grain="snapshot"),
    Probe("book_depth", "BGT", f"{BGT_B}/api/v2/mix/market/merge-depth",
          {"symbol": "BTCUSDT", "productType": "USDT-FUTURES", "limit": "20"},
          grain="snapshot"),
    Probe("book_depth", "GAT", f"{GAT_B}/futures/usdt/order_book",
          {"contract": "BTC_USDT", "limit": 20}, grain="snapshot"),
    Probe("book_depth", "KCN", f"{KCN_F}/api/v1/level2/depth20",
          {"symbol": "XBTUSDTM"}, grain="snapshot"),
    # ---------------------------------------------------------------- instrument specs
    Probe("specs_perp", "BIN", f"{BIN_F}/fapi/v1/exchangeInfo", {}, grain="static",
          note="tick/lot/minNotional/underlyingType/onboardDate"),
    Probe("specs_perp", "OKX", f"{OKX_B}/api/v5/public/instruments",
          {"instType": "SWAP"}, grain="static", note="ctVal/ctMult/tickSz/listTime"),
    Probe("specs_perp", "BGT", f"{BGT_B}/api/v2/mix/market/contracts",
          {"productType": "USDT-FUTURES"}, grain="static"),
    Probe("specs_perp", "GAT", f"{GAT_B}/futures/usdt/contracts", {}, grain="static",
          note="quanto_multiplier/maker_fee_rate"),
    Probe("specs_perp", "KCN", f"{KCN_F}/api/v1/contracts/active", {}, grain="static",
          note="multiplier/isInverse/openInterest/firstOpenDate"),
    Probe("specs_spot", "BIN", f"{BIN_S}/api/v3/exchangeInfo", {}, grain="static"),
    Probe("specs_spot", "OKX", f"{OKX_B}/api/v5/public/instruments",
          {"instType": "SPOT"}, grain="static"),
    Probe("specs_spot", "BGT", f"{BGT_B}/api/v2/spot/public/symbols", {}, grain="static"),
    Probe("specs_spot", "GAT", f"{GAT_B}/spot/currency_pairs", {}, grain="static"),
    Probe("specs_spot", "KCN", f"{KCN_S}/api/v2/symbols", {}, grain="static"),
    # ---------------------------------------------------------------- stablecoin FX
    Probe("fx_usdt_usd", "KRAKEN", "https://api.kraken.com/0/public/OHLC",
          {"pair": "USDTZUSD", "interval": 1440}),
    Probe("fx_usdc_usd", "KRAKEN", "https://api.kraken.com/0/public/OHLC",
          {"pair": "USDCUSD", "interval": 1440}),
]


def count_rows(j):
    """Best-effort row count across the five venues' response envelopes."""
    if j is None:
        return None
    if isinstance(j, list):
        return len(j)
    if isinstance(j, dict):
        for k in ("data", "result", "symbols", "tickers"):
            v = j.get(k)
            if isinstance(v, list):
                return len(v)
            if isinstance(v, dict):
                if "items" in v and isinstance(v["items"], list):
                    return len(v["items"])
                inner = next((x for kk, x in v.items() if isinstance(x, list)), None)
                if inner is not None:
                    return len(inner)
                return 1
        return 1
    return None


def sample_keys(j, n=14):
    if isinstance(j, list) and j:
        first = j[0]
    elif isinstance(j, dict):
        v = j.get("data") or j.get("symbols") or j.get("result")
        if isinstance(v, dict):
            v = v.get("items") or next((x for x in v.values() if isinstance(x, list)), None)
        first = v[0] if isinstance(v, list) and v else v
    else:
        return ""
    if isinstance(first, dict):
        return ",".join(sorted(first.keys())[:n])
    if isinstance(first, list):
        return f"<array len {len(first)}>"
    return ""


def run():
    out = []
    print(f"proxy: {PROXY}\n")
    for p in PROBES:
        t0 = time.time()
        status, err, j = None, None, None
        try:
            r = requests.get(p.endpoint, params=p.params or None, proxies=PROXY,
                             timeout=45)
            status = r.status_code
            if status == 200:
                j = r.json()
                # venue-level error envelopes still return HTTP 200
                if isinstance(j, dict):
                    code = str(j.get("code", ""))
                    if code and code not in ("0", "200000", "00000"):
                        err = f"venue code {code}: {str(j.get('msg'))[:60]}"
            else:
                body = r.text[:100]
                err = f"HTTP {status}: {body}"
        except Exception as e:                                   # noqa: BLE001
            err = f"{type(e).__name__}: {str(e)[:80]}"
        n = count_rows(j) if not err else None
        rec = {"metric": p.metric, "venue": p.venue, "grain": p.grain,
               "endpoint": p.endpoint.split(".com")[-1] or p.endpoint,
               "params": json.dumps(p.params, separators=(",", ":"))[:150],
               "ok": err is None, "rows": n, "secs": round(time.time() - t0, 2),
               "fields": sample_keys(j) if not err else "",
               "note": p.note, "error": err or ""}
        out.append(rec)
        flag = "ok  " if err is None else "FAIL"
        print(f"[{flag}] {p.metric:22s} {p.venue:6s} rows={str(n):>6s} "
              f"{rec['endpoint'][:52]:52s} {p.note or err or ''}"[:190])
        time.sleep(0.12)                      # be polite across venues
    return out


if __name__ == "__main__":
    import pandas as pd
    df = pd.DataFrame(run())
    here = Path(__file__).parent
    df["probed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    df.to_csv(here / "coverage.csv", index=False)

    print(f"\n{int(df.ok.sum())}/{len(df)} probes ok")
    piv = df.assign(cell=df.apply(
        lambda r: (f"{r.rows}" if r.ok else "-"), axis=1)).pivot_table(
        index="metric", columns="venue", values="cell", aggfunc="first").fillna("n/a")
    print("\nrows returned per metric x venue ('-' = endpoint failed, n/a = no candidate):")
    print(piv.to_string())

    lines = ["| metric | " + " | ".join(piv.columns) + " |",
             "|" + "---|" * (len(piv.columns) + 1)]
    for m, row in piv.iterrows():
        lines.append(f"| `{m}` | " + " | ".join(str(v) for v in row) + " |")
    (here / "COVERAGE_TABLE.md").write_text("\n".join(lines) + "\n")
    print(f"\n-> {here/'coverage.csv'}\n-> {here/'COVERAGE_TABLE.md'}")
