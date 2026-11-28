"""Canonical primary microsector loader for Market Intelligence Terminal.

The classification master is the source of truth for primary microsector membership.
Legacy broad groups are exposed only as reference labels; they are not canonical
NSE sectors. Official NSE hierarchy columns are currently blank in the supplied
422-stock master and must not be inferred from legacy labels.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd


ROOT = Path(__file__).resolve().parent
MASTER_PATH = ROOT / "data" / "stock_classification_master.csv"
COVERAGE_PATH = ROOT / "data" / "nse_ohlcv_422_symbol_coverage_2026-11-28.csv"

# The GFL ticker in the historical coverage file points to FLUOROCHEM.NS.
# GFL is therefore withheld from price-based baskets until its security identity
# and ticker are independently resolved. Its master classification is retained.
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
    """Return canonical microsector baskets, validated metadata and the joined master."""
    if not MASTER_PATH.exists() or not COVERAGE_PATH.exists():
        raise FileNotFoundError(
            "Canonical classification data files are missing from the repository data/ folder."
        )

    master = pd.read_csv(MASTER_PATH, dtype=str, keep_default_na=False)
    coverage = pd.read_csv(COVERAGE_PATH, dtype=str, keep_default_na=False)

    required_master = {
        "Symbol", "Company", "MIT_Primary_Microsector",
        "Verification_Status", "Legacy_Broad_Groups",
        "NSE_Macro_Economic_Sector", "NSE_Sector",
        "NSE_Industry", "NSE_Basic_Industry",
    }
    required_coverage = {"Symbol", "Yahoo_Symbol", "OHLCV_Mapping_Status"}
    missing_master = required_master.difference(master.columns)
    missing_coverage = required_coverage.difference(coverage.columns)
    if missing_master or missing_coverage:
        raise ValueError(
            "Canonical data schema mismatch. "
            f"Master missing: {sorted(missing_master)}; "
            f"coverage missing: {sorted(missing_coverage)}"
        )

    for frame, label in ((master, "classification master"), (coverage, "ticker coverage")):
        frame["Symbol"] = frame["Symbol"].astype(str).str.strip().str.upper()
        if frame["Symbol"].eq("").any() or frame["Symbol"].duplicated().any():
            raise ValueError(f"{label} contains blank or duplicate stock symbols.")

    if len(master) != 422 or len(coverage) != 422:
        raise ValueError(
            f"Expected 422 master rows and 422 coverage rows; found "
            f"{len(master)} and {len(coverage)}."
        )
    if not master["Verification_Status"].str.upper().eq("VERIFIED").all():
        raise ValueError("The classification master contains unverified stock assignments.")
    if master["MIT_Primary_Microsector"].str.strip().eq("").any():
        raise ValueError("At least one stock has no primary microsector assignment.")

    joined = master.merge(
        coverage[["Symbol", "Yahoo_Symbol", "OHLCV_Mapping_Status"]],
        on="Symbol",
        how="left",
        validate="one_to_one",
        indicator=True,
    )
    if not joined["_merge"].eq("both").all():
        missing = joined.loc[joined["_merge"] != "both", "Symbol"].tolist()
        raise ValueError(f"Ticker coverage is missing master symbols: {missing}")
    joined = joined.drop(columns=["_merge"])
    joined["Yahoo_Symbol"] = joined["Yahoo_Symbol"].astype(str).str.strip()

    # Keep a reference label for continuity with existing dashboard summaries.
    # It is explicitly not treated as an approved canonical sector hierarchy.
    joined["Reference_Group"] = joined["Legacy_Broad_Groups"].apply(
        lambda value: str(value).split("|")[0].strip() if str(value).strip() else "Unassigned"
    )

    mapped = joined[
        joined["OHLCV_Mapping_Status"].str.upper().eq("MAPPED")
        & joined["Yahoo_Symbol"].ne("")
    ].copy()
    duplicate_mask = mapped["Yahoo_Symbol"].duplicated(keep=False)
    duplicate_ticker_rows = mapped.loc[duplicate_mask, ["Symbol", "Yahoo_Symbol"]]
    duplicate_ticker_symbols = sorted(duplicate_ticker_rows["Symbol"].unique().tolist())
    excluded_symbols = set(UNRESOLVED_PRICE_SYMBOLS) | set(duplicate_ticker_symbols)

    # Never count one Yahoo price series twice under two different NSE master symbols.
    # Keep those classification rows in the joined master but omit them from price baskets.
    eligible = mapped[~mapped["Symbol"].isin(excluded_symbols)].copy()

    baskets = {}
    for microsector, rows in eligible.groupby("MIT_Primary_Microsector", sort=True):
        symbols = sorted(rows["Yahoo_Symbol"].tolist())
        if symbols:
            reference_groups = sorted(
                value for value in rows["Reference_Group"].dropna().unique()
                if str(value).strip()
            )
            reference_group = reference_groups[0] if len(reference_groups) == 1 else "Mixed / legacy"
            baskets[str(microsector)] = (reference_group, symbols)

    basket_sizes = [len(symbols) for _, symbols in baskets.values()]
    single_stock_baskets = sum(size == 1 for size in basket_sizes)
    three_plus_stock_baskets = sum(size >= 3 for size in basket_sizes)

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
        "master_stocks": int(len(master)),
        "eligible_price_stocks": int(len(eligible)),
        "unresolved_price_symbols": sorted(UNRESOLVED_PRICE_SYMBOLS.intersection(set(joined["Symbol"]))),
        "duplicate_ticker_symbols": duplicate_ticker_symbols,
        "excluded_price_symbols": sorted(excluded_symbols.intersection(set(joined["Symbol"]))),
        "canonical_microsectors": int(master["MIT_Primary_Microsector"].nunique()),
        "microsectors_with_price_coverage": int(len(baskets)),
        "single_stock_microsectors": int(single_stock_baskets),
        "microsectors_with_3plus_stocks": int(three_plus_stock_baskets),
        "nse_hierarchy_populated_cells": hierarchy_populated,
        "nse_hierarchy_total_cells": int(len(joined) * len(hierarchy_columns)),
        "excluded_unresolved_count": int(len(excluded_symbols.intersection(set(joined["Symbol"])))),
    }
    return baskets, metadata, joined
