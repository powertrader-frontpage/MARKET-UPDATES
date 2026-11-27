"""Load and validate the MIT stock classification master.

This module is intentionally read-only: it does not alter rotation scores,
constituent returns, breadth calculations, or ranking logic.
"""
from pathlib import Path
from typing import Dict, List

import pandas as pd

REQUIRED_COLUMNS = [
    "Symbol", "Company", "NSE_Macro_Economic_Sector", "NSE_Sector",
    "NSE_Industry", "NSE_Basic_Industry", "MIT_Primary_Microsector",
    "MIT_Secondary_Tags", "Evidence_Source", "Evidence_URL", "Evidence_Type",
    "Verified_On", "Verification_Status", "Legacy_Microsector_Labels",
    "Legacy_Broad_Groups", "Review_Notes",
]


def load_classification_master(path: str | Path) -> pd.DataFrame:
    """Read the master CSV as strings and validate its schema and key fields."""
    csv_path = Path(path)
    if not csv_path.is_file():
        raise FileNotFoundError(f"Classification master not found: {csv_path}")

    frame = pd.read_csv(csv_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    missing_columns = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing_columns:
        raise ValueError("Classification master is missing required columns: " + ", ".join(missing_columns))

    frame = frame[REQUIRED_COLUMNS].copy()
    for column in REQUIRED_COLUMNS:
        frame[column] = frame[column].astype(str).str.strip()

    frame["Symbol"] = frame["Symbol"].str.upper()
    duplicate_symbols = sorted(frame.loc[frame["Symbol"].duplicated(keep=False), "Symbol"].unique())
    if duplicate_symbols:
        raise ValueError("Duplicate symbols in classification master: " + ", ".join(duplicate_symbols))

    if frame["Symbol"].eq("").any():
        raise ValueError("Classification master contains blank symbols.")

    verified = frame["Verification_Status"].str.upper().eq("VERIFIED")
    missing_primary = frame.loc[verified & frame["MIT_Primary_Microsector"].eq(""), "Symbol"].tolist()
    if missing_primary:
        raise ValueError("Verified symbols missing MIT_Primary_Microsector: " + ", ".join(missing_primary))

    return frame


def primary_microsector_map(frame: pd.DataFrame) -> Dict[str, str]:
    """Return one verified primary microsector per symbol, excluding unresolved rows."""
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("frame must be a pandas DataFrame")
    required = {"Symbol", "MIT_Primary_Microsector", "Verification_Status"}
    if not required.issubset(frame.columns):
        raise ValueError("frame does not contain the required classification columns")
    verified = frame["Verification_Status"].astype(str).str.upper().eq("VERIFIED")
    selected = frame.loc[verified, ["Symbol", "MIT_Primary_Microsector"]]
    return dict(zip(selected["Symbol"].astype(str).str.upper(), selected["MIT_Primary_Microsector"].astype(str)))


def primary_microsector_baskets(frame: pd.DataFrame, suffix: str = ".NS") -> Dict[str, List[str]]:
    """Return exclusive primary-microsector ticker baskets for rotation calculations."""
    mapping = primary_microsector_map(frame)
    baskets: Dict[str, List[str]] = {}
    for symbol, microsector in mapping.items():
        if not symbol or not microsector:
            continue
        baskets.setdefault(microsector, []).append(f"{symbol}{suffix}")
    return {name: sorted(symbols) for name, symbols in sorted(baskets.items())}
