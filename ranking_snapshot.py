"""Deterministic input snapshots for microsector ranking-invariance audits.

This module is deliberately independent of Streamlit and ranking formulas. It
stores the exact daily OHLCV inputs (including the benchmark) used by the
microsector rotation calculation, with stable ordering and a SHA-256 digest.
"""
from __future__ import annotations

import base64
import hashlib
import json
import zlib
from datetime import datetime, timezone
from typing import Mapping

import pandas as pd

REQUIRED_COLUMNS = ("Date", "Symbol", "Open", "High", "Low", "Close", "Volume")
SCHEMA_VERSION = 1


def build_ranking_snapshot(frame: pd.DataFrame, *, as_of: str, source: str,
                           benchmark_symbol: str = "^CRSLDX") -> dict:
    """Validate and pack a complete OHLCV frame into a deterministic snapshot."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("Price history is empty; refusing to create an audit snapshot.")
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"Price history is missing columns: {', '.join(missing)}")
    data = frame.loc[:, REQUIRED_COLUMNS].copy()
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce", utc=True)
    if data["Date"].isna().any():
        raise ValueError("Price history contains invalid dates.")
    data["Symbol"] = data["Symbol"].astype(str).str.strip()
    if (data["Symbol"] == "").any():
        raise ValueError("Price history contains blank symbols.")
    for column in ("Open", "High", "Low", "Close", "Volume"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    if data[["Open", "High", "Low", "Close", "Volume"]].isna().any().any():
        raise ValueError("Price history contains missing/non-numeric OHLCV values.")
    if (data["Close"] <= 0).any() or (data["Volume"] < 0).any():
        raise ValueError("Price history contains invalid close or volume values.")
    data = data.sort_values(["Symbol", "Date"], kind="mergesort")
    if data.duplicated(["Symbol", "Date"]).any():
        raise ValueError("Price history contains duplicate symbol/date rows.")
    symbols = sorted(data["Symbol"].unique().tolist())
    if benchmark_symbol not in symbols:
        raise ValueError(f"Required benchmark {benchmark_symbol} is missing.")
    records = []
    for row in data.itertuples(index=False, name=None):
        date, symbol, open_, high, low, close, volume = row
        records.append([date.isoformat(), symbol, float(open_), float(high),
                        float(low), float(close), float(volume)])
    payload = {
        "schema_version": SCHEMA_VERSION,
        "as_of": str(as_of),
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "price_adjustment": "auto_adjusted",
        "benchmark_symbol": benchmark_symbol,
        "symbols": symbols,
        "row_count": len(records),
        "columns": list(REQUIRED_COLUMNS),
        "records": records,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    packed = zlib.compress(canonical.encode("utf-8"), level=9)
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": str(as_of),
        "source": str(source),
        "sha256": digest,
        "encoding": "zlib+base64+json",
        "payload": base64.b64encode(packed).decode("ascii"),
        "uncompressed_bytes": len(canonical.encode("utf-8")),
        "compressed_bytes": len(packed),
        "row_count": len(records),
        "symbol_count": len(symbols),
    }


def load_ranking_snapshot(snapshot: Mapping) -> tuple[pd.DataFrame, dict]:
    """Decode and integrity-check a packed ranking snapshot."""
    if snapshot.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported ranking snapshot schema.")
    if snapshot.get("encoding") != "zlib+base64+json":
        raise ValueError("Unsupported ranking snapshot encoding.")
    try:
        raw = zlib.decompress(base64.b64decode(snapshot["payload"], validate=True))
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("Ranking snapshot payload cannot be decoded.") from exc
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if digest != snapshot.get("sha256"):
        raise ValueError("Ranking snapshot SHA-256 integrity check failed.")
    if payload.get("row_count") != len(payload.get("records", [])):
        raise ValueError("Ranking snapshot row count does not match its payload.")
    frame = pd.DataFrame(payload["records"], columns=payload["columns"])
    frame["Date"] = pd.to_datetime(frame["Date"], utc=True)
    return frame, payload
