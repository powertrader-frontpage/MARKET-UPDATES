"""Canonical microsector utilities for the MIT classification master.

The alias map is a presentation/taxonomy layer only. It never changes the
verified stock master and does not alter live rotation calculations.
"""
from pathlib import Path
from typing import Dict, Iterable

import pandas as pd

ALIAS_COLUMNS = [
    "Alias_Microsector", "Canonical_Microsector", "Affected_Symbols",
    "Candidate_ID", "Rationale", "Approval_Status",
]


def load_microsector_alias_map(path: str | Path) -> pd.DataFrame:
    """Load approved aliases and validate one-to-one alias keys."""
    csv_path = Path(path)
    if not csv_path.is_file():
        raise FileNotFoundError(f"Microsector alias map not found: {csv_path}")
    aliases = pd.read_csv(csv_path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    missing = [c for c in ALIAS_COLUMNS if c not in aliases.columns]
    if missing:
        raise ValueError("Alias map is missing required columns: " + ", ".join(missing))
    aliases = aliases[ALIAS_COLUMNS].copy()
    for col in ALIAS_COLUMNS:
        aliases[col] = aliases[col].astype(str).str.strip()
    aliases["Alias_Microsector"] = aliases["Alias_Microsector"].str.casefold()
    aliases["Approval_Status"] = aliases["Approval_Status"].str.upper()
    invalid = aliases[
        aliases["Alias_Microsector"].eq("")
        | aliases["Canonical_Microsector"].eq("")
        | ~aliases["Approval_Status"].eq("APPROVED_FOR_MAPPING_ONLY")
    ]
    if not invalid.empty:
        raise ValueError("Alias map contains blank or non-approved mapping rows.")
    duplicates = aliases.loc[aliases["Alias_Microsector"].duplicated(keep=False), "Alias_Microsector"].tolist()
    if duplicates:
        raise ValueError("Duplicate alias keys: " + ", ".join(sorted(set(duplicates))))
    return aliases


def canonicalize_microsector(label: str, aliases: pd.DataFrame) -> str:
    """Return approved canonical label, otherwise preserve original label."""
    key = str(label).strip().casefold()
    if not key:
        return ""
    mapping = dict(zip(aliases["Alias_Microsector"], aliases["Canonical_Microsector"]))
    return mapping.get(key, str(label).strip())


def build_canonical_classification(master: pd.DataFrame, aliases: pd.DataFrame) -> pd.DataFrame:
    """Return verified stock rows with an additional canonical label column."""
    required = {"Symbol", "MIT_Primary_Microsector", "Verification_Status"}
    missing = required.difference(master.columns)
    if missing:
        raise ValueError("Master is missing columns: " + ", ".join(sorted(missing)))
    out = master.copy()
    verified = out["Verification_Status"].astype(str).str.upper().eq("VERIFIED")
    out = out.loc[verified].copy()
    out["Canonical_Microsector"] = out["MIT_Primary_Microsector"].map(
        lambda value: canonicalize_microsector(value, aliases)
    )
    if out["Symbol"].duplicated().any():
        raise ValueError("Verified master contains duplicate symbols.")
    if out["Canonical_Microsector"].eq("").any():
        raise ValueError("Verified master contains blank canonical microsectors.")
    return out


def validate_alias_symbol_coverage(master: pd.DataFrame, aliases: pd.DataFrame) -> list[str]:
    """Check each alias's declared symbols exactly match master rows using that label."""
    verified = master[master["Verification_Status"].astype(str).str.upper().eq("VERIFIED")]
    label_by_symbol = dict(zip(verified["Symbol"].astype(str).str.upper(), verified["MIT_Primary_Microsector"].astype(str)))
    errors = []
    for row in aliases.to_dict("records"):
        alias_label = row["Alias_Microsector"]
        declared = {s.strip().upper() for s in row["Affected_Symbols"].split(";") if s.strip()}
        actual = {symbol for symbol, label in label_by_symbol.items() if label.strip().casefold() == alias_label}
        if declared != actual:
            errors.append(
                f"{row['Candidate_ID']} {alias_label}: declared_only={sorted(declared-actual)}; "
                f"master_only={sorted(actual-declared)}"
            )
    return errors


def canonical_microsector_baskets(classification: pd.DataFrame, suffix: str = ".NS") -> Dict[str, list[str]]:
    """Build exclusive canonical baskets: one verified primary microsector per stock."""
    baskets: Dict[str, list[str]] = {}
    for row in classification[["Symbol", "Canonical_Microsector"]].itertuples(index=False):
        symbol, label = str(row.Symbol).strip().upper(), str(row.Canonical_Microsector).strip()
        if symbol and label:
            baskets.setdefault(label, []).append(f"{symbol}{suffix}")
    return {label: sorted(set(symbols)) for label, symbols in sorted(baskets.items())}
