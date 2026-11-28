"""Canonical primary microsector runtime for Market Intelligence Terminal.

Uses the audited Phase 1 alias map to consolidate verified master labels into
278 canonical peer groups. The master remains the stock-level source of truth;
the alias layer only consolidates approved label variants.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

from canonical_microsector_runtime import load_canonical_runtime
from price_symbol_aliases import load_price_symbol_aliases

ROOT = Path(__file__).resolve().parent
MASTER_PATH = ROOT / "data" / "stock_classification_master.csv"
COVERAGE_PATH = ROOT / "data" / "nse_ohlcv_422_symbol_coverage_2026-11-28.csv"
PRICE_ALIAS_PATH = ROOT / "data" / "price_symbol_alias_map.csv"

# These exceptions are retained in the verified classification master but not
# used in live price baskets until their security identity/history is resolved.
UNRESOLVED_PRICE_SYMBOLS = {"GFL"}


def rotation_zone_from_relative_strength(rel1: float, rel3: float) -> str:
    """Apply the dashboard's existing four-quadrant relative-strength thresholds."""
    if pd.isna(rel1) or pd.isna(rel3):
        return "Unclassified"
    return (
        "Leading" if rel3 >= 0 and rel1 >= 0 else
        "Weakening" if rel3 >= 0 else
        "Improving" if rel1 >= 0 else
        "Lagging"
    )


def relative_rotation_zone(aligned: pd.DataFrame, position: int | None = None) -> str | None:
    """Classify a stock against its benchmark using the dashboard's existing 1M/3M rules."""
    if aligned is None or aligned.empty:
        return None
    current = aligned if position is None else aligned.iloc[:position + 1]
    if len(current) < 22:
        return None
    rel1 = (
        (current["stock"].iloc[-1] / current["stock"].iloc[-22] - 1) * 100
        - (current["bench"].iloc[-1] / current["bench"].iloc[-22] - 1) * 100
    )
    rel3 = (
        (current["stock"].iloc[-1] / current["stock"].iloc[-min(64, len(current))] - 1) * 100
        - (current["bench"].iloc[-1] / current["bench"].iloc[-min(64, len(current))] - 1) * 100
    )
    return rotation_zone_from_relative_strength(rel1, rel3)


@lru_cache(maxsize=1)
def load_canonical_microsectors():
    """Return validated canonical baskets, diagnostics and the joined stock master."""
    if not MASTER_PATH.is_file() or not COVERAGE_PATH.is_file():
        raise FileNotFoundError("Canonical master or audited ticker coverage file is missing.")

    canonical, symbol_to_canonical, canonical_baskets, audit = load_canonical_runtime(
        master_path=MASTER_PATH
    )
    coverage = pd.read_csv(COVERAGE_PATH, dtype=str, keep_default_na=False)
    required_coverage = {"Symbol", "Yahoo_Symbol", "OHLCV_Mapping_Status"}
    missing = required_coverage.difference(coverage.columns)
    if missing:
        raise ValueError(f"Ticker coverage missing required columns: {sorted(missing)}")

    coverage["Symbol"] = coverage["Symbol"].astype(str).str.strip().str.upper()
    if len(coverage) != 422 or coverage["Symbol"].eq("").any() or coverage["Symbol"].duplicated().any():
        raise ValueError("Ticker coverage must contain exactly 422 unique nonblank master symbols.")

    joined = canonical.merge(
        coverage[["Symbol", "Yahoo_Symbol", "OHLCV_Mapping_Status"]],
        on="Symbol", how="left", validate="one_to_one", indicator=True
    )
    if not joined["_merge"].eq("both").all():
        missing_symbols = joined.loc[joined["_merge"] != "both", "Symbol"].tolist()
        raise ValueError(f"Ticker coverage missing canonical master symbols: {missing_symbols}")
    joined = joined.drop(columns=["_merge"])
    joined["Yahoo_Symbol"] = joined["Yahoo_Symbol"].astype(str).str.strip().str.upper()
    joined["Reference_Group"] = joined["Legacy_Broad_Groups"].apply(
        lambda value: str(value).split("|")[0].strip() if str(value).strip() else "Unassigned"
    )

    price_aliases = load_price_symbol_aliases(PRICE_ALIAS_PATH)
    alias_mismatches = []
    for master_symbol, price_symbol in price_aliases.items():
        rows = joined.loc[joined["Symbol"].eq(master_symbol), "Yahoo_Symbol"]
        expected = price_symbol + ".NS"
        if len(rows) != 1 or rows.iloc[0] != expected:
            alias_mismatches.append(
                f"{master_symbol}: expected {expected}, coverage has {rows.iloc[0] if len(rows) else 'MISSING'}"
            )
    if alias_mismatches:
        raise ValueError("Verified price-symbol alias mismatch: " + " | ".join(alias_mismatches))

    mapped = joined[
        joined["OHLCV_Mapping_Status"].str.upper().eq("MAPPED")
        & joined["Yahoo_Symbol"].ne("")
    ].copy()
    duplicate_mask = mapped["Yahoo_Symbol"].duplicated(keep=False)
    duplicate_symbols = sorted(mapped.loc[duplicate_mask, "Symbol"].unique().tolist())
    excluded_symbols = set(UNRESOLVED_PRICE_SYMBOLS) | set(duplicate_symbols)
    eligible = mapped[~mapped["Symbol"].isin(excluded_symbols)].copy()

    baskets = {}
    for microsector, rows in eligible.groupby("Canonical_Microsector", sort=True):
        tickers = sorted(rows["Yahoo_Symbol"].tolist())
        if tickers:
            reference_groups = sorted(
                value for value in rows["Reference_Group"].dropna().unique()
                if str(value).strip()
            )
            reference_group = reference_groups[0] if len(reference_groups) == 1 else "Mixed / legacy"
            baskets[str(microsector)] = (reference_group, tickers)

    basket_sizes = [len(tickers) for _, tickers in baskets.values()]
    hierarchy_columns = [
        "NSE_Macro_Economic_Sector", "NSE_Sector",
        "NSE_Industry", "NSE_Basic_Industry",
    ]
    hierarchy_populated = int(
        joined[hierarchy_columns].apply(
            lambda column: column.astype(str).str.strip().ne("")
        ).sum().sum()
    )
    metadata = {
        "master_stocks": int(len(canonical)),
        "eligible_price_stocks": int(len(eligible)),
        "unresolved_price_symbols": sorted(UNRESOLVED_PRICE_SYMBOLS.intersection(set(joined["Symbol"]))),
        "duplicate_ticker_symbols": duplicate_symbols,
        "excluded_price_symbols": sorted(excluded_symbols.intersection(set(joined["Symbol"]))),
        "canonical_microsectors": int(canonical["Canonical_Microsector"].nunique()),
        "microsectors_with_price_coverage": int(len(baskets)),
        "single_stock_microsectors": int(sum(size == 1 for size in basket_sizes)),
        "microsectors_with_3plus_stocks": int(sum(size >= 3 for size in basket_sizes)),
        "nse_hierarchy_populated_cells": hierarchy_populated,
        "nse_hierarchy_total_cells": int(len(joined) * len(hierarchy_columns)),
        "alias_rows": int(audit["alias_rows"]),
        "alias_coverage_errors": int(audit["alias_coverage_errors"]),
        "verified_price_aliases": int(len(price_aliases)),
    }
    return baskets, metadata, joined


@lru_cache(maxsize=1)
def canonical_price_ticker_lookup():
    """Map only unambiguous master symbols to audited Yahoo price tickers."""
    _, metadata, joined = load_canonical_microsectors()
    excluded = set(metadata["excluded_price_symbols"])
    return {
        str(row.Symbol).upper(): str(row.Yahoo_Symbol).upper()
        for row in joined.itertuples(index=False)
        if str(row.Symbol).upper() not in excluded
        and str(row.OHLCV_Mapping_Status).upper() == "MAPPED"
        and str(row.Yahoo_Symbol).strip()
    }


def map_to_canonical_price_ticker(symbol: str) -> str:
    """Return the audited Yahoo ticker for a master symbol, or preserve unknown symbols."""
    value = str(symbol).strip().upper()
    if value.startswith("^"):
        return value
    suffix = ".NS" if value.endswith(".NS") else ""
    base = value[:-3] if suffix else value
    mapped = canonical_price_ticker_lookup().get(base, base + ".NS")
    return mapped if mapped.endswith(".NS") or mapped.startswith("^") else mapped + ".NS"


@lru_cache(maxsize=1)
def canonical_microsector_lookup():
    """Map master symbols and unique current price symbols to one canonical group."""
    _, metadata, joined = load_canonical_microsectors()
    lookup = dict(zip(
        joined["Symbol"].astype(str).str.upper(),
        joined["Canonical_Microsector"].astype(str)
    ))
    excluded = set(metadata["excluded_price_symbols"])
    # Add current Yahoo symbols only when the crosswalk is one-to-one and eligible.
    eligible = joined[
        ~joined["Symbol"].isin(excluded)
        & joined["OHLCV_Mapping_Status"].str.upper().eq("MAPPED")
        & joined["Yahoo_Symbol"].ne("")
    ]
    for row in eligible.itertuples(index=False):
        ticker = str(row.Yahoo_Symbol).upper()
        if ticker.endswith(".NS"):
            lookup[ticker[:-3]] = str(row.Canonical_Microsector)
    price_aliases = load_price_symbol_aliases(PRICE_ALIAS_PATH)
    for master_symbol, price_symbol in price_aliases.items():
        if master_symbol in lookup:
            lookup[price_symbol] = lookup[master_symbol]
    return lookup
