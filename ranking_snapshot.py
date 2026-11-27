"""Deterministic input snapshots for microsector ranking-invariance audits.

This module is independent of Streamlit and ranking formulas. It stores the
exact daily OHLCV inputs (including the benchmark) with stable ordering and a
SHA-256 digest so old and canonical baskets can be tested on identical inputs.
"""
from __future__ import annotations

import base64
import hashlib
import json
import zlib
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
    for date, symbol, open_, high, low, close, volume in data.itertuples(index=False, name=None):
        records.append([date.isoformat(), symbol, float(open_), float(high),
                        float(low), float(close), float(volume)])
    payload = {
        "schema_version": SCHEMA_VERSION,
        "as_of": str(as_of),
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



def save_ranking_snapshot_to_github(frame: pd.DataFrame, *, token: str, repository: str,
                                   branch: str, path: str, as_of: str,
                                   source: str = "Yahoo Finance auto-adjusted daily OHLCV",
                                   benchmark_symbol: str = "^CRSLDX") -> dict:
    """Write one immutable-input audit snapshot to a dedicated GitHub data file."""
    import requests

    snapshot = build_ranking_snapshot(
        frame, as_of=as_of, source=source, benchmark_symbol=benchmark_symbol
    )
    body = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    url = f"https://api.github.com/repos/{repository}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = requests.get(url, headers=headers, params={"ref": branch}, timeout=20)
    if response.status_code == 404:
        existing_sha = None
    else:
        response.raise_for_status()
        existing_sha = response.json().get("sha")
    import base64
    payload = {
        "message": f"Capture ranking invariance OHLCV snapshot {as_of}",
        "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if existing_sha:
        payload["sha"] = existing_sha
    written = requests.put(url, headers=headers, json=payload, timeout=45)
    written.raise_for_status()
    return {
        "sha256": snapshot["sha256"],
        "as_of": snapshot["as_of"],
        "row_count": snapshot["row_count"],
        "symbol_count": snapshot["symbol_count"],
        "compressed_bytes": snapshot["compressed_bytes"],
        "commit_sha": written.json().get("commit", {}).get("sha"),
    }
