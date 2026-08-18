"""asset_key derivation + the internal-ClickHouse <-> exchange symbol mapping.

Two separate symbologies have to be reconciled:

  exchange   venue-native, inconsistent: BTCUSDT / BTC-USDT-SWAP / BTC_USDT / XBTUSDTM,
             and for RWAs the issuer wrapper is part of the base currency (CRCLX on Gate).
  internal   strat__*_alex.symbols_record: "{VENUE}-{KIND}-{BASE}{QUOTE}", e.g.
             BIN-P-UBUSDT, OKX-IP-BTCUSD, GAT-S-CRCLONUSDT. Note our perp names DROP
             Gate's issuer suffix (GAT-P-CRCLUSDT is really CRCLX_USDT) while our spot
             names KEEP it (GAT-S-CRCLONUSDT) - the convention is not self-consistent,
             so the mapping needs the variant search in `map_internal`.

Every rule below is a whitelist with a guard. Nothing is a bare regex, because a bare
regex folds CRCL3L (a 3x leveraged token) into CRCL and turns 1000X (a real token
called "1000X") into X.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

# RWA is a SUPERSET, not a sibling of equity: every equity/commodity/index perp is a
# real-world asset. So class and RWA-ness are two orthogonal fields.
#
# Venue vocabularies differ in specificity:
#   Binance  COIN | EQUITY | HK_EQUITY | KR_EQUITY | COMMODITY | INDEX | PREMARKET
#   Bitget   RWA (binary) - no sub-type at all
# so Binance's tag always beats Bitget's generic RWA for the same asset. SPECIFICITY
# ranks that resolution; the winner is taken per asset_key, not per instrument, so an
# asset declared EQUITY on one venue is equity everywhere.
CLASS_MAP = {"COIN": "crypto", "": "crypto",
             "EQUITY": "equity", "HK_EQUITY": "equity", "KR_EQUITY": "equity",
             "COMMODITY": "commodity", "INDEX": "index",
             "PREMARKET": "premarket", "PRE_MARKET": "premarket",
             "RWA": "rwa_unclassified"}
EQUITY_REGION = {"EQUITY": "US", "HK_EQUITY": "HK", "KR_EQUITY": "KR"}
# higher wins when venues disagree
SPECIFICITY = {"equity": 4, "commodity": 4, "index": 4, "premarket": 4,
               "rwa_unclassified": 2, "crypto": 1}
RWA_CLASSES = {"equity", "commodity", "index", "premarket", "rwa_unclassified"}

# quote currencies our internal names ever use, longest first so USDT beats USD
INTERNAL_QUOTES = ("USDT", "USDC", "USD")
INTERNAL_KIND = {"S": "spot", "P": "linear_perp", "IP": "inverse_perp",
                 "F": "future", "IF": "inverse_future"}

# leveraged / structured tokens - never part of the universe
LEVERAGED_RE = re.compile(r"(3L|3S|5L|5S|2L|2S)$")
# scale prefixes, whitelisted
SCALE_RE = re.compile(r"^(1000000|100000|10000|1000|1M)(?=[A-Z])")
# tokens whose NAME starts with a scale prefix - never strip these
SCALE_EXCEPTIONS = {"1000X", "1000SATS", "1000CAT"}
# RWA issuer wrappers, per venue+kind. Applied only when the remainder is a known
# RWA underlying somewhere else in the universe.
ISSUER_SUFFIXES = {
    ("BIN", "spot"): ("B",),
    ("GAT", "spot"): ("ON", "X", "G"),
    ("GAT", "linear_perp"): ("X",),
}
# OKX puts the wrapper on the FRONT: XAAPL, XNVDA, XTSLA, XGOOGL - 63 of them, all
# reading as `crypto` under their wrapped ticker until this existed. Same idea as
# ISSUER_SUFFIXES, opposite end of the string.
ISSUER_PREFIXES = {
    ("OKX", "spot"): ("X",),
    ("OKX", "linear_perp"): ("X",),
}
# Bitget "rStocks" - tokenised equities under an r prefix. EXCLUDED, not merged.
#
# Bitget reports quoteVolume / baseVolume / usdtVolume that all agree with each other on
# figures like $39.4B/day for rSPYUSDT. The daily CANDLE endpoint returns the identical
# number, so there is no second volume field to cross-check against - but the trade tape
# settles it: rSPY's last 100 fills span 41 HOURS and total $10,238, a run-rate near
# $6k/day against $39.4B reported, and rNVDA's span 4.3 hours for $3,418. The same
# measurement on BTCUSDT and HYPEUSDT lands within 2x of their reported figures, so the
# method is sound and the rStock number is not platform volume. They are 99.95% of
# Bitget's reported spot book ($850.5B of $850.9B); the genuine book is ~$412M.
#
# IDENTIFICATION IS EXACT, NOT HEURISTIC. Bitget encodes the product in the CASE of
# baseCoin: rSPY, rNVDA, rV - lowercase r, then the equity ticker. 671 of 1,280 spot
# symbols match ^r[A-Z] on the native string, and they are exactly the rStocks: every
# real R-token (RAY, RUNE, RENDER, ROSE, RSR, RVN, RLC, RONIN, RATS, RIVER, RTX, RWA,
# RLUSD ...) is natively UPPERCASE and cannot match. So this needs no `declared COIN`
# veto and no length guard - the previous rule tested the uppercased string with
# `len > 2`, which let through rA rB rC rD rF rO rT rU rV (R + a one-letter ticker:
# Visa, AT&T, Ford, Agilent, Realty Income) worth $10.5B/day of phantom volume.
#
# Not used: `areaSymbol == "yes"` is a superset (680) - it is a regional-restriction
# flag that also catches USDGO, PI, SOPH and the preSPCX/preOPAI pre-IPO tokens, so it
# would exclude ~$9.7M/day of real volume.
RSTOCK_NATIVE_RE = {("BGT", "spot"): re.compile(r"^r[A-Z]")}


def derive_asset_keys(spec: pd.DataFrame) -> pd.DataFrame:
    """Add asset_key / asset_class / issuer / scale_factor / is_excluded / id_confidence."""
    df = spec.copy()

    # --- RWA underlyings, from the two venues that declare it natively ------------
    rwa = set(df.loc[df["is_rwa"] == 1, "base_raw"].dropna().unique())
    all_bases = set(df["base_raw"].dropna().unique())
    # Tokens a venue explicitly calls a COIN. Used only to VETO a prefix strip: a
    # venue calling something a coin is the strongest available denial that it is a
    # tokenised stock, and the `stem in rwa` guard alone is too weak at the front of
    # the string. XPL (Plasma, COIN on all five venues) would otherwise strip to PL,
    # which Bitget tags as generic RWA - merging Plasma into platinum.
    declared_coin = set(df.loc[df["underlying_type"].astype(str).str.upper() == "COIN",
                               "base_raw"].dropna().unique())

    keys, classes, issuers, scales, excl, reasons, conf = [], [], [], [], [], [], []
    regions = []
    for r in df.itertuples():
        b, venue, kind = r.base_raw, r.venue, r.kind
        key, issuer, scale = b, "", 1.0
        ex, reason, cf = 0, "", "verified"

        _rs = RSTOCK_NATIVE_RE.get((venue, kind))
        # the venue's own casing, before fetch_all folded base_raw to upper
        native = getattr(r, "base_native", None) or b
        if kind in ("future", "inverse_future"):
            ex, reason = 1, "dated_future"
        elif LEVERAGED_RE.search(b):
            ex, reason = 1, "leveraged_token"
        elif _rs and _rs.match(native):
            # See RSTOCK_NATIVE_RE: reported volume is not platform volume.
            ex, reason = 1, "rstock_volume_unverifiable"
        else:
            # 1. scale prefix
            m = SCALE_RE.match(b)
            if m and b not in SCALE_EXCEPTIONS:
                rest = b[m.end():]
                if rest in all_bases:
                    key, scale = rest, float(m.group(1).replace("1M", "1000000"))
                    cf = "proposed"          # needs the price-agreement check
            # 2. RWA issuer wrapper, suffix
            for suf in ISSUER_SUFFIXES.get((venue, kind), ()):
                if key.endswith(suf) and len(key) > len(suf):
                    stem = key[: -len(suf)]
                    if stem in rwa:
                        key, issuer = stem, suf
                        break
            # 3. RWA issuer wrapper, prefix (OKX). Two guards, both load-bearing:
            # the stem must be a known RWA underlying, AND the wrapped token must not
            # be declared a COIN anywhere. Verified against the live universe: strips
            # 63 tokenised equities, blocks all 16 real tickers that merely start with
            # X - XRP, XAG, XAU, XAUT, XCH, XCU, XPT, XPD, XLM, XTZ, XIAOMI, XPL.
            for pre in ISSUER_PREFIXES.get((venue, kind), ()):
                if key.startswith(pre) and len(key) > len(pre):
                    stem = key[len(pre):]
                    if stem in rwa and key not in declared_coin:
                        key, issuer = stem, pre
                        break

        ut = str(r.underlying_type or "").upper()
        cls = CLASS_MAP.get(ut, "rwa_unclassified" if key in rwa else "crypto")
        regions.append(EQUITY_REGION.get(ut, ""))
        keys.append(key); classes.append(cls); issuers.append(issuer)
        scales.append(scale); excl.append(ex); reasons.append(reason); conf.append(cf)

    df["asset_key"] = keys
    df["asset_class"] = classes
    df["equity_region"] = regions
    df["issuer"] = issuers
    df["scale_factor"] = scales
    df["is_excluded"] = excl
    df["exclude_reason"] = reasons
    df["id_confidence"] = conf

    # resolve class ONCE per asset: the most specific declaration across all venues
    # wins, so Bitget's generic RWA never overrides Binance's EQUITY/COMMODITY.
    spec_rank = df["asset_class"].map(SPECIFICITY).fillna(0)
    best = (df.assign(_r=spec_rank).sort_values("_r", ascending=False)
              .drop_duplicates("asset_key").set_index("asset_key"))
    df["asset_class"] = df["asset_key"].map(best["asset_class"])
    reg = best["equity_region"].replace("", np.nan)
    df["equity_region"] = df["asset_key"].map(reg).fillna("")
    # RWA is the superset: true for every non-crypto class
    df["is_rwa"] = df["asset_class"].isin(RWA_CLASSES).astype(int)
    # helper only - not in the instrument_ref schema, and leaving it in makes
    # ch_load.conform log a dropped-column warning on every run
    df = df.drop(columns=["base_native"], errors="ignore")
    return df


def split_internal(sym: str) -> tuple[str | None, str | None]:
    """'UBUSDT' -> ('UB','USDT'). Only our three quote ccys are considered, so the
    UB/UBER ambiguity cannot arise here (UBER-quoted pairs do not exist)."""
    for q in INTERNAL_QUOTES:
        if sym.endswith(q) and len(sym) > len(q):
            return sym[: -len(q)], q
    return None, None


def parse_internal_names(names: pd.Series) -> pd.DataFrame:
    """strat symbols_record names -> (venue, kind, base, quote)."""
    rows = []
    for n in names:
        parts = str(n).split("-", 2)
        if len(parts) != 3 or parts[0] not in ("BIN", "OKX", "BGT", "GAT", "KCN"):
            rows.append({"name": n, "venue": None, "kind": None,
                         "base": None, "quote": None})
            continue
        venue, k, sym = parts
        base, quote = split_internal(sym)
        rows.append({"name": n, "venue": venue, "kind": INTERNAL_KIND.get(k),
                     "base": base, "quote": quote})
    return pd.DataFrame(rows)


def map_internal(internal: pd.DataFrame, spec: pd.DataFrame) -> pd.DataFrame:
    """Attach the venue-native symbol + asset_key to each internal instrument.

    Search order, most specific first. Each fallback is recorded in `match_rule` so a
    loose match is never mistaken for an exact one.
    """
    idx: dict[tuple, dict] = {}
    for r in spec.itertuples():
        idx.setdefault((r.venue, r.kind, r.base_raw, r.quote), r._asdict())

    out = []
    for r in internal.itertuples():
        rec = {"name": r.name, "venue": r.venue, "kind": r.kind,
               "base": r.base, "quote": r.quote,
               "symbol": None, "asset_key": None, "asset_class": None,
               "match_rule": None}
        if not r.venue or not r.base:
            rec["match_rule"] = "unparsed_internal_name"
            out.append(rec); continue

        cands = [((r.venue, r.kind, r.base, r.quote), "exact")]
        # Gate perps drop the issuer suffix internally: GAT-P-CRCLUSDT -> CRCLX_USDT
        if r.venue == "GAT" and r.kind == "linear_perp":
            for suf in ("X", "ON", "G"):
                cands.append(((r.venue, r.kind, r.base + suf, r.quote),
                              f"gate_issuer_{suf}"))
        # Bitget USDC perps are <BASE>PERP with quoteCoin USDC
        if r.venue == "BGT" and r.kind == "linear_perp" and r.quote == "USDC":
            cands.append(((r.venue, r.kind, r.base, "USDC"), "bgt_usdc_perp"))
        # inverse perps quote USD internally; venues may declare USD or the base
        if r.kind == "inverse_perp":
            cands.append(((r.venue, r.kind, r.base, "USD"), "inverse_usd"))

        for keyt, rule in cands:
            hit = idx.get(keyt)
            if hit:
                rec.update(symbol=hit["symbol"], asset_key=hit["asset_key"],
                           asset_class=hit["asset_class"], match_rule=rule)
                break
        else:
            rec["match_rule"] = "UNMATCHED"
        out.append(rec)
    return pd.DataFrame(out)
