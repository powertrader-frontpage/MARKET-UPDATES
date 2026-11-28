"""Explicit, audit-only mapping from legacy master symbols to current price-feed symbols."""
from __future__ import annotations

from pathlib import Path
import pandas as pd


def load_price_symbol_aliases(path: str | Path) -> dict[str, str]:
    frame = pd.read_csv(path, dtype=str).fillna("")
    required = {"Master_Symbol", "Price_Symbol", "Review_Status"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Price-symbol alias map is missing columns: {', '.join(sorted(missing))}")
    frame["Master_Symbol"] = frame["Master_Symbol"].str.strip().str.upper()
    frame["Price_Symbol"] = frame["Price_Symbol"].str.strip().str.upper()
    frame["Review_Status"] = frame["Review_Status"].str.strip().str.upper()
    if frame["Master_Symbol"].eq("").any() or frame["Price_Symbol"].eq("").any():
        raise ValueError("Price-symbol alias map contains blank symbols.")
    if frame["Master_Symbol"].duplicated().any():
        duplicates = sorted(frame.loc[frame["Master_Symbol"].duplicated(False), "Master_Symbol"].unique())
        raise ValueError(f"Duplicate master symbols in price-symbol alias map: {duplicates}")
    if not frame["Review_Status"].eq("VERIFIED").all():
        raise ValueError("Only VERIFIED rows may be used for price-symbol mapping.")
    aliases = dict(zip(frame["Master_Symbol"], frame["Price_Symbol"]))
    if len(set(aliases.values())) != len(aliases):
        raise ValueError("Price-symbol alias map is not one-to-one; resolve collisions before ranking.")
    return aliases


def map_price_symbol(symbol: str, aliases: dict[str, str]) -> str:
    """Map a master symbol to its current price-feed symbol, preserving .NS."""
    value = str(symbol).strip().upper()
    suffix = ".NS" if value.endswith(".NS") else ""
    base = value[:-3] if suffix else value
    return aliases.get(base, base) + suffix
