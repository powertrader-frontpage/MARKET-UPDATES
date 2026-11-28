"""Validated master-driven microsector runtime for shadow migration.

This module centralizes the canonical classification consumed by dashboard
sections. It is deliberately independent of pricing, scoring, breadth, zones,
and ranking calculations. Callers can use it in shadow mode before switching
any live consumer.
"""
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd

from classification_master import load_classification_master, primary_microsector_map
from canonical_microsectors import (
    load_microsector_alias_map,
    build_canonical_classification,
    validate_alias_symbol_coverage,
    canonical_microsector_baskets,
)

ROOT = Path(__file__).resolve().parent
MASTER_PATH = ROOT / "data" / "stock_classification_master.csv"
ALIAS_PATH = ROOT / "data" / "microsector_canonical_alias_map.csv"


def load_canonical_runtime(
    master_path: str | Path = MASTER_PATH,
    alias_path: str | Path = ALIAS_PATH,
    suffix: str = ".NS",
) -> Tuple[pd.DataFrame, Dict[str, str], Dict[str, List[str]], dict]:
    """Load, validate and return one canonical primary microsector per symbol.

    Raises on invalid master/alias data. It never guesses classifications or
    modifies source rows. NSE hierarchy completeness is reported separately
    because MIT microsector verification does not imply NSE hierarchy review.
    """
    master = load_classification_master(master_path)
    aliases = load_microsector_alias_map(alias_path)
    alias_errors = validate_alias_symbol_coverage(master, aliases)
    if alias_errors:
        raise ValueError("Alias symbol coverage failed: " + " | ".join(alias_errors))

    canonical = build_canonical_classification(master, aliases)
    mapping = dict(zip(canonical["Symbol"], canonical["Canonical_Microsector"]))
    if len(mapping) != len(canonical):
        raise ValueError("Canonical runtime must contain exactly one row per symbol.")

    baskets = canonical_microsector_baskets(canonical, suffix=suffix)
    basket_symbols = [ticker.removesuffix(suffix) for members in baskets.values() for ticker in members]
    if len(basket_symbols) != len(set(basket_symbols)):
        raise ValueError("A stock appears in more than one canonical primary basket.")
    if set(basket_symbols) != set(mapping):
        raise ValueError("Canonical basket symbols do not exactly match verified master symbols.")

    nse_fields = [
        "NSE_Macro_Economic_Sector", "NSE_Sector",
        "NSE_Industry", "NSE_Basic_Industry",
    ]
    nse_missing = {
        field: int(master[field].astype(str).str.strip().eq("").sum())
        for field in nse_fields
    }
    audit = {
        "master_rows": int(len(master)),
        "verified_rows": int(len(canonical)),
        "canonical_microsectors": int(canonical["Canonical_Microsector"].nunique()),
        "primary_memberships": int(len(basket_symbols)),
        "duplicate_primary_memberships": 0,
        "alias_rows": int(len(aliases)),
        "alias_coverage_errors": 0,
        "nse_hierarchy_blank_by_field": nse_missing,
        "nse_hierarchy_complete": all(value == 0 for value in nse_missing.values()),
    }
    return canonical, mapping, baskets, audit
