"""Evidence-first stock classification registry for Market Intelligence Terminal."""
from __future__ import annotations
from pathlib import Path
import pandas as pd
import streamlit as st

CLASSIFICATION_PATH = Path(__file__).resolve().parent / "data" / "stock_classification_master.csv"
REQUIRED_COLUMNS = ["Symbol","Company","NSE_Macro_Economic_Sector","NSE_Sector","NSE_Industry","NSE_Basic_Industry","MIT_Primary_Microsector","MIT_Secondary_Tags","Evidence_Source","Evidence_URL","Evidence_Type","Verified_On","Verification_Status","Legacy_Microsector_Labels","Legacy_Broad_Groups","Review_Notes"]
APPROVED_STATUS = "VERIFIED"

def _normalise_symbol(value: object) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip().upper().removesuffix(".NS")

@st.cache_data(ttl=300, show_spinner=False)
def load_classification_master() -> pd.DataFrame:
    """Load reviewed classifications; fail closed on schema or duplicate-symbol errors."""
    if not CLASSIFICATION_PATH.exists():
        raise FileNotFoundError(f"Classification master not found: {CLASSIFICATION_PATH}")
    frame = pd.read_csv(CLASSIFICATION_PATH, dtype=str).fillna("")
    missing = [c for c in REQUIRED_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError("Classification master missing columns: " + ", ".join(missing))
    frame["Symbol"] = frame["Symbol"].map(_normalise_symbol)
    frame = frame[frame["Symbol"].ne("")]
    if frame["Symbol"].duplicated().any():
        duplicates = sorted(frame.loc[frame["Symbol"].duplicated(False), "Symbol"].unique())
        raise ValueError("Duplicate symbols in classification master: " + ", ".join(duplicates[:25]))
    frame["Verification_Status"] = frame["Verification_Status"].str.strip().str.upper()
    return frame[REQUIRED_COLUMNS].copy()

def build_classified_universe(constituents: pd.DataFrame) -> pd.DataFrame:
    """Join official constituent data; expose MIT labels only after explicit verification."""
    if constituents is None or constituents.empty or "Symbol" not in constituents:
        raise ValueError("Official constituent universe is empty or lacks Symbol.")
    universe = constituents.copy()
    universe["Symbol"] = universe["Symbol"].map(_normalise_symbol)
    master = load_classification_master()
    merged = universe.merge(master, on="Symbol", how="left", suffixes=("", "_master"), validate="one_to_one")
    # Carry the official constituent feed's available classification fields into
    # the corresponding master columns without inventing the deeper NSE levels.
    if "Company" in universe.columns:
        merged["Company"] = merged["Company"].fillna("").replace("", pd.NA).fillna(merged.get("Company_master", ""))
    if "Sector" in universe.columns:
        merged["NSE_Sector"] = merged.get("NSE_Sector", pd.Series("", index=merged.index)).fillna("").replace("", pd.NA).fillna(merged["Sector"])
    if "Industry" in universe.columns:
        merged["NSE_Industry"] = merged.get("NSE_Industry", pd.Series("", index=merged.index)).fillna("").replace("", pd.NA).fillna(merged["Industry"])
    for column in REQUIRED_COLUMNS:
        if column != "Symbol":
            if column not in merged:
                merged[column] = ""
            merged[column] = merged[column].fillna("").astype(str)
    approved = merged["Verification_Status"].str.upper().eq(APPROVED_STATUS) & merged["MIT_Primary_Microsector"].str.strip().ne("")
    merged.loc[~approved, ["MIT_Primary_Microsector", "MIT_Secondary_Tags"]] = ""
    merged.loc[~approved, "Verification_Status"] = "PENDING_VERIFICATION"
    merged.loc[approved, "Verification_Status"] = APPROVED_STATUS
    return merged

def classification_audit(universe: pd.DataFrame) -> dict:
    """Coverage and duplicate checks; never infer a missing microsector."""
    if universe is None or universe.empty:
        return {"universe_rows": 0, "verified": 0, "pending": 0, "duplicate_symbols": [], "unmapped": []}
    symbols = universe["Symbol"].map(_normalise_symbol)
    status = universe.get("Verification_Status", pd.Series("", index=universe.index)).astype(str).str.upper()
    primary = universe.get("MIT_Primary_Microsector", pd.Series("", index=universe.index)).fillna("").astype(str).str.strip()
    verified = status.eq(APPROVED_STATUS) & primary.ne("")
    return {"universe_rows": int(len(universe)), "verified": int(verified.sum()),
            "pending": int((~verified).sum()),
            "duplicate_symbols": sorted(symbols[symbols.duplicated(False)].unique().tolist()),
            "unmapped": sorted(symbols[~verified].unique().tolist())}
