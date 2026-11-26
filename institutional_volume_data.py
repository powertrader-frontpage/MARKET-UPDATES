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

FYERS_FO_MASTER_URL = "https://public.fyers.in/sym_details/NSE_FO.csv"
FYERS_HISTORY_URL = "https://api-t1.fyers.in/data/history"
FYERS_DAILY_LOOKBACK_DAYS = 370


def load_nse_futures_master() -> tuple[pd.DataFrame, Optional[str]]:
    """Load the FYERS NSE_FO master and select the nearest listed future per underlying."""
    try:
        response = requests.get(FYERS_FO_MASTER_URL, timeout=30)
        response.raise_for_status()
        frame = pd.read_csv(StringIO(response.text.lstrip("\ufeff")), low_memory=False)
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
        if option_col:
            is_future = frame[option_col].astype(str).str.upper().str.strip().eq("XX")
        else:
            is_future = frame["_ticker"].str.upper().str.contains("FUT", regex=False)
        frame = frame[is_future & frame["_ticker"].str.startswith("NSE:")].copy()
        frame = frame.dropna(subset=["_expiry"])
        frame = frame[frame["_expiry"].dt.date >= date.today()]
        if frame.empty:
            return pd.DataFrame(), "No current/future-dated NSE futures contracts were found in the FYERS symbol master."
        frame = frame.sort_values(["_underlying", "_expiry", "_ticker"])
        return frame.drop_duplicates("_underlying", keep="first").reset_index(drop=True), None
    except Exception as exc:
        return pd.DataFrame(), f"FYERS NSE futures master unavailable ({exc.__class__.__name__})."


def fetch_continuous_futures_daily(app_id: str, access_token: str, symbol: str,
                                   lookback_days: int = FYERS_DAILY_LOOKBACK_DAYS):
    """Fetch one year of continuous daily futures OHLCV; enough for HVY and HVQ."""
    end = date.today()
    start = end - timedelta(days=max(lookback_days, 370))
    try:
        response = requests.get(
            FYERS_HISTORY_URL,
            params={"symbol": symbol, "resolution": "D", "date_format": "1",
                    "range_from": start.isoformat(), "range_to": end.isoformat(),
                    "cont_flag": "1"},
            headers={"Authorization": f"{app_id}:{access_token}"},
            timeout=30,
        )
        payload = response.json()
        if not response.ok or payload.get("s") != "ok":
            return pd.DataFrame(), str(payload.get("message") or payload.get("code") or "FYERS history request failed.")
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
    """Compare the latest completed futures session with the trailing 252/63 sessions."""
    if frame is None or frame.empty or "Volume" not in frame:
        return {"HVY": None, "HVQ": None, "history_sessions": 0,
                "HVY_status": "Unavailable", "HVQ_status": "Unavailable"}
    data = frame.copy()
    if "Date" in data:
        data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
        data = data.dropna(subset=["Date"]).sort_values("Date")
        # Exclude today's still-forming candle to prevent an intraday false record.
        today = pd.Timestamp.now(tz="Asia/Kolkata").date()
        if not data.empty and data["Date"].iloc[-1].date() == today:
            now_ist = pd.Timestamp.now(tz="Asia/Kolkata")
            if now_ist.hour < 16:
                data = data.iloc[:-1]
    volume = pd.to_numeric(data["Volume"], errors="coerce").dropna().reset_index(drop=True)
    n = len(volume)
    latest = float(volume.iloc[-1]) if n else float("nan")
    out = {"history_sessions": n}
    for label, window in [("HVY", 252), ("HVQ", 63)]:
        if n < window:
            out[label] = None
            out[label + "_status"] = f"Insufficient history ({n}/{window} sessions)"
        else:
            out[label] = bool(latest >= float(volume.iloc[-window:].max()))
            out[label + "_status"] = "Record" if out[label] else "No record"
    return out
