"""Assemble one normalised daily row per (venue, symbol, date) from the venue adapters.

    from daily import fetch_instrument_daily, resolve
    ins  = resolve("BIN", "linear_perp", "BTC", "USDT")
    rows = fetch_instrument_daily(ins, start, end)
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, datetime, timezone

from .fx import FxTable
from .venues import ADAPTERS, CAPABILITIES, Instrument, _d, _get, _f

log = logging.getLogger(__name__)

# ---------------------------------------------------------------- symbol resolution
# venue-native symbol formatting per (venue, market_type). Base/quote come from the
# caller (which gets them from the venue's own instrument list), never from parsing.
def native_symbol(venue: str, market_type: str, base: str, quote: str) -> str:
    b, q = base.upper(), quote.upper()
    if venue == "BIN":
        return f"{b}{q}" if market_type != "inverse_perp" else f"{b}{q}_PERP"
    if venue == "OKX":
        return f"{b}-{q}" if market_type == "spot" else f"{b}-{q}-SWAP"
    if venue == "BGT":
        if market_type == "spot":
            return f"{b}{q}"
        return f"{b}PERP" if q == "USDC" else f"{b}{q}"      # USDC-M perps are <BASE>PERP
    if venue == "GAT":
        return f"{b}_{q}"
    if venue == "KCN":
        b = "XBT" if b == "BTC" else b
        if market_type == "spot":
            return f"{'BTC' if b == 'XBT' else b}-{q}"
        return f"{b}{q}M" if market_type == "linear_perp" else f"{b}USDM"
    raise ValueError(venue)


def contract_multiplier(venue: str, market_type: str, symbol: str) -> float:
    """Only KuCoin (volume) and the contract-denominated venues (OI) need this."""
    if market_type == "spot":
        return 1.0
    try:
        if venue == "KCN":
            d = (_get(f"https://api-futures.kucoin.com/api/v1/contracts/{symbol}")
                 or {}).get("data") or {}
            return _f(d.get("multiplier")) or 1.0
        if venue == "GAT":
            settle = "usdt" if symbol.endswith("_USDT") else \
                     ("usdc" if symbol.endswith("_USDC") else "btc")
            d = _get(f"https://api.gateio.ws/api/v4/futures/{settle}/contracts/{symbol}") or {}
            return _f(d.get("quanto_multiplier")) or 1.0
        if venue == "OKX":
            # XPERP perps are returned under instType=FUTURES; asking for SWAP gets
            # an empty list and a silent multiplier of 1.0, which for BTC (ctVal
            # 0.0001) would overstate contract-denominated figures by 10,000x.
            it = "FUTURES" if "_XPERP" in symbol else "SWAP"
            rows = (_get("https://www.okx.com/api/v5/public/instruments",
                         {"instType": it, "instId": symbol}) or {}).get("data") or []
            return (_f(rows[0].get("ctVal")) or 1.0) if rows else 1.0
    except Exception as e:                                    # noqa: BLE001
        log.warning("multiplier lookup failed %s %s: %s", venue, symbol, e)
    return 1.0


def resolve(venue: str, market_type: str, base: str, quote: str,
            symbol: str | None = None) -> Instrument:
    """Build an Instrument, rebuilding the native symbol from (base, quote).

    `symbol` overrides that reconstruction for instruments whose native id CANNOT be
    derived - currently only OKX XPERP, where the instId carries a per-asset expiry
    (BTC-USD_UM_XPERP-310404) that no formula produces. Pass it only for specs
    flagged exact_symbol, and take it from the venue's own instrument list: an
    override skips the round-trip check that catches symbology bugs everywhere else.
    """
    sym = symbol or native_symbol(venue, market_type, base, quote)
    return Instrument(venue=venue, market_type=market_type, symbol=sym,
                      base_ccy=base.upper(), quote_ccy=quote.upper(),
                      contract_mult=contract_multiplier(venue, market_type, sym),
                      inverse=(market_type == "inverse_perp"))


# ---------------------------------------------------------------- funding -> daily
def _funding_daily(events: list[dict]) -> dict[date, dict]:
    """Settled funding events -> per-day sum/count/interval. `rate` is per interval,
    so the daily total is the sum, and the APR uses the observed interval count."""
    by_day: dict[date, list] = defaultdict(list)
    for e in events:
        if e["rate"] is None:
            continue
        by_day[_d(e["funding_ts"])].append(e)
    stamps = sorted({e["funding_ts"] for e in events})
    diffs = [(b - a) / 3_600_000 for a, b in zip(stamps, stamps[1:]) if b > a]
    interval_h = None
    if diffs:                                   # modal gap, robust to a missed event
        interval_h = max(set(round(x) for x in diffs),
                         key=[round(x) for x in diffs].count)
    out = {}
    for d, evs in by_day.items():
        tot = sum(e["rate"] for e in evs)
        out[d] = {"funding_sum": tot, "funding_events": len(evs),
                  "funding_interval_h": interval_h,
                  "funding_apr_pct": tot * 365 * 100}
    return out


# ---------------------------------------------------------------- main entry point
def fetch_instrument_daily(ins: Instrument, start: date, end: date,
                           fx: FxTable | None = None) -> list[dict]:
    A = ADAPTERS[ins.venue]
    cap = CAPABILITIES[ins.venue]

    kl = A.klines(ins, start, end)
    fnd = _funding_daily(A.funding(ins, start, end))
    oi = A.oi(ins, start, end) if hasattr(A, "oi") else {}
    fx = fx or FxTable([ins.quote_ccy], start, end)

    rows = []
    for d in sorted(kl):
        k, o = kl[d], oi.get(d, {})
        rate, fx_src = fx.rate(ins.quote_ccy, d)
        vq = k.get("vol_quote")
        oi_usd = o.get("oi_usd")
        if oi_usd is None and o.get("oi_native") is not None and k.get("px_close"):
            oi_usd = (o["oi_native"] if ins.inverse
                      else o["oi_native"] * k["px_close"])
            oi_usd = oi_usd * rate if (rate and not ins.inverse) else oi_usd
        rows.append({
            "date": d, "venue": ins.venue, "market_type": ins.market_type,
            "symbol": ins.symbol, "base_ccy": ins.base_ccy, "quote_ccy": ins.quote_ccy,
            "px_open": k.get("px_open"), "px_high": k.get("px_high"),
            "px_low": k.get("px_low"), "px_close": k.get("px_close"),
            "vol_base": k.get("vol_base"), "vol_quote": vq,
            "vol_usd": (vq * rate) if (vq is not None and rate) else None,
            "fx_rate": rate, "fx_source": fx_src,
            "vol_quote_is_estimate": bool(k.get("vol_quote_is_estimate")),
            "trades": k.get("trades"),
            "taker_buy_quote": k.get("taker_buy_quote"),
            "taker_imbalance": (
                (2 * k["taker_buy_quote"] / vq - 1)
                if (k.get("taker_buy_quote") is not None and vq) else o.get("taker_imbalance")),
            "oi_native": o.get("oi_native"), "oi_usd": oi_usd,
            "oi_source": o.get("oi_source"),
            "mark_close": o.get("mark_close"),
            "lsr_account": o.get("lsr_account"), "lsr_taker": o.get("lsr_taker"),
            "liq_long_usd": o.get("liq_long_usd"), "liq_short_usd": o.get("liq_short_usd"),
            "contract_mult": ins.contract_mult,
            **{f"funding_{k2.split('funding_')[-1]}" if k2.startswith("funding_") else k2: v
               for k2, v in fnd.get(d, {}).items()},
            "caps_trades": cap["trades"], "caps_oi_hist_days": cap["oi_hist_days"],
            "ingest_ts": datetime.now(timezone.utc).replace(microsecond=0),
        })
    return rows
