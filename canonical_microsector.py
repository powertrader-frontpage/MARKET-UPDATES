"""Canonical 500-stock Sector/Microsector rotation universe for Market Terminal.

The Nifty 500 mapping is the sole source for rotation grouping. The detailed
stock-classification master is joined only for descriptive stock metadata.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

from classification_master import load_classification_master

ROOT = Path(__file__).resolve().parent
ROTATION_PATH = ROOT / "data" / "nse500_sector_microsector_master.csv"
DETAIL_PATH = ROOT / "data" / "stock_classification_master.csv"

DETAIL_COLUMNS = [
    "Symbol", "MIT_Primary_Microsector", "MIT_Secondary_Tags",
    "Evidence_Source", "Evidence_URL", "Evidence_Type", "Verified_On",
    "Verification_Status", "Review_Notes",
]


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
    """Classify a stock against its benchmark using the existing 1M/3M rules."""
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
    """Return Sector-parented Microsector baskets, diagnostics and joined stock details."""
    if not ROTATION_PATH.is_file() or not DETAIL_PATH.is_file():
        raise FileNotFoundError("Nifty 500 rotation mapping or stock detail master is missing.")

    rotation = pd.read_csv(ROTATION_PATH, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    required = {"Symbol", "Company", "Sector", "Microsector", "Assignment_Status"}
    missing = required.difference(rotation.columns)
    if missing:
        raise ValueError("Nifty 500 rotation mapping is missing columns: " + ", ".join(sorted(missing)))

    for column in required:
        rotation[column] = rotation[column].astype(str).str.strip()
    rotation["Symbol"] = rotation["Symbol"].str.upper()
    duplicate_symbols = sorted(rotation.loc[rotation["Symbol"].duplicated(keep=False), "Symbol"].unique())
    if duplicate_symbols:
        raise ValueError("Duplicate NSE symbols in Nifty 500 rotation mapping: " + ", ".join(duplicate_symbols))
    if len(rotation) != 500 or rotation["Symbol"].eq("").any():
        raise ValueError("Rotation mapping must contain exactly 500 unique, nonblank NSE symbols.")
    for column in ("Company", "Sector", "Microsector"):
        if rotation[column].eq("").any():
            raise ValueError(f"Rotation mapping contains blank {column} values.")

    sector_counts = rotation.groupby("Microsector")["Sector"].nunique()
    conflicting = sorted(sector_counts[sector_counts.ne(1)].index.tolist())
    if conflicting:
        raise ValueError("A Microsector maps to multiple Sectors: " + ", ".join(conflicting))

    details = load_classification_master(DETAIL_PATH)
    detail_fields = [column for column in DETAIL_COLUMNS if column in details.columns]
    joined = rotation.merge(
        details[detail_fields], on="Symbol", how="left",
        validate="one_to_one", indicator=True
    )
    detail_match = joined["_merge"].eq("both")
    joined["Stock_Detail_Match"] = detail_match
    joined = joined.drop(columns=["_merge"])
    joined["Yahoo_Symbol"] = joined["Symbol"] + ".NS"
    # Compatibility fields: consumers still use this canonical label name,
    # while the displayed parent group is now the actual Sector.
    joined["Canonical_Microsector"] = joined["Microsector"]
    joined["Reference_Group"] = joined["Sector"]

    baskets = {}
    for microsector, rows in joined.groupby("Microsector", sort=True):
        sector_values = sorted(rows["Sector"].unique().tolist())
        if len(sector_values) != 1:
            raise ValueError(f"Microsector {microsector!r} does not have exactly one parent Sector.")
        tickers = sorted(set(rows["Yahoo_Symbol"].tolist()))
        baskets[str(microsector)] = (sector_values[0], tickers)

    sizes = joined.groupby("Microsector").size()
    status_counts = joined["Assignment_Status"].value_counts().to_dict()
    metadata = {
        "master_stocks": int(len(joined)),
        "eligible_price_stocks": int(len(joined)),
        "sectors": int(joined["Sector"].nunique()),
        "canonical_microsectors": int(joined["Microsector"].nunique()),
        "microsectors_with_price_coverage": 0,
        "single_stock_microsectors": int((sizes == 1).sum()),
        "two_stock_microsectors": int((sizes == 2).sum()),
        "microsectors_with_3plus_stocks": int((sizes >= 3).sum()),
        "microsectors_without_price_coverage": 0,
        "detail_master_matches": int(detail_match.sum()),
        "rotation_only_stocks": int((~detail_match).sum()),
        "detail_master_only_stocks": int(len(details) - detail_match.sum()),
        "assignment_status_counts": status_counts,
        "draft_assignments": int(joined["Assignment_Status"].str.casefold().eq("draft - verify").sum()),
        "price_symbols_are_provisional": True,
    }
    return baskets, metadata, joined


@lru_cache(maxsize=1)
def canonical_microsector_lookup():
    """Map each Nifty 500 NSE symbol and its direct Yahoo ticker to one Microsector."""
    _, _, joined = load_canonical_microsectors()
    lookup = dict(zip(joined["Symbol"], joined["Microsector"]))
    lookup.update({symbol + ".NS": micro for symbol, micro in lookup.copy().items()})
    return lookup


@lru_cache(maxsize=1)
def canonical_sector_lookup():
    """Map each Nifty 500 NSE symbol and its direct Yahoo ticker to one Sector."""
    _, _, joined = load_canonical_microsectors()
    lookup = dict(zip(joined["Symbol"], joined["Sector"]))
    lookup.update({symbol + ".NS": sector for symbol, sector in lookup.copy().items()})
    return lookup


@lru_cache(maxsize=1)
def canonical_classification_lookup():
    """Return the shared Sector, Microsector and assignment status for each stock."""
    _, _, joined = load_canonical_microsectors()
    return {
        row.Symbol: {
            "Sector": row.Sector,
            "Microsector": row.Microsector,
            "Assignment_Status": row.Assignment_Status,
        }
        for row in joined.itertuples(index=False)
    }


@lru_cache(maxsize=1)
def canonical_price_ticker_lookup():
    """Return direct NSE-to-Yahoo symbol mappings for the 500-stock trial universe."""
    _, _, joined = load_canonical_microsectors()
    return dict(zip(joined["Symbol"], joined["Yahoo_Symbol"]))


def map_to_canonical_price_ticker(symbol: str) -> str:
    """Map a stock to its current trial-universe Yahoo ticker."""
    value = str(symbol).strip().upper()
    if not value:
        return value
    if value.startswith("^"):
        return value
    base = value[:-3] if value.endswith(".NS") else value
    return canonical_price_ticker_lookup().get(base, base + ".NS")
