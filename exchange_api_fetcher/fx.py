"""Stablecoin -> USD daily rates. Notional must be in real USD, not "USDT is a dollar".

USDT/USD and USDC/USD both trade on Kraken with ~2 years of daily history and no auth.
The correction is a few bps in calm markets but USDC/USDT has dislocated 10-30 bps in
stress, which is exactly when a cross-venue volume comparison (one venue USDT-quoted,
another USDC-quoted) would otherwise be silently wrong.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from .venues import _get, _f

KRAKEN = "https://api.kraken.com/0/public/OHLC"
# Kraken pair ids for the two stables we quote in
PAIRS = {"USDT": "USDTZUSD", "USDC": "USDCUSD"}
# currencies that ARE the dollar
NATIVE_USD = {"USD"}


def fetch_stable_usd(ccy: str, start: date, end: date) -> dict[date, float]:
    """Daily close of <ccy>/USD. Empty dict if the venue has no such pair."""
    if ccy.upper() in NATIVE_USD:
        return {}
    pair = PAIRS.get(ccy.upper())
    if not pair:
        return {}
    since = int(datetime(start.year, start.month, start.day,
                         tzinfo=timezone.utc).timestamp())
    res = (_get(KRAKEN, {"pair": pair, "interval": 1440, "since": since})
           or {}).get("result", {})
    rows = next((v for k, v in res.items() if k != "last"), []) or []
    out = {}
    for r in rows:
        d = datetime.fromtimestamp(r[0], timezone.utc).date()
        if start <= d <= end:
            out[d] = _f(r[4])          # close
    return out


class FxTable:
    """quote-ccy -> {date: usd_rate}, with an explicit fallback flag."""

    def __init__(self, quote_ccys, start: date, end: date):
        self.rates: dict[str, dict[date, float]] = {}
        self.missing: set[str] = set()
        for c in {q.upper() for q in quote_ccys}:
            if c in NATIVE_USD:
                continue
            r = fetch_stable_usd(c, start, end)
            if r:
                self.rates[c] = r
            else:
                self.missing.add(c)

    def rate(self, ccy: str, d: date) -> tuple[float | None, str]:
        """-> (rate, source). source in {'native','market','ffill','assumed_1','none'}"""
        c = ccy.upper()
        if c in NATIVE_USD:
            return 1.0, "native"
        series = self.rates.get(c)
        if not series:
            return (1.0, "assumed_1") if c in self.missing else (None, "none")
        if d in series:
            return series[d], "market"
        prior = [k for k in series if k <= d]
        if prior:
            return series[max(prior)], "ffill"       # weekends/holidays on Kraken
        return 1.0, "assumed_1"
