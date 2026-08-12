"""Streaming reducers: raw Tardis CSV rows -> per-symbol time-bucketed stats.

Why aggregate during the fetch: a single grouped daily file is 100 MB - 2 GB
gzipped (binance-futures `trades/PERPETUALS` = 1.9 GB/day, `derivative_ticker
/PERPETUALS` = 1.1 GB/day).  Storing raw is ~TB/month; the dashboard only needs
1h/1d bars, so we reduce on the fly in a single pass, O(symbols x buckets) RAM.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

US_PER_HOUR = 3_600_000_000
US_PER_MIN = 60_000_000


def _f(v: str | None) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except ValueError:
        return None
    return None if math.isnan(x) else x


def _i(v: str | None) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# derivative_ticker  (perp/futures only: OI, funding, mark/index/last)
# ---------------------------------------------------------------------------
@dataclass
class _DerivBucket:
    updates: int = 0
    oi_open: float | None = None
    oi_close: float | None = None
    oi_high: float | None = None
    oi_low: float | None = None
    oi_sum: float = 0.0
    oi_obs: int = 0
    funding_rate: float | None = None
    predicted_funding_rate: float | None = None
    funding_timestamp: int | None = None
    funding_changes: int = 0
    mark_open: float | None = None
    mark_close: float | None = None
    index_close: float | None = None
    last_open: float | None = None
    last_close: float | None = None
    last_high: float | None = None
    last_low: float | None = None
    ts_first: int | None = None
    ts_last: int | None = None


@dataclass
class DerivativeTickerAggregator:
    """One instance per (exchange, day). Bucket size defaults to 1h.

    derivative_ticker rows are *partial updates* — a row carries only the fields
    that changed, everything else is blank.  We therefore forward-fill per symbol
    across the whole day before bucketing.
    """

    bucket_us: int = US_PER_HOUR
    buckets: dict[tuple[str, int], _DerivBucket] = field(default_factory=dict)
    state: dict[str, dict] = field(default_factory=lambda: defaultdict(dict))
    rows_seen: int = 0

    def update(self, row: dict[str, str]) -> None:
        self.rows_seen += 1
        sym = row["symbol"]
        ts = _i(row.get("timestamp")) or _i(row.get("local_timestamp"))
        if ts is None:
            return
        st = self.state[sym]

        oi = _f(row.get("open_interest"))
        fr = _f(row.get("funding_rate"))
        pfr = _f(row.get("predicted_funding_rate"))
        fts = _i(row.get("funding_timestamp"))
        last = _f(row.get("last_price"))
        index = _f(row.get("index_price"))
        mark = _f(row.get("mark_price"))

        if oi is not None:
            st["oi"] = oi
        if fr is not None:
            if st.get("funding_rate") != fr:
                st["funding_changes"] = st.get("funding_changes", 0) + 1
            st["funding_rate"] = fr
        if pfr is not None:
            st["predicted_funding_rate"] = pfr
        if fts is not None:
            st["funding_timestamp"] = fts
        if last is not None:
            st["last"] = last
        if index is not None:
            st["index"] = index
        if mark is not None:
            st["mark"] = mark

        key = (sym, ts - (ts % self.bucket_us))
        b = self.buckets.get(key)
        if b is None:
            b = self.buckets[key] = _DerivBucket()
        b.updates += 1
        b.ts_first = b.ts_first if b.ts_first is not None else ts
        b.ts_last = ts

        cur_oi = st.get("oi")
        if cur_oi is not None:
            if b.oi_open is None:
                b.oi_open = cur_oi
            b.oi_close = cur_oi
            b.oi_high = cur_oi if b.oi_high is None else max(b.oi_high, cur_oi)
            b.oi_low = cur_oi if b.oi_low is None else min(b.oi_low, cur_oi)
            if oi is not None:            # only count real observations in the mean
                b.oi_sum += oi
                b.oi_obs += 1

        cur_last = st.get("last")
        if cur_last is not None:
            if b.last_open is None:
                b.last_open = cur_last
            b.last_close = cur_last
            b.last_high = cur_last if b.last_high is None else max(b.last_high, cur_last)
            b.last_low = cur_last if b.last_low is None else min(b.last_low, cur_last)

        cur_mark = st.get("mark")
        if cur_mark is not None:
            if b.mark_open is None:
                b.mark_open = cur_mark
            b.mark_close = cur_mark
        if st.get("index") is not None:
            b.index_close = st["index"]
        if st.get("funding_rate") is not None:
            b.funding_rate = st["funding_rate"]
        if st.get("predicted_funding_rate") is not None:
            b.predicted_funding_rate = st["predicted_funding_rate"]
        if st.get("funding_timestamp") is not None:
            b.funding_timestamp = st["funding_timestamp"]
        b.funding_changes = st.get("funding_changes", 0)

    def records(self, exchange: str, day: str) -> list[dict]:
        out = []
        for (sym, bstart), b in self.buckets.items():
            out.append({
                "exchange": exchange, "symbol": sym, "date": day,
                "bucket_start_us": bstart,
                "updates": b.updates,
                "oi_open": b.oi_open, "oi_high": b.oi_high, "oi_low": b.oi_low,
                "oi_close": b.oi_close,
                "oi_mean": (b.oi_sum / b.oi_obs) if b.oi_obs else None,
                "oi_obs": b.oi_obs,
                "funding_rate": b.funding_rate,
                "predicted_funding_rate": b.predicted_funding_rate,
                "next_funding_ts_us": b.funding_timestamp,
                "funding_updates": b.funding_changes,
                "mark_open": b.mark_open, "mark_close": b.mark_close,
                "index_close": b.index_close,
                "last_open": b.last_open, "last_high": b.last_high,
                "last_low": b.last_low, "last_close": b.last_close,
                "ts_first_us": b.ts_first, "ts_last_us": b.ts_last,
            })
        out.sort(key=lambda r: (r["symbol"], r["bucket_start_us"]))
        return out


# ---------------------------------------------------------------------------
# trades  (volume / OHLC / taker flow) — works for spot and derivatives
# ---------------------------------------------------------------------------
@dataclass
class _TradeBucket:
    n: int = 0
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    vol_base: float = 0.0
    vol_quote: float = 0.0
    buy_base: float = 0.0
    sell_base: float = 0.0
    buy_quote: float = 0.0
    sell_quote: float = 0.0
    buy_n: int = 0
    sell_n: int = 0
    ts_first: int | None = None
    ts_last: int | None = None


@dataclass
class TradesAggregator:
    bucket_us: int = US_PER_HOUR
    buckets: dict[tuple[str, int], _TradeBucket] = field(default_factory=dict)
    rows_seen: int = 0

    def update(self, row: dict[str, str]) -> None:
        self.rows_seen += 1
        ts = _i(row.get("timestamp")) or _i(row.get("local_timestamp"))
        price = _f(row.get("price"))
        amount = _f(row.get("amount"))
        if ts is None or price is None or amount is None:
            return
        key = (row["symbol"], ts - (ts % self.bucket_us))
        b = self.buckets.get(key)
        if b is None:
            b = self.buckets[key] = _TradeBucket()
        notional = price * amount
        b.n += 1
        if b.open is None:
            b.open = price
            b.ts_first = ts
        b.close = price
        b.ts_last = ts
        b.high = price if b.high is None else max(b.high, price)
        b.low = price if b.low is None else min(b.low, price)
        b.vol_base += amount
        b.vol_quote += notional
        side = row.get("side")
        if side == "buy":
            b.buy_base += amount
            b.buy_quote += notional
            b.buy_n += 1
        elif side == "sell":
            b.sell_base += amount
            b.sell_quote += notional
            b.sell_n += 1

    def records(self, exchange: str, day: str) -> list[dict]:
        out = []
        for (sym, bstart), b in self.buckets.items():
            chg_bps = None
            if b.open and b.close:
                chg_bps = (b.close / b.open - 1.0) * 10_000
            out.append({
                "exchange": exchange, "symbol": sym, "date": day,
                "bucket_start_us": bstart,
                "trades": b.n, "buy_trades": b.buy_n, "sell_trades": b.sell_n,
                "open": b.open, "high": b.high, "low": b.low, "close": b.close,
                "vwap": (b.vol_quote / b.vol_base) if b.vol_base else None,
                "change_bps": chg_bps,
                "volume_base": b.vol_base, "volume_quote": b.vol_quote,
                "buy_volume_base": b.buy_base, "sell_volume_base": b.sell_base,
                "buy_volume_quote": b.buy_quote, "sell_volume_quote": b.sell_quote,
                "taker_imbalance": ((b.buy_quote - b.sell_quote) / b.vol_quote)
                                   if b.vol_quote else None,
                "ts_first_us": b.ts_first, "ts_last_us": b.ts_last,
            })
        out.sort(key=lambda r: (r["symbol"], r["bucket_start_us"]))
        return out


# ---------------------------------------------------------------------------
# liquidations (optional, cheap)
# ---------------------------------------------------------------------------
@dataclass
class LiquidationsAggregator:
    bucket_us: int = US_PER_HOUR
    buckets: dict[tuple[str, int], dict] = field(default_factory=dict)
    rows_seen: int = 0

    def update(self, row: dict[str, str]) -> None:
        self.rows_seen += 1
        ts = _i(row.get("timestamp")) or _i(row.get("local_timestamp"))
        price, amount = _f(row.get("price")), _f(row.get("amount"))
        if ts is None or price is None or amount is None:
            return
        key = (row["symbol"], ts - (ts % self.bucket_us))
        b = self.buckets.setdefault(key, {"n": 0, "long_liq_quote": 0.0,
                                          "short_liq_quote": 0.0, "liq_quote": 0.0})
        notional = price * amount
        b["n"] += 1
        b["liq_quote"] += notional
        # side=sell => a long was liquidated; side=buy => a short was liquidated
        if row.get("side") == "sell":
            b["long_liq_quote"] += notional
        elif row.get("side") == "buy":
            b["short_liq_quote"] += notional

    def records(self, exchange: str, day: str) -> list[dict]:
        return sorted(
            [{"exchange": exchange, "symbol": s, "date": day, "bucket_start_us": t,
              "liquidations": v["n"], "liq_quote": v["liq_quote"],
              "long_liq_quote": v["long_liq_quote"], "short_liq_quote": v["short_liq_quote"]}
             for (s, t), v in self.buckets.items()],
            key=lambda r: (r["symbol"], r["bucket_start_us"]))


AGGREGATORS = {
    "derivative_ticker": DerivativeTickerAggregator,
    "trades": TradesAggregator,
    "liquidations": LiquidationsAggregator,
}
