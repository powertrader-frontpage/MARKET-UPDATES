"""FYERS-first futures volume-record data for Institutional Volume Intelligence.

Only HVQ (63 sessions) is calculated. Cash-equity
volume is never substituted for futures volume.
"""
from __future__ import annotations

from datetime import date, timedelta
import time
from typing import Optional

import pandas as pd
import requests

FYERS_FO_MASTER_URL = "https://public.fyers.in/sym_details/NSE_FO_sym_master.json"
FYERS_HISTORY_URL = "https://api-t1.fyers.in/data/history"
FYERS_DAILY_LOOKBACK_DAYS = 365
NSE_INDEX_FUTURES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def load_nse_futures_master() -> tuple[pd.DataFrame, Optional[str]]:
    """Load FYERS' keyed JSON symbol master and select the nearest listed future per underlying."""
    try:
        response = requests.get(FYERS_FO_MASTER_URL, timeout=30)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not payload:
            return pd.DataFrame(), "FYERS returned an empty or unexpected NSE_FO JSON master."
        records = []
        for ticker, item in payload.items():
            if not isinstance(item, dict) or str(item.get("optType", "")).upper() != "XX":
                continue
            symbol = str(item.get("symTicker") or ticker).strip()
            underlying = str(item.get("underSym") or "").strip()
            expiry_raw = item.get("expiryDate")
            try:
                expiry = pd.to_datetime(float(expiry_raw), unit="s") if expiry_raw not in (None, "") else pd.NaT
            except (TypeError, ValueError, OverflowError):
                expiry = pd.NaT
            if symbol.startswith("NSE:") and underlying and underlying.upper() not in NSE_INDEX_FUTURES and pd.notna(expiry):
                records.append({"_ticker": symbol, "_underlying": underlying, "_expiry": expiry})
        frame = pd.DataFrame(records)
        if frame.empty:
            return pd.DataFrame(), "No NSE futures were identified in the FYERS JSON symbol master."
        frame = frame[frame["_expiry"].dt.date >= date.today()]
        if frame.empty:
            return pd.DataFrame(), "No current/future-dated NSE futures contracts were found."
        frame = frame.sort_values(["_underlying", "_expiry", "_ticker"])
        return frame.drop_duplicates("_underlying", keep="first").reset_index(drop=True), None
    except Exception as exc:
        return pd.DataFrame(), f"FYERS NSE futures master unavailable ({exc.__class__.__name__})."


def fetch_continuous_futures_daily(app_id: str, access_token: str, symbol: str,
                                   lookback_days: int = FYERS_DAILY_LOOKBACK_DAYS):
    """Fetch up to one year of continuous daily futures OHLCV for HVQ."""
    end = date.today()
    start = end - timedelta(days=min(max(int(lookback_days), 252), 366))
    try:
        params = {
            "symbol": symbol, "resolution": "D", "date_format": "1",
            "range_from": start.isoformat(), "range_to": end.isoformat(), "cont_flag": "1",
        }
        headers = {"Authorization": f"{app_id}:{access_token}"}
        payload = {}
        response = None
        for attempt in range(4):
            response = requests.get(FYERS_HISTORY_URL, params=params, headers=headers, timeout=30)
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if response.status_code == 429 or str(payload.get("code", "")) in {"-429", "429"}:
                if attempt < 3:
                    time.sleep(2 ** (attempt + 1))
                    continue
            break
        if response is None or not response.ok or payload.get("s") != "ok":
            status_code = response.status_code if response is not None else "unknown"
            return pd.DataFrame(), str(payload.get("message") or payload.get("code") or f"FYERS history request failed (HTTP {status_code}).")
        candles = payload.get("candles") or []
        if not candles:
            return pd.DataFrame(), "FYERS returned no continuous futures candles."
        frame = pd.DataFrame(candles)
        if frame.shape[1] < 6:
            return pd.DataFrame(), "FYERS returned an unexpected candle schema."
        frame = frame.iloc[:, :6]
        frame.columns = ["Epoch", "Open", "High", "Low", "Close", "Volume"]
        frame["Date"] = pd.to_datetime(frame["Epoch"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame = frame.dropna(subset=["Date", "Open", "High", "Low", "Close", "Volume"])
        frame = frame.sort_values("Date").drop_duplicates("Date", keep="last").reset_index(drop=True)
        frame["Source"] = "FYERS continuous futures"
        return frame, None
    except Exception as exc:
        return pd.DataFrame(), f"FYERS futures history unavailable ({exc.__class__.__name__})."


def volume_record_flags(frame: pd.DataFrame) -> dict:
    """Check whether the latest completed futures session is the 63-session volume high."""
    if frame is None or frame.empty or "Volume" not in frame:
        return {"HVQ": None, "history_sessions": 0, "HVQ_status": "Unavailable"}
    data = frame.copy()
    if "Date" in data:
        data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
        data = data.dropna(subset=["Date"]).sort_values("Date")
        now_ist = pd.Timestamp.now(tz="Asia/Kolkata")
        if not data.empty and data["Date"].iloc[-1].date() == now_ist.date() and now_ist.hour < 16:
            data = data.iloc[:-1]
    volume = pd.to_numeric(data["Volume"], errors="coerce").dropna().reset_index(drop=True)
    n = len(volume)
    if n < 63:
        return {"HVQ": None, "history_sessions": n, "HVQ_status": f"Insufficient history ({n}/63 sessions)"}
    latest = float(volume.iloc[-1])
    max_63 = float(volume.iloc[-63:].max())
    record_high = latest >= max_63
    above_minimum = latest > 100000
    qualifies = record_high and above_minimum
    status = "HVQ" if qualifies else "Below 100,000 volume threshold" if not above_minimum else "Not a 63-session high"
    return {"HVQ": bool(qualifies), "is_63_session_high": bool(record_high),
            "above_100k": bool(above_minimum), "history_sessions": n,
            "latest_volume": latest, "HVQ_63D_max": max_63, "HVQ_status": status}
