"""FYERS-first futures data source for Institutional Volume Intelligence.

This module deliberately does not substitute cash-equity volume for futures volume.
FYERS Standard daily history is limited to the latest five years; therefore a
2,000-session HVE calculation must remain unavailable until 2,000 valid sessions
are present. Yahoo Finance is not used as a futures-volume fallback.
"""
from __future__ import annotations

from datetime import date, timedelta
from io import StringIO
import time
from typing import Optional

import pandas as pd
import requests

FYERS_FO_MASTER_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
FYERS_HISTORY_URL = "https://api-t1.fyers.in/data/history"
FYERS_STANDARD_DAILY_YEARS = 5
FYERS_DAILY_REQUEST_DAYS = 366


def load_nse_futures_master() -> tuple[pd.DataFrame, Optional[str]]:
    """Load FYERS NSE_FO symbol master and return verified futures rows only."""
    try:
        response = requests.get(FYERS_FO_MASTER_URL, timeout=30)
        response.raise_for_status()
        raw = response.text.lstrip("\ufeff")
        frame = pd.read_csv(StringIO(raw), low_memory=False)
        # FYERS publishes a headered CSV. Normalize labels but retain original fields.
        frame.columns = [str(c).strip() for c in frame.columns]
        ticker_col = next((c for c in frame.columns if c.lower() == "symbol ticker"), None)
        underlying_col = next((c for c in frame.columns if c.lower() == "underlying symbol"), None)
        expiry_col = next((c for c in frame.columns if c.lower() == "expiry date"), None)
        option_col = next((c for c in frame.columns if c.lower() == "option type"), None)
        if not all([ticker_col, underlying_col, expiry_col]):
            return pd.DataFrame(), "FYERS NSE_FO master schema did not contain the expected ticker, underlying and expiry columns."
        frame["_ticker"] = frame[ticker_col].astype(str).str.strip()
        frame["_underlying"] = frame[underlying_col].astype(str).str.strip()
        frame["_expiry"] = pd.to_datetime(frame[expiry_col], errors="coerce", dayfirst=True)
        # Futures have XX option type and FUT ticker suffix; exclude CE/PE options.
        if option_col:
            is_future = frame[option_col].astype(str).str.upper().str.strip().eq("XX")
        else:
            is_future = frame["_ticker"].str.upper().str.contains("FUT", regex=False)
        frame = frame[is_future & frame["_ticker"].str.startswith("NSE:")].copy()
        frame = frame.dropna(subset=["_expiry"])
        frame = frame[frame["_expiry"].dt.date >= date.today()]
        if frame.empty:
            return pd.DataFrame(), "No current/future-dated NSE futures contracts were found in the FYERS symbol master."
        # One nearest-expiry listed futures contract per underlying; continuous flag is
        # requested on this verified contract symbol for the historical continuous series.
        frame = frame.sort_values(["_underlying", "_expiry", "_ticker"])
        frame = frame.drop_duplicates("_underlying", keep="first").reset_index(drop=True)
        return frame, None
    except Exception as exc:
        return pd.DataFrame(), f"FYERS NSE futures master unavailable ({exc.__class__.__name__})."


def _request_history_chunk(app_id: str, access_token: str, symbol: str,
                           start: date, end: date, timeout: int = 25):
    response = requests.get(
        FYERS_HISTORY_URL,
        params={
            "symbol": symbol,
            "resolution": "D",
            "date_format": "1",
            "range_from": start.isoformat(),
            "range_to": end.isoformat(),
            "cont_flag": "1",
        },
        headers={"Authorization": f"{app_id}:{access_token}"},
        timeout=timeout,
    )
    payload = response.json()
    if not response.ok or payload.get("s") != "ok":
        return pd.DataFrame(), str(payload.get("message") or payload.get("code") or "FYERS history request failed.")
    candles = payload.get("candles") or []
    if not candles:
        return pd.DataFrame(), None
    frame = pd.DataFrame(candles)
    if frame.shape[1] < 6:
        return pd.DataFrame(), "FYERS returned an unexpected candle schema."
    frame = frame.iloc[:, :6]
    frame.columns = ["Epoch", "Open", "High", "Low", "Close", "Volume"]
    frame["Date"] = pd.to_datetime(frame["Epoch"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["Date", "Open", "High", "Low", "Close", "Volume"])
    return frame[["Date", "Open", "High", "Low", "Close", "Volume"]].sort_values("Date"), None


def fetch_continuous_futures_daily(app_id: str, access_token: str, symbol: str,
                                   lookback_years: int = FYERS_STANDARD_DAILY_YEARS,
                                   pause_seconds: float = 1.25) -> tuple[pd.DataFrame, Optional[str]]:
    """Fetch continuous daily futures OHLCV in FYERS' documented <=366-day chunks.

    FYERS Standard supports up to five years of daily history. The caller should
    cache this result; this function intentionally does not run in a bulk loop
    without an explicit user scan action.
    """
    end = date.today()
    start = end - timedelta(days=365 * min(max(int(lookback_years), 1), FYERS_STANDARD_DAILY_YEARS) + 2)
    chunks = []
    cursor = start
    errors = []
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=FYERS_DAILY_REQUEST_DAYS - 1), end)
        data, error = _request_history_chunk(app_id, access_token, symbol, cursor, chunk_end)
        if error:
            errors.append(error)
        elif not data.empty:
            chunks.append(data)
        cursor = chunk_end + timedelta(days=1)
        if cursor <= end:
            time.sleep(max(0.0, pause_seconds))
    if not chunks:
        return pd.DataFrame(), errors[0] if errors else "FYERS returned no continuous futures candles."
    result = pd.concat(chunks, ignore_index=True)
    result = result.drop_duplicates("Date", keep="last").sort_values("Date").reset_index(drop=True)
    result["Source"] = "FYERS continuous futures"
    warning = "; ".join(dict.fromkeys(errors)) if errors else None
    return result, warning


def volume_record_flags(frame: pd.DataFrame) -> dict:
    """Calculate published 2,000/252/63-session volume records without look-ahead."""
    if frame is None or frame.empty or "Volume" not in frame:
        return {"HVE": None, "HVY": None, "HVQ": None, "history_sessions": 0,
                "HVE_status": "Unavailable", "HVY_status": "Unavailable", "HVQ_status": "Unavailable"}
    volume = pd.to_numeric(frame["Volume"], errors="coerce").dropna().reset_index(drop=True)
    n = len(volume)
    latest = float(volume.iloc[-1]) if n else float("nan")
    out = {"history_sessions": n}
    for label, window in [("HVE", 2000), ("HVY", 252), ("HVQ", 63)]:
        if n < window:
            out[label] = None
            out[label + "_status"] = f"Insufficient history ({n}/{window} sessions)"
        else:
            out[label] = bool(latest >= float(volume.iloc[-window:].max()))
            out[label + "_status"] = "Record" if out[label] else "No record"
    return out
