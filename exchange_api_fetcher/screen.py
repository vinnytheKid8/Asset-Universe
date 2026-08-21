"""Universe screen: cheap venue-wide snapshot -> shortlist -> deep daily history.

Two tiers, because a full deep pull of 10.7k instruments is ~30k requests:
  tier 1  fetch_snapshot()  ~10 requests, ALL perps on all 5 venues: 24h volume, OI,
          BBO, funding, mark/index. Enough to rank the universe and pick a shortlist.
  tier 2  fetch_deep()      per-instrument 30d daily klines + settled funding + OI
          history, only for the shortlist.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import pandas as pd

from .daily import fetch_instrument_daily
from .fx import FxTable
from .venues import Instrument, _f, _get

log = logging.getLogger(__name__)

BIN_F, OKX_B, BGT_B = ("https://fapi.binance.com", "https://www.okx.com",
                       "https://api.bitget.com")
GAT_B, KCN_F = "https://api.gateio.ws/api/v4", "https://api-futures.kucoin.com"


def _snap_bin() -> list[dict]:
    t24 = {r["symbol"]: r for r in _get(f"{BIN_F}/fapi/v1/ticker/24hr")}
    bbo = {r["symbol"]: r for r in _get(f"{BIN_F}/fapi/v1/ticker/bookTicker")}
    prem = {r["symbol"]: r for r in _get(f"{BIN_F}/fapi/v1/premiumIndex")}
    out = []
    for s, t in t24.items():
        b, p = bbo.get(s, {}), prem.get(s, {})
        out.append(dict(venue="BIN", symbol=s, last=_f(t.get("lastPrice")),
                        vol24h_quote=_f(t.get("quoteVolume")),
                        vol24h_base=_f(t.get("volume")), trades24h=_f(t.get("count")),
                        bid=_f(b.get("bidPrice")), ask=_f(b.get("askPrice")),
                        oi_native=None, funding_rate=_f(p.get("lastFundingRate")),
                        mark=_f(p.get("markPrice")), index=_f(p.get("indexPrice"))))
    return out


def _snap_okx() -> list[dict]:
    out = []
    # SWAP and FUTURES both: OKX's XPERP perps (BTC-USD_UM_XPERP-310404) are returned
    # under FUTURES, and the merge in fetch_snapshot is an inner join - without a
    # ticker row here they are dropped from the universe entirely, which is how
    # OKX-P-BTCUSDC stayed invisible while taking fills. Genuine dated futures come
    # back too and are filtered by specs.okx(), which only keeps _XPERP.
    for it in ("SWAP", "FUTURES"):
        for r in _get(f"{OKX_B}/api/v5/market/tickers", {"instType": it})["data"]:
            base_v, last = _f(r.get("volCcy24h")), _f(r.get("last"))
            out.append(dict(venue="OKX", symbol=r["instId"], last=last,
                            vol24h_quote=(base_v * last) if (base_v and last) else None,
                            vol24h_base=base_v, trades24h=None,
                            bid=_f(r.get("bidPx")), ask=_f(r.get("askPx")),
                            oi_native=None, funding_rate=None, mark=None, index=None))
    oi = {}
    for it in ("SWAP", "FUTURES"):
        oi.update({r["instId"]: _f(r.get("oi"))
                   for r in _get(f"{OKX_B}/api/v5/public/open-interest",
                                 {"instType": it})["data"]})
    for r in out:
        r["oi_native"] = oi.get(r["symbol"])
    return out


def _snap_bgt() -> list[dict]:
    out = []
    for pt in ("USDT-FUTURES", "USDC-FUTURES", "COIN-FUTURES"):
        try:
            rows = _get(f"{BGT_B}/api/v2/mix/market/tickers", {"productType": pt})["data"]
        except Exception as e:                                  # noqa: BLE001
            log.warning("bgt tickers %s: %s", pt, e); continue
        for r in rows:
            out.append(dict(venue="BGT", symbol=r["symbol"], last=_f(r.get("lastPr")),
                            vol24h_quote=_f(r.get("quoteVolume")),
                            vol24h_base=_f(r.get("baseVolume")), trades24h=None,
                            bid=_f(r.get("bidPr")), ask=_f(r.get("askPr")),
                            oi_native=_f(r.get("holdingAmount")),
                            funding_rate=_f(r.get("fundingRate")),
                            mark=_f(r.get("markPrice")), index=_f(r.get("indexPrice"))))
    return out


def _snap_gat() -> list[dict]:
    out = []
    for settle in ("usdt", "usdc", "btc"):
        try:
            rows = _get(f"{GAT_B}/futures/{settle}/tickers")
        except Exception as e:                                  # noqa: BLE001
            log.warning("gate tickers %s: %s", settle, e); continue
        for r in rows:
            out.append(dict(venue="GAT", symbol=r["contract"], last=_f(r.get("last")),
                            vol24h_quote=_f(r.get("volume_24h_quote")),
                            vol24h_base=_f(r.get("volume_24h_base")), trades24h=None,
                            bid=_f(r.get("highest_bid")), ask=_f(r.get("lowest_ask")),
                            oi_native=_f(r.get("total_size")),
                            funding_rate=_f(r.get("funding_rate")),
                            mark=_f(r.get("mark_price")), index=_f(r.get("index_price"))))
    return out


def _snap_kcn() -> list[dict]:
    """contracts/active carries 24h turnover, OI, mark and index in one request."""
    out = []
    for r in _get(f"{KCN_F}/api/v1/contracts/active")["data"]:
        # openInterest is in CONTRACTS - do NOT apply the multiplier here, the
        # caller does it once for every venue (applying it twice gave $17T of OI)
        out.append(dict(venue="KCN", symbol=r["symbol"], last=_f(r.get("lastTradePrice")),
                        vol24h_quote=_f(r.get("turnoverOf24h")),
                        vol24h_base=_f(r.get("volumeOf24h")), trades24h=None,
                        bid=None, ask=None,
                        oi_native=_f(r.get("openInterest")),
                        funding_rate=_f(r.get("fundingFeeRate")),
                        mark=_f(r.get("markPrice")), index=_f(r.get("indexPrice"))))
    return out


# ------------------------------------------------------------------ spot
# One venue-wide request each, same shape as the perp snappers. Spot matters for
# the universe screen because a hedgeable spot leg is what makes an asset safe to
# quote size on - venues_spot from the specs only says the pair EXISTS, not that
# anyone trades it. Field names verified live 2026-08-10 on all five.

BIN_S, OKX_S, BGT_S = ("https://api.binance.com", "https://www.okx.com",
                       "https://api.bitget.com")
GAT_S, KCN_S = "https://api.gateio.ws/api/v4", "https://api.kucoin.com"


def _snap_bin_spot() -> list[dict]:
    # /ticker/24hr already carries bid/ask, so no second bookTicker request
    return [dict(venue="BIN", symbol=t["symbol"], last=_f(t.get("lastPrice")),
                 vol24h_quote=_f(t.get("quoteVolume")), vol24h_base=_f(t.get("volume")),
                 trades24h=_f(t.get("count")), bid=_f(t.get("bidPrice")),
                 ask=_f(t.get("askPrice")), oi_native=None, funding_rate=None,
                 mark=None, index=None)
            for t in _get(f"{BIN_S}/api/v3/ticker/24hr")]


def _snap_okx_spot() -> list[dict]:
    # for SPOT, volCcy24h IS quote volume (for SWAP it is base x ctVal - different
    # meaning on the same field name)
    return [dict(venue="OKX", symbol=r["instId"], last=_f(r.get("last")),
                 vol24h_quote=_f(r.get("volCcy24h")), vol24h_base=_f(r.get("vol24h")),
                 trades24h=None, bid=_f(r.get("bidPx")), ask=_f(r.get("askPx")),
                 oi_native=None, funding_rate=None, mark=None, index=None)
            for r in _get(f"{OKX_S}/api/v5/market/tickers", {"instType": "SPOT"})["data"]]


def _snap_bgt_spot() -> list[dict]:
    return [dict(venue="BGT", symbol=r["symbol"], last=_f(r.get("lastPr")),
                 vol24h_quote=_f(r.get("quoteVolume")), vol24h_base=_f(r.get("baseVolume")),
                 trades24h=None, bid=_f(r.get("bidPr")), ask=_f(r.get("askPr")),
                 oi_native=None, funding_rate=None, mark=None, index=None)
            for r in _get(f"{BGT_S}/api/v2/spot/market/tickers")["data"]]


def _snap_gat_spot() -> list[dict]:
    return [dict(venue="GAT", symbol=r["currency_pair"], last=_f(r.get("last")),
                 vol24h_quote=_f(r.get("quote_volume")),
                 vol24h_base=_f(r.get("base_volume")), trades24h=None,
                 bid=_f(r.get("highest_bid")), ask=_f(r.get("lowest_ask")),
                 oi_native=None, funding_rate=None, mark=None, index=None)
            for r in _get(f"{GAT_S}/spot/tickers")]


def _snap_kcn_spot() -> list[dict]:
    return [dict(venue="KCN", symbol=r["symbol"], last=_f(r.get("last")),
                 vol24h_quote=_f(r.get("volValue")), vol24h_base=_f(r.get("vol")),
                 trades24h=None, bid=_f(r.get("buy")), ask=_f(r.get("sell")),
                 oi_native=None, funding_rate=None, mark=None, index=None)
            for r in _get(f"{KCN_S}/api/v1/market/allTickers")["data"]["ticker"]]


SNAPPERS = {"BIN": _snap_bin, "OKX": _snap_okx, "BGT": _snap_bgt,
            "GAT": _snap_gat, "KCN": _snap_kcn}
SPOT_SNAPPERS = {"BIN": _snap_bin_spot, "OKX": _snap_okx_spot, "BGT": _snap_bgt_spot,
                 "GAT": _snap_gat_spot, "KCN": _snap_kcn_spot}


def fetch_snapshot(spec: pd.DataFrame, fx: FxTable | None = None,
                   spot: bool = True) -> pd.DataFrame:
    """Venue-wide snapshot joined to instrument_ref. ~10 requests, ~20 with spot."""
    rows = []
    for mkt, snappers in (("perp", SNAPPERS), ("spot", SPOT_SNAPPERS if spot else {})):
        for v, fn in snappers.items():
            try:
                r = fn()
                for x in r:
                    x["mkt"] = mkt
                rows += r
                print(f"  snapshot {v} {mkt}: {len(r)} instruments")
            except Exception as e:                              # noqa: BLE001
                log.error("snapshot %s %s: %s", v, mkt, e)
    snap = pd.DataFrame(rows)
    # only quote legs we can trade - Binance also lists BTCUSD1 (quote USD1) and
    # BTCU (quote "U"), which would otherwise inflate an asset's venue count
    kinds = ["linear_perp", "inverse_perp"] + (["spot"] if spot else [])
    ref = spec[spec["kind"].isin(kinds)
               & spec["quote"].isin(["USDT", "USDC", "USD"])].copy()
    # A venue lists the same symbol string as BOTH spot and perp - BIN BTCUSDT is
    # both - so the join has to carry market type. On (venue, symbol) alone every
    # spot row duplicates onto its perp and the asset's volume roughly doubles.
    ref["mkt"] = ref["kind"].map(lambda k: "spot" if k == "spot" else "perp")
    df = snap.merge(ref, on=["venue", "symbol", "mkt"], how="inner", suffixes=("", "_ref"))

    fx = fx or FxTable(df["quote"].dropna().unique(), date.today() - timedelta(days=5),
                       date.today())
    today = date.today()
    rate = [fx.rate(q, today)[0] or 1.0 for q in df["quote"]]
    df["fx_rate"] = rate
    df["vol24h_usd"] = df["vol24h_quote"] * df["fx_rate"]
    # OI -> USD: inverse contracts are USD-denominated already
    df["oi_native"] = df["oi_native"].abs()
    df["oi_usd"] = df.apply(
        lambda r: (r["oi_native"] * r["contract_mult"]) if r["inverse"] == 1
        else (r["oi_native"] * r["contract_mult"] * r["last"] * r["fx_rate"]
              if pd.notna(r["oi_native"]) and pd.notna(r["last"]) else None), axis=1)
    df["spread_bps"] = ((df["ask"] - df["bid"]) / ((df["ask"] + df["bid"]) / 2) * 1e4)
    df["tick_bps"] = df["tick"] / df["last"] * 1e4
    return df


def build_shortlist(snap: pd.DataFrame, traded_assets: set[str],
                    top_n: int = 90, min_usd: float = 3e6) -> pd.DataFrame:
    """Assets we trade + the top non-traded assets by cross-venue 24h PERP volume.

    Ranked on perp volume specifically, not perp+spot. We quote the derivative, so
    an asset with no perp market is not a candidate however much spot it turns over
    - and ranking on the combined number fills the shortlist with spot-only names.
    (It did: the first spot-enabled run shortlisted 130 assets carrying only 265
    perp instruments between them, against ~650 before.)
    """
    perp = snap[snap.get("mkt", "perp") == "perp"] if "mkt" in snap.columns else snap
    a = (perp[perp["is_excluded"] == 0]
         .groupby("asset_key")
         .agg(vol24h_usd=("vol24h_usd", "sum"),
              n_venues=("venue", "nunique"),
              asset_class=("asset_class", "first")).reset_index())
    a["traded"] = a["asset_key"].isin(traded_assets)
    cand = a[(~a["traded"]) & (a["vol24h_usd"] >= min_usd)] \
        .nlargest(top_n, "vol24h_usd")
    return pd.concat([a[a["traded"]], cand]).drop_duplicates("asset_key")


def fetch_deep(instruments: list[Instrument], start: date, end: date,
               fx: FxTable, workers: int = 8) -> pd.DataFrame:
    """30d daily history per instrument, thread-parallel. 3 requests per instrument."""
    rows, done, n = [], 0, len(instruments)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fetch_instrument_daily, ins, start, end, fx): ins
                for ins in instruments}
        for f in as_completed(futs):
            ins = futs[f]
            done += 1
            try:
                rows += f.result()
            except Exception as e:                              # noqa: BLE001
                log.warning("deep %s %s: %s", ins.venue, ins.symbol, e)
            if done % 50 == 0 or done == n:
                print(f"  deep fetch {done}/{n}")
    return pd.DataFrame(rows)
