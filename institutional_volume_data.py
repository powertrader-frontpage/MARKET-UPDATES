"""FYERS-first futures volume-record data for Institutional Volume Intelligence.

Only HVY (252 sessions) and HVQ (63 sessions) are calculated. Cash-equity
volume is never substituted for futures volume.
"""
from __future__ import annotations

from datetime import date, timedelta
from io import StringIO
from typing import Optional

import pandas as pd
import requests

FYERS_FO_MASTER_URL = "https://public.fyers.in/sym_details/NSE_FO_sym_master.json"
FYERS_HISTORY_URL = "https://api-t1.fyers.in/data/history"
FYERS_DAILY_LOOKBACK_DAYS = 370


def load_nse_futures_master() -> tuple[pd.DataFrame, Optional[str]]:
    """Load FYERS' keyed JSON symbol master and select the nearest active future per underlying."""
    try:
        response = requests.get(FYERS_FO_MASTER_URL, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload:
            return pd.DataFrame(), "FYERS returned an empty or unexpected NSE_FO JSON master."
        records = []
        for ticker, item in payload.items():
            if not isinstance(item, dict):
                continue
            if str(item.get("optType", "")).upper() != "XX":
                continue
            symbol = str(item.get("symTicker") or ticker).strip()
            underlying = str(item.get("underSym") or "").strip()
            expiry_raw = item.get("expiryDate")
            try:
                expiry = pd.to_datetime(float(expiry_raw), unit="s") if expiry_raw not in (None, "") else pd.NaT
            except (TypeError, ValueError, OverflowError):
                expiry = pd.NaT
            if symbol.startswith("NSE:") and underlying and pd.notna(expiry):
                records.append({"_ticker": symbol, "_underlying": underlying, "_expiry": expiry})
        frame = pd.DataFrame(records)
        if frame.empty:
            return pd.DataFrame(), "No NSE single-stock futures were identified in the FYERS JSON symbol master."
        frame = frame[frame["_expiry"].dt.date >= date.today()]
        if frame.empty:
            return pd.DataFrame(), "No current/future-dated NSE futures contracts were found in the FYERS symbol master."
        frame = frame.sort_values(["_underlying", "_expiry", "_ticker"])
        return frame.drop_duplicates("_underlying", keep="first").reset_index(drop=True), None
    except Exception as exc:
        return pd.DataFrame(), f"FYERS NSE futures master unavailable ({exc.__class__.__name__})."

