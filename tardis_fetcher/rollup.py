"""Hourly bars -> daily bars -> the wide per-asset dashboard frame.

Pure pandas, no network. Everything downstream of the fetch layer.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import FEEDS, classify_symbol


def _daily_from_trade_bars(tb: pd.DataFrame) -> pd.DataFrame:
    if tb.empty:
        return tb
    tb = tb.sort_values(["exchange", "symbol", "bucket_start_us"])
    g = tb.groupby(["exchange", "symbol", "date"], sort=False)
    out = g.agg(
        trades=("trades", "sum"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume_base=("volume_base", "sum"),
        volume_quote=("volume_quote", "sum"),
        buy_volume_quote=("buy_volume_quote", "sum"),
        sell_volume_quote=("sell_volume_quote", "sum"),
        active_hours=("bucket_start_us", "nunique"),
    ).reset_index()
    out["vwap"] = out["volume_quote"] / out["volume_base"].replace(0, pd.NA)
    out["change_bps"] = (out["close"] / out["open"] - 1.0) * 10_000
    out["taker_imbalance"] = ((out["buy_volume_quote"] - out["sell_volume_quote"])
                              / out["volume_quote"].replace(0, pd.NA))
    return out


def _daily_from_deriv_bars(db: pd.DataFrame) -> pd.DataFrame:
    if db.empty:
        return db
    db = db.sort_values(["exchange", "symbol", "bucket_start_us"])
    g = db.groupby(["exchange", "symbol", "date"], sort=False)
    out = g.agg(
        oi_open=("oi_open", "first"),
        oi_close=("oi_close", "last"),
        oi_high=("oi_high", "max"),
        oi_low=("oi_low", "min"),
        oi_mean=("oi_mean", "mean"),
        oi_obs=("oi_obs", "sum"),
        mark_close=("mark_close", "last"),
        index_close=("index_close", "last"),
        last_close=("last_close", "last"),
        funding_rate_close=("funding_rate", "last"),
        funding_rate_mean=("funding_rate", "mean"),
        funding_events=("next_funding_ts_us", "nunique"),
        updates=("updates", "sum"),
    ).reset_index()
    # funding interval differs per venue/symbol (1h, 2h, 4h, 8h) -> derive it
    out["funding_periods_per_day"] = out["funding_events"].clip(lower=1)
    out["oi_change_bps"] = (out["oi_close"] / out["oi_open"] - 1.0) * 10_000
    out["basis_bps"] = ((out["mark_close"] / out["index_close"] - 1.0) * 10_000)
    return out


def daily_table(out_dir: Path) -> pd.DataFrame:
    """Join trade + derivative daily aggregates into one per-symbol-per-day row."""
    from . import store
    tb = _daily_from_trade_bars(store.read_table(out_dir, "trade_bars"))
    db = _daily_from_deriv_bars(store.read_table(out_dir, "deriv_bars"))
    if tb.empty and db.empty:
        return pd.DataFrame()
    if tb.empty:
        df = db
    elif db.empty:
        df = tb
    else:
        df = tb.merge(db, on=["exchange", "symbol", "date"], how="outer")
    df["venue"] = df["exchange"].map(lambda e: FEEDS[e].venue if e in FEEDS else None)
    df["segment"] = [classify_symbol(e, s) if e in FEEDS else None
                     for e, s in zip(df["exchange"], df["symbol"])]
    if {"oi_close", "mark_close"} <= set(df.columns):
        # only valid where OI is denominated in the base asset - see caveat below
        df["oi_notional_usd"] = df["oi_close"] * df["mark_close"]
    return df.sort_values(["exchange", "symbol", "date"])


def dashboard_frame(daily: pd.DataFrame, lookbacks: tuple[int, ...] = (1, 7, 14)
                    ) -> pd.DataFrame:
    """Latest snapshot per instrument + N-day deltas, one row per instrument.

    NOTE on `oi_notional_usd`: only correct where open_interest is denominated in
    the base asset (binance-futures, bitget-futures).  For contract-denominated
    venues (binance-delivery, okex-swap, gate-io-futures, kucoin-futures) multiply
    by the contract multiplier first — see config.ExchangeFeed.oi_unit.
    """
    if daily.empty:
        return daily
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values(["exchange", "symbol", "date"])
    g = d.groupby(["exchange", "symbol"], sort=False)

    for n in lookbacks:
        for col, name in (("volume_quote", "vol"), ("oi_close", "oi"), ("close", "px")):
            if col not in d:
                continue
            prev = g[col].shift(n)
            d[f"{name}_chg_{n}d_pct"] = (d[col] / prev - 1.0) * 100
            if name == "px":
                d[f"px_chg_{n}d_bps"] = (d[col] / prev - 1.0) * 10_000
    if "volume_quote" in d:
        d["vol_7d_avg_quote"] = g["volume_quote"].transform(
            lambda s: s.rolling(7, min_periods=1).mean())
        d["vol_vs_7d_avg"] = d["volume_quote"] / d["vol_7d_avg_quote"]
    if "oi_close" in d:
        d["oi_7d_avg"] = g["oi_close"].transform(lambda s: s.rolling(7, min_periods=1).mean())
    if "funding_rate_close" in d.columns:
        per_day = d["funding_periods_per_day"] if "funding_periods_per_day" in d else 3
        d["funding_apr_pct"] = d["funding_rate_close"] * per_day * 365 * 100

    latest = d.groupby(["exchange", "symbol"], as_index=False).tail(1)
    sort_col = "volume_usd" if "volume_usd" in latest else "volume_quote"
    return latest.sort_values(sort_col, ascending=False, na_position="last")


def sparkline_series(out_dir: Path, table: str = "deriv_bars",
                     value: str = "oi_close") -> pd.DataFrame:
    """Long-format hourly series for the 1-2 week OI / volume charts."""
    from . import store
    df = store.read_table(out_dir, table)
    if df.empty:
        return df
    df["ts"] = pd.to_datetime(df["bucket_start_us"], unit="us", utc=True)
    return df[["exchange", "symbol", "ts", value]].sort_values(
        ["exchange", "symbol", "ts"])
