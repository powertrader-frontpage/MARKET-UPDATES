from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
import json
import html
import queue
import threading

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh
import streamlit.components.v1 as components
import yfinance as yf
from nse500_screener import render_nse500_screener

import hashlib
import hmac
import secrets
import time
import re
from urllib.parse import urlencode

st.set_page_config(page_title="Market Intelligence Terminal", page_icon="📈", layout="wide")
st.markdown("""
<style>
.block-container{padding-top:1rem;padding-bottom:2rem}
[data-testid="stMetric"]{border-left:4px solid #64748b}
.signal-good [data-testid="stMetric"]{border-left-color:#22c55e}
.signal-bad [data-testid="stMetric"]{border-left-color:#ef4444}
.signal-neutral [data-testid="stMetric"]{border-left-color:#f59e0b}
div[data-testid="stExpander"]{border:1px solid rgba(148,163,184,.3);border-radius:12px;overflow:hidden}
div[data-testid="stExpander"] summary{font-weight:750;color:#25d6b1} div[data-testid="stExpander"] summary p{background:linear-gradient(90deg,#20d6a5,#38bdf8,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;font-weight:800;letter-spacing:.15px}
div[data-testid="stDataFrame"]{border-radius:10px;overflow:hidden}

[data-testid="stMetric"]{background:rgba(100,120,145,.08);border:1px solid rgba(120,140,160,.25);padding:12px;border-radius:12px}
.small-note{font-size:.85rem;opacity:.8}\n[data-testid='stDataFrame']{max-width:100%}
[data-testid="stMetricDelta"] svg{display:inline-block}
[data-testid="stMetricDelta"] [data-testid="stMarkdownContainer"]{font-weight:650}
[data-testid="stMetricDelta"][data-testid*="positive"],[data-testid="stMetricDelta"] svg[fill="green"]{color:#22c55e}
@media(max-width:700px){.block-container{padding-left:.7rem;padding-right:.7rem}[data-testid='stMetricValue']{font-size:1.35rem}h1{font-size:1.7rem}h2{font-size:1.35rem}}
</style>
""", unsafe_allow_html=True)

st.title("📈 Market Intelligence Terminal")
st.caption("Free hybrid edition · No broker API · Source-labelled data · Missing data stays missing")

INDEXES = {
    "NIFTY 50": "^NSEI",
    "BANK NIFTY": "^NSEBANK",
    "India VIX": "^INDIAVIX",
    "Brent Crude": "BZ=F",
    "USD/INR": "INR=X",
    "INFY": "INFY.NS",
    "WIPRO": "WIPRO.NS",
    "RELIANCE": "RELIANCE.NS",
    "HDFCBANK": "HDFCBANK.NS",
}
SECTOR_TICKERS = {
    "IT": "^CNXIT", "Pharma": "^CNXPHARMA", "Auto": "^CNXAUTO",
    "Metal": "^CNXMETAL", "Energy": "^CNXENERGY", "FMCG": "^CNXFMCG",
    "Realty": "^CNXREALTY", "Financial Services": "NIFTY_FIN_SERVICE.NS",
    "PSU Bank": "^CNXPSUBANK", "Media": "^CNXMEDIA", "Infrastructure": "^CNXINFRA",
    "Consumption": "^CNXCONSUM",
}

# Upstox read-only OAuth integration. Secrets stay in Streamlit Cloud Secrets.
def upstox_oauth_credentials():
    try:
        values = (
            str(st.secrets["UPSTOX_API_KEY"]).strip(),
            str(st.secrets["UPSTOX_API_SECRET"]).strip(),
            str(st.secrets["UPSTOX_REDIRECT_URI"]).strip(),
        )
        return values if all(values) else None
    except Exception:
        return None

def upstox_make_state(client_secret: str) -> str:
    # Signed state survives OAuth redirects even if Streamlit creates a fresh session.
    payload = str(int(time.time())) + "." + secrets.token_urlsafe(24)
    signature = hmac.new(client_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return payload + "." + signature

def upstox_verify_state(state: str, client_secret: str, max_age_seconds: int = 900) -> bool:
    try:
        payload, signature = state.rsplit(".", 1)
        timestamp_text, nonce = payload.split(".", 1)
        timestamp = int(timestamp_text)
        if not nonce or abs(int(time.time()) - timestamp) > max_age_seconds:
            return False
        expected = hmac.new(client_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return secrets.compare_digest(signature, expected)
    except Exception:
        return False

def upstox_login_url(client_id: str, redirect_uri: str, state: str) -> str:
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "state": state}
    return "https://api.upstox.com/v2/login/authorization/dialog?" + urlencode(params)

@st.cache_resource
def upstox_token_vault():
    return {}

def upstox_exchange_auth_code(client_id: str, client_secret: str, redirect_uri: str, code: str):
    try:
        response = requests.post(
            "https://api.upstox.com/v2/login/authorization/token",
            headers={"accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            data={"code": code, "client_id": client_id, "client_secret": client_secret,
                  "redirect_uri": redirect_uri, "grant_type": "authorization_code"},
            timeout=20,
        )
        payload = response.json()
        if response.ok and payload.get("access_token"):
            return payload["access_token"], None
        return None, str(payload.get("errors") or payload.get("message") or "Upstox did not return an access token.")
    except Exception as exc:
        return None, f"Upstox token exchange failed ({exc.__class__.__name__})."

def upstox_get_profile(access_token: str):
    try:
        response = requests.get(
            "https://api.upstox.com/v2/user/profile",
            headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        payload = response.json()
        if response.ok and payload.get("status") == "success":
            return payload.get("data") or {}, None
        return None, str(payload.get("errors") or payload.get("message") or f"HTTP {response.status_code}")
    except Exception as exc:
        return None, f"Upstox profile request failed ({exc.__class__.__name__})."

# Complete Upstox OAuth redirects and keep the newly issued token in the app's in-memory vault.
try:
    _upstox_params = st.query_params
    _upstox_code = str(_upstox_params.get("code", "")).strip()
    _upstox_state = str(_upstox_params.get("state", "")).strip()
    _upstox_oauth = upstox_oauth_credentials()
    if _upstox_code and _upstox_state and _upstox_oauth:
        _upstox_client_id, _upstox_client_secret, _upstox_redirect_uri = _upstox_oauth
        if upstox_verify_state(_upstox_state, _upstox_client_secret):
            _upstox_new_token, _upstox_token_error = upstox_exchange_auth_code(
                _upstox_client_id, _upstox_client_secret, _upstox_redirect_uri, _upstox_code
            )
            if _upstox_new_token:
                upstox_token_vault()["access_token"] = _upstox_new_token
                st.cache_data.clear()
                st.query_params.clear()
                st.session_state["upstox_oauth_connected"] = True
            else:
                st.session_state["upstox_oauth_error"] = _upstox_token_error
        else:
            st.session_state["upstox_oauth_error"] = "Upstox login state expired or invalid. Start login again."
except Exception:
    pass

with st.sidebar:
    st.header("Dashboard controls")
    _upstox_oauth = upstox_oauth_credentials()
    if _upstox_oauth:
        _upstox_client_id, _upstox_client_secret, _upstox_redirect_uri = _upstox_oauth
        _upstox_login = upstox_login_url(
            _upstox_client_id, _upstox_redirect_uri, upstox_make_state(_upstox_client_secret)
        )
        st.markdown(f"[Connect / Reconnect Upstox for Brent]({_upstox_login})")
        if st.session_state.get("upstox_oauth_connected"):
            st.success("Upstox connected for this app session.")
        if st.session_state.get("upstox_oauth_error"):
            st.warning("Upstox login issue: " + str(st.session_state["upstox_oauth_error"]))
        st.caption("If the quote still falls back to EIA, confirm the Upstox app redirect URI exactly matches this dashboard URL.")
    else:
        st.caption("Upstox Brent login unavailable: configure UPSTOX_API_KEY, UPSTOX_API_SECRET and UPSTOX_REDIRECT_URI in Streamlit Secrets.")
    period = st.selectbox("Historical lookback", ["1mo", "3mo", "6mo", "1y", "2y", "5y"], index=2)
    interval = st.selectbox("Intraday bar interval", ["5m", "15m", "30m", "60m"], index=0)
    st.caption("Intraday Yahoo Finance history is limited by the upstream source. If it is unavailable, the app will show an error instead of substituting sample values.")
    refresh = st.button("↻ Refresh ALL data now", use_container_width=True, help="Clears Streamlit data caches and reruns every dashboard data fetch.")
    if refresh:
        st.cache_data.clear()
        # Clear session-held scan tables as well as Streamlit's function caches.
        # Otherwise a global refresh can rerun the page while still rendering old
        # NSE 500 / breakout / turnaround results from session_state.
        for _key in (
            "nse500_rs_stage_results", "nse500_rs_stage_sectors",
            "nse500_rs_stage_industries", "nse500_rs_stage_time",
            "nse500_rs_stage_epoch", "nse500_rs_stage_counts",
            "nse500_rs_stage_weeks", "nse500_rs_stage_price_date",
            "nse500_rs_stage_last_error",
            "explosive_breakout_scan_data", "explosive_breakout_scan_time",
            "explosive_breakout_scan_epoch", "explosive_breakout_scan_version",
            "auto_turnaround_scan_results", "auto_turnaround_scan_time",
            "auto_turnaround_scan_warning",
        ):
            st.session_state.pop(_key, None)
        st.rerun()
    auto_refresh_enabled = st.checkbox("Automatic refresh", value=True, key="market_auto_refresh_enabled", help="Refreshes the dashboard every 5 minutes by default. Some upstream sources may still publish delayed data.")
    if auto_refresh_enabled:
        auto_refresh_minutes = st.selectbox("Refresh interval", [1, 5, 10, 15, 30], index=1, format_func=lambda n: f"Every {n} minutes", key="market_auto_refresh_minutes")
        st_autorefresh(interval=auto_refresh_minutes * 60 * 1000, limit=None, key="market_auto_refresh_timer")
        st.caption(f"Automatic page refresh enabled · every {auto_refresh_minutes} minutes. Data freshness depends on each source and its cache.")
    else:
        st.caption("Automatic refresh is off. Use Refresh market data for a manual refresh.")
    st.divider()
    st.markdown("**Public source links**")
    st.markdown("- [NSE reports](https://www.nseindia.com/all-reports)")
    st.markdown("- [Nifty Indices historical data](https://www.niftyindices.com/reports/historical-data)")
    st.markdown("- [NSE FII/DII reports](https://www.nseindia.com/reports/fii-dii)")
    st.markdown("- [RBI](https://www.rbi.org.in/)")
    st.markdown("- [TradingView widgets](https://www.tradingview.com/widget/)")
    st.markdown("- [Yahoo Finance data coverage/delays](https://help.yahoo.com/kb/SLN2310.html)")



# Upstox read-only market-data connection. Keep credentials in Streamlit Cloud Secrets.
def upstox_credentials():
    """Read required Upstox secrets and return safe configuration diagnostics."""
    required = ("UPSTOX_API_KEY", "UPSTOX_API_SECRET", "UPSTOX_REDIRECT_URI", "UPSTOX_ACCESS_TOKEN")
    missing = []
    values = {}
    for name in required:
        try:
            value = str(st.secrets[name]).strip()
            if value:
                values[name] = value
            else:
                missing.append(name + " (empty)")
        except Exception:
            missing.append(name)
    if missing:
        return None, missing
    return (
        values["UPSTOX_API_KEY"],
        values["UPSTOX_API_SECRET"],
        values["UPSTOX_REDIRECT_URI"],
        values["UPSTOX_ACCESS_TOKEN"],
    ), []

def upstox_ltp(access_token: str, instrument_keys: list[str]):
    """Fetch read-only LTP quotes from Upstox; never place orders."""
    try:
        response = requests.get(
            "https://api.upstox.com/v2/market-quote/ltp",
            params={"instrument_key": ",".join(instrument_keys)},
            headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        try:
            payload = response.json()
        except Exception:
            return {}, f"Upstox returned a non-JSON response (HTTP {response.status_code})."
        if response.ok and payload.get("status") == "success":
            return payload.get("data") or {}, None
        errors = payload.get("errors") or []
        detail = errors[0].get("message") if errors and isinstance(errors[0], dict) else payload.get("message")
        return {}, f"Upstox quote request failed (HTTP {response.status_code}): {detail or 'check token, instrument keys and API access'}"
    except Exception as exc:
        return {}, f"Upstox request failed ({exc.__class__.__name__})."

# FYERS read-only market-data connection. Credentials must live in Streamlit Cloud Secrets.
def fyers_credentials():
    try:
        app_id = str(st.secrets["FYERS_APP_ID"]).strip()
        app_secret = str(st.secrets["FYERS_APP_SECRET"]).strip()
        redirect_uri = str(st.secrets["FYERS_REDIRECT_URI"]).strip()
        if app_id and app_secret and redirect_uri:
            return app_id, app_secret, redirect_uri
    except Exception:
        pass
    return None

def fyers_make_state(app_secret: str) -> str:
    issued = str(int(time.time()))
    nonce = secrets.token_urlsafe(18)
    message = f"{issued}.{nonce}"
    signature = hmac.new(app_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{message}.{signature}"

def fyers_check_state(app_secret: str, state: str) -> bool:
    try:
        issued, nonce, signature = state.split(".", 2)
        message = f"{issued}.{nonce}"
        expected = hmac.new(app_secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
        return abs(int(time.time()) - int(issued)) <= 600 and hmac.compare_digest(signature, expected)
    except Exception:
        return False

def fyers_login_url(app_id: str, redirect_uri: str, state: str) -> str:
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state,
    }
    return "https://api-t1.fyers.in/api/v3/generate-authcode?" + urlencode(params)

@st.cache_resource
def fyers_token_vault():
    """Process-wide in-memory token cache shared by Streamlit sessions."""
    return {}

def fyers_access_expiry_iso():
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    cutoff = now.replace(hour=6, minute=30, second=0, microsecond=0)
    if now >= cutoff:
        cutoff = cutoff + pd.Timedelta(days=1)
    return cutoff.isoformat()

def fyers_exchange_auth_code(app_id: str, app_secret: str, auth_code: str):
    app_hash = hashlib.sha256(f"{app_id}:{app_secret}".encode("utf-8")).hexdigest()
    try:
        response = requests.post(
            "https://api-t1.fyers.in/api/v3/validate-authcode",
            json={"grant_type": "authorization_code", "appIdHash": app_hash, "code": auth_code},
            timeout=20,
        )
        payload = response.json()
        if response.ok and payload.get("s") == "ok" and payload.get("access_token"):
            return payload["access_token"], payload.get("refresh_token"), None
        return None, None, str(payload.get("message", "FYERS did not return an access token."))
    except Exception as exc:
        return None, None, f"FYERS authentication request failed: {exc.__class__.__name__}"

def fyers_refresh_access_token(app_id: str, app_secret: str, refresh_token: str, pin: str):
    app_hash = hashlib.sha256(f"{app_id}:{app_secret}".encode("utf-8")).hexdigest()
    try:
        response = requests.post(
            "https://api-t1.fyers.in/api/v3/validate-refresh-token",
            json={"grant_type": "refresh_token", "appIdHash": app_hash, "refresh_token": refresh_token, "pin": pin},
            timeout=20,
        )
        payload = response.json()
        if response.ok and payload.get("s") == "ok" and payload.get("access_token"):
            return payload["access_token"], None
        return None, str(payload.get("message") or payload.get("messageCode") or "FYERS could not refresh the access token.")
    except Exception as exc:
        return None, f"FYERS token refresh failed: {exc.__class__.__name__}"


@st.cache_data(ttl=86400, show_spinner=False)
def fyers_nse_equity_symbols():
    """Load FYERS' public NSE capital-market symbol master; return verified equity symbols only."""
    try:
        response = requests.get("https://public.fyers.in/sym_details/NSE_CM_sym_master.json", timeout=20)
        response.raise_for_status()
        payload = response.json()
        symbols = []
        if isinstance(payload, dict):
            for symbol, details in payload.items():
                if not isinstance(symbol, str) or not symbol.startswith("NSE:") or not symbol.endswith("-EQ"):
                    continue
                name = ""
                if isinstance(details, dict):
                    name = str(details.get("description") or details.get("short_name") or details.get("symbol") or "")
                symbols.append((symbol, name))
        return sorted(set(symbols), key=lambda row: (row[1].upper(), row[0])), None
    except Exception as exc:
        return [], f"Symbol master unavailable ({exc.__class__.__name__})."

def fyers_history(app_id: str, access_token: str, symbol: str, resolution: str, days: int):
    date_to = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    date_from = date_to - pd.Timedelta(days=days)
    try:
        response = requests.get(
            "https://api-t1.fyers.in/data/history",
            params={"symbol": symbol, "resolution": resolution, "date_format": "1",
                    "range_from": str(date_from), "range_to": str(date_to), "cont_flag": "1"},
            headers={"Authorization": f"{app_id}:{access_token}"},
            timeout=25,
        )
        payload = response.json()
        if not response.ok or payload.get("s") != "ok":
            return pd.DataFrame(), str(payload.get("message", "FYERS historical candle request failed."))
        candles = payload.get("candles") or []
        if not candles:
            return pd.DataFrame(), "FYERS returned no candles for this symbol and interval."
        frame = pd.DataFrame(candles, columns=["Epoch", "Open", "High", "Low", "Close", "Volume"][:len(candles[0])])
        frame["Date"] = pd.to_datetime(frame["Epoch"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            if col in frame:
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
        return frame.dropna(subset=["Date", "Close"]).sort_values("Date"), None
    except Exception as exc:
        return pd.DataFrame(), f"FYERS historical request failed ({exc.__class__.__name__})."

def fyers_live_quotes(app_id: str, access_token: str, symbols: list[str]):
    try:
        response = requests.get(
            "https://api-t1.fyers.in/data/quotes",
            params={"symbols": ",".join(symbols)},
            headers={"Authorization": f"{app_id}:{access_token}"},
            timeout=15,
        )
        payload = response.json()
        if response.ok and payload.get("s") == "ok":
            return payload.get("d", []), None
        return [], str(payload.get("message", "FYERS quote request failed."))
    except Exception as exc:
        return [], f"FYERS quote request failed: {exc.__class__.__name__}"

@st.cache_data(ttl=20, show_spinner=False)
def fyers_live_quotes_cached(app_id: str, access_token: str, symbols: tuple[str, ...]):
    """Short-lived shared quote snapshot; never cache beyond 20 seconds."""
    return fyers_live_quotes(app_id, access_token, list(symbols))

def fyers_quote_frame(item: dict, fetched_at: datetime) -> pd.DataFrame:
    """Normalize one FYERS quote to the dashboard's existing OHLC snapshot interface."""
    values = item.get("v") or {}
    price = pd.to_numeric(values.get("lp"), errors="coerce")
    if pd.isna(price) or float(price) <= 0:
        return pd.DataFrame()
    stamp = pd.Timestamp(fetched_at)
    row = {
        "Date": stamp, "Open": pd.to_numeric(values.get("open_price"), errors="coerce"),
        "High": pd.to_numeric(values.get("high_price"), errors="coerce"),
        "Low": pd.to_numeric(values.get("low_price"), errors="coerce"),
        "Close": float(price), "Volume": pd.to_numeric(values.get("volume"), errors="coerce"),
        "DailyChange": pd.to_numeric(values.get("ch"), errors="coerce"),
        "DailyChangePct": pd.to_numeric(values.get("chp"), errors="coerce"),
        "Source": "FYERS live quote", "FetchedAt": stamp,
    }
    return pd.DataFrame([row])


def fyers_fundamental_probe(app_id: str, access_token: str, selected_symbol: str):
    """Inspect FYERS screener fields and test whether the selected stock appears in a fundamentals screen."""
    headers = {"Authorization": f"{app_id}:{access_token}"}
    base_url = "https://api-t1.fyers.in/api/v3/screeners"
    try:
        config_response = requests.get(f"{base_url}/config", headers=headers, timeout=15)
        config_payload = config_response.json()
        if config_response.status_code != 200 or config_payload.get("s") != "ok":
            message = config_payload.get("message") or config_payload.get("code") or config_payload.get("messageCode") or "No error message returned"
            error_code = config_payload.get("code", config_payload.get("messageCode", "not provided"))
            return {
                "ok": False,
                "message": (
                    f"FYERS Screeners config failed (HTTP {config_response.status_code}; "
                    f"API code: {error_code}). Details: {message}. "
                    "Check that the connected FYERS session belongs to the configured FYERS_APP_ID "
                    "and that Screeners API access is enabled for this app."
                ),
            }
        data = config_payload.get("data") or {}
        columns = data.get("columns") or []
        metric_terms = ("p/e", "pe ", "price earnings", "debt/equity", "debt to equity", "peg", "roe", "roce", "return on equity", "return on capital", "industry p/e")
        candidate_columns = []
        for col in columns:
            name = str(col.get("name", "")).strip()
            category = str(col.get("category", "")).strip().lower()
            normalized_name = name.lower()
            if any(term in normalized_name for term in metric_terms) or category in {
                "valuation ratios", "margin & profitability", "solvency & liquidity"
            }:
                col_id = col.get("id")
                if col_id and col_id not in [item["id"] for item in candidate_columns]:
                    candidate_columns.append({"id": col_id, "name": name, "category": category})
        fields = ",".join(item["id"] for item in candidate_columns[:9])
        screeners = [
            item for item in (data.get("screeners") or [])
            if "fundamental" in str(item.get("category", "")).lower()
            or "valuation" in str(item.get("name", "")).lower()
            or "profitability" in str(item.get("name", "")).lower()
        ]
        target = selected_symbol.replace("NSE:", "").replace("-EQ", "").replace(".NS", "").strip().upper()
        checked = []
        for screener in screeners[:5]:
            screener_id = screener.get("id")
            if not screener_id:
                continue
            params = {"screener": screener_id}
            if fields:
                params["fields"] = fields
            params["universe"] = "iw010"
            response = requests.get(f"{base_url}/query", params=params, headers=headers, timeout=15)
            payload = response.json()
            if response.status_code != 200 or payload.get("s") != "ok":
                error_code = payload.get("code", payload.get("messageCode", "not provided"))
                error_message = payload.get("message") or payload.get("code") or payload.get("messageCode") or "No error message returned"
                checked.append({
                    "name": screener.get("name", screener_id),
                    "error": f"HTTP {response.status_code}; API code {error_code}: {error_message}",
                })
                continue
            result = payload.get("data") or {}
            result_headers = result.get("header") or []
            result_body = result.get("body") or []
            symbol_index = next((i for i, item in enumerate(result_headers) if str(item.get("id", "")).lower() in {"symbolticker", "symbol"}), 0)
            for row in result_body:
                if len(row) <= symbol_index:
                    continue
                returned_symbol = str(row[symbol_index]).replace("NSE:", "").replace("-EQ", "").upper()
                if returned_symbol == target:
                    values = []
                    for index, column in enumerate(result_headers):
                        if index < len(row):
                            values.append({"Field": column.get("name", column.get("id", f"Field {index+1}")), "Value": row[index]})
                    return {
                        "ok": True, "message": "Selected stock found in a FYERS fundamentals screener.",
                        "screener": screener.get("name", screener_id), "values": values,
                        "columns": candidate_columns, "checked": checked,
                    }
            checked.append({"name": screener.get("name", screener_id), "matches": len(result_body)})
        return {
            "ok": True,
            "message": "FYERS configuration loaded, but the selected stock was not found in the first five relevant fundamentals screeners checked. This does not prove the stock's ratios are unavailable.",
            "columns": candidate_columns, "checked": checked,
        }
    except Exception as exc:
        return {"ok": False, "message": f"FYERS screener test failed: {exc.__class__.__name__}"}


def fyers_option_chain(app_id: str, access_token: str, symbol: str, strikecount: int = 10, expiry_timestamp: str = ""):
    """Fetch an authentic FYERS option-chain snapshot; never substitute sample data."""
    try:
        params = {"symbol": symbol, "strikecount": str(strikecount), "greeks": "1"}
        if expiry_timestamp:
            params["timestamp"] = str(expiry_timestamp)
        response = requests.get(
            "https://api-t1.fyers.in/data/options-chain-v3",
            params=params,
            headers={"Authorization": f"{app_id}:{access_token}"},
            timeout=20,
        )
        payload = response.json()
        if not response.ok:
            return {}, str(payload.get("message") or payload.get("messageCode") or f"HTTP {response.status_code}")
        if payload.get("s") == "error":
            return {}, str(payload.get("message") or payload.get("messageCode") or "FYERS rejected the option-chain request.")
        data = payload.get("data", payload)
        if not isinstance(data, dict) or not data.get("optionsChain"):
            return {}, str(payload.get("message") or "FYERS returned no option-chain rows for this symbol/expiry.")
        return data, None
    except Exception as exc:
        return {}, f"FYERS option-chain request failed: {exc.__class__.__name__}"

with st.sidebar:
    st.divider()
    st.subheader("FYERS live data")
    fyers_config = fyers_credentials()
    token_vault = fyers_token_vault()
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    # Clear expired session tokens so the refresh-token path is offered instead of a stale "connected" state.
    if st.session_state.get("fyers_access_token"):
        try:
            session_expiry = datetime.fromisoformat(token_vault.get("access_expires_at", ""))
            if now_ist >= session_expiry:
                st.session_state.pop("fyers_access_token", None)
        except Exception:
            # Older session tokens without expiry metadata are not trusted indefinitely.
            st.session_state.pop("fyers_access_token", None)
    # Restore the process-wide token into a new browser/session when it is still valid.
    if not st.session_state.get("fyers_access_token") and token_vault.get("access_token"):
        try:
            token_expiry = datetime.fromisoformat(token_vault.get("access_expires_at", ""))
            if now_ist < token_expiry:
                st.session_state["fyers_access_token"] = token_vault["access_token"]
                st.session_state["fyers_connected_at"] = token_vault.get("connected_at", now_ist.isoformat())
        except Exception:
            pass
    if not fyers_config:
        st.warning("Add FYERS_APP_ID, FYERS_APP_SECRET and FYERS_REDIRECT_URI in Streamlit Cloud Secrets to enable the connection.")
    else:
        fyers_app_id, fyers_app_secret, fyers_redirect_uri = fyers_config
        active_token = st.session_state.get("fyers_access_token")
        if active_token:
            st.success("FYERS connected · shared across app sessions until daily token expiry.")
            if st.button("Disconnect FYERS session", use_container_width=True):
                st.session_state.pop("fyers_access_token", None)
                st.session_state.pop("fyers_connected_at", None)
                token_vault.clear()
                st.rerun()
        elif token_vault.get("refresh_token"):
            st.info("Your access token has expired. Refresh it here without repeating the full FYERS login, if the refresh token is still valid.")
            fyers_pin = st.text_input("FYERS PIN (used only for refresh)", type="password", key="fyers_refresh_pin")
            if st.button("Refresh FYERS session", use_container_width=True, disabled=not fyers_pin):
                with st.spinner("Refreshing FYERS access token…"):
                    refreshed_token, refresh_error = fyers_refresh_access_token(fyers_app_id, fyers_app_secret, token_vault["refresh_token"], fyers_pin)
                if refreshed_token:
                    token_vault["access_token"] = refreshed_token
                    token_vault["access_expires_at"] = fyers_access_expiry_iso()
                    st.session_state["fyers_access_token"] = refreshed_token
                    st.session_state["fyers_connected_at"] = now_ist.isoformat()
                    st.session_state.pop("fyers_refresh_pin", None)
                    st.success("FYERS session refreshed.")
                    st.rerun()
                else:
                    st.error(f"Token refresh failed: {refresh_error}. Reconnect using the full FYERS login below.")
            # Always keep a full-login option visible when a refresh token exists but no longer works.
            st.link_button(
                "Log in to FYERS",
                fyers_login_url(fyers_app_id, fyers_redirect_uri, fyers_make_state(fyers_app_secret)),
                use_container_width=True,
            )
            st.caption("Use full FYERS login if session refresh fails. Access tokens expire daily at 6:30 AM.")
        else:
            st.link_button(
                "Log in to FYERS",
                fyers_login_url(fyers_app_id, fyers_redirect_uri, fyers_make_state(fyers_app_secret)),
                use_container_width=True,
            )
            st.caption("The token is shared across app sessions in memory. FYERS access tokens expire daily at 6:30 AM; a server restart or expired refresh token may require reconnecting.")



# Handle the redirect from FYERS after user authorization.
if "auth_code" in st.query_params:
    fyers_config = fyers_credentials()
    returned_state = st.query_params.get("state", "")
    auth_code = st.query_params.get("auth_code", "")
    if not fyers_config:
        st.error("FYERS credentials are not configured in Streamlit Secrets.")
    elif not returned_state or not fyers_check_state(fyers_config[1], returned_state):
        st.error("FYERS login state could not be verified or has expired. Please click Connect FYERS account and try again.")
    elif auth_code:
        with st.spinner("Completing FYERS authorization..."):
            token, refresh_token, token_error = fyers_exchange_auth_code(fyers_config[0], fyers_config[1], auth_code)
        if token:
            connected_at = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
            token_vault = fyers_token_vault()
            token_vault["access_token"] = token
            token_vault["refresh_token"] = refresh_token
            token_vault["access_expires_at"] = fyers_access_expiry_iso()
            token_vault["connected_at"] = connected_at
            st.session_state["fyers_access_token"] = token
            st.session_state["fyers_connected_at"] = connected_at
            st.query_params.clear()
            st.success("FYERS connected. Token is shared across app sessions in memory; refresh token is cached only for this server process.")
            st.rerun()
        else:
            st.error(f"FYERS authorization was not completed: {token_error}")
            st.query_params.clear()
    else:
        st.error("FYERS returned without an authorization code. Please retry the connection.")
        st.query_params.clear()

if "fyers_access_token" in st.session_state and fyers_credentials():
    fyers_app_id = fyers_credentials()[0]
    # Keep the optional live-quote tool available without taking space on the main dashboard.
    with st.expander("FYERS live market snapshot · optional", expanded=False):
        fyers_symbols = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "NSE:RELIANCE-EQ", "NSE:HDFCBANK-EQ", "NSE:INFY-EQ"]
        if st.button("Fetch FYERS live quotes", key="fyers_fetch_quotes"):
            quote_rows, quote_error = fyers_live_quotes(fyers_app_id, st.session_state["fyers_access_token"], fyers_symbols)
            if quote_error:
                st.warning(f"FYERS quote data unavailable: {quote_error}")
            elif not quote_rows:
                st.info("FYERS returned no quote rows for the selected symbols.")
            else:
                normalized = []
                for item in quote_rows:
                    values = item.get("v") or {}
                    normalized.append({
                        "Symbol": item.get("n", values.get("symbol", "Unknown")),
                        "Last price": values.get("lp"),
                        "Change": values.get("ch"),
                        "Change %": values.get("chp"),
                        "Open": values.get("open_price"),
                        "High": values.get("high_price"),
                        "Low": values.get("low_price"),
                        "Previous close": values.get("prev_close_price"),
                        "Volume": values.get("volume"),
                    })
                st.dataframe(pd.DataFrame(normalized), use_container_width=True, hide_index=True)
                st.caption("Fetched at " + datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S IST"))



@st.cache_data(ttl=60, show_spinner=False)
def get_upstox_brent_quote() -> tuple[pd.DataFrame, str | None]:
    """Fetch Upstox's global Brent instrument (Oil (Brent), BZUSD) using the full quote API."""
    try:
        # Prefer a fresh OAuth token from this app session; otherwise use the configured secret.
        access_token = str(upstox_token_vault().get("access_token", "")).strip()
        if not access_token:
            try:
                access_token = str(st.secrets["UPSTOX_ACCESS_TOKEN"]).strip()
            except Exception:
                return pd.DataFrame(), "Upstox not connected: use the Connect Upstox login in the sidebar."
        if not access_token:
            return pd.DataFrame(), "Upstox access token is empty; reconnect Upstox from the sidebar."
        response = requests.get(
            "https://api.upstox.com/v2/market-quote/quotes",
            params={"instrument_key": "GLOBAL_INDEX|BZUSD"},
            headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        payload = response.json()
        if not response.ok or payload.get("status") != "success":
            errors = payload.get("errors") or []
            detail = errors[0].get("message") if errors and isinstance(errors[0], dict) else payload.get("message")
            return pd.DataFrame(), f"Upstox Brent quote unavailable (HTTP {response.status_code}): {detail or 'check access token and instrument access'}"
        quotes = payload.get("data") or {}
        quote = quotes.get("GLOBAL_INDEX:BZUSD") or quotes.get("GLOBAL_INDEX|BZUSD")
        if not quote and len(quotes) == 1:
            quote = next(iter(quotes.values()))
        if not isinstance(quote, dict):
            return pd.DataFrame(), "Upstox did not return the Oil (Brent) quote."
        last_price = pd.to_numeric(quote.get("last_price"), errors="coerce")
        net_change = pd.to_numeric(quote.get("net_change"), errors="coerce")
        if pd.isna(last_price) or pd.isna(net_change) or float(last_price) <= 0:
            return pd.DataFrame(), "Upstox Brent quote is missing a valid price or daily change."
        ts = quote.get("timestamp") or quote.get("last_trade_time")
        if ts is not None:
            stamp = pd.to_datetime(float(ts), unit="ms", utc=True).tz_convert("Asia/Kolkata")
        else:
            stamp = datetime.now(ZoneInfo("Asia/Kolkata"))
        previous_close = float(last_price) - float(net_change)
        if previous_close <= 0:
            return pd.DataFrame(), "Upstox Brent quote returned an invalid previous-close calculation."
        # Two rows preserve the existing quote_snapshot interface; values are from the same Upstox quote.
        daily_change_pct = float(net_change) / previous_close * 100
        frame = pd.DataFrame([
            {"Date": stamp - pd.Timedelta(days=1), "Open": previous_close, "High": previous_close, "Low": previous_close, "Close": previous_close, "Volume": np.nan, "Source": "Upstox", "DailyChange": float(net_change), "DailyChangePct": daily_change_pct},
            {"Date": stamp, "Open": quote.get("ohlc", {}).get("open", last_price), "High": quote.get("ohlc", {}).get("high", last_price), "Low": quote.get("ohlc", {}).get("low", last_price), "Close": float(last_price), "Volume": quote.get("volume", np.nan), "Source": "Upstox", "DailyChange": float(net_change), "DailyChangePct": daily_change_pct},
        ])
        return frame, None
    except Exception as exc:
        return pd.DataFrame(), f"Upstox Brent quote failed: {type(exc).__name__}: {str(exc)[:160]}"


@st.cache_data(ttl=300, show_spinner=False)
def get_oilprice_brent_quote() -> tuple[pd.DataFrame, str | None]:
    """Fetch Brent price and daily move from OilPrice.com's public oil-price table.
    Quotes may be delayed; the retrieval timestamp is not represented as an exchange timestamp.
    """
    url = "https://oilprice.com/oil-price-charts/"
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MarketIntelligenceTerminal/1.0)", "Accept": "text/html"},
            timeout=15,
        )
        response.raise_for_status()
        tables = pd.read_html(response.text)
        price = change = pct = None
        for table in tables:
            if table.empty:
                continue
            table = table.copy()
            if isinstance(table.columns, pd.MultiIndex):
                table.columns = [" ".join(str(part) for part in col if str(part) != "nan").strip() for col in table.columns]
            for _, row in table.iterrows():
                values = [str(v).strip() for v in row.tolist()]
                if not values or not any(re.search(r"\bBrent Crude\b", v, re.I) for v in values[:2]):
                    continue
                # OilPrice tables typically expose: instrument, last, change, % change, last updated.
                numeric = []
                for value in values[1:]:
                    cleaned = value.replace(",", "").replace("$", "").replace("%", "").strip()
                    match = re.search(r"[-+]?\d+(?:\.\d+)?", cleaned)
                    if match:
                        numeric.append(float(match.group(0)))
                if len(numeric) >= 3:
                    price, change, pct = numeric[0], numeric[1], numeric[2]
                    break
            if price is not None:
                break
        if price is None or change is None or pct is None or price <= 0:
            return pd.DataFrame(), "OilPrice.com page did not expose a parsable Brent price, change and percentage."
        stamp = datetime.now(ZoneInfo("Asia/Kolkata"))
        previous_close = price - change
        if previous_close <= 0:
            return pd.DataFrame(), "OilPrice.com returned an invalid Brent previous-close calculation."
        frame = pd.DataFrame([
            {"Date": stamp - pd.Timedelta(days=1), "Open": previous_close, "High": previous_close, "Low": previous_close, "Close": previous_close, "Volume": np.nan, "Source": "OilPrice.com", "DailyChange": change, "DailyChangePct": pct},
            {"Date": stamp, "Open": price, "High": price, "Low": price, "Close": price, "Volume": np.nan, "Source": "OilPrice.com", "DailyChange": change, "DailyChangePct": pct},
        ])
        return frame, None
    except Exception as exc:
        return pd.DataFrame(), f"OilPrice.com Brent fetch failed ({type(exc).__name__})."


@st.cache_data(ttl=300, show_spinner=False)
def get_tradingeconomics_brent_quote() -> tuple[pd.DataFrame, str | None]:
    """Read Trading Economics' public Brent page as a fallback reference quote.
    This is a CFD-based market reference, not an official Brent futures settlement.
    """
    url = "https://tradingeconomics.com/commodity/brent-crude-oil"
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; MarketIntelligenceTerminal/1.0)", "Accept": "text/html"},
            timeout=15,
        )
        if not response.ok:
            return pd.DataFrame(), f"Trading Economics Brent page unavailable (HTTP {response.status_code})."
        tables = pd.read_html(response.text)
        price = change = None
        for table in tables:
            if table.empty:
                continue
            for _, row in table.iterrows():
                values = [str(v).strip() for v in row.tolist()]
                if not values or not any(re.search(r"\bBrent\b", v, re.I) for v in values[:2]):
                    continue
                numeric = []
                for value in values[1:]:
                    cleaned = value.replace(",", "").replace("%", "").strip()
                    try:
                        numeric.append(float(cleaned))
                    except Exception:
                        continue
                if len(numeric) >= 2 and numeric[0] > 0:
                    price, change = numeric[0], numeric[1]
                    break
            if price is not None:
                break
        if price is None or change is None:
            return pd.DataFrame(), "Trading Economics page did not expose a parsable Brent price/change."
        # The page summary gives the date of its latest daily observation; never pretend it is intraday.
        html_text = re.sub(r"<[^>]+>", " ", response.text)
        html_text = re.sub(r"\s+", " ", html_text)
        date_match = re.search(r"Brent rose to [0-9,.]+ USD/Bbl on ([A-Za-z]+ \d{1,2}, \d{4})", html_text, re.I)
        if date_match:
            stamp = pd.to_datetime(date_match.group(1), errors="coerce")
        else:
            return pd.DataFrame(), "Trading Economics page did not provide a reliable observation date."
        if pd.isna(stamp):
            return pd.DataFrame(), "Trading Economics Brent observation date could not be parsed."
        previous_close = float(price) - float(change)
        if previous_close <= 0:
            return pd.DataFrame(), "Trading Economics Brent quote returned an invalid previous-close calculation."
        daily_change_pct = float(change) / previous_close * 100
        frame = pd.DataFrame([
            {"Date": stamp - pd.Timedelta(days=1), "Open": previous_close, "High": previous_close, "Low": previous_close, "Close": previous_close, "Volume": np.nan, "Source": "Trading Economics", "DailyChange": float(change), "DailyChangePct": daily_change_pct},
            {"Date": stamp, "Open": price, "High": price, "Low": price, "Close": float(price), "Volume": np.nan, "Source": "Trading Economics", "DailyChange": float(change), "DailyChangePct": daily_change_pct},
        ])
        return frame, None
    except Exception as exc:
        return pd.DataFrame(), f"Trading Economics Brent fetch failed ({type(exc).__name__})."


@st.cache_data(ttl=21600, show_spinner=False)
def get_eia_brent_spot() -> tuple[pd.DataFrame, str | None]:
    """Fetch official EIA daily Europe Brent spot prices (not futures) via EIA API."""
    try:
        url = "https://api.eia.gov/v2/petroleum/pri/spt/data/"
        params = {
            "api_key": "DEMO_KEY",
            "frequency": "daily",
            "data[0]": "value",
            "facets[series][]": "RBRTE",
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "length": "30",
        }
        response = requests.get(url, params=params, timeout=15, headers={"User-Agent": "MarketIntelligenceTerminal/1.0"})
        response.raise_for_status()
        rows = response.json().get("response", {}).get("data", [])
        if not rows:
            return pd.DataFrame(), "EIA returned no daily Brent spot observations."
        frame = pd.DataFrame(rows)
        frame["Date"] = pd.to_datetime(frame["period"], errors="coerce")
        frame["Close"] = pd.to_numeric(frame["value"], errors="coerce")
        frame = frame.dropna(subset=["Date", "Close"]).sort_values("Date").drop_duplicates("Date", keep="last")
        if frame.empty:
            return pd.DataFrame(), "EIA Brent data contained no valid dated prices."
        frame["Open"] = frame["Close"]
        frame["High"] = frame["Close"]
        frame["Low"] = frame["Close"]
        frame["Volume"] = np.nan
        frame["Date"] = frame["Date"].dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")
        frame["Source"] = "EIA"
        return frame[["Date", "Open", "High", "Low", "Close", "Volume", "Source"]].reset_index(drop=True), None
    except Exception as exc:
        return pd.DataFrame(), f"EIA Brent fetch failed: {type(exc).__name__}: {str(exc)[:180]}"


def _yahoo_chart_history(ticker: str, period: str, interval: str) -> tuple[pd.DataFrame, str | None]:
    """Direct Yahoo chart endpoint fallback; returns only source-provided candles."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
        response = requests.get(
            url,
            params={"range": period, "interval": interval, "events": "history"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12,
        )
        response.raise_for_status()
        payload = response.json().get("chart", {})
        if payload.get("error"):
            return pd.DataFrame(), "Yahoo chart endpoint returned a source error."
        result = (payload.get("result") or [None])[0]
        if not result:
            return pd.DataFrame(), "Yahoo chart endpoint returned no result."
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        if not timestamps:
            return pd.DataFrame(), "Yahoo chart endpoint returned no candles."
        frame = pd.DataFrame({"Date": pd.to_datetime(timestamps, unit="s", utc=True).tz_convert("Asia/Kolkata")})
        for source_col, target_col in (("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close"), ("volume", "Volume")):
            values = quote.get(source_col)
            if values is not None:
                frame[target_col] = values
        frame["Source"] = "Yahoo chart API"
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            if col in frame:
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame = frame.dropna(subset=["Date", "Close"]).sort_values("Date").drop_duplicates("Date", keep="last")
        return frame.reset_index(drop=True), None
    except Exception as exc:
        return pd.DataFrame(), f"Yahoo chart fallback failed: {type(exc).__name__}"


@st.cache_data(ttl=120, show_spinner=False)
def get_history(ticker: str, period: str, interval: str) -> tuple[pd.DataFrame, str | None]:
    """Fetch Yahoo history and use the direct chart endpoint if it has a newer candle."""
    yf_error = None
    try:
        data = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False, actions=False, raise_errors=True)
        if data is not None and not data.empty:
            data = data.reset_index()
            date_col = "Datetime" if "Datetime" in data.columns else "Date"
            data = data.rename(columns={date_col: "Date"})
            data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
            if data["Date"].dt.tz is None:
                data["Date"] = data["Date"].dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")
            else:
                data["Date"] = data["Date"].dt.tz_convert("Asia/Kolkata")
            for col in ["Open", "High", "Low", "Close", "Volume"]:
                if col in data:
                    data[col] = pd.to_numeric(data[col], errors="coerce")
            data = data.dropna(subset=["Date", "Close"]).sort_values("Date").drop_duplicates("Date", keep="last")
            data["Source"] = "yfinance"
        else:
            data = pd.DataFrame()
            yf_error = "yfinance returned no rows."
    except Exception as exc:
        data = pd.DataFrame()
        yf_error = f"yfinance failed: {type(exc).__name__}"
    fallback, fallback_error = _yahoo_chart_history(ticker, period, interval)
    if not fallback.empty and (data.empty or fallback["Date"].max() > data["Date"].max()):
        return fallback, None
    if not data.empty:
        return data.reset_index(drop=True), None
    return pd.DataFrame(), yf_error or fallback_error or "Both Yahoo history routes returned no data."

# Actually invalidate only the short-lived Yahoo history cache once per new browser session.
# Keep longer-running research/scanner caches untouched to avoid expensive reloads.
if not st.session_state.get("_market_data_cache_cleared_on_open", False):
    get_history.clear()
    st.session_state["_market_data_cache_cleared_on_open"] = True

# Public-data universe used to make stock research less manual. Industry labels are static classifications,
# while prices are fetched live from Yahoo Finance and must be verified against source timestamps.
AUTO_UNIVERSE = {
    "RELIANCE.NS": "Energy", "TCS.NS": "IT", "INFY.NS": "IT", "HCLTECH.NS": "IT",
    "WIPRO.NS": "IT", "TECHM.NS": "IT", "HDFCBANK.NS": "Financials", "ICICIBANK.NS": "Financials",
    "SBIN.NS": "Financials", "KOTAKBANK.NS": "Financials", "AXISBANK.NS": "Financials",
    "BAJFINANCE.NS": "Financials", "BAJAJFINSV.NS": "Financials", "BHARTIARTL.NS": "Telecom",
    "ITC.NS": "FMCG", "HINDUNILVR.NS": "FMCG", "NESTLEIND.NS": "FMCG", "TATAMOTORS.NS": "Auto",
    "M&M.NS": "Auto", "MARUTI.NS": "Auto", "SUNPHARMA.NS": "Pharma", "DRREDDY.NS": "Pharma",
    "CIPLA.NS": "Pharma", "LT.NS": "Industrials", "ULTRACEMCO.NS": "Cement", "TATASTEEL.NS": "Metals",
    "JSWSTEEL.NS": "Metals", "NTPC.NS": "Utilities", "POWERGRID.NS": "Utilities", "ONGC.NS": "Energy",
    "COALINDIA.NS": "Energy", "ASIANPAINT.NS": "Consumer", "TITAN.NS": "Consumer", "ADANIPORTS.NS": "Infrastructure",
    "TRENT.NS": "Retail", "BEL.NS": "Industrials", "INDUSINDBK.NS": "Financials",
}

@st.cache_data(ttl=21600, show_spinner=False)
def get_auto_universe(period: str = "2y") -> pd.DataFrame:
    """Fetch actual daily OHLCV for a fixed large-cap universe; return empty data on source failure."""
    tickers = list(AUTO_UNIVERSE)
    try:
        raw = yf.download(tickers, period=period, interval="1d", auto_adjust=False, group_by="ticker",
                          threads=False, progress=False, actions=False)
        if raw is None or raw.empty:
            return pd.DataFrame()
        rows=[]
        for ticker in tickers:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if ticker not in raw.columns.get_level_values(0):
                        continue
                    part=raw[ticker].copy()
                else:
                    part=raw.copy()
                part=part.reset_index()
                date_col="Date" if "Date" in part.columns else "Datetime"
                if date_col not in part.columns or "Close" not in part.columns:
                    continue
                part=part.rename(columns={date_col:"Date"})
                part["Date"]=pd.to_datetime(part["Date"],errors="coerce")
                part["Symbol"]=ticker.replace(".NS","")
                part["Industry"]=AUTO_UNIVERSE[ticker]
                for c in ["Open","High","Low","Close","Volume"]:
                    if c in part.columns: part[c]=pd.to_numeric(part[c],errors="coerce")
                part=part.dropna(subset=["Date","Close"])
                if not part.empty:
                    rows.append(part[[c for c in ["Date","Symbol","Industry","Open","High","Low","Close","Volume"] if c in part.columns]])
            except Exception:
                continue
        return pd.concat(rows,ignore_index=True) if rows else pd.DataFrame()
    except Exception:
        return pd.DataFrame()

def get_news_rss(query: str, limit: int = 12) -> tuple[pd.DataFrame, str | None]:
    """Fetch public Google News RSS search results; headlines are attributed and never treated as verified causal explanations."""
    import xml.etree.ElementTree as ET
    from urllib.parse import quote_plus
    try:
        url = "https://news.google.com/rss/search?q=" + quote_plus(query) + "&hl=en-IN&gl=IN&ceid=IN:en"
        response = requests.get(url, headers={"User-Agent":"Mozilla/5.0 MarketIntelligenceTerminal/1.0"}, timeout=12)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        rows = []
        for item in root.findall(".//item")[:limit]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            source = item.find("source")
            publisher = (source.text or "").strip() if source is not None else ""
            published_at = pd.to_datetime(pub, errors="coerce", utc=True)
            if title and link:
                rows.append({"Published (RSS)":pub,"_published_sort":published_at,"Headline":title,"Publisher":publisher,"Source link":link})
        if not rows:
            return pd.DataFrame(), "RSS returned no headline rows."
        frame = pd.DataFrame(rows)
        # RSS relevance feeds can return mixed dates; sort newest first, with unparseable dates last.
        frame = frame.sort_values("_published_sort", ascending=False, na_position="last", kind="stable")
        frame = frame.drop(columns=["_published_sort"])
        return frame.reset_index(drop=True), None
    except Exception as exc:
        return pd.DataFrame(), f"News RSS unavailable: {type(exc).__name__}: {str(exc)[:150]}"


def indicators(data: pd.DataFrame) -> pd.DataFrame:
    out = data.copy()
    close = out["Close"]
    for n in [5, 11, 20, 50, 100, 200]:
        out[f"SMA {n}"] = close.rolling(n, min_periods=n).mean()
        out[f"EMA {n}"] = close.ewm(span=n, adjust=False, min_periods=n).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    out["RSI 14"] = 100 - 100 / (1 + rs)
    out.loc[(loss == 0) & gain.notna(), "RSI 14"] = 100
    if {"High", "Low"}.issubset(out.columns):
        out["Daily Range"] = out["High"] - out["Low"]
        for n in [5, 10, 14, 20]:
            out[f"ADR {n}"] = out["Daily Range"].rolling(n, min_periods=n).mean()
        prev_close = close.shift(1)
        tr = pd.concat([out["High"]-out["Low"], (out["High"]-prev_close).abs(), (out["Low"]-prev_close).abs()], axis=1).max(axis=1)
        out["ATR 14"] = tr.rolling(14, min_periods=14).mean()
    out["Return %"] = close.pct_change() * 100
    return out

def fmt(value, decimals=2):
    try:
        if pd.isna(value): return "—"
        return f"{float(value):,.{decimals}f}"
    except Exception:
        return "—"

def tv_widget(kind: str, config: dict, height: int = 500):
    config_json = json.dumps(config, ensure_ascii=False).replace("</", "<\\/")
    script = f'''<div class="tradingview-widget-container" style="height:{height}px;width:100%"><div class="tradingview-widget-container__widget" style="height:calc(100% - 28px);width:100%"></div><div class="tradingview-widget-copyright"><a href="https://www.tradingview.com/" rel="noopener nofollow" target="_blank"><span>TradingView</span></a></div><script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-{kind}.js" async>{config_json}</script></div>'''
    components.html(script, height=height + 10, scrolling=False)

def advanced_chart(symbol: str, height: int = 720):
    tv_widget("advanced-chart", {"autosize": True, "symbol": symbol, "interval": "D", "timezone": "Asia/Kolkata", "theme": "dark", "style": "1", "locale": "en", "allow_symbol_change": True, "calendar": False, "support_host": "https://www.tradingview.com"}, height)

def quote_snapshot(df: pd.DataFrame):
    if df.empty: return None
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else None
    # Quote APIs can provide a more reliable session change than deriving it from
    # synthetic rows or daily history. Use it whenever both explicit fields exist.
    explicit_change = pd.to_numeric(last.get("DailyChange"), errors="coerce")
    explicit_pct = pd.to_numeric(last.get("DailyChangePct"), errors="coerce")
    if pd.notna(explicit_change) and pd.notna(explicit_pct):
        change, pct = float(explicit_change), float(explicit_pct)
    else:
        last_close = pd.to_numeric(last.get("Close"), errors="coerce")
        prev_close = pd.to_numeric(prev.get("Close"), errors="coerce") if prev is not None else np.nan
        change = float(last_close - prev_close) if pd.notna(last_close) and pd.notna(prev_close) else np.nan
        pct = change / float(prev_close) * 100 if pd.notna(change) and pd.notna(prev_close) and float(prev_close) != 0 else np.nan
    return last, change, pct

# Pull key instruments. FYERS is the preferred source for authenticated live Indian quotes;
# historical candles remain available for indicator calculations and fallback.
market_data: dict[str, pd.DataFrame] = {}
market_errors: dict[str, str | None] = {}
fyers_snapshot_at = datetime.now(ZoneInfo("Asia/Kolkata"))
fyers_snapshot_symbols = {
    "NIFTY 50": "NSE:NIFTY50-INDEX",
    "BANK NIFTY": "NSE:NIFTYBANK-INDEX",
    "India VIX": "NSE:INDIAVIX-INDEX",
    "INFY": "NSE:INFY-EQ",
    "WIPRO": "NSE:WIPRO-EQ",
    "RELIANCE": "NSE:RELIANCE-EQ",
    "HDFCBANK": "NSE:HDFCBANK-EQ",
}
fyers_quote_lookup = {}
fyers_quote_error = "FYERS is not connected; using the configured historical source."
_active_fyers_token = st.session_state.get("fyers_access_token")
_fyers_cfg = fyers_credentials()
if _active_fyers_token and _fyers_cfg:
    _quote_symbols = tuple(fyers_snapshot_symbols.values())
    _quote_rows, fyers_quote_error = fyers_live_quotes_cached(_fyers_cfg[0], _active_fyers_token, _quote_symbols)
    if not fyers_quote_error:
        fyers_quote_lookup = {str(item.get("n", "")).upper(): item for item in _quote_rows if isinstance(item, dict)}
with st.spinner("Loading market data…"):
    for name, ticker in INDEXES.items():
        # Brent Crude remains exclusively sourced from OilPrice.com.
        if name == "Brent Crude":
            hist, err = get_oilprice_brent_quote()
        else:
            hist, err = get_history(ticker, period, "1d")
        fyers_symbol = fyers_snapshot_symbols.get(name)
        fyers_item = fyers_quote_lookup.get(str(fyers_symbol or "").upper())
        if fyers_item:
            live_frame = fyers_quote_frame(fyers_item, fyers_snapshot_at)
            if not live_frame.empty:
                hist, err = live_frame, None
        market_data[name], market_errors[name] = hist, err

@st.cache_data(ttl=180, show_spinner=False)
def get_nse_index_archive_latest() -> tuple[pd.DataFrame, str | None]:
    """Fetch several recent official NSE daily archives and select the newest actual trading date."""
    try:
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36",
            "Cache-Control": "no-cache, no-store, max-age=0",
            "Pragma": "no-cache",
        }
        candidates = []
        errors = []
        # Do not stop at the first valid file: an archive named for today can contain
        # an older session. Compare the actual Index Date inside every recent file.
        for offset in range(0, 8):
            day = today - pd.Timedelta(days=offset)
            response = None
            last_host_error = None
            # NSE serves the same official archive through two hostnames. Cloud
            # deployments can be blocked/rate-limited on one hostname while the
            # alternate archive host remains reachable.
            for archive_host in ("archives.nseindia.com", "nsearchives.nseindia.com"):
                url = f"https://{archive_host}/content/indices/ind_close_all_{day:%d%m%Y}.csv"
                try:
                    candidate_response = requests.get(
                        url, params={"_": int(time.time())}, headers=headers, timeout=12
                    )
                    if candidate_response.status_code == 404:
                        continue
                    candidate_response.raise_for_status()
                    if len(candidate_response.content) < 5000:
                        last_host_error = f"{archive_host}: short archive response"
                        continue
                    response = candidate_response
                    break
                except Exception as host_exc:
                    last_host_error = f"{archive_host}: {type(host_exc).__name__}"
            if response is None:
                if last_host_error:
                    errors.append(f"{day:%d-%b}: {last_host_error}")
                continue
            try:
                frame = pd.read_csv(pd.io.common.BytesIO(response.content))
                required = {"Index Name", "Index Date", "Open", "High", "Low", "Closing"}
                if not required.issubset(frame.columns):
                    errors.append(f"{day:%d-%b}: unexpected archive columns")
                    continue
                frame["Index Date"] = pd.to_datetime(frame["Index Date"], format="%d-%m-%Y", errors="coerce")
                frame = frame.dropna(subset=["Index Date"])
                if frame.empty:
                    continue
                for col in ["Open", "High", "Low", "Closing", "Volume", "Points Change", "Change(%)"]:
                    if col in frame:
                        frame[col] = pd.to_numeric(frame[col].astype(str).str.replace(",", "", regex=False), errors="coerce")
                candidates.append(frame)
            except Exception as exc:
                errors.append(f"{day:%d-%b}: {type(exc).__name__}")
        if not candidates:
            detail = "; ".join(errors[-3:])
            return pd.DataFrame(), "No usable recent NSE index archive was returned." + (f" Details: {detail}" if detail else "")
        combined = pd.concat(candidates, ignore_index=True)
        combined = combined.sort_values("Index Date").drop_duplicates(
            subset=["Index Name", "Index Date"], keep="last"
        )
        latest_date = combined["Index Date"].max()
        latest = combined[combined["Index Date"] == latest_date].copy()
        latest.attrs["archive_latest_date"] = latest_date.strftime("%d %b %Y")
        return latest, None
    except Exception as exc:
        return pd.DataFrame(), f"NSE archive unavailable: {type(exc).__name__}"

def apply_nse_index_archive(market_data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """Append newer official NSE EOD bars to Yahoo history for Indian benchmark indices."""
    archive, archive_error = get_nse_index_archive_latest()
    if archive.empty:
        return market_data
    aliases = {
        "NIFTY 50": {"nifty50", "nifty 50"},
        "BANK NIFTY": {"niftybank", "nifty bank", "bank nifty"},
        "India VIX": {"indiavix", "india vix"},
    }
    for dashboard_name, names in aliases.items():
        matches = archive[archive["Index Name"].astype(str).str.strip().str.lower().isin(names)]
        if matches.empty:
            continue
        source_row = matches.sort_values("Index Date").iloc[-1]
        official_bar = {
            "Date": source_row["Index Date"].tz_localize("Asia/Kolkata"),
            "Open": source_row.get("Open", np.nan),
            "High": source_row.get("High", np.nan),
            "Low": source_row.get("Low", np.nan),
            "Close": source_row.get("Closing", np.nan),
            "Volume": source_row.get("Volume", np.nan),
            "Source": "NSE India official archive",
        }
        current = market_data.get(dashboard_name, pd.DataFrame()).copy()
        if current.empty:
            market_data[dashboard_name] = pd.DataFrame([official_bar])
            continue
        current_dates = pd.to_datetime(current["Date"], errors="coerce")
        if current_dates.dt.tz is None:
            current_dates = current_dates.dt.tz_localize("Asia/Kolkata")
        else:
            current_dates = current_dates.dt.tz_convert("Asia/Kolkata")
        latest_current = current_dates.max()
        if pd.isna(latest_current) or official_bar["Date"] > latest_current:
            market_data[dashboard_name] = pd.concat([current, pd.DataFrame([official_bar])], ignore_index=True).sort_values("Date").drop_duplicates("Date", keep="last").reset_index(drop=True)
    return market_data


market_data = apply_nse_index_archive(market_data)

now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
# Hidden refresh/source note retained in code to keep the dashboard focused on key headings and values.

# Headline metrics from the latest returned historical bar (not described as real-time).
headline_names = ["NIFTY 50", "BANK NIFTY", "India VIX", "Brent Crude", "USD/INR"]
# Responsive headline cards: keep full numeric values readable on narrow mobile screens.
st.markdown("""
<style>
[data-testid="stMetricValue"] {
    font-size: clamp(1.05rem, 2.2vw, 1.65rem) !important;
    white-space: normal !important;
    overflow-wrap: anywhere !important;
    line-height: 1.2 !important;
}
[data-testid="stMetricLabel"] {
    font-size: clamp(0.72rem, 1.5vw, 0.9rem) !important;
    white-space: normal !important;
}
[data-testid="stMetricDelta"] {
    font-size: clamp(0.68rem, 1.35vw, 0.82rem) !important;
    white-space: normal !important;
    overflow-wrap: anywhere !important;
}
</style>
""", unsafe_allow_html=True)
cols = st.columns(5)
for col, name in zip(cols, headline_names):
    df = market_data[name]
    snap = quote_snapshot(df)
    if snap:
        last, change, pct = snap
        # Brent Crude is sourced exclusively from OilPrice.com; its public quote may be delayed.
        if name in ("NIFTY 50", "BANK NIFTY", "India VIX"):
            benchmark_date = pd.to_datetime(last["Date"], errors="coerce")
            benchmark_age_days = (now_ist.date() - benchmark_date.date()).days if pd.notna(benchmark_date) else 999
            if benchmark_age_days > 2:
                col.metric(name, "STALE", f"Last source date · {benchmark_date.strftime('%d %b %Y') if pd.notna(benchmark_date) else 'unknown'}", delta_color="off")
                continue
        if name == "Brent Crude":
            brent_date = pd.to_datetime(last["Date"])
            brent_age_days = (now_ist.date() - brent_date.date()).days
            brent_source = str(last.get("Source", "EIA")).strip() if "Source" in df.columns else "EIA"
            is_eia_source = brent_source.upper().startswith("EIA")
            if is_eia_source and brent_age_days > 3:
                col.metric(name, fmt(last["Close"]), f"STALE · EIA date {brent_date.strftime('%d %b %Y')}", delta_color="off")
                continue
            # Prefer a source-provided daily move. Otherwise derive it from the latest two
            # genuine observations; never display a fabricated zero when a percentage is missing.
            if brent_source in ("Upstox", "Trading Economics") and pd.notna(last.get("DailyChange")):
                change = float(last["DailyChange"])
                if pd.notna(last.get("DailyChangePct")):
                    pct = float(last["DailyChangePct"])
                elif pd.notna(last.get("Close")) and float(last["Close"]) - change > 0:
                    pct = change / (float(last["Close"]) - change) * 100
            if pd.isna(pct) and len(df) >= 2:
                brent_closes = pd.to_numeric(df["Close"], errors="coerce").dropna()
                if len(brent_closes) >= 2 and float(brent_closes.iloc[-2]) != 0:
                    change = float(brent_closes.iloc[-1] - brent_closes.iloc[-2])
                    pct = float(change / brent_closes.iloc[-2] * 100)
        # Inverse-impact instruments: lower USD/INR (stronger rupee) and lower India VIX
        # (less expected volatility) are generally supportive; higher values are headwinds.
        if name == "USD/INR":
            delta_text = f"{fmt(change)} ({fmt(pct)}%)"
            delta_text += " · INR stronger" if change < 0 else (" · INR weaker" if change > 0 else "")
            col.metric(name, fmt(last["Close"]), delta_text, delta_color="inverse")
        elif name == "India VIX":
            delta_text = f"{fmt(change)} ({fmt(pct)}%)"
            delta_text += " · volatility easing" if change < 0 else (" · volatility rising" if change > 0 else "")
            col.metric(name, fmt(last["Close"]), delta_text, delta_color="inverse")
        else:
            col.metric(name, fmt(last["Close"]), f"{fmt(change)} ({fmt(pct)}%)")
        # Source-bar timestamp retained in data, but hidden from the compact headline display.
    else:
        col.metric(name, "Unavailable")
        # Error details remain available in market_errors; avoid displaying extra helper text here.

# Show the actual source-candle date so a page-refresh timestamp cannot disguise stale prices.
headline_dates = []
for _name in headline_names:
    _frame = market_data.get(_name, pd.DataFrame())
    if not _frame.empty and "Date" in _frame.columns:
        _date = pd.to_datetime(_frame["Date"], errors="coerce").dropna()
        if not _date.empty:
            _source = str(_frame.iloc[-1].get("Source", "Yahoo Finance"))
            _age = (now_ist.date() - _date.iloc[-1].date()).days
            _freshness = " · STALE" if _name in ("NIFTY 50", "BANK NIFTY", "India VIX") and _age > 2 else ""
            headline_dates.append(f"{_name}: {_date.iloc[-1]:%d %b} ({_source}){_freshness}")
    else:
        headline_dates.append(f"{_name}: unavailable")
st.caption("Actual source candle dates · " + "  |  ".join(headline_dates))

# Daily global-cue sentiment panel is intentionally placed immediately above Market Regime.
st.subheader("Today's Market Direction · Global Sentiment")
# Hidden sentiment explanation retained here in code; display the main heading and sentiment output.
sentiment_parts = []
sentiment_details = []
sentiment_errors = []
def _sentiment_change(label, ticker, weight, inverse=False):
    try:
        # Yahoo Finance can leave the London-listed Reliance GDR on the prior session.
        # Use Sharecast's current quote for this instrument and never present a stale Yahoo bar as today's move.
        if ticker == "RIGD.L":
            rigd_url = "https://www.sharecast.com/equity/Reliance_Industries_Ltd_GDR_144A/charts"
            rigd_response = requests.get(
                rigd_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36"},
                timeout=12,
            )
            rigd_response.raise_for_status()
            rigd_text = html.unescape(re.sub(r"<[^>]+>", " ", rigd_response.text))
            rigd_text = re.sub(r"\s+", " ", rigd_text)
            rigd_pct_match = re.search(r"%\s*Change\s*([+-]?\d+(?:\.\d+)?)\s*%", rigd_text, re.IGNORECASE)
            rigd_date_match = re.search(r"(\d{1,2}:\d{2})\s+(\d{1,2}/\d{1,2}/\d{2})", rigd_text)
            if not rigd_pct_match:
                raise ValueError("Sharecast did not provide a parseable current percentage change")
            pct = float(rigd_pct_match.group(1))
            score = 1 if pct > 0 else -1 if pct < 0 else 0
            sentiment_parts.append((score, weight))
            sentiment_signal = "Positive" if score > 0 else "Negative" if score < 0 else "Flat"
            quote_stamp = f"{rigd_date_match.group(2)} {rigd_date_match.group(1)} UK time" if rigd_date_match else "current quote; timestamp unavailable"
            sentiment_details.append({"Factor": label, "Weight": f"{weight * 100:g}%", "Latest change": f"{pct:+.2f}%", "Signal": sentiment_signal, "Source bar": f"Sharecast · {quote_stamp}"})
            return
        adr_tickers = {"INFY", "WIT", "HDB", "IBN"}
        if ticker in adr_tickers:
            # Ignore page-level timestamps: some quote pages advance their header while
            # retaining prior-session rows. Only accept a dated Yahoo daily bar.
            frame, err = get_history(ticker, "1mo", "1d")
            snap = quote_snapshot(frame)
            if snap:
                last, change, pct = snap
                adr_date = pd.to_datetime(last["Date"], errors="coerce")
                adr_age_days = (now_ist.date() - adr_date.date()).days if pd.notna(adr_date) else 999
                if pd.notna(pct) and 0 <= adr_age_days <= 2:
                    pct = float(pct)
                    score = 1 if pct > 0 else -1 if pct < 0 else 0
                    sentiment_parts.append((score, weight))
                    sentiment_signal = "Positive" if score > 0 else "Negative" if score < 0 else "Flat"
                    sentiment_details.append({"Factor": label, "Weight": f"{weight * 100:g}%", "Latest change": f"{pct:+.2f}%", "Signal": sentiment_signal, "Source bar": f'{adr_date:%d %b %Y} · Yahoo Finance daily bar'})
                    return
            sentiment_errors.append(f"{label}: excluded because the latest Yahoo ADR daily bar is older than 2 calendar days or unavailable")
            return
        frame, err = get_history(ticker, "1mo", "1d")
        snap = quote_snapshot(frame)
        if not snap:
            sentiment_errors.append(f"{label}: unavailable")
            return
        last, change, pct = snap
        pct = float(pct)
        score = -1 if pct > 0 else 1 if pct < 0 else 0 if inverse else (1 if pct > 0 else -1 if pct < 0 else 0)
        # For crude, rising prices are a headwind for India; for ADRs/GIFT, positive moves are supportive.
        if inverse:
            score = -1 if pct > 0 else 1 if pct < 0 else 0
        else:
            score = 1 if pct > 0 else -1 if pct < 0 else 0
        sentiment_parts.append((score, weight))
        # Signal is India-market impact, not simply whether the instrument rose or fell.
        # Rising Brent is negative for India; falling Brent is positive. ADRs follow their price move.
        sentiment_signal = ("Positive" if score > 0 else "Negative" if score < 0 else "Flat")
        sentiment_details.append({"Factor": label, "Weight": f"{weight * 100:g}%", "Latest change": f"{pct:+.2f}%", "Signal": sentiment_signal, "Source bar": last["Date"].strftime("%d %b %Y")})
    except Exception:
        sentiment_errors.append(f"{label}: unavailable")

# Try multiple public GIFT Nifty quote pages because individual sites may block cloud-hosted requests.
# These public quotes can be delayed/indicative; never silently substitute a different instrument.
gift_sources = [
    ("GiftCityNifty.com", "https://giftcitynifty.com/"),
    ("Investing.com GIFT Nifty futures", "https://www.investing.com/indices/gift-nifty-50-c1-futures"),
    ("Moneycontrol indicative quote", "https://www.moneycontrol.com/live-index/gift-nifty"),
]
gift_loaded = False
gift_failures = []
for gift_source_name, gift_url in gift_sources:
    try:
        gift_response = requests.get(
            gift_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36",
                "Accept-Language": "en-IN,en;q=0.9",
            },
            timeout=10,
        )
        gift_response.raise_for_status()
        gift_html = gift_response.text
        gift_text = html.unescape(re.sub(r"<[^>]+>", " ", gift_html))
        gift_text = re.sub(r"\s+", " ", gift_text)
        # First inspect the browser-rendered page's usual text pattern; also search raw HTML
        # because some site templates keep quote values in embedded JSON/data attributes.
        gift_upper = gift_text.upper()
        gift_anchor = gift_upper.find("GIFT NIFTY NSE IFSC")
        if gift_anchor < 0:
            gift_anchor = gift_upper.find("GIFT NIFTY")
        gift_search_text = gift_text[gift_anchor:gift_anchor + 8000] if gift_anchor >= 0 else gift_text[:8000]
        # Require an actual traded GIFT Nifty level as well as its move.
        # GiftCityNifty publishes a sentence like: "Gift Nifty is trading at
        # 23,561.50, up 159.00 (+0.68%)". Do not mistake the point change for LTP.
        gift_quote_patterns = [
            re.compile(
                r"gift\s*nifty(?:\s+nse\s+ifsc)?\s+is\s+trading\s+at\s+([\d,]+\.\d{1,2})\s*,?\s*(up|down)\s+([\d,]+\.\d{1,2})\s*\(\s*([+-]?\d+\.\d{1,2})\s*%\s*\)",
                re.IGNORECASE,
            ),
            re.compile(
                r"(?:last\s*price|current\s*price|ltp)\s*[:|]?\s*([\d,]+\.\d{1,2}).{0,180}?(?:change|up|down)\s*[:|]?\s*([+-]?[\d,]+\.\d{1,2}).{0,80}?([+-]?\d+\.\d{1,2})\s*%",
                re.IGNORECASE,
            ),
        ]
        gift_quote = None
        for pattern in gift_quote_patterns:
            gift_quote = pattern.search(gift_text)
            if gift_quote:
                break
        if not gift_quote:
            gift_failures.append(f"{gift_source_name}: dated quote level and change not found")
            continue
        if gift_quote.lastindex == 4:
            gift_last_price = float(gift_quote.group(1).replace(",", ""))
            gift_direction = gift_quote.group(2).lower()
            gift_change_points = float(gift_quote.group(3).replace(",", ""))
            gift_change_pct = float(gift_quote.group(4))
            if gift_direction == "down":
                gift_change_points = -abs(gift_change_points)
                gift_change_pct = -abs(gift_change_pct)
            else:
                gift_change_points = abs(gift_change_points)
                gift_change_pct = abs(gift_change_pct)
        else:
            gift_last_price = float(gift_quote.group(1).replace(",", ""))
            gift_change_points = float(gift_quote.group(2).replace(",", ""))
            gift_change_pct = float(gift_quote.group(3))
        if gift_last_price <= 0:
            gift_failures.append(f"{gift_source_name}: invalid GIFT Nifty level")
            continue
        gift_score = 1 if gift_change_pct > 0 else -1 if gift_change_pct < 0 else 0

        # Keep the existing 60% GIFT weight when it is positive.
        # When negative, scale its influence by the size of the point drop:
        # 30-59 pts = 10%, 60-89 = 20%, 90-119 = 30%,
        # 120-149 = 50%, and 150+ = 60%. Below 30 negative points = 0%.
        if gift_change_points < 0:
            gift_drop_points = abs(gift_change_points)
            if gift_drop_points >= 150:
                gift_weight = 0.60
            elif gift_drop_points >= 120:
                gift_weight = 0.50
            elif gift_drop_points >= 90:
                gift_weight = 0.30
            elif gift_drop_points >= 60:
                gift_weight = 0.20
            elif gift_drop_points >= 30:
                gift_weight = 0.10
            else:
                gift_weight = 0.0
        else:
            gift_weight = 0.60

        sentiment_parts.append((gift_score, gift_weight))
        gift_asof_match = re.search(r"Last updated:\s*([^·]{1,80})", gift_search_text, flags=re.IGNORECASE)
        gift_asof = gift_asof_match.group(1).strip()[:24] if gift_asof_match else "Timestamp unavailable"
        sentiment_details.append({
            "Factor": f"GIFT Nifty · {gift_source_name}",
            "Weight": f"{gift_weight * 100:g}%",
            "Latest change": f"{gift_last_price:,.2f} · {gift_change_pct:+.2f}% ({gift_change_points:+,.2f} pts)",
            "Signal": "Positive" if gift_score > 0 else "Negative" if gift_score < 0 else "Flat",
            "Source bar": gift_asof,
        })
        gift_loaded = True
        break
    except Exception as exc:
        gift_failures.append(f"{gift_source_name}: {exc.__class__.__name__}")
if not gift_loaded:
    # Never display a remembered GIFT Nifty value as if it were current.
    # Missing live data remains unavailable and contributes no sentiment score.
    sentiment_details.append({
        "Factor": "GIFT Nifty · live quote unavailable",
        "Weight": "60% (not scored)",
        "Latest change": "Unavailable",
        "Signal": "Unavailable",
        "Source bar": "No verified current source quote",
    })
    sentiment_errors.append("GIFT Nifty live quote unavailable; excluded from the weighted sentiment score (" + "; ".join(gift_failures) + ")")
# Prefer Upstox's global Oil (Brent) quote; use EIA only as a dated fallback.
try:
    brent_frame = market_data.get("Brent Crude", pd.DataFrame())
    brent_snap = quote_snapshot(brent_frame)
    if brent_snap:
        last, change, pct = brent_snap
        brent_date = pd.to_datetime(last["Date"])
        brent_source = str(last.get("Source", "EIA")).strip() if "Source" in brent_frame.columns else "EIA"
        brent_age_days = (now_ist.date() - brent_date.date()).days
        if brent_source.upper().startswith("EIA") and brent_age_days > 3:
            sentiment_errors.append(f"Brent crude excluded from sentiment: EIA observation is stale ({brent_date.strftime('%Y-%m-%d')}, {brent_age_days} days old)")
        else:
            contribution = -float(np.clip(pct, -5, 5)) * 0.10
            sentiment_parts.append((contribution, 0.10))
            sentiment_details.append({
                "Factor": "Brent crude (inverse impact)",
                "Latest change": f"{fmt(change)} ({fmt(pct)}%)",
                "Signal": "Positive" if change < 0 else "Negative" if change > 0 else "Neutral",
                "Source bar": brent_date.strftime("%Y-%m-%d %H:%M IST") + " · " + brent_source,
            })
    else:
        sentiment_errors.append("Brent crude: Upstox and EIA sources unavailable")
except Exception as exc:
    sentiment_errors.append("Brent crude source error: " + type(exc).__name__)
_sentiment_change("USD/INR (inverse impact)", "INR=X", 0.05, inverse=True)

# Fetch the latest NSE-published cash-market FII/FPI and DII net flows.
# Prefer NSE's JSON endpoint; retain HTML-table parsing as a fallback. Never invent values.
def _nse_fii_dii_latest():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-IN,en;q=0.9",
        "Referer": "https://www.nseindia.com/reports/fii-dii",
        "Connection": "keep-alive",
    }

    def _number(value):
        if value is None:
            return np.nan
        return pd.to_numeric(str(value).replace(",", "").replace("₹", "").strip(), errors="coerce")

    def _date_text(value):
        parsed = pd.to_datetime(str(value).strip(), errors="coerce", dayfirst=True)
        return parsed.strftime("%d %b %Y") if pd.notna(parsed) else str(value).strip() or "Date unavailable"

    def _normalize_records(records):
        if isinstance(records, dict):
            records = records.get("data", records.get("dataTable", records.get("rows", [records])))
        if not isinstance(records, list):
            return {}
        rows = []
        for item in records:
            if not isinstance(item, dict):
                continue
            lowered = {str(k).strip().lower(): v for k, v in item.items()}
            category = str(lowered.get("category", lowered.get("investorcategory", ""))).upper()
            if "FII" in category or "FPI" in category:
                key = "FII/FPI"
            elif "DII" in category:
                key = "DII"
            else:
                # Some NSE API responses provide the two categories as paired fields.
                continue
            net_raw = lowered.get("netvalue", lowered.get("net value", lowered.get("net", None)))
            if net_raw is None:
                net_raw = lowered.get("fiinet" if key == "FII/FPI" else "diinet")
            net_value = _number(net_raw)
            if pd.isna(net_value):
                continue
            date_raw = lowered.get("date", lowered.get("tradedate", ""))
            parsed_date = pd.to_datetime(str(date_raw), errors="coerce", dayfirst=True)
            rows.append((key, float(net_value), parsed_date, _date_text(date_raw)))
        if not rows:
            # Alternate API shape: one record may contain fiinet/diinet and one shared date.
            for item in records:
                if not isinstance(item, dict):
                    continue
                lowered = {str(k).strip().lower(): v for k, v in item.items()}
                date_raw = lowered.get("date", "")
                parsed_date = pd.to_datetime(str(date_raw), errors="coerce", dayfirst=True)
                for key, field in [("FII/FPI", "fiinet"), ("DII", "diinet")]:
                    value = _number(lowered.get(field))
                    if pd.notna(value):
                        rows.append((key, float(value), parsed_date, _date_text(date_raw)))
        result = {}
        for key, net_value, parsed_date, date_label in rows:
            current = result.get(key)
            if current is None or (pd.notna(parsed_date) and (pd.isna(current.get("_parsed_date")) or parsed_date > current["_parsed_date"])):
                result[key] = {"net": net_value, "date": date_label, "_parsed_date": parsed_date}
        # Only publish the pair when both categories have a valid, matching reporting date.
        # This prevents a newer FII value being combined with an older DII value.
        if "FII/FPI" not in result or "DII" not in result:
            return {}
        fii_date = result["FII/FPI"].get("_parsed_date")
        dii_date = result["DII"].get("_parsed_date")
        # Compare parsed dates when possible. If the source date format cannot be parsed,
        # accept only identical normalized date labels instead of suppressing valid values.
        dates_match = (
            pd.notna(fii_date) and pd.notna(dii_date)
            and pd.Timestamp(fii_date).date() == pd.Timestamp(dii_date).date()
        )
        if pd.isna(fii_date) or pd.isna(dii_date):
            fii_label = re.sub(r"[^a-z0-9]", "", str(result["FII/FPI"].get("date", "")).lower())
            dii_label = re.sub(r"[^a-z0-9]", "", str(result["DII"].get("date", "")).lower())
            dates_match = bool(fii_label) and fii_label == dii_label
        if not dates_match:
            return {}
        for value in result.values():
            value.pop("_parsed_date", None)
        return result

    session = requests.Session()
    try:
        # Warm the session to obtain the NSE cookies required by its data endpoint.
        session.get("https://www.nseindia.com/", headers=headers, timeout=10)
        api_response = session.get("https://www.nseindia.com/api/fiidiiTradeReact", headers=headers, timeout=15)
        api_response.raise_for_status()
        api_data = api_response.json()
        normalized = _normalize_records(api_data)
        if normalized:
            return normalized
    except Exception:
        pass

    # Fallback: parse the official NSE report tables if the JSON endpoint is blocked or changes.
    try:
        page_response = session.get("https://www.nseindia.com/reports/fii-dii", headers=headers, timeout=15)
        page_response.raise_for_status()
        for table in pd.read_html(page_response.text):
            table = table.copy()
            if isinstance(table.columns, pd.MultiIndex):
                table.columns = [" ".join(str(part) for part in col if str(part) != "nan").strip() for col in table.columns]
            cols = {str(col).strip().lower(): col for col in table.columns}
            category_col = next((col for name, col in cols.items() if "category" in name), None)
            net_col = next((col for name, col in cols.items() if "net value" in name), None)
            date_col = next((col for name, col in cols.items() if name == "date" or name.endswith(" date") or "date" in name), None)
            if category_col is None or net_col is None:
                continue
            records = []
            for _, row in table.iterrows():
                category = str(row.get(category_col, "")).strip().upper()
                if "FII" not in category and "FPI" not in category and "DII" not in category:
                    continue
                records.append({
                    "category": category,
                    "date": row.get(date_col, "") if date_col is not None else "",
                    "netValue": row.get(net_col),
                })
            normalized = _normalize_records(records)
            if normalized:
                return normalized
    except Exception:
        pass

    # Last-resort fallback: independent public collector that publishes NSE-sourced
    # daily cash-market flows as JSON. Reject placeholders, stale dates, and mismatched values.
    try:
        fallback_url = "https://chirag127.github.io/fii-dii-activity-api/data/latest.json"
        fallback_response = requests.get(
            fallback_url,
            headers={"User-Agent": headers["User-Agent"], "Accept": "application/json"},
            timeout=10,
        )
        fallback_response.raise_for_status()
        payload = fallback_response.json()
        source_name = str(payload.get("source", "")).strip().lower()
        date_raw = payload.get("date", "")
        parsed_date = pd.to_datetime(str(date_raw), errors="coerce", dayfirst=True)
        today = pd.Timestamp.now(tz="Asia/Kolkata").date()
        if source_name not in {"", "placeholder"} and pd.notna(parsed_date) and 0 <= (today - parsed_date.date()).days <= 5:
            equity = payload.get("equity", {})
            fii_net = _number(equity.get("fii_net"))
            dii_net = _number(equity.get("dii_net"))
            if pd.notna(fii_net) and pd.notna(dii_net):
                date_label = _date_text(date_raw)
                label = f"Public FII/DII fallback ({source_name})"
                return {
                    "FII/FPI": {"net": float(fii_net), "date": date_label, "source": label},
                    "DII": {"net": float(dii_net), "date": date_label, "source": label},
                }
    except Exception:
        pass

    # Secondary public-source fallback: Groww publishes the same daily cash-market flow
    # figures in a structured HTML table when NSE endpoints are blocked for cloud hosts.
    try:
        groww_response = requests.get(
            "https://groww.in/fii-dii-data",
            headers={
                "User-Agent": headers["User-Agent"],
                "Accept-Language": "en-IN,en;q=0.9",
            },
            timeout=12,
        )
        groww_response.raise_for_status()
        for table in pd.read_html(groww_response.text):
            table = table.copy()
            if isinstance(table.columns, pd.MultiIndex):
                table.columns = [" ".join(str(part) for part in col if str(part) != "nan").strip() for col in table.columns]
            column_names = {str(col).strip().lower(): col for col in table.columns}
            date_col = next((col for name, col in column_names.items() if name == "date" or "date" in name), None)
            fii_col = next((col for name, col in column_names.items() if "fii" in name and ("net" in name or "buy/sell" in name)), None)
            dii_col = next((col for name, col in column_names.items() if "dii" in name and ("net" in name or "buy/sell" in name)), None)
            if fii_col is None or dii_col is None:
                # Groww's daily table may use grouped headers; inspect flattened labels.
                continue
            for _, row in table.iterrows():
                date_raw = row.get(date_col, "") if date_col is not None else ""
                parsed_date = pd.to_datetime(str(date_raw), errors="coerce", dayfirst=True)
                today = pd.Timestamp.now(tz="Asia/Kolkata").date()
                if pd.isna(parsed_date) or not 0 <= (today - parsed_date.date()).days <= 5:
                    continue
                fii_net = _number(row.get(fii_col))
                dii_net = _number(row.get(dii_col))
                if pd.notna(fii_net) and pd.notna(dii_net):
                    date_label = _date_text(date_raw)
                    label = "Groww (NSE cash-market data)"
                    return {
                        "FII/FPI": {"net": float(fii_net), "date": date_label, "source": label},
                        "DII": {"net": float(dii_net), "date": date_label, "source": label},
                    }
    except Exception:
        pass
    return {}

fii_dii_flows = _nse_fii_dii_latest()
for flow_name, flow_weight in [("FII/FPI", 0.025), ("DII", 0.025)]:
    flow = fii_dii_flows.get(flow_name)
    if flow is None:
        sentiment_errors.append(f"{flow_name} cash-market flow: unavailable from configured sources")
        sentiment_details.append({"Factor": f"{flow_name} net flow (cash market)", "Weight": f"{flow_weight * 100:g}%", "Latest change": "Unavailable", "Signal": "Unavailable", "Source bar": "No verified data retrieved"})
        continue
    net_flow = flow["net"]
    flow_score = 1 if net_flow > 0 else -1 if net_flow < 0 else 0
    sentiment_parts.append((flow_score, flow_weight))
    sentiment_details.append({
        "Factor": f"{flow_name} net flow (cash market)",
        "Weight": f"{flow_weight * 100:g}%",
        "Latest change": f"₹{net_flow:+,.2f} crore",
        "Signal": "Positive" if flow_score > 0 else "Negative" if flow_score < 0 else "Flat",
        "Source bar": f'{flow["date"]} · {flow.get("source", "NSE")}',
    })
_sentiment_change("Infosys ADR", "INFY", 0.10, inverse=False)
_sentiment_change("Wipro ADR", "WIT", 0.00, inverse=False)
_sentiment_change("HDFC Bank ADR", "HDB", 0.10, inverse=False)
_sentiment_change("ICICI Bank ADR", "IBN", 0.00, inverse=False)
_sentiment_change("Reliance Industries GDR", "RIGD.L", 0.00, inverse=False)
# Do not publish a confident full-market direction when the highest-weight GIFT Nifty cue is missing.
# Partial score remains visible for transparency, but the headline is explicitly marked incomplete.
# Only a quote successfully parsed during this run counts as live/usable GIFT Nifty.
# The stale reference row is displayed for context but must never satisfy this check.
gift_available = bool(gift_loaded)
sentiment_weight = sum(weight for _, weight in sentiment_parts)
sentiment_score = sum(score * weight for score, weight in sentiment_parts) / sentiment_weight if sentiment_weight else np.nan
if not gift_available:
    sentiment_color, sentiment_label = "#f59e0b", "INCOMPLETE · GIFT Nifty unavailable"
elif pd.notna(sentiment_score) and sentiment_score >= 0.25:
    sentiment_color, sentiment_label = "#22c55e", "POSITIVE · Bullish bias"
elif pd.notna(sentiment_score) and sentiment_score <= -0.25:
    sentiment_color, sentiment_label = "#ef4444", "NEGATIVE · Bearish bias"
elif pd.notna(sentiment_score):
    sentiment_color, sentiment_label = "#f59e0b", "MIXED · Wait for confirmation"
else:
    sentiment_color, sentiment_label = "#94a3b8", "UNAVAILABLE · Insufficient market cues"
# Compact layout: keep the existing font sizes, put the summary in a single horizontal strip,
# then give the complete factor table the full available width.
st.markdown(
    f'<div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px 22px;'
    f'border:1px solid #343a46;border-left:5px solid {sentiment_color};border-radius:12px;'
    f'padding:10px 14px;margin-bottom:10px;">'
    f'<div style="font-size:13px;color:#aeb6c2;">PREDICTED NIFTY DIRECTION</div>'
    f'<div style="font-size:clamp(19px,2.8vw,27px);font-weight:750;color:{sentiment_color};">{sentiment_label}</div>'
    f'<div style="font-size:12px;color:#aeb6c2;">Weighted cue score: {f"{sentiment_score:+.2f}" if pd.notna(sentiment_score) else "—"}</div>'
    f'<div style="font-size:12px;color:#aeb6c2;">Updated {now_ist:%d %b %Y %H:%M IST}</div>'
    f'</div>',
    unsafe_allow_html=True,
)
if sentiment_details:
    # Keep factor weights in the scoring logic, but hide the Weight column from the dashboard table.
    sentiment_df = pd.DataFrame(sentiment_details).drop(columns=["Weight"], errors="ignore")

    def _sentiment_cell_style(value):
        text_value = str(value).strip().lower()
        if text_value == "positive":
            return "color: #22c55e; font-weight: 700; background-color: rgba(34,197,94,0.10)"
        if text_value == "negative":
            return "color: #ef4444; font-weight: 700; background-color: rgba(239,68,68,0.10)"
        if text_value == "flat":
            return "color: #f59e0b; font-weight: 700; background-color: rgba(245,158,11,0.10)"
        return ""

    def _sentiment_change_style(row):
        signal = str(row.get("Signal", "")).lower()
        if signal == "positive":
            color = "#22c55e"
        elif signal == "negative":
            color = "#ef4444"
        else:
            color = "#f59e0b"
        return [f"color: {color}; font-weight: 600" if col == "Latest change" else "" for col in row.index]

    styled_sentiment = sentiment_df.style.applymap(_sentiment_cell_style, subset=["Signal"]).apply(_sentiment_change_style, axis=1)
    # Split the 10 factors into two compact tables to reduce vertical scrolling.
    sentiment_left, sentiment_right = st.columns(2, gap="small")
    with sentiment_left:
        st.dataframe(styled_sentiment.data.iloc[:5].style.applymap(_sentiment_cell_style, subset=["Signal"]).apply(_sentiment_change_style, axis=1), use_container_width=True, hide_index=True)
    with sentiment_right:
        st.dataframe(styled_sentiment.data.iloc[5:].style.applymap(_sentiment_cell_style, subset=["Signal"]).apply(_sentiment_change_style, axis=1), use_container_width=True, hide_index=True)
else:
    st.warning("No usable global cue data returned. No direction is guessed.")
# Unavailable-factor details are retained in sentiment_errors but not displayed.
# Hidden sentiment methodology note retained in code; keep the panel visually concise.

# At-a-glance regime panel is intentionally placed above the navigation tabs.
st.subheader("Market regime & trend strength")
@st.cache_data(ttl=86400, show_spinner=False)
def get_nifty500_symbols() -> tuple[list[str], str | None]:
    """Fetch the official NIFTY 500 constituent list; return NSE Yahoo Finance tickers."""
    url = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
        response.raise_for_status()
        frame = pd.read_csv(pd.io.common.BytesIO(response.content))
        if "Symbol" not in frame.columns:
            return [], "The NIFTY 500 constituent file did not contain a Symbol column."
        symbols = sorted({
            str(value).strip().upper() + ".NS"
            for value in frame["Symbol"].dropna()
            if str(value).strip()
        })
        return symbols, None
    except Exception as exc:
        return [], f"NIFTY 500 constituent list unavailable ({exc.__class__.__name__})."

nifty500_symbols, nifty500_symbols_error = get_nifty500_symbols()
regime_options = list(INDEXES.keys())
if nifty500_symbols:
    regime_options += [ticker.removesuffix(".NS") for ticker in nifty500_symbols]
overview_regime_name = st.selectbox(
    "Regime instrument",
    regime_options,
    index=0,
    key="overview_regime_symbol",
    help="Includes the standard indices and the current official NIFTY 500 constituent list."
)
if overview_regime_name in market_data:
    overview_regime_df = market_data[overview_regime_name]
else:
    overview_regime_df, overview_regime_error = get_history(overview_regime_name + ".NS", period, "1d")
    if overview_regime_df.empty:
        st.warning(f"History unavailable for {overview_regime_name}: {overview_regime_error or nifty500_symbols_error or 'No source data returned.'}")
if overview_regime_df.empty or len(overview_regime_df) < 30:
    st.warning("Insufficient returned OHLC history to assess regime. No regime label generated.")
else:
    overview_rc = indicators(overview_regime_df).copy()
    if {"High", "Low", "Close"}.issubset(overview_rc.columns):
        overview_prevc = overview_rc["Close"].shift(1)
        overview_tr = pd.concat([(overview_rc["High"]-overview_rc["Low"]), (overview_rc["High"]-overview_prevc).abs(), (overview_rc["Low"]-overview_prevc).abs()], axis=1).max(axis=1)
        overview_up = overview_rc["High"].diff()
        overview_down = -overview_rc["Low"].diff()
        overview_plus_dm = overview_up.where((overview_up > overview_down) & (overview_up > 0), 0.0)
        overview_minus_dm = overview_down.where((overview_down > overview_up) & (overview_down > 0), 0.0)
        overview_atr14 = overview_tr.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        overview_pdi = 100 * overview_plus_dm.ewm(alpha=1/14, min_periods=14, adjust=False).mean() / overview_atr14.replace(0, np.nan)
        overview_mdi = 100 * overview_minus_dm.ewm(alpha=1/14, min_periods=14, adjust=False).mean() / overview_atr14.replace(0, np.nan)
        overview_dx = 100 * (overview_pdi-overview_mdi).abs() / (overview_pdi+overview_mdi).replace(0, np.nan)
        overview_adx = overview_dx.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
        overview_last = overview_rc.iloc[-1]
        overview_adx_value = overview_adx.iloc[-1]
        overview_rsi_value = overview_last.get("RSI 14", np.nan)
        overview_prev_rsi = overview_rc["RSI 14"].iloc[-2] if "RSI 14" in overview_rc.columns and len(overview_rc) >= 2 else np.nan
        overview_rsi_delta = overview_rsi_value - overview_prev_rsi if pd.notna(overview_rsi_value) and pd.notna(overview_prev_rsi) else np.nan
        if pd.isna(overview_rsi_value):
            overview_rsi_zone = "Insufficient RSI data"
            overview_rsi_color = "#94a3b8"
        elif overview_rsi_value < 40:
            overview_rsi_zone = "Bearish zone"
            overview_rsi_color = "#ef4444"
        elif overview_rsi_value > 60:
            overview_rsi_zone = "Bullish zone"
            overview_rsi_color = "#22c55e"
        elif pd.isna(overview_rsi_delta) or abs(overview_rsi_delta) < 0.05:
            overview_rsi_zone = "Neutral · little change"
            overview_rsi_color = "#94a3b8"
        elif overview_rsi_delta > 0:
            overview_rsi_zone = "Neutral → Positive"
            overview_rsi_color = "#22c55e"
        else:
            overview_rsi_zone = "Neutral → Negative"
            overview_rsi_color = "#ef4444"
        overview_close = overview_last["Close"]
        overview_ema20 = overview_last.get("EMA 20", np.nan)
        overview_ema50 = overview_last.get("EMA 50", np.nan)
        overview_regime = "Insufficient indicator history"
        if pd.notna(overview_adx_value) and pd.notna(overview_ema20) and pd.notna(overview_ema50):
            if overview_adx_value >= 25 and overview_close > overview_ema20 > overview_ema50:
                overview_regime = "Trending bullish"
            elif overview_adx_value >= 25 and overview_close < overview_ema20 < overview_ema50:
                overview_regime = "Trending bearish"
            elif overview_adx_value < 20:
                overview_regime = "Range / weak trend"
            else:
                overview_regime = "Transition / mixed"
        # Return % is in percentage points; convert to decimal before annualizing.
        overview_vol = overview_rc["Return %"].rolling(20).std().iloc[-1] / 100 * np.sqrt(252)
        # Weekly EMA 200 distance is always measured for NIFTY 50, regardless of the selected regime instrument.
        weekly_ema200 = np.nan
        weekly_nifty_close = np.nan
        weekly_nifty_timestamp = "Unavailable"
        weekly_ema_error = None
        try:
            weekly_nifty = yf.download("^NSEI", period="10y", interval="1wk", auto_adjust=False, progress=False, threads=False)
            if not weekly_nifty.empty:
                if isinstance(weekly_nifty.columns, pd.MultiIndex):
                    weekly_nifty.columns = weekly_nifty.columns.get_level_values(0)
                weekly_nifty = weekly_nifty.rename_axis("Date").reset_index()
                weekly_nifty.columns = [str(c).strip().title() for c in weekly_nifty.columns]
                if "Close" in weekly_nifty.columns:
                    weekly_nifty["Close"] = pd.to_numeric(weekly_nifty["Close"], errors="coerce")
                    weekly_nifty = weekly_nifty.dropna(subset=["Close"]).sort_values("Date")
                    if len(weekly_nifty) >= 200:
                        weekly_nifty_close = float(weekly_nifty["Close"].iloc[-1])
                        weekly_ema200 = float(weekly_nifty["Close"].ewm(span=200, adjust=False, min_periods=200).mean().iloc[-1])
                        weekly_nifty_timestamp = pd.to_datetime(weekly_nifty["Date"].iloc[-1]).strftime("%d %b %Y")
                    else:
                        weekly_ema_error = "Fewer than 200 weekly closes returned"
                else:
                    weekly_ema_error = "Weekly Close column unavailable"
            else:
                weekly_ema_error = "Yahoo Finance returned no weekly NIFTY data"
        except Exception as exc:
            weekly_ema_error = f"Weekly data unavailable ({exc.__class__.__name__})"
        weekly_ema_gap_pct = ((weekly_nifty_close / weekly_ema200) - 1) * 100 if pd.notna(weekly_nifty_close) and pd.notna(weekly_ema200) and weekly_ema200 != 0 else np.nan

        # NIFTY 50 PCR uses actual aggregate put/call open interest from FYERS; no guessed fallback.
        nifty_pcr = np.nan
        nifty_pcr_status = "Connect FYERS for live option-chain PCR"
        nifty_pcr_source_time = "Unavailable"
        if st.session_state.get("fyers_access_token") and fyers_credentials():
            try:
                pcr_app_id = fyers_credentials()[0]
                pcr_token = st.session_state["fyers_access_token"]
                pcr_chain, pcr_error = fyers_option_chain(pcr_app_id, pcr_token, "NSE:NIFTY50-INDEX", strikecount=10)
                if not pcr_error:
                    pcr_rows = pcr_chain.get("optionsChain") or pcr_chain.get("options_chain") or []
                    pcr_calls = [row for row in pcr_rows if str(row.get("option_type", "")).upper() == "CE"]
                    pcr_puts = [row for row in pcr_rows if str(row.get("option_type", "")).upper() == "PE"]
                    pcr_call_oi = pcr_chain.get("callOi")
                    pcr_put_oi = pcr_chain.get("putOi")
                    if pcr_call_oi is None:
                        pcr_call_oi = sum(float(row.get("oi") or 0) for row in pcr_calls)
                    if pcr_put_oi is None:
                        pcr_put_oi = sum(float(row.get("oi") or 0) for row in pcr_puts)
                    if float(pcr_call_oi or 0) > 0:
                        nifty_pcr = float(pcr_put_oi or 0) / float(pcr_call_oi)
                        nifty_pcr_status = "Bounce-back chances are high" if nifty_pcr < 0.6 else "Coming-down chances are high" if nifty_pcr > 1.4 else "Neutral PCR range"
                        nifty_pcr_source_time = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y %H:%M IST")
                    else:
                        nifty_pcr_status = "PCR unavailable: call OI is zero or missing"
                else:
                    nifty_pcr_status = "FYERS option-chain unavailable"
            except Exception:
                nifty_pcr_status = "FYERS option-chain unavailable"
        if pd.notna(nifty_pcr) and nifty_pcr < 0.6:
            nifty_pcr_color = "#22c55e"
        elif pd.notna(nifty_pcr) and nifty_pcr > 1.4:
            nifty_pcr_color = "#ef4444"
        elif pd.notna(nifty_pcr):
            nifty_pcr_color = "#f59e0b"
        else:
            nifty_pcr_color = "#94a3b8"

        if pd.notna(weekly_ema_gap_pct) and weekly_ema_gap_pct >= 13:
            weekly_ema_color = "#ef4444"
            weekly_ema_status = "Extended · elevated pullback risk"
        elif pd.notna(weekly_ema_gap_pct) and weekly_ema_gap_pct <= 3:
            weekly_ema_color = "#22c55e"
            weekly_ema_status = "Near weekly EMA 200"
        elif pd.notna(weekly_ema_gap_pct) and weekly_ema_gap_pct < 0:
            weekly_ema_color = "#22c55e"
            weekly_ema_status = "Below weekly EMA 200"
        elif pd.notna(weekly_ema_gap_pct):
            weekly_ema_color = "#f59e0b"
            weekly_ema_status = "Moderately above weekly EMA 200"
        else:
            weekly_ema_color = "#94a3b8"
            weekly_ema_status = "Insufficient weekly history"
        regime_cols = st.columns([1.1, 0.8, 0.8, 1.15, 1.15])
        regime_color = "#22c55e" if overview_regime == "Trending bullish" else "#ef4444" if overview_regime == "Trending bearish" else "#f59e0b"
        regime_cols[0].markdown(
            f'<div style="border:1px solid #343a46;border-radius:12px;padding:12px 14px;min-height:92px;">'
            f'<div style="font-size:13px;color:#aeb6c2;margin-bottom:8px;">Market regime</div>'
            f'<div style="font-size:clamp(16px,2.8vw,25px);line-height:1.25;font-weight:650;color:{regime_color};overflow-wrap:anywhere;">{overview_regime}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
        adx_color = "#22c55e" if pd.notna(overview_adx_value) and overview_adx_value >= 25 else "#f59e0b" if pd.notna(overview_adx_value) and overview_adx_value >= 20 else "#94a3b8"
        rsi_color = overview_rsi_color
        regime_cols[1].markdown(f'<div style="border:1px solid #343a46;border-left:4px solid {adx_color};border-radius:12px;padding:12px 14px;min-height:92px;"><div style="font-size:13px;color:#aeb6c2;margin-bottom:8px;">ADX 14 · trend strength</div><div style="font-size:clamp(20px,2.8vw,27px);font-weight:700;color:{adx_color};">{fmt(overview_adx_value)}</div><div style="font-size:12px;color:#aeb6c2;">{ "Strong trend" if pd.notna(overview_adx_value) and overview_adx_value >= 25 else "Trend building" if pd.notna(overview_adx_value) and overview_adx_value >= 20 else "Weak trend" }</div></div>', unsafe_allow_html=True)
        regime_cols[2].markdown(f'<div style="border:1px solid #343a46;border-left:4px solid {rsi_color};border-radius:12px;padding:12px 14px;min-height:92px;"><div style="font-size:13px;color:#aeb6c2;margin-bottom:8px;">RSI 14 · momentum</div><div style="font-size:clamp(20px,2.8vw,27px);font-weight:700;color:{rsi_color};">{fmt(overview_rsi_value)}</div><div style="font-size:12px;color:{rsi_color};margin-top:3px;">{overview_rsi_zone}</div></div>', unsafe_allow_html=True)
        weekly_close_text = f"{weekly_nifty_close:,.2f}" if pd.notna(weekly_nifty_close) else "—"
        weekly_ema_text = f"{weekly_ema200:,.2f}" if pd.notna(weekly_ema200) else "—"
        weekly_gap_text = f"{weekly_ema_gap_pct:+.2f}%" if pd.notna(weekly_ema_gap_pct) else "—"
        weekly_subtext = weekly_ema_status if pd.notna(weekly_ema_gap_pct) else weekly_ema_error or weekly_ema_status
        regime_cols[3].markdown(f'<div style="border:1px solid #343a46;border-left:4px solid {weekly_ema_color};border-radius:12px;padding:12px 14px;min-height:92px;"><div style="font-size:13px;color:#aeb6c2;margin-bottom:8px;">NIFTY · Weekly EMA 200 gap</div><div style="font-size:clamp(20px,2.8vw,27px);font-weight:700;color:{weekly_ema_color};">{weekly_gap_text}</div><div style="font-size:12px;color:#aeb6c2;">Close {weekly_close_text} · EMA {weekly_ema_text}</div><div style="font-size:12px;color:{weekly_ema_color};margin-top:4px;">{weekly_subtext}</div></div>', unsafe_allow_html=True)
        nifty_pcr_text = f"{nifty_pcr:.2f}" if pd.notna(nifty_pcr) else "—"
        regime_cols[4].markdown(f'<div style="border:1px solid #343a46;border-left:4px solid {nifty_pcr_color};border-radius:12px;padding:12px 14px;min-height:92px;"><div style="font-size:13px;color:#aeb6c2;margin-bottom:8px;">NIFTY 50 · PCR (OI)</div><div style="font-size:clamp(20px,2.8vw,27px);font-weight:700;color:{nifty_pcr_color};">{nifty_pcr_text}</div><div style="font-size:12px;color:{nifty_pcr_color};margin-top:4px;">{nifty_pcr_status}</div><div style="font-size:11px;color:#aeb6c2;margin-top:4px;">Source: FYERS · {nifty_pcr_source_time}</div></div>', unsafe_allow_html=True)
        # Compact Fear & Greed proxy beside the existing price trend chart.
        # This is an explicitly labelled dashboard-derived proxy, not an official NSE index.
        st.markdown("#### Price trend")
        trend_col, fear_col = st.columns([2, 1], gap="small")
        trend_fig = go.Figure()
        trend_fig.add_trace(go.Scatter(x=overview_rc["Date"], y=overview_rc["Close"], name="Close", mode="lines", line=dict(color="#60a5fa", width=2)))
        if "EMA 20" in overview_rc:
            trend_fig.add_trace(go.Scatter(x=overview_rc["Date"], y=overview_rc["EMA 20"], name="EMA 20", mode="lines", line=dict(color="#f59e0b", width=1.5)))
        if "EMA 50" in overview_rc:
            trend_fig.add_trace(go.Scatter(x=overview_rc["Date"], y=overview_rc["EMA 50"], name="EMA 50", mode="lines", line=dict(color="#a78bfa", width=1.5)))
        trend_fig.update_layout(template="plotly_dark", height=300, margin=dict(l=10,r=10,t=20,b=10), hovermode="x unified", xaxis_title="Source candle date", yaxis_title="Price", legend=dict(orientation="h", y=1.02, x=0))
        with trend_col:
            st.plotly_chart(trend_fig, use_container_width=True, config={"displaylogo": False}, key="overview_regime_trend_chart")

        # Minimal NIFTY 50 pivot strip directly below Price Trend.
        nifty_levels_df = market_data.get("NIFTY 50", pd.DataFrame()).copy()
        if not nifty_levels_df.empty and {"Date", "High", "Low", "Close"}.issubset(nifty_levels_df.columns):
            # Normalize source timestamps to IST and use the latest completed session
            # strictly before today. Never use a partial current-day candle.
            nifty_levels_df["_pivot_date_ist"] = (
                pd.to_datetime(nifty_levels_df["Date"], errors="coerce", utc=True)
                .dt.tz_convert("Asia/Kolkata")
            )
            for _col in ["High", "Low", "Close"]:
                nifty_levels_df[_col] = pd.to_numeric(nifty_levels_df[_col], errors="coerce")
            nifty_levels_df = nifty_levels_df.dropna(subset=["_pivot_date_ist", "High", "Low", "Close"])
            nifty_today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
            nifty_levels_df["_pivot_session"] = nifty_levels_df["_pivot_date_ist"].dt.date
            completed_sessions = nifty_levels_df[nifty_levels_df["_pivot_session"] < nifty_today]
            if not completed_sessions.empty:
                nifty_pivot_row = completed_sessions.sort_values("_pivot_date_ist").iloc[-1]
                nifty_pivot_date = nifty_pivot_row["_pivot_session"]
                nifty_pivot_age = (nifty_today - nifty_pivot_date).days
                if nifty_pivot_age <= 7:
                    nifty_ph = float(nifty_pivot_row["High"])
                    nifty_pl = float(nifty_pivot_row["Low"])
                    nifty_pc = float(nifty_pivot_row["Close"])
                    nifty_p = (nifty_ph + nifty_pl + nifty_pc) / 3
                    nifty_s1, nifty_r1 = 2 * nifty_p - nifty_ph, 2 * nifty_p - nifty_pl
                    nifty_s2, nifty_r2 = nifty_p - (nifty_ph - nifty_pl), nifty_p + (nifty_ph - nifty_pl)
                    st.markdown(
                        f'<div style="font-size:12px;font-weight:700;color:#cbd5e1;margin:5px 0 6px;">NIFTY 50 · QUICK SUPPORT / RESISTANCE '
                        f'<span style="font-size:10px;font-weight:400;color:#94a3b8;">Pivot estimates · ref. {nifty_pivot_date:%d %b %Y} · previous completed session</span></div>',
                        unsafe_allow_html=True,
                    )
                    nifty_level_cols = st.columns(5, gap="small")
                    for level_col, level_name, level_value, level_color in zip(
                        nifty_level_cols,
                        ["S2", "S1", "Pivot", "R1", "R2"],
                        [nifty_s2, nifty_s1, nifty_p, nifty_r1, nifty_r2],
                        ["#4ade80", "#22c55e", "#f59e0b", "#fb7185", "#ef4444"],
                    ):
                        level_col.markdown(
                            f'<div style="border:1px solid #343a46;border-top:2px solid {level_color};border-radius:7px;'
                            f'padding:6px 5px;text-align:center;background:rgba(15,23,42,.42);">'
                            f'<div style="font-size:10px;color:#aeb6c2;">{level_name}</div>'
                            f'<div style="font-size:13px;font-weight:750;color:{level_color};">{level_value:,.2f}</div></div>',
                            unsafe_allow_html=True,
                        )
                    st.caption("Classic pivot estimates for information only; not guaranteed price levels.")
                else:
                    st.caption(f"NIFTY 50 pivot levels hidden: latest completed-session data is stale ({nifty_pivot_date:%d %b %Y}).")

        render_nse500_screener()

        with st.expander("Sector rotation", expanded=False):
            st.markdown("""<div style="font-size:clamp(24px,3vw,34px);font-weight:850;background:linear-gradient(90deg,#38bdf8,#818cf8);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Sector rotation</div>""", unsafe_allow_html=True)
            st.caption("Only sectors that return usable source rows are plotted. Relative strength is computed against NIFTY 50 over the selected lookback; it is not institutional money-flow data.")
            if st.button("Load / refresh sector rotation", key="load_sector_rotation"):
                get_history.clear()
                st.session_state["load_sector_rotation_loaded"] = True
            if st.session_state.get("load_sector_rotation_loaded", False):
                sector_rows = []
                rotation_rows = []
                base_df = market_data["NIFTY 50"].copy()
                if not base_df.empty:
                    base_df = base_df.dropna(subset=["Date", "Close"]).sort_values("Date").drop_duplicates("Date", keep="last").copy()
                    base_df["_day"] = pd.to_datetime(base_df["Date"]).dt.date
                    base_df = base_df.set_index("_day")["Close"]
                for sector, ticker in SECTOR_TICKERS.items():
                    sdf, err = get_history(ticker, "6mo", "1d")
                    if sdf.empty or len(sdf) < 12 or len(base_df) < 12:
                        continue
                    sdf = sdf.dropna(subset=["Date", "Close"]).sort_values("Date").drop_duplicates("Date", keep="last").copy()
                    sdf["_day"] = pd.to_datetime(sdf["Date"]).dt.date
                    s_close = sdf.set_index("_day")["Close"]
                    aligned = pd.concat([s_close.rename("sector"), base_df.rename("nifty")], axis=1).dropna()
                    if len(aligned) < 12:
                        continue
                    relative = (aligned["sector"] / aligned["nifty"]) * 100
                    rs_mean = relative.rolling(20, min_periods=8).mean()
                    rs_momentum = relative.pct_change(5) * 100
    
                    def _rotation_zone(pos):
                        if pos < 0 or pos >= len(relative) or pd.isna(rs_mean.iloc[pos]) or pd.isna(rs_momentum.iloc[pos]):
                            return None
                        strong = relative.iloc[pos] >= rs_mean.iloc[pos]
                        accelerating = rs_momentum.iloc[pos] >= 0
                        if strong and accelerating:
                            return "Leading"
                        if strong and not accelerating:
                            return "Weakening"
                        if not strong and accelerating:
                            return "Improving"
                        return "Lagging"
    
                    cur_pos = len(aligned) - 1
                    prev_pos = max(0, cur_pos - 5)
                    current_zone = _rotation_zone(cur_pos)
                    previous_zone = _rotation_zone(prev_pos)
                    recent = aligned.iloc[-min(21, len(aligned)):]
                    perf = (recent["sector"].iloc[-1] / recent["sector"].iloc[0] - 1) * 100
                    base_perf = (recent["nifty"].iloc[-1] / recent["nifty"].iloc[0] - 1) * 100
                    last_date = pd.to_datetime(aligned.index[-1]).strftime("%d %b %Y")
                    relative_pp = perf - base_perf
                    def _relative_window(window):
                        sample = aligned.iloc[-min(window, len(aligned)):]
                        if len(sample) < 2:
                            return np.nan
                        return ((sample["sector"].iloc[-1] / sample["sector"].iloc[0] - 1) -
                                (sample["nifty"].iloc[-1] / sample["nifty"].iloc[0] - 1)) * 100
                    sector_rows.append({
                        "Sector": sector, "Ticker": ticker, "Period return %": perf,
                        "NIFTY return %": base_perf, "Relative return vs NIFTY (pp)": relative_pp,
                        "1D vs NIFTY (pp)": _relative_window(2),
                        "1W vs NIFTY (pp)": _relative_window(6),
                        "3W vs NIFTY (pp)": _relative_window(16),
                        "6W vs NIFTY (pp)": _relative_window(31),
                        "Last source bar": last_date
                    })
                    if current_zone:
                        rotation_rows.append({"Sector": sector, "Previous zone": previous_zone or current_zone, "Current zone": current_zone, "Relative return vs NIFTY (pp)": relative_pp, "Last source bar": last_date})
    
                if sector_rows:
                    sector_board = pd.DataFrame(sector_rows)
                    sector_period = st.segmented_control(
                        "Rank sector money flow by period",
                        ["1D", "1W", "3W", "6W"],
                        default="1W",
                        key="sector_rotation_period",
                    )
                    sector_metric = {"1D": "1D vs NIFTY (pp)", "1W": "1W vs NIFTY (pp)", "3W": "3W vs NIFTY (pp)", "6W": "6W vs NIFTY (pp)"}[sector_period]
                    sector_board = sector_board.dropna(subset=[sector_metric]).sort_values(sector_metric, ascending=False)
                    rising_sectors = sector_board.head(5)
                    falling_sectors = sector_board.tail(5).sort_values(sector_metric)
                    st.markdown("### What changed · Where sector money is moving")
                    st.caption(f"Relative performance versus NIFTY 50 · {sector_period} window · latest available source bar")
                    climb_col, fall_col = st.columns(2, gap="medium")
                    for board_col, board_title, board_data, board_color in [
                        (climb_col, "CLIMBERS", rising_sectors, "#329b72"),
                        (fall_col, "FALLERS", falling_sectors, "#c56b72"),
                    ]:
                        with board_col:
                            st.markdown(f'<div style="font-size:12px;font-weight:800;letter-spacing:1px;color:{board_color};margin:4px 0 8px;">{board_title}</div>', unsafe_allow_html=True)
                            for rank_no, (_, sector_item) in enumerate(board_data.iterrows(), 1):
                                score = float(sector_item[sector_metric])
                                st.markdown(
                                    f'<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;padding:10px 9px;border-bottom:1px solid rgba(130,145,135,.25);">'
                                    f'<b>{rank_no}. {html.escape(str(sector_item["Sector"]))}</b>'
                                    f'<span style="font-weight:800;color:{board_color};">{score:+.2f} pp</span></div>',
                                    unsafe_allow_html=True,
                                )
    
                if rotation_rows:
                    zone_colors = {"Leading": "#198754", "Weakening": "#b45309", "Improving": "#2864c5", "Lagging": "#dc3545"}
                    zone_order = {"Leading": 0, "Improving": 1, "Weakening": 2, "Lagging": 3}
                    for item in rotation_rows:
                        item["Changed this week"] = item["Previous zone"] != item["Current zone"]
                        item["Signal"] = f'{item["Previous zone"]} → {item["Current zone"]}' if item["Changed this week"] else f'{item["Current zone"]} · holding'
                    # Always show six useful sector signals where six sectors have valid data.
                    # Actual zone changes rank first; remaining slots show the strongest current setups by zone and relative strength.
                    top_rotation_signals = sorted(
                        rotation_rows,
                        key=lambda x: (
                            not x["Changed this week"],
                            zone_order.get(x["Current zone"], 4),
                            -float(x.get("Relative return vs NIFTY (pp)", -999))
                        )
                    )[:6]
                    st.markdown("### Top 6 sector rotation signals")
                    st.caption("Actual zone transitions are prioritised. If fewer than six sectors changed zones this week, the remaining cards show the strongest current zone/relative-strength setups and are marked 'holding' rather than presented as new transitions.")
                    pills = []
                    for item in top_rotation_signals:
                        sector_name = item["Sector"]
                        transition = item["Signal"]
                        color = zone_colors.get(item["Current zone"], "#64748b")
                        stamp = item["Last source bar"]
                        pills.append(
                            f'<div style="display:inline-flex;flex-wrap:wrap;align-items:center;gap:10px;'
                            f'border:2px solid {color}55;border-radius:999px;background:rgba(255,255,255,.025);'
                            f'padding:10px 18px;margin:0 8px 8px 0;font-size:clamp(14px,1.5vw,19px);font-weight:650;">'
                            f'<span style="color:var(--text-color);">{html.escape(sector_name)}</span>'
                            f'<span style="color:{color};">{html.escape(transition)}</span>'
                            f'<span style="font-size:11px;font-weight:400;opacity:.7;">{html.escape(stamp)}</span></div>'
                        )
                    st.markdown('<div style="background:rgba(80,120,200,.07);border-radius:24px;padding:18px;">' + "".join(pills) + '</div>', unsafe_allow_html=True)
                    with st.expander("View all current rotation zones", expanded=False):
                        current_zones = sorted(rotation_rows, key=lambda x: (zone_order.get(x["Current zone"], 4), -float(x.get("Relative return vs NIFTY (pp)", -999))))
                        st.dataframe(pd.DataFrame(current_zones), use_container_width=True, hide_index=True)
                else:
                    st.warning("Not enough overlapping daily sector and NIFTY data to calculate rotation zones. No values have been substituted.")
    
                # Constituent stock-level rotation for each conventional NSE sector.
                SECTOR_STOCKS = {
                    "IT": ["TCS.NS","INFY.NS","HCLTECH.NS","WIPRO.NS","TECHM.NS","LTIM.NS","PERSISTENT.NS","COFORGE.NS","MPHASIS.NS","OFSS.NS"],
                    "Pharma": ["SUNPHARMA.NS","DRREDDY.NS","CIPLA.NS","DIVISLAB.NS","TORNTPHARM.NS","LUPIN.NS","AUROPHARMA.NS","ALKEM.NS","ZYDUSLIFE.NS","MANKIND.NS"],
                    "Auto": ["MARUTI.NS","M&M.NS","TATAMOTORS.NS","BAJAJ-AUTO.NS","EICHERMOT.NS","HEROMOTOCO.NS","TVSMOTOR.NS","ASHOKLEY.NS","BOSCHLTD.NS","MOTHERSON.NS"],
                    "Metal": ["TATASTEEL.NS","JSWSTEEL.NS","HINDALCO.NS","VEDL.NS","COALINDIA.NS","JINDALSTEL.NS","NMDC.NS","SAIL.NS","NATIONALUM.NS","HINDZINC.NS"],
                    "Energy": ["RELIANCE.NS","ONGC.NS","NTPC.NS","POWERGRID.NS","BPCL.NS","IOC.NS","GAIL.NS","TATAPOWER.NS","ADANIGREEN.NS","JSWENERGY.NS"],
                    "FMCG": ["HINDUNILVR.NS","ITC.NS","NESTLEIND.NS","BRITANNIA.NS","TATACONSUM.NS","GODREJCP.NS","DABUR.NS","MARICO.NS","COLPAL.NS","UNITDSPR.NS"],
                    "Realty": ["DLF.NS","GODREJPROP.NS","OBEROIRLTY.NS","PRESTIGE.NS","PHOENIXLTD.NS","LODHA.NS","BRIGADE.NS","SOBHA.NS","MAHLIFE.NS","ANANTRAJ.NS"],
                    "Financial Services": ["HDFCBANK.NS","ICICIBANK.NS","SBIN.NS","AXISBANK.NS","KOTAKBANK.NS","BAJFINANCE.NS","BAJAJFINSV.NS","SHRIRAMFIN.NS","CHOLAFIN.NS","PFC.NS"],
                    "PSU Bank": ["SBIN.NS","BANKBARODA.NS","CANBK.NS","PNB.NS","UNIONBANK.NS","INDIANB.NS","IOB.NS","MAHABANK.NS","CENTRALBK.NS","UCOBANK.NS"],
                    "Media": ["SUNTV.NS","ZEEL.NS","NETWORK18.NS","TVTODAY.NS","PVRINOX.NS","SAREGAMA.NS","NAZARA.NS","DBCORP.NS"],
                    "Infrastructure": ["LT.NS","ADANIPORTS.NS","ADANIENT.NS","SIEMENS.NS","ABB.NS","BHEL.NS","IRB.NS","KEC.NS","KALPATPOWR.NS","NBCC.NS"],
                    "Consumption": ["TITAN.NS","TRENT.NS","DMART.NS","ETERNAL.NS","NYKAA.NS","INDIGO.NS","JUBLFOOD.NS","PAGEIND.NS","ASIANPAINT.NS","HAVELLS.NS"],
                }
                with st.expander("Sector constituent stocks · stock-level rotation and transitions", expanded=True):
                    sector_choice = st.selectbox("Choose sector to inspect its stocks", sorted(SECTOR_STOCKS.keys()), key="sector_stock_rotation_choice")
                    sector_symbols = SECTOR_STOCKS[sector_choice]
                    sector_stock_tickers = tuple(sorted(set(sector_symbols + ["^CRSLDX"])))
                    @st.cache_data(ttl=900, show_spinner=False)
                    def _sector_stock_download(symbols):
                        try:
                            return yf.download(list(symbols), period="1y", interval="1d", group_by="ticker", auto_adjust=True, progress=False, threads=False)
                        except Exception:
                            return pd.DataFrame()
                    if st.button("↻ Refresh sector constituent data", key="refresh_sector_constituents"):
                        _sector_stock_download.clear()
                    with st.spinner(f"Fetching constituent history for {sector_choice}…"):
                        sector_raw = _sector_stock_download(sector_stock_tickers)
                    def _sector_stock_close(symbol):
                        try:
                            if isinstance(sector_raw.columns, pd.MultiIndex):
                                if (symbol, "Close") in sector_raw.columns:
                                    return sector_raw[(symbol, "Close")].dropna()
                                if ("Close", symbol) in sector_raw.columns:
                                    return sector_raw[("Close", symbol)].dropna()
                            elif symbol == sector_stock_tickers[0] and "Close" in sector_raw.columns:
                                return sector_raw["Close"].dropna()
                        except Exception:
                            pass
                        return pd.Series(dtype=float)
                    sector_bench = _sector_stock_close("^CRSLDX")
                    constituent_rows = []
                    constituent_transitions = []
                    for symbol in sector_symbols:
                        close = _sector_stock_close(symbol)
                        if len(close) < 60 or len(sector_bench) < 60:
                            continue
                        close = close.sort_index()
                        aligned_stock = pd.concat([close.rename("stock"), sector_bench.rename("bench")], axis=1).dropna()
                        if len(aligned_stock) < 60:
                            continue
                        def _stock_rotation_zone(position):
                            window = aligned_stock.iloc[:position+1]
                            if len(window) < 22:
                                return None
                            rel1 = (window["stock"].iloc[-1] / window["stock"].iloc[-22] - 1) * 100 - (window["bench"].iloc[-1] / window["bench"].iloc[-22] - 1) * 100
                            rel3 = (window["stock"].iloc[-1] / window["stock"].iloc[-min(64,len(window))] - 1) * 100 - (window["bench"].iloc[-1] / window["bench"].iloc[-min(64,len(window))] - 1) * 100
                            return "Leading" if rel3 >= 0 and rel1 >= 0 else "Weakening" if rel3 >= 0 else "Improving" if rel1 >= 0 else "Lagging"
                        current_stock_zone = _stock_rotation_zone(len(aligned_stock)-1)
                        prior_stock_zone = _stock_rotation_zone(len(aligned_stock)-6) if len(aligned_stock) >= 27 else None
                        last_close = float(close.iloc[-1])
                        previous_close = float(close.iloc[-2])
                        ema50 = close.ewm(span=50, adjust=False, min_periods=50).mean().iloc[-1]
                        rel1_now = (aligned_stock["stock"].iloc[-1] / aligned_stock["stock"].iloc[-22] - 1) * 100 - (aligned_stock["bench"].iloc[-1] / aligned_stock["bench"].iloc[-22] - 1) * 100
                        rel3_now = (aligned_stock["stock"].iloc[-1] / aligned_stock["stock"].iloc[-min(64,len(aligned_stock))] - 1) * 100 - (aligned_stock["bench"].iloc[-1] / aligned_stock["bench"].iloc[-min(64,len(aligned_stock))] - 1) * 100
                        change = f"{prior_stock_zone} → {current_stock_zone}" if prior_stock_zone and current_stock_zone and prior_stock_zone != current_stock_zone else "No zone change"
                        if prior_stock_zone and current_stock_zone and prior_stock_zone != current_stock_zone:
                            constituent_transitions.append({"Stock":symbol.replace(".NS",""),"Transition":change})
                        constituent_rows.append({"Stock":symbol.replace(".NS",""),"Rotation zone":current_stock_zone,"Zone change (5 sessions)":change,"1D %":(last_close/previous_close-1)*100 if previous_close else np.nan,"1M %":(last_close/float(close.iloc[-min(22,len(close))])-1)*100,"3M %":(last_close/float(close.iloc[-min(64,len(close))])-1)*100,"1M vs NIFTY 500 (pp)":rel1_now,"3M vs NIFTY 500 (pp)":rel3_now,"Above 50 EMA":bool(last_close>ema50) if pd.notna(ema50) else "Insufficient history"})
                    if constituent_rows:
                        constituent_df = pd.DataFrame(constituent_rows)
                        st.caption(f"{sector_choice} · {len(constituent_df)} of {len(sector_symbols)} mapped constituents with usable price history. Stock zones compare relative 1M and 3M returns against NIFTY 500.")
                        counts = constituent_df["Rotation zone"].value_counts()
                        zcols = st.columns(4)
                        zcolors = {"Leading":"#198754","Improving":"#2864c5","Weakening":"#c27c16","Lagging":"#c92a2a"}
                        for zi, zname in enumerate(["Leading","Improving","Weakening","Lagging"]):
                            with zcols[zi]:
                                st.metric(zname, int(counts.get(zname,0)))
                        st.dataframe(constituent_df.sort_values(["Rotation zone","3M vs NIFTY 500 (pp)"],ascending=[True,False]),use_container_width=True,hide_index=True)
                        st.markdown("**Stocks changing rotation zone over the latest five available sessions**")
                        if constituent_transitions:
                            st.dataframe(pd.DataFrame(constituent_transitions),use_container_width=True,hide_index=True)
                        else:
                            st.info("No constituent stock zone changes detected over the latest five available sessions.")
                        st.markdown("**Strongest stocks in this sector**")
                        st.dataframe(constituent_df.sort_values("3M vs NIFTY 500 (pp)",ascending=False).head(5)[["Stock","Rotation zone","1D %","1M %","3M %","1M vs NIFTY 500 (pp)","3M vs NIFTY 500 (pp)"]],use_container_width=True,hide_index=True)
                    else:
                        st.warning("No constituent price history returned for this sector. No sample values are shown.")
    
                if sector_rows:
                    sec = pd.DataFrame(sector_rows).sort_values("Relative return vs NIFTY (pp)", ascending=False)
                    st.dataframe(sec, use_container_width=True, hide_index=True)
                    sec["Direction"] = np.where(sec["Relative return vs NIFTY (pp)"] >= 0, "Outperforming", "Underperforming")
                    fig = px.bar(sec.sort_values("Relative return vs NIFTY (pp)"), x="Relative return vs NIFTY (pp)", y="Sector", color="Direction", color_discrete_map={"Outperforming":"#22c55e","Underperforming":"#ef4444"}, orientation="h", template="plotly_dark", title="Sector relative performance vs NIFTY")
                    fig.update_layout(height=440)
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.warning("No sector index series were returned by the public source. No values have been substituted.")
    
        with st.expander("Micro Sector Rotation", expanded=False):
            st.markdown("""<div style="font-size:clamp(24px,3vw,34px);font-weight:850;background:linear-gradient(90deg,#a78bfa,#ec4899);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Micro Sector Rotation</div>""", unsafe_allow_html=True)
            st.caption("Explore leading, improving, weakening and lagging micro industries, compare their strength against NIFTY 500, and drill down to constituent stocks.")
            st.caption("Custom equal-weight stock baskets · ranks combine 1M/3M/6M returns and 50-EMA breadth. Public prices may be delayed. This is a research tool, not investment advice.")
            if st.button("Load / refresh micro-sector rotation", key="load_micro_sector_rotation"):
                st.session_state["load_micro_sector_rotation_loaded"] = True
            if st.session_state.get("load_micro_sector_rotation_loaded", False):
                MICRO_SECTORS = {
                    "API & CDMO": ("Pharma", ["DIVISLAB.NS","LAURUSLABS.NS","NEULANDLAB.NS","SHILPAMED.NS","SUVENPHAR.NS"]),
                    "Gaming": ("Niche", ["NAZARA.NS","ZENTEC.NS","DREAMFOLKS.NS"]),
                    "Engineering R&D": ("Technology", ["LTTS.NS","TATAELXSI.NS","KPITTECH.NS","CYIENT.NS"]),
                    "Telecom Equipment": ("Industrials", ["TEJASNET.NS","HFCL.NS","STLTECH.NS","ITI.NS"]),
                    "Jewellery": ("Consumer", ["TITAN.NS","KALYANKJIL.NS","PNGJL.NS","THANGAMAYL.NS","SENCO.NS"]),
                    "Broadcasting": ("Media & Services", ["SUNTV.NS","ZEEL.NS","TVTODAY.NS","NETWORK18.NS","NAZARA.NS"]),
                    "Brokers": ("Financials", ["ANGELONE.NS","MOTILALOFS.NS","BSE.NS","CDSL.NS","IIFLSEC.NS"]),
                    "Exchanges & Depositories": ("Financials", ["BSE.NS","CDSL.NS","CAMS.NS","KFINTECH.NS"]),
                    "Fintech & Payments": ("Financials", ["PAYTM.NS","POLICYBZR.NS","PBFINTECH.NS","CAMS.NS","KFINTECH.NS"]),
                    "Cooling (ACs & Fans)": ("Consumer Durables", ["VOLTAS.NS","BLUESTARCO.NS","AMBER.NS","HAVELLS.NS","CROMPTON.NS"]),
                    "Fluorochemicals": ("Chemicals", ["SRF.NS","NAVINFLUOR.NS","FLUOROCHEM.NS","GFL.NS"]),
                    "Specialty Chemicals": ("Chemicals", ["AARTIIND.NS","DEEPAKNTR.NS","CLEAN.NS","FINEORG.NS","GALAXYSURF.NS"]),
                    "Agrochemicals": ("Chemicals", ["PIIND.NS","UPL.NS","BAYERCROP.NS","RALLIS.NS","DHANUKA.NS"]),
                    "Paints & Coatings": ("Chemicals", ["ASIANPAINT.NS","BERGEPAINT.NS","KANSAINER.NS","INDIGOPNTS.NS"]),
                    "Cables & Wires": ("Industrials", ["POLYCAB.NS","KEI.NS","RRKABEL.NS","FINCABLES.NS"]),
                    "EMS & Electronics": ("Industrials", ["DIXON.NS","AMBER.NS","KAYNES.NS","SYRMA.NS","AVALON.NS"]),
                    "Defence Electronics": ("Defence", ["BEL.NS","BDL.NS","DATAPATTNS.NS","ASTRAMICRO.NS","PARAS.NS"]),
                    "Shipbuilding": ("Industrials", ["COCHINSHIP.NS","GRSE.NS","MAZDOCK.NS","SWANENERGY.NS"]),
                    "Railway Equipment": ("Industrials", ["TITAGARH.NS","RVNL.NS","TEXRAIL.NS","JWL.NS","RITES.NS"]),
                    "Industrial Automation": ("Industrials", ["ABB.NS","SIEMENS.NS","THERMAX.NS","HONAUT.NS","CGPOWER.NS"]),
                    "Power Transformers": ("Power", ["TRANSFORMERS.NS","VOLTAMP.NS","CGPOWER.NS","POWERINDIA.NS"]),
                    "Renewable Equipment": ("Power", ["SUZLON.NS","INOXWIND.NS","WAAREEENER.NS","PREMIERENE.NS","KPIGREEN.NS"]),
                    "Power Transmission": ("Power", ["KALPATPOWR.NS","KEC.NS","PFC.NS","REC.NS","POWERGRID.NS"]),
                    "Data Centres": ("Technology", ["ANANTRAJ.NS","NETWEB.NS","E2E.NS","TECHNOE.NS"]),
                    "Cybersecurity & IT Services": ("Technology", ["TANLA.NS","KELLTONTEC.NS","ZENSARTECH.NS","CYIENT.NS","LTTS.NS"]),
                    "Digital Platforms": ("Technology", ["INFOEDGE.NS","JUSTDIAL.NS","NAZARA.NS","CARTRADE.NS","INDIAMART.NS"]),
                    "Hospitals": ("Healthcare", ["APOLLOHOSP.NS","MAXHEALTH.NS","FORTIS.NS","RAINBOW.NS","KIMS.NS"]),
                    "Diagnostics": ("Healthcare", ["METROPOLIS.NS","LALPATHLAB.NS","VIJAYA.NS","THYROCARE.NS"]),
                    "Medical Devices": ("Healthcare", ["POLYMED.NS","CONCORDBIO.NS","YATHARTH.NS","ARTEMISMED.NS"]),
                    "Domestic Formulations": ("Pharma", ["ALKEM.NS","MANKIND.NS","TORNTPHARM.NS","GLENMARK.NS","AJANTAPHARM.NS"]),
                    "Biotech & Biosimilars": ("Pharma", ["BIOCON.NS","SYNGENE.NS","DRREDDY.NS","ZYDUSLIFE.NS"]),
                    "Diagnostics & Lab Services": ("Healthcare", ["METROPOLIS.NS","LALPATHLAB.NS","VIJAYA.NS","KRSNAA.NS"]),
                    "QSR & Food Service": ("Consumer", ["JUBLFOOD.NS","DEVYANI.NS","SAPPHIRE.NS","WESTLIFE.NS"]),
                    "Packaged Foods": ("Consumer", ["BRITANNIA.NS","BIKAJI.NS","GODREJAGRO.NS","TATACONSUM.NS","DODLA.NS"]),
                    "Sugar & Ethanol": ("Agri", ["BALRAMCHIN.NS","TRIVENI.NS","DWARKESH.NS","EIDPARRY.NS","RENUKA.NS"]),
                    "Dairy": ("Consumer", ["HATSUN.NS","DODLA.NS","HERITGFOOD.NS","PARAGMILK.NS"]),
                    "Spinning & Yarn": ("Textiles", ["VTL.NS","GOKEX.NS","SANGAMIND.NS","TRIDENT.NS","WELSPUNLIV.NS"]),
                    "Technical Textiles": ("Textiles", ["GARFIBRES.NS","SRF.NS","KPRMILL.NS","WELSPUNLIV.NS"]),
                    "Footwear": ("Consumer", ["BATAINDIA.NS","CAMPUS.NS","RELAXO.NS","METROBRAND.NS"]),
                    "Hotels & Resorts": ("Travel", ["INDHOTEL.NS","LEMONTREE.NS","CHALET.NS","EIHOTEL.NS","ITDC.NS"]),
                    "Travel Platforms": ("Travel", ["EASEMYTRIP.NS","IXIGO.NS","YATRA.NS","THOMASCOOK.NS"]),
                    "Logistics & Warehousing": ("Transport", ["DELHIVERY.NS","TCI.NS","MAHLOG.NS","BLUEDART.NS","ALLCARGO.NS"]),
                    "Ports & Marine Logistics": ("Transport", ["ADANIPORTS.NS","JSWINFRA.NS","GPPL.NS","COCHINSHIP.NS"]),
                    "Passenger Vehicles": ("Auto", ["M&M.NS","MARUTI.NS","TATAMOTORS.NS","HYUNDAI.NS"]),
                    "Auto Components": ("Auto", ["BHARATFORG.NS","BOSCHLTD.NS","SONACOMS.NS","UNOMINDA.NS","MOTHERSON.NS"]),
                    "Tyres": ("Auto", ["MRF.NS","APOLLOTYRE.NS","BALKRISIND.NS","CEATLTD.NS"]),
                    "EV Components": ("Auto", ["EXIDEIND.NS","AMARAJABAT.NS","SONACOMS.NS","UNOMINDA.NS","SANSERA.NS"]),
                    "Two-Wheeler Components": ("Auto", ["ENDURANCE.NS","BOSCHLTD.NS","MOTHERSON.NS","SUPRAJIT.NS"]),
                    "Building Materials": ("Construction", ["ASTRAL.NS","SUPREMEIND.NS","FINOLEXIND.NS","PRINCEPIPE.NS"]),
                    "Cement & Building Products": ("Construction", ["ULTRACEMCO.NS","SHREECEM.NS","DALBHARAT.NS","JKCEMENT.NS","RAMCOCEM.NS"]),
                    "Pipes & Fittings": ("Construction", ["ASTRAL.NS","SUPREMEIND.NS","FINPIPE.NS","JINDALSAW.NS"]),
                    "Real Estate Developers": ("Real Estate", ["DLF.NS","GODREJPROP.NS","OBEROIRLTY.NS","PRESTIGE.NS","PHOENIXLTD.NS"]),
                    "Building Interiors": ("Construction", ["GREENPANEL.NS","CENTURYPLY.NS","GREENPLY.NS","KAJARIACER.NS"]),
                    "Gold Loans": ("Financials", ["MUTHOOTFIN.NS","MANAPPURAM.NS","BAJAJHFL.NS"]),
                    "Microfinance": ("Financials", ["CREDITACC.NS","SPANDANA.NS","SURYODAY.NS","UGROCAP.NS"]),
                    "Small Finance Banks": ("Financials", ["AUBANK.NS","UJJIVANSFB.NS","EQUITASBNK.NS","ESAFSFB.NS"]),
                    "Asset Management": ("Financials", ["HDFCAMC.NS","NAM-INDIA.NS","UTIAMC.NS","ABSLAMC.NS"]),
                    "Insurance": ("Financials", ["HDFCLIFE.NS","SBILIFE.NS","ICICIPRULI.NS","LICI.NS"]),
                    "Capital Market Services": ("Financials", ["CDSL.NS","CAMS.NS","KFINTECH.NS","BSE.NS","MCX.NS"]),
                    "Paper & Packaging": ("Materials", ["JKPAPER.NS","TNPL.NS","HUHTAMAKI.NS","PFOCUS.NS"]),
                    "Glass & Containers": ("Materials", ["BOROSIL.NS","BORORENEW.NS","HINDWAREAP.NS","AGI.NS"]),
                    "Metals Recycling": ("Materials", ["MIDHANI.NS","HINDCOPPER.NS","NATIONALUM.NS","MOIL.NS"]),
                    "Mining & Minerals": ("Materials", ["NMDC.NS","COALINDIA.NS","VEDL.NS","HINDZINC.NS"]),
                    "Oilfield Services": ("Energy", ["OIL.NS","ONGC.NS","SEAMECLTD.NS","GTLINFRA.NS"]),
                    "City Gas Distribution": ("Energy", ["IGL.NS","MGL.NS","GUJGASLTD.NS","ATGL.NS"]),
                    "LPG & Gas Equipment": ("Energy", ["GAIL.NS","PETRONET.NS","GESHIP.NS","GULFPETRO.NS"]),
                    "Water & Waste Management": ("Utilities", ["IONEXCHANG.NS","EMS.NS","VA TECH WABAG.NS","ANTONY.NS"]),
                    "Agri Inputs": ("Agri", ["COROMANDEL.NS","CHAMBLFERT.NS","FACT.NS","NFL.NS"]),
                    "Seeds & Crop Science": ("Agri", ["KAVVERITEL.NS","PIIND.NS","RALLIS.NS","DHANUKA.NS"]),
                    "Fertilizers": ("Agri", ["COROMANDEL.NS","CHAMBLFERT.NS","FACT.NS","RCF.NS","NFL.NS"]),
                    "Education Services": ("Services", ["NIITLTD.NS","NIITMTS.NS","VERANDA.NS","APTECHT.NS"]),
                    "Staffing & HR Services": ("Services", ["TEAMLEASE.NS","QUESS.NS","NIITMTS.NS"]),
                    "Security & Facility Services": ("Services", ["SIS.NS","QUESS.NS","BVCL.NS"]),
                    "Hospitals & Wellness": ("Healthcare", ["KIMS.NS","YATHARTH.NS","RAINBOW.NS","ARTEMISMED.NS"]),
                    "Specialty Retail": ("Consumer", ["TRENT.NS","V2RETAIL.NS","ABFRL.NS","SHOPERSTOP.NS"]),
                    "Home Furnishings": ("Consumer", ["DREAMFOLKS.NS","WELSPUNLIV.NS","TRIDENT.NS","HINDWAREAP.NS"]),
                    "Alcoholic Beverages": ("Consumer", ["UNITDSPR.NS","RADICO.NS","UBL.NS","GLOBUSSPR.NS"]),
                    "Tobacco & Allied": ("Consumer", ["ITC.NS","GODFRYPHLP.NS","VSTIND.NS"]),
                    "Renewable Power Producers": ("Power", ["ADANIGREEN.NS","NTPCGREEN.NS","JSWENERGY.NS","NHPC.NS","SJVN.NS"]),
                    "Power Utilities": ("Power", ["NTPC.NS","POWERGRID.NS","TATAPOWER.NS","CESC.NS","TORNTPOWER.NS"]),
                    "IT Hardware": ("Technology", ["NETWEB.NS","DIXON.NS","KAYNES.NS","SYRMA.NS"]),
                    "Telecom Services": ("Telecom", ["BHARTIARTL.NS","IDEA.NS","INDUSTOWER.NS","TATACOMM.NS"]),
                    "Financial Technology": ("Financials", ["PAYTM.NS","POLICYBZR.NS","PBFINTECH.NS","ANGELONE.NS"]),
                }
                micro_tickers = sorted({ticker for _, tickers in MICRO_SECTORS.values() for ticker in tickers} | {"^CRSLDX"})
                @st.cache_data(ttl=900, show_spinner=False)
                def _micro_sector_download(symbols):
                    try:
                        raw = yf.download(list(symbols), period="1y", interval="1d", group_by="ticker", auto_adjust=True, progress=False, threads=False)
                        return raw
                    except Exception:
                        return pd.DataFrame()
                if st.button("↻ Refresh micro-sector data", key="refresh_micro_sector_data"):
                    _micro_sector_download.clear()
                with st.spinner(f"Fetching daily prices for {len(micro_tickers)-1} listed constituents…"):
                    micro_raw = _micro_sector_download(tuple(micro_tickers))
                def _micro_close(symbol):
                    try:
                        if isinstance(micro_raw.columns, pd.MultiIndex):
                            if (symbol, "Close") in micro_raw.columns:
                                return micro_raw[(symbol, "Close")].dropna()
                            if ("Close", symbol) in micro_raw.columns:
                                return micro_raw[("Close", symbol)].dropna()
                            return pd.Series(dtype=float)
                        if symbol == micro_tickers[0] and "Close" in micro_raw.columns:
                            return micro_raw["Close"].dropna()
                    except Exception:
                        pass
                    return pd.Series(dtype=float)
                bench = _micro_close("^CRSLDX")
                if bench.empty:
                    st.warning("NIFTY 500 benchmark history is unavailable. Relative rotation scores are withheld until benchmark data is available.")
                micro_rows = []
                micro_histories = {}
                if not micro_raw.empty:
                    for micro_name, (micro_group, symbols) in MICRO_SECTORS.items():
                        constituent_returns = []
                        constituent_breadth = []
                        constituent_series = []
                        for symbol in symbols:
                            close = _micro_close(symbol)
                            if len(close) < 60:
                                continue
                            close = close.sort_index()
                            last = float(close.iloc[-1])
                            ret1d = (last / float(close.iloc[-2]) - 1) * 100 if len(close) >= 2 else np.nan
                            ret1w = (last / float(close.iloc[-min(6, len(close))]) - 1) * 100
                            ret1 = (last / float(close.iloc[-min(22, len(close))]) - 1) * 100
                            ret3 = (last / float(close.iloc[-min(64, len(close))]) - 1) * 100
                            ret6 = (last / float(close.iloc[-min(127, len(close))]) - 1) * 100
                            ret1y = (last / float(close.iloc[0]) - 1) * 100
                            ema50 = close.ewm(span=50, adjust=False, min_periods=50).mean()
                            constituent_returns.append((ret1d, ret1w, ret1, ret3, ret6, ret1y))
                            constituent_breadth.append(float(last > float(ema50.iloc[-1])))
                            constituent_series.append(close / float(close.iloc[-min(127, len(close))]) * 100)
                        if not constituent_returns:
                            continue
                        arr = np.array(constituent_returns, dtype=float)
                        avg1d, avg1w, avg1, avg3, avg6, avg1y = np.nanmean(arr, axis=0)
                        breadth = float(np.mean(constituent_breadth)) * 100
                        composite = 0.25 * avg1 + 0.40 * avg3 + 0.25 * avg6 + 0.10 * ((breadth - 50) / 5)
                        micro_rows.append({"Micro sector": micro_name, "Group": micro_group, "Stocks available": len(constituent_returns), "Stocks listed": len(symbols), "1D %": avg1d, "1W %": avg1w, "1M %": avg1, "3M %": avg3, "6M %": avg6, "1Y %": avg1y, "50 EMA breadth %": breadth, "_raw_score": composite})
                        if constituent_series:
                            aligned = pd.concat(constituent_series, axis=1).sort_index()
                            micro_histories[micro_name] = aligned.mean(axis=1)
                micro_df = pd.DataFrame(micro_rows)
                if not micro_df.empty:
                    for col in ["1M %", "3M %", "6M %", "_raw_score"]:
                        micro_df[col] = pd.to_numeric(micro_df[col], errors="coerce")
                    for col in ["1M %", "3M %", "6M %", "50 EMA breadth %"]:
                        micro_df[col + " percentile"] = micro_df[col].rank(pct=True) * 100
                    micro_df["Score"] = (micro_df["1M % percentile"] * 0.25 + micro_df["3M % percentile"] * 0.35 + micro_df["6M % percentile"] * 0.25 + micro_df["50 EMA breadth % percentile"] * 0.15).round().astype(int)
                    bench1 = bench.iloc[-1] / bench.iloc[-min(22, len(bench))] * 100 - 100 if len(bench) >= 22 else np.nan
                    bench3 = bench.iloc[-1] / bench.iloc[-min(64, len(bench))] * 100 - 100 if len(bench) >= 64 else np.nan
                    micro_df["1M vs NIFTY 500 (pp)"] = micro_df["1M %"] - bench1
                    micro_df["3M vs NIFTY 500 (pp)"] = micro_df["3M %"] - bench3
                    def _micro_zone(row):
                        ahead = pd.notna(row["3M vs NIFTY 500 (pp)"]) and row["3M vs NIFTY 500 (pp)"] >= 0
                        rising = pd.notna(row["1M vs NIFTY 500 (pp)"]) and row["1M vs NIFTY 500 (pp)"] >= 0
                        return "Leading" if ahead and rising else "Weakening" if ahead else "Improving" if rising else "Lagging"
                    micro_df["Zone"] = micro_df.apply(_micro_zone, axis=1)
                    micro_df = micro_df.sort_values("Score", ascending=False).reset_index(drop=True)
                    latest_dates = [s.index[-1] for s in [bench] if not s.empty]
                    asof = pd.Timestamp(latest_dates[0]).strftime("%d %b %Y") if latest_dates else "date unavailable"
                    st.markdown(f"**Data date:** {asof} · **Micro sectors available:** {len(micro_df)} · **Constituent prices fetched:** {len(micro_tickers)-1}")
                    st.markdown("### What changed · Where money is moving")
                    micro_period = st.segmented_control("Theme movement period", ["1D", "1W", "1M", "3M", "6M"], default="1D", key="micro_movers_period")
                    micro_climbers = micro_df.sort_values(micro_period + " %", ascending=False).head(6)
                    micro_fallers = micro_df.sort_values(micro_period + " %", ascending=True).head(6)
                    mc, mf = st.columns(2, gap="medium")
                    for panel, heading, frame, panel_color in [
                        (mc, "CLIMBERS", micro_climbers, "#329b72"),
                        (mf, "FALLERS", micro_fallers, "#c56b72"),
                    ]:
                        with panel:
                            st.markdown(f'<div style="font-size:12px;font-weight:800;letter-spacing:1px;color:{panel_color};margin:4px 0 8px;">{heading}</div>', unsafe_allow_html=True)
                            for rank_no, (_, theme_row) in enumerate(frame.iterrows(), 1):
                                move = float(theme_row[micro_period + " %"])
                                st.markdown(
                                    f'<div style="display:flex;justify-content:space-between;gap:8px;padding:9px 10px;border-bottom:1px solid rgba(130,145,135,.25);">'
                                    f'<b>{rank_no}. {html.escape(str(theme_row["Micro sector"]))}</b>'
                                    f'<span style="font-weight:800;color:{panel_color};">{move:+.2f}%</span></div>',
                                    unsafe_allow_html=True,
                                )
                    rank_metric = st.segmented_control("Rank leading micro sectors by", options=["Score", "1D %", "1W %", "1M %", "3M %", "6M %", "1Y %"], default="Score", key="micro_rank_mode")
                    rank_col = rank_metric if rank_metric in micro_df.columns else "Score"
                    ranked = micro_df.sort_values(rank_col, ascending=False).head(6)
                    st.subheader("Leading micro sectors")
                    cards = st.columns(2)
                    for idx, (_, row) in enumerate(ranked.iterrows()):
                        with cards[idx % 2]:
                            zone_color = {"Leading":"#198754","Improving":"#2864c5","Weakening":"#c27c16","Lagging":"#c92a2a"}.get(row["Zone"], "#64748b")
                            metric_html = "".join(
                                f'<div><small>{label}</small><br><b style="color:{("#198754" if value >= 0 else "#c92a2a")}">{value:+.1f}%</b></div>'
                                for label, value in [("1M", row["1M %"]), ("3M", row["3M %"]), ("6M", row["6M %"])]
                            )
                            card_html = (
                                f'<div style="border:1px solid #5b5747;border-radius:16px;padding:15px;margin-bottom:12px;background:#171d1b;color:#f2f0e8;">'
                                f'<div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">'
                                f'<span style="background:#d7b967;color:#20251f;border-radius:999px;padding:4px 10px;font-weight:800;">#{idx+1}</span>'
                                f'<span style="border:1px solid {zone_color};color:{zone_color};border-radius:999px;padding:3px 10px;font-weight:700;">{html.escape(str(row["Zone"]))}</span></div>'
                                f'<h3 style="margin:12px 0 4px;color:#f2f0e8;">{html.escape(str(row["Micro sector"]))}</h3>'
                                f'<div style="opacity:.75;font-size:13px;">{html.escape(str(row["Group"]))} · {int(row["Stocks available"])} stocks · Score {int(row["Score"])}</div>'
                                f'<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:5px;margin-top:14px;">{metric_html}</div></div>'
                            )
                            st.markdown(card_html, unsafe_allow_html=True)
                            hist = micro_histories.get(row["Micro sector"], pd.Series(dtype=float))
                            if len(hist) > 2:
                                spark = go.Figure(go.Scatter(x=hist.index, y=hist.values, mode="lines", line=dict(color="#198754" if hist.iloc[-1] >= hist.iloc[-min(22,len(hist))] else "#c92a2a", width=2), fill="tozeroy"))
                                spark.update_layout(height=75, margin=dict(l=0,r=0,t=0,b=0), showlegend=False, xaxis=dict(visible=False), yaxis=dict(visible=False), template="plotly_white")
                                st.plotly_chart(spark, use_container_width=True, config={"displayModeBar":False}, key=f"micro_spark_{idx}")
                    st.subheader("Rotation zones")
                    zone_counts = micro_df["Zone"].value_counts()
                    zone_cols = st.columns(4)
                    zone_text = {"Leading":"Outperforming over 3M and gaining relative ground over 1M","Improving":"Behind over 3M, but gaining over 1M","Weakening":"Ahead over 3M, but losing relative ground over 1M","Lagging":"Behind over both 3M and 1M"}
                    zone_colors = {"Leading":"#198754","Improving":"#2864c5","Weakening":"#c27c16","Lagging":"#c92a2a"}
                    for j, zone in enumerate(["Leading","Improving","Weakening","Lagging"]):
                        with zone_cols[j]:
                            st.markdown(f'<div style="border-top:4px solid {zone_colors[zone]};border-radius:12px;padding:12px;border-left:1px solid #dbe2ea;border-right:1px solid #dbe2ea;border-bottom:1px solid #dbe2ea;"><b style="color:{zone_colors[zone]}">{zone} · {int(zone_counts.get(zone,0))}</b><div style="font-size:13px;margin-top:8px;opacity:.8;">{zone_text[zone]}</div></div>', unsafe_allow_html=True)
                    st.subheader("All micro sectors · current rotation status")
                    zone_filter = st.selectbox("Filter micro sectors by rotation zone", ["All zones", "Leading", "Improving", "Weakening", "Lagging"], key="micro_zone_filter")
                    zone_view = micro_df if zone_filter == "All zones" else micro_df[micro_df["Zone"] == zone_filter]
                    display_cols = ["Micro sector", "Group", "Zone", "Score", "1W %", "1M %", "3M %", "6M %", "1Y %", "50 EMA breadth %", "Stocks available"]
                    st.dataframe(zone_view[display_cols].sort_values("Score", ascending=False), use_container_width=True, hide_index=True)
                    st.caption("This list shows every available micro sector in each rotation zone—not only the top six cards.")
    
                    st.subheader("Rotation map: every micro sector against NIFTY 500")
                    map_df = micro_df.dropna(subset=["3M vs NIFTY 500 (pp)", "1M vs NIFTY 500 (pp)"]).copy()
                    if not map_df.empty:
                        map_fig = px.scatter(map_df, x="3M vs NIFTY 500 (pp)", y="1M vs NIFTY 500 (pp)", size="Stocks available", color="Zone", hover_name="Micro sector", hover_data=["Group","Score","1M %","3M %","6M %"], color_discrete_map=zone_colors, template="plotly_white", title="Relative return and momentum")
                        map_fig.add_vline(x=0,line_color="#9ca3af")
                        map_fig.add_hline(y=0,line_color="#9ca3af")
                        map_fig.update_layout(height=560, xaxis_title="3M return vs NIFTY 500 (percentage points)", yaxis_title="1M return vs NIFTY 500 (percentage points)", legend_title="Rotation zone")
                        st.plotly_chart(map_fig, use_container_width=True, config={"displaylogo":False}, key="micro_rotation_map")
                    st.subheader("Rotation signals · zone changes this week")
                    # Daily data may not include a prior saved weekly snapshot; compare latest zone with the zone five sessions ago where enough history exists.
                    signal_rows = []
                    for micro_name, (micro_group, symbols) in MICRO_SECTORS.items():
                        hist = micro_histories.get(micro_name)
                        if hist is None or len(hist) < 30 or bench.empty:
                            continue
                        b_aligned = pd.concat([hist.rename("sector"), bench.rename("bench")], axis=1).dropna()
                        if len(b_aligned) < 30:
                            continue
                        def _zone_at(pos):
                            curr = b_aligned.iloc[:pos+1]
                            if len(curr) < 22:
                                return None
                            r1 = (curr["sector"].iloc[-1] / curr["sector"].iloc[-22] - 1) * 100 - (curr["bench"].iloc[-1] / curr["bench"].iloc[-22] - 1) * 100
                            r3 = (curr["sector"].iloc[-1] / curr["sector"].iloc[-min(64,len(curr))] - 1) * 100 - (curr["bench"].iloc[-1] / curr["bench"].iloc[-min(64,len(curr))] - 1) * 100
                            return "Leading" if r3 >= 0 and r1 >= 0 else "Weakening" if r3 >= 0 else "Improving" if r1 >= 0 else "Lagging"
                        old_zone = _zone_at(len(b_aligned)-6)
                        new_zone = _zone_at(len(b_aligned)-1)
                        if old_zone and new_zone and old_zone != new_zone:
                            signal_rows.append({"Micro sector":micro_name,"Group":micro_group,"Transition":f"{old_zone} → {new_zone}"})
                    if signal_rows:
                        signal_df = pd.DataFrame(signal_rows)
                        signal_cols = st.columns(2)
                        for j, (_, sr) in enumerate(signal_df.iterrows()):
                            with signal_cols[j % 2]:
                                new_zone = sr["Transition"].split(" → ")[-1]
                                zc = zone_colors[new_zone]
                                st.markdown(f'<div style="border:2px solid {zc}66;border-radius:999px;padding:11px 15px;margin-bottom:8px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;"><b>{html.escape(sr["Micro sector"])}</b><span style="color:{zc};font-weight:750;">{html.escape(sr["Transition"])}</span></div>', unsafe_allow_html=True)
                    else:
                        st.info("No zone changes detected across the latest five available sessions, or insufficient history to verify transitions.")
                    st.subheader("Group strength")
                    group_df = micro_df.groupby("Group", as_index=False).agg(**{"Average score":("Score","mean"),"Micro sectors":("Micro sector","count"),"Leading":("Zone",lambda s:int((s=="Leading").sum()))}).sort_values("Average score",ascending=False)
                    group_cols = st.columns(2)
                    for j, (_, gr) in enumerate(group_df.head(12).iterrows()):
                        with group_cols[j % 2]:
                            group_score = float(gr["Average score"])
                            group_name = html.escape(str(gr["Group"]))
                            group_leading = int(gr["Leading"])
                            group_count = int(gr["Micro sectors"])
                            st.markdown(
                                f'<div style="border:1px solid #dbe2ea;border-radius:14px;padding:13px;margin-bottom:9px;">'
                                f'<b>{group_name}</b><span style="float:right;font-size:20px;font-weight:800;">{group_score:.0f}</span>'
                                f'<div style="height:7px;background:#e2e8f0;border-radius:99px;margin:8px 0;">'
                                f'<div style="height:7px;width:{max(0,min(100,group_score)):.0f}%;background:linear-gradient(90deg,#60a5fa,#16a34a);border-radius:99px;"></div></div>'
                                f'<small>{group_leading} of {group_count} leading · {group_count} micro sectors</small></div>',
                                unsafe_allow_html=True,
                            )
                    st.subheader("Find a micro sector or stock")
                    choices = sorted(MICRO_SECTORS.keys())
                    chosen = st.selectbox("Select micro sector", choices, key="micro_sector_drilldown")
                    chosen_group, chosen_symbols = MICRO_SECTORS[chosen]
                    st.write(f"**{chosen}** · {chosen_group} · equal-weight basket")
                    drill_rows = []
                    stock_transition_rows = []
                    stock_zone_colors = {"Leading":"#198754","Improving":"#2864c5","Weakening":"#c27c16","Lagging":"#c92a2a"}
                    for symbol in chosen_symbols:
                        close = _micro_close(symbol)
                        if len(close) < 60:
                            continue
                        close = close.sort_index()
                        last = float(close.iloc[-1])
                        prev = float(close.iloc[-2])
                        e50_series = close.ewm(span=50, adjust=False, min_periods=50).mean()
                        e50 = e50_series.iloc[-1]
                        stock_aligned = pd.concat([close.rename("stock"), bench.rename("bench")], axis=1).dropna() if not bench.empty else pd.DataFrame()
                        def _stock_zone_at(aligned, pos):
                            current = aligned.iloc[:pos+1]
                            if len(current) < 22:
                                return None
                            rel1 = (current["stock"].iloc[-1] / current["stock"].iloc[-22] - 1) * 100 - (current["bench"].iloc[-1] / current["bench"].iloc[-22] - 1) * 100
                            rel3 = (current["stock"].iloc[-1] / current["stock"].iloc[-min(64,len(current))] - 1) * 100 - (current["bench"].iloc[-1] / current["bench"].iloc[-min(64,len(current))] - 1) * 100
                            return "Leading" if rel3 >= 0 and rel1 >= 0 else "Weakening" if rel3 >= 0 else "Improving" if rel1 >= 0 else "Lagging"
                        current_zone = _stock_zone_at(stock_aligned, len(stock_aligned)-1) if len(stock_aligned) >= 22 else "Unclassified"
                        previous_zone = _stock_zone_at(stock_aligned, len(stock_aligned)-6) if len(stock_aligned) >= 27 else None
                        transition = f"{previous_zone} → {current_zone}" if previous_zone and current_zone and previous_zone != current_zone else "No zone change"
                        if previous_zone and current_zone and previous_zone != current_zone:
                            stock_transition_rows.append({"Stock":symbol.replace(".NS",""),"Transition":transition})
                        rel1_value = (last / float(close.iloc[-min(22,len(close))]) - 1) * 100 - (float(bench.iloc[-1]) / float(bench.iloc[-min(22,len(bench))]) - 1) * 100 if len(bench) >= 22 else np.nan
                        rel3_value = (last / float(close.iloc[-min(64,len(close))]) - 1) * 100 - (float(bench.iloc[-1]) / float(bench.iloc[-min(64,len(bench))]) - 1) * 100 if len(bench) >= 64 else np.nan
                        drill_rows.append({"Stock":symbol.replace(".NS",""),"Rotation zone":current_zone,"Zone change (5 sessions)":transition,"Latest close":last,"1D %":(last/prev-1)*100 if prev else np.nan,"1M %":(last/close.iloc[-min(22,len(close))]-1)*100,"3M %":(last/close.iloc[-min(64,len(close))]-1)*100,"1M vs NIFTY 500 (pp)":rel1_value,"3M vs NIFTY 500 (pp)":rel3_value,"Above 50 EMA":bool(last>e50) if pd.notna(e50) else "Insufficient history"})
                    if drill_rows:
                        drill_df = pd.DataFrame(drill_rows)
                        st.markdown("**Constituent stock rotation** · each stock is classified against NIFTY 500 using relative 1M and 3M performance.")
                        counts = drill_df["Rotation zone"].value_counts()
                        zone_cols = st.columns(4)
                        for zi, zone in enumerate(["Leading","Improving","Weakening","Lagging"]):
                            with zone_cols[zi]:
                                st.metric(zone, int(counts.get(zone,0)))
                        st.dataframe(drill_df.sort_values(["Rotation zone","3M vs NIFTY 500 (pp)"],ascending=[True,False]),use_container_width=True,hide_index=True)
                        st.markdown("**Stocks changing rotation zone over the latest five available sessions**")
                        if stock_transition_rows:
                            st.dataframe(pd.DataFrame(stock_transition_rows),use_container_width=True,hide_index=True)
                        else:
                            st.info("No constituent stock zone changes detected over the latest five available sessions.")
                        st.markdown("**Strongest stocks in this micro sector**")
                        st.dataframe(drill_df.sort_values("3M vs NIFTY 500 (pp)",ascending=False).head(5)[["Stock","Rotation zone","1D %","1M %","3M %","1M vs NIFTY 500 (pp)","3M vs NIFTY 500 (pp)"]],use_container_width=True,hide_index=True)
                        st.code(" ".join(s.replace(".NS","") for s in chosen_symbols), language="text")
                        st.caption("Copy the displayed symbols into your TradingView watchlist. Stock rotation zones compare each constituent with NIFTY 500; this is a relative-strength classification, not an investment recommendation.")
                    else:
                        st.warning("No constituent prices were returned for this micro sector.")
                else:
                    st.warning("No usable micro-sector prices were returned. Refresh later; no sample values are shown.")
    
    
    
            with fear_col:
                fear_score = np.nan
                fear_components = []
                recent_rc = overview_rc.dropna(subset=["Close"]).copy()
                if len(recent_rc) >= 20:
                    # RSI contribution: RSI 50 maps to neutral; 0..100 maps to the same scale.
                    rsi_series = recent_rc["RSI 14"] if "RSI 14" in recent_rc.columns else pd.Series(dtype=float)
                    rsi_series = pd.to_numeric(rsi_series, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
                    if rsi_series.empty:
                        # Calculate RSI directly from the returned close series if the indicator frame lacks RSI.
                        close_delta = pd.to_numeric(recent_rc["Close"], errors="coerce").diff()
                        close_gain = close_delta.clip(lower=0).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
                        close_loss = (-close_delta.clip(upper=0)).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
                        close_rs = close_gain / close_loss.replace(0, np.nan)
                        fallback_rsi = (100 - 100 / (1 + close_rs)).replace([np.inf, -np.inf], np.nan).dropna()
                        if not fallback_rsi.empty:
                            rsi_series = fallback_rsi
                    if not rsi_series.empty:
                        fear_components.append((float(np.clip(rsi_series.iloc[-1], 0, 100)), 0.35))
                    # Trend contribution: close vs EMA 20 and EMA 50, mapped around neutral.
                    trend_parts = []
                    for ema_col in ["EMA 20", "EMA 50"]:
                        if ema_col in recent_rc.columns and pd.notna(recent_rc[ema_col].iloc[-1]) and recent_rc[ema_col].iloc[-1] != 0:
                            gap_pct = (float(recent_rc["Close"].iloc[-1]) / float(recent_rc[ema_col].iloc[-1]) - 1) * 100
                            trend_parts.append(float(np.clip(50 + gap_pct * 12, 0, 100)))
                    if trend_parts:
                        # Price vs EMA 20/50 contributes 25% to the proxy score.
                        fear_components.append((float(np.mean(trend_parts)), 0.25))
                    # Medium-term momentum: 20-session return mapped around neutral.
                    if len(recent_rc) >= 21 and recent_rc["Close"].iloc[-21] != 0:
                        ret20 = (float(recent_rc["Close"].iloc[-1]) / float(recent_rc["Close"].iloc[-21]) - 1) * 100
                        fear_components.append((float(np.clip(50 + ret20 * 8, 0, 100)), 0.15))
                    # India VIX: higher volatility tilts the proxy toward fear.
                    vix_frame = market_data.get("India VIX", pd.DataFrame())
                    vix_snap = quote_snapshot(vix_frame) if not vix_frame.empty else None
                    if vix_snap:
                        vix_value = float(vix_snap[0]["Close"])
                        # India VIX contributes 25% to the proxy score.
                        fear_components.append((float(np.clip(100 - vix_value * 3, 0, 100)), 0.25))
                    # Keep only finite, valid components. Missing/invalid inputs must not turn into a fake 0/100 reading.
                    fear_components = [
                        (float(value), float(weight))
                        for value, weight in fear_components
                        if pd.notna(value) and np.isfinite(float(value)) and 0 <= float(value) <= 100 and weight > 0
                    ]
                    if fear_components:
                        fear_weight = sum(weight for _, weight in fear_components)
                        if fear_weight > 0:
                            fear_score = sum(value * weight for value, weight in fear_components) / fear_weight
                            if not np.isfinite(fear_score):
                                fear_score = np.nan
                if pd.notna(fear_score):
                    fear_score = int(round(float(np.clip(fear_score, 0, 100))))
                    fear_label = "Extreme Fear" if fear_score < 20 else "Fear" if fear_score < 40 else "Neutral" if fear_score < 60 else "Greed" if fear_score < 80 else "Extreme Greed"
                    fear_color = "#ef4444" if fear_score < 20 else "#f97316" if fear_score < 40 else "#f59e0b" if fear_score < 60 else "#84cc16" if fear_score < 80 else "#16a34a"
                    gauge_fig = go.Figure(go.Indicator(
                        mode="gauge+number",
                        value=fear_score,
                        number={"font": {"size": 30, "color": fear_color}, "suffix": "/100"},
                        title={"text": "Fear & Greed<br><span style='font-size:11px;color:#aeb6c2'>Market proxy</span>", "font": {"size": 15}},
                        gauge={
                            "axis": {"range": [0, 100], "tickmode": "array", "tickvals": [0, 20, 40, 60, 80, 100], "ticktext": ["0", "20", "40", "60", "80", "100"], "tickfont": {"size": 9}},
                            "bar": {"color": fear_color, "thickness": 0.22},
                            "bgcolor": "#11151d",
                            "borderwidth": 0,
                            "steps": [
                                {"range": [0, 20], "color": "#7f1d1d"},
                                {"range": [20, 40], "color": "#c2410c"},
                                {"range": [40, 60], "color": "#b45309"},
                                {"range": [60, 80], "color": "#4d7c0f"},
                                {"range": [80, 100], "color": "#047857"},
                            ],
                            "threshold": {"line": {"color": "#e5e7eb", "width": 2}, "thickness": 0.8, "value": fear_score},
                        },
                    ))
                    gauge_fig.update_layout(template="plotly_dark", height=250, margin=dict(l=8,r=8,t=48,b=8), paper_bgcolor="rgba(0,0,0,0)")
                    fear_col.plotly_chart(gauge_fig, use_container_width=True, config={"displaylogo": False}, key="overview_fear_greed_gauge")
                    fear_col.markdown(f'<div style="text-align:center;font-weight:700;color:{fear_color};font-size:16px;margin-top:-8px;">{fear_label}</div>', unsafe_allow_html=True)
                    # Explanatory caption removed to save vertical space.
                else:
                    fear_col.info("Fear & Greed proxy unavailable: insufficient indicator data.")
    
    
    
from explosive_breakout_scanner import render_explosive_breakout_scanner

with st.expander("🚀 Explosive Breakout Scanner", expanded=False):
    render_explosive_breakout_scanner()

from historical_breakout_postmortem import render_historical_breakout_postmortem

with st.expander("🔬 Historical Explosive Winners & Pre-Breakout Signature", expanded=False):
    render_historical_breakout_postmortem()

from b1_fundamental_screener import render_b1_fundamentals_tab

b1_fundamental_tabs = st.tabs(["Smallcap & Midcap Stage 2 B1 Fundamentals"])
with b1_fundamental_tabs[0]:
    render_b1_fundamentals_tab()


# Leading sectors: rank recent 1-week performance against NIFTY 50, independent of sidebar lookback.
st.markdown("""<div style="font-size:clamp(26px,3.5vw,38px);font-weight:850;letter-spacing:-1px;line-height:1.15;margin:10px 0 8px;background:linear-gradient(90deg,#19e6a0,#29c7d6,#4aa8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Leading sectors</div>""", unsafe_allow_html=True)
# Explanatory caption removed to save vertical space.
# Representative liquid stocks by sector; rank only when source history is available.
SECTOR_LEADING_STOCKS = {
    "Pharma": [("SUN Pharma", "SUNPHARMA.NS"), ("Dr Reddy's", "DRREDDY.NS"), ("Cipla", "CIPLA.NS"), ("Divi's Labs", "DIVISLAB.NS"), ("Lupin", "LUPIN.NS"), ("Torrent Pharma", "TORNTPHARM.NS")],
    "IT": [("TCS", "TCS.NS"), ("Infosys", "INFY.NS"), ("HCLTech", "HCLTECH.NS"), ("Wipro", "WIPRO.NS"), ("Tech Mahindra", "TECHM.NS"), ("LTIMindtree", "LTIM.NS")],
    "Infrastructure": [("Larsen & Toubro", "LT.NS"), ("Bharat Electronics", "BEL.NS"), ("Adani Ports", "ADANIPORTS.NS"), ("Siemens", "SIEMENS.NS"), ("Cummins India", "CUMMINSIND.NS"), ("ABB India", "ABB.NS")],
    "Auto": [("M&M", "M&M.NS"), ("Maruti Suzuki", "MARUTI.NS"), ("Bajaj Auto", "BAJAJ-AUTO.NS"), ("Eicher Motors", "EICHERMOT.NS"), ("Hero MotoCorp", "HEROMOTOCO.NS"), ("TVS Motor", "TVSMOTOR.NS")],
    "Metal": [("Tata Steel", "TATASTEEL.NS"), ("Hindalco", "HINDALCO.NS"), ("JSW Steel", "JSWSTEEL.NS"), ("Coal India", "COALINDIA.NS"), ("Vedanta", "VEDL.NS"), ("Jindal Steel", "JINDALSTEL.NS")],
    "Energy": [("Reliance Industries", "RELIANCE.NS"), ("ONGC", "ONGC.NS"), ("NTPC", "NTPC.NS"), ("Power Grid", "POWERGRID.NS"), ("Coal India", "COALINDIA.NS"), ("Adani Green", "ADANIGREEN.NS")],
    "FMCG": [("ITC", "ITC.NS"), ("Hindustan Unilever", "HINDUNILVR.NS"), ("Nestle India", "NESTLEIND.NS"), ("Britannia", "BRITANNIA.NS"), ("Tata Consumer", "TATACONSUM.NS"), ("Varun Beverages", "VBL.NS")],
    "Realty": [("DLF", "DLF.NS"), ("Godrej Properties", "GODREJPROP.NS"), ("Prestige Estates", "PRESTIGE.NS"), ("Phoenix Mills", "PHOENIXLTD.NS"), ("Oberoi Realty", "OBEROIRLTY.NS"), ("Lodha", "LODHA.NS")],
    "Financial Services": [("HDFC Bank", "HDFCBANK.NS"), ("ICICI Bank", "ICICIBANK.NS"), ("Bajaj Finance", "BAJFINANCE.NS"), ("Shriram Finance", "SHRIRAMFIN.NS"), ("Cholamandalam Finance", "CHOLAFIN.NS"), ("SBI", "SBIN.NS")],
    "PSU Bank": [("SBI", "SBIN.NS"), ("Bank of Baroda", "BANKBARODA.NS"), ("Canara Bank", "CANBK.NS"), ("Punjab National Bank", "PNB.NS"), ("Union Bank", "UNIONBANK.NS"), ("Indian Bank", "INDIANB.NS")],
    "Media": [("Sun TV", "SUNTV.NS"), ("Zee Entertainment", "ZEEL.NS"), ("PVR INOX", "PVRINOX.NS"), ("Network18", "NETWORK18.NS"), ("Nazara Technologies", "NAZARA.NS")],
    "Consumption": [("Trent", "TRENT.NS"), ("Titan", "TITAN.NS"), ("Eternal", "ETERNAL.NS"), ("Avenue Supermarts", "DMART.NS"), ("Bajaj Consumer", "BAJAJCON.NS"), ("Indian Hotels", "INDHOTEL.NS")],
}

if st.button("Load / refresh leading-sector rankings", key="load_leading_sector_rankings"):
    get_history.clear()
    st.session_state["load_leading_sector_rankings_loaded"] = True
if st.session_state.get("load_leading_sector_rankings_loaded", False):
        leading_sector_rows = []
        rotation_zone_rows = []
        with st.spinner("Checking multi-timeframe sector strength…"):
            weekly_nifty_df, weekly_nifty_err = get_history("^NSEI", "3mo", "1d")
            weekly_nifty_close = weekly_nifty_df["Close"].dropna() if not weekly_nifty_df.empty else pd.Series(dtype=float)
            if len(weekly_nifty_close) >= 6:
                nifty_week_return = (float(weekly_nifty_close.iloc[-1]) / float(weekly_nifty_close.iloc[-6]) - 1) * 100
                nifty_month_return = (float(weekly_nifty_close.iloc[-1]) / float(weekly_nifty_close.iloc[-22]) - 1) * 100 if len(weekly_nifty_close) >= 22 else np.nan
                nifty_3m_return = (float(weekly_nifty_close.iloc[-1]) / float(weekly_nifty_close.iloc[-63]) - 1) * 100 if len(weekly_nifty_close) >= 63 else np.nan
                for sector, ticker in SECTOR_TICKERS.items():
                    sector_df, sector_err = get_history(ticker, "3mo", "1d")
                    if not sector_df.empty:
                        sector_close = sector_df["Close"].dropna()
                        if len(sector_close) >= 6 and len(weekly_nifty_close) >= 6:
                            sector_week_return = (float(sector_close.iloc[-1]) / float(sector_close.iloc[-6]) - 1) * 100
                            sector_month_return = (float(sector_close.iloc[-1]) / float(sector_close.iloc[-22]) - 1) * 100 if len(sector_close) >= 22 else np.nan
                            sector_3m_return = (float(sector_close.iloc[-1]) / float(sector_close.iloc[-63]) - 1) * 100 if len(sector_close) >= 63 else np.nan
                            relative_week = sector_week_return - nifty_week_return
                            relative_month = sector_month_return - nifty_month_return if pd.notna(sector_month_return) and pd.notna(nifty_month_return) else np.nan
                            relative_3m = sector_3m_return - nifty_3m_return if pd.notna(sector_3m_return) and pd.notna(nifty_3m_return) else np.nan
                            # Relative-strength quadrant: 1M relative performance and short-term momentum.
                            def _rotation_zone(rs, momentum):
                                if pd.isna(rs) or pd.isna(momentum):
                                    return "Insufficient data"
                                if rs >= 0 and momentum >= 0:
                                    return "Leading"
                                if rs >= 0 and momentum < 0:
                                    return "Weakening"
                                if rs < 0 and momentum < 0:
                                    return "Lagging"
                                return "Improving"
                            current_zone = _rotation_zone(relative_month, relative_week - relative_month if pd.notna(relative_month) else np.nan)
                            if len(sector_close) >= 27 and len(weekly_nifty_close) >= 27:
                                sec_old_week = (float(sector_close.iloc[-6]) / float(sector_close.iloc[-11]) - 1) * 100
                                nifty_old_week = (float(weekly_nifty_close.iloc[-6]) / float(weekly_nifty_close.iloc[-11]) - 1) * 100
                                sec_old_month = (float(sector_close.iloc[-6]) / float(sector_close.iloc[-27]) - 1) * 100
                                nifty_old_month = (float(weekly_nifty_close.iloc[-6]) / float(weekly_nifty_close.iloc[-27]) - 1) * 100
                                old_week_relative = sec_old_week - nifty_old_week
                                old_month_relative = sec_old_month - nifty_old_month
                                previous_zone = _rotation_zone(old_month_relative, old_week_relative - old_month_relative)
                            else:
                                previous_zone = "Insufficient data"
                            rotation_zone_rows.append({"Sector": sector, "Previous zone": previous_zone, "Current zone": current_zone})
                            score_parts = [(relative_week, 0.30), (relative_month, 0.40), (relative_3m, 0.30)]
                            valid_parts = [(value, weight) for value, weight in score_parts if pd.notna(value)]
                            leadership_score = sum(value * weight for value, weight in valid_parts) / sum(weight for _, weight in valid_parts) if valid_parts else np.nan
                            if pd.notna(leadership_score):
                                leading_sector_rows.append({
                                    "Sector": sector,
                                    "Sector 1W (%)": sector_week_return,
                                    "NIFTY 1W (%)": nifty_week_return,
                                    "1W outperformance (pp)": relative_week,
                                    "1M outperformance (pp)": relative_month,
                                    "3M outperformance (pp)": relative_3m,
                                    "Multi-timeframe score (pp)": leadership_score,
                                })
        if leading_sector_rows:
            leading_df = pd.DataFrame(leading_sector_rows)
            ranked_sectors = leading_df.sort_values("Multi-timeframe score (pp)", ascending=False).copy()
            # Always show the four strongest sectors, even if fewer than four have a positive composite score.
            leaders = ranked_sectors.head(4).copy()
            current_top_names = leaders["Sector"].tolist()
        
            st.markdown("### Rotation signals · zone changes this week")
            st.caption("Zones use 1-month relative performance vs NIFTY 50 and short-term momentum (1-week relative performance minus 1-month relative performance). Weekly change compares with the close five sessions earlier.")
            zone_colors = {"Leading": "#169b62", "Improving": "#316ac5", "Weakening": "#c58a18", "Lagging": "#cf4b55", "Insufficient data": "#64748b"}
            zone_transition_rows = [row for row in rotation_zone_rows if row["Previous zone"] != row["Current zone"] and row["Previous zone"] != "Insufficient data" and row["Current zone"] != "Insufficient data"]
            if zone_transition_rows:
                for row in zone_transition_rows:
                    color = zone_colors.get(row["Current zone"], "#64748b")
                    label = html.escape(row["Sector"])
                    transition = html.escape(row["Previous zone"] + " → " + row["Current zone"])
                    st.markdown(
                        f'<div style="display:inline-flex;align-items:center;gap:10px;flex-wrap:wrap;border:1px solid {color}88;border-radius:40px;padding:8px 16px;margin:4px 6px 4px 0;background:rgba(100,116,139,.05);font-size:16px;"><span style="font-weight:750;color:#e5e7eb;">{label}</span><span style="font-weight:750;color:{color};">{transition}</span></div>',
                        unsafe_allow_html=True,
                    )
            else:
                st.info("No verified sector-zone changes detected versus five sessions ago, or insufficient historical data.")
            with st.expander("View all current sector zones", expanded=False):
                zone_table = pd.DataFrame(rotation_zone_rows)
                if not zone_table.empty:
                    st.dataframe(zone_table.rename(columns={"Previous zone": "Zone 5 sessions ago"}), use_container_width=True, hide_index=True)
        
            # Detect a genuine transition from relative underperformance (<= 0 pp)
            # to outperformance (> 0 pp), using the prior successful dashboard run.
            current_relative = {
                str(row["Sector"]): float(row["1W outperformance (pp)"])
                for _, row in ranked_sectors.iterrows()
                if pd.notna(row["1W outperformance (pp)"])
            }
            previous_relative = st.session_state.get("previous_sector_relative_performance")
            if previous_relative is not None:
                # Alert only for a genuinely fresh leader: it was underperforming,
                # has now crossed above NIFTY 50, and has entered the current top-three list.
                newly_outperforming = [
                    name for name, value in current_relative.items()
                    if value > 0
                    and name in previous_relative
                    and previous_relative[name] <= 0
                    and name in current_top_names
                ]
                if newly_outperforming:
                    alert_text = "🟨 SECTOR ROTATION ALERT · Now outperforming NIFTY 50: " + ", ".join(newly_outperforming)
                    st.toast(alert_text, icon="🟨")
                    st.markdown(
                        f'<div style="background:#fef3c7;color:#78350f;border:1px solid #f59e0b;'
                        f'border-left:6px solid #eab308;border-radius:10px;padding:12px 16px;'
                        f'font-weight:750;margin:8px 0 12px;">{html.escape(alert_text)}</div>',
                        unsafe_allow_html=True,
                    )
            st.session_state["previous_sector_relative_performance"] = current_relative
        
            # Newly entered sectors are highlighted yellow for this dashboard session.
            newly_entered_sectors = []
            previous_top_names = st.session_state.get("previous_top_4_sectors")
            if previous_top_names is not None:
                newly_entered_sectors = [name for name in current_top_names if name not in previous_top_names]
                exited = [name for name in previous_top_names if name not in current_top_names]
                if newly_entered_sectors or exited:
                    alert_parts = []
                    if newly_entered_sectors:
                        alert_parts.append("New sector(s) entering top 4: " + ", ".join(newly_entered_sectors))
                    if exited:
                        alert_parts.append("Sector(s) leaving top 4: " + ", ".join(exited))
                    if newly_entered_sectors:
                        st.markdown(
                            '<div style="background:rgba(234,179,8,.12);color:#fde68a;border:1px solid #eab308;'
                            'border-left:5px solid #eab308;border-radius:10px;padding:10px 14px;'
                            'font-weight:750;margin:8px 0 12px;">🟨 SECTOR ROTATION · ' + html.escape(" · ".join(alert_parts)) + '</div>',
                            unsafe_allow_html=True,
                        )
                    else:
                        st.warning("🔔 Sector leadership changed · " + " · ".join(alert_parts))
            st.session_state["previous_top_4_sectors"] = current_top_names
            if leaders.empty:
                st.info("Sector strength data is currently unavailable.")
            else:
                leader_cols = st.columns(len(leaders))
                for leader_col, (_, leader) in zip(leader_cols, leaders.iterrows()):
                    month_confirmation = f'{leader["1M outperformance (pp)"]:+.2f} pp' if pd.notna(leader["1M outperformance (pp)"]) else "N/A"
                    three_month_confirmation = f'{leader["3M outperformance (pp)"]:+.2f} pp' if pd.notna(leader["3M outperformance (pp)"]) else "N/A"
                    is_new_sector = leader["Sector"] in newly_entered_sectors
                    card_border = "#eab308" if is_new_sector else "#166534"
                    card_accent = "#eab308" if is_new_sector else "#22c55e"
                    card_bg = "rgba(234,179,8,0.10)" if is_new_sector else "rgba(34,197,94,0.08)"
                    card_title = "#fde68a" if is_new_sector else "#86efac"
                    card_text = "#fef3c7" if is_new_sector else "#a7f3d0"
                    leader_col.markdown(
                        f'<div style="border:1px solid {card_border};border-left:4px solid {card_accent};border-radius:10px;padding:9px 10px;min-height:100px;background:{card_bg};">'
                        f'<div style="font-size:12px;color:{card_title};margin-bottom:4px;">{"🟨 NEW · " if is_new_sector else ""}{leader["Sector"]}</div>'
                        f'<div style="font-size:clamp(17px,2vw,22px);font-weight:700;color:{card_accent};">{leader["Multi-timeframe score (pp)"]:+.2f} pp</div>'
                        f'<div style="font-size:10px;color:{card_text};">1W: {leader["Sector 1W (%)"]:+.2f}% · NIFTY {leader["NIFTY 1W (%)"]:+.2f}%</div>'
                        f'<div style="font-size:10px;color:{card_text};">vs NIFTY: 1W {leader["1W outperformance (pp)"]:+.2f} · 1M {month_confirmation} · 3M {three_month_confirmation}</div>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                    stock_universe = SECTOR_LEADING_STOCKS.get(leader["Sector"], [])
                    stock_leaders = []
                    with st.spinner(f'Ranking {leader["Sector"]} stocks…'):
                        for stock_name, stock_ticker in stock_universe:
                            stock_df, stock_err = get_history(stock_ticker, "3mo", "1d")
                            if stock_df.empty:
                                continue
                            stock_close = pd.to_numeric(stock_df["Close"], errors="coerce").dropna()
                            if len(stock_close) >= 6 and stock_close.iloc[-6] != 0:
                                stock_return = (float(stock_close.iloc[-1]) / float(stock_close.iloc[-6]) - 1) * 100
                                stock_leaders.append({"Stock": stock_name, "Ticker": stock_ticker, "1W return (%)": stock_return})
                    leader_col.markdown("**Top 5 stocks · latest 5 sessions**")
                    if stock_leaders:
                        stock_leaders = sorted(stock_leaders, key=lambda row: row["1W return (%)"], reverse=True)[:5]
                        for rank, stock in enumerate(stock_leaders, start=1):
                            stock_color = "#22c55e" if stock["1W return (%)"] >= 0 else "#ef4444"
                            leader_col.markdown(
                                f'<div style="display:flex;justify-content:space-between;gap:8px;padding:6px 0;border-bottom:1px solid #343a46;">'
                                f'<span style="font-size:13px;color:#d1d5db;">{rank}. {stock["Stock"]}</span>'
                                f'<span style="font-size:13px;font-weight:700;color:{stock_color};">{stock["1W return (%)"]:+.2f}%</span></div>',
                                unsafe_allow_html=True,
                            )
                    else:
                        leader_col.caption("Stock-level data unavailable; no ranking shown.")
        else:
            st.info("Weekly sector momentum is temporarily unavailable because usable 3-month daily history was not returned.")
        
st.markdown("""<div style="font-size:clamp(26px,3.5vw,38px);font-weight:850;letter-spacing:-1px;line-height:1.15;margin:10px 0 8px;background:linear-gradient(90deg,#60a5fa,#a78bfa,#f472b6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">NIFTY 50 stock heatmap</div>""", unsafe_allow_html=True)
st.caption("NIFTY 50 constituents only · Tile size is equal-weighted in this view · Green = positive daily change, red = negative daily change")
NIFTY50_HEATMAP_SYMBOLS = [
    "ADANIENT.NS", "ADANIPORTS.NS", "APOLLOHOSP.NS", "ASIANPAINT.NS", "AXISBANK.NS",
    "BAJAJ-AUTO.NS", "BAJFINANCE.NS", "BAJAJFINSV.NS", "BEL.NS", "BHARTIARTL.NS",
    "CIPLA.NS", "COALINDIA.NS", "DRREDDY.NS", "EICHERMOT.NS", "ETERNAL.NS",
    "GRASIM.NS", "HCLTECH.NS", "HDFCBANK.NS", "HDFCLIFE.NS", "HINDALCO.NS",
    "HINDUNILVR.NS", "ICICIBANK.NS", "INDUSINDBK.NS", "INFY.NS", "ITC.NS",
    "JIOFIN.NS", "JSWSTEEL.NS", "KOTAKBANK.NS", "LT.NS", "M&M.NS",
    "MARUTI.NS", "NESTLEIND.NS", "NTPC.NS", "ONGC.NS", "POWERGRID.NS",
    "RELIANCE.NS", "SBILIFE.NS", "SBIN.NS", "SHRIRAMFIN.NS", "SUNPHARMA.NS",
    "TATACONSUM.NS", "TATAMOTORS.NS", "TATASTEEL.NS", "TCS.NS", "TECHM.NS",
    "TITAN.NS", "TRENT.NS", "ULTRACEMCO.NS", "WIPRO.NS", "TMPV.NS"
]
@st.cache_data(ttl=300, show_spinner=False)
def _nifty50_heatmap_data(symbols: tuple[str, ...]):
    raw = yf.download(list(symbols), period="5d", interval="1d", group_by="ticker", auto_adjust=False, progress=False, threads=False)
    rows = []
    for symbol in symbols:
        try:
            frame = raw[symbol].dropna(subset=["Close"])
            closes = frame["Close"].dropna()
            if len(closes) >= 2 and float(closes.iloc[-2]) != 0:
                last_close = float(closes.iloc[-1])
                change_pct = (last_close / float(closes.iloc[-2]) - 1) * 100
                rows.append({"Symbol": symbol.removesuffix(".NS"), "Close": last_close, "Change (%)": change_pct})
        except (KeyError, TypeError, ValueError, IndexError):
            continue
    return pd.DataFrame(rows)
if st.button("Load / refresh NIFTY 50 heatmap", key="load_nifty50_heatmap"):
    _nifty50_heatmap_data.clear()
    st.session_state["nifty50_heatmap_loaded"] = True
if st.session_state.get("nifty50_heatmap_loaded", False):
    with st.spinner("Loading NIFTY 50 constituent performance…"):
        heatmap_df = _nifty50_heatmap_data(tuple(NIFTY50_HEATMAP_SYMBOLS))
    if heatmap_df.empty:
        st.warning("NIFTY 50 heatmap data is temporarily unavailable from the public data source.")
    else:
        # Drop invalid percentage rows before plotting; NaN labels usually indicate an invalid
        # text-template field or a source row without a valid close.
        heatmap_df["Change (%)"] = pd.to_numeric(heatmap_df["Change (%)"], errors="coerce")
        heatmap_df = heatmap_df.replace([np.inf, -np.inf], np.nan).dropna(subset=["Change (%)", "Close"])
        heatmap_df["Direction"] = np.where(heatmap_df["Change (%)"] >= 0, "Positive", "Negative")
        heatmap_df["Display change"] = heatmap_df["Change (%)"].map(lambda value: f"{value:+.2f}%")
        heatmap_fig = px.treemap(
            heatmap_df, path=["Direction", "Symbol"], values=np.ones(len(heatmap_df)),
            color="Change (%)", color_continuous_scale=["#ef4444", "#252a34", "#16a34a"],
            color_continuous_midpoint=0,
            custom_data=["Display change", "Close"],
            hover_data={"Close": ":,.2f", "Change (%)": ":.2f", "Direction": False, "Display change": False}
        )
        # Use explicit customdata for the percentage text; %{color} is not a reliable
        # numeric placeholder in Plotly treemap text templates.
        heatmap_fig.update_traces(
            texttemplate="%{label}<br>%{customdata[0]}",
            textfont_size=12,
            hovertemplate="<b>%{label}</b><br>Daily change: %{customdata[0]}<br>Close: ₹%{customdata[1]:,.2f}<extra></extra>",
        )
        heatmap_fig.update_layout(template="plotly_dark", height=520, margin=dict(l=4, r=4, t=8, b=4), coloraxis_colorbar_title="Daily %")
        st.plotly_chart(heatmap_fig, use_container_width=True, config={"displaylogo": False}, key="nifty50_only_treemap")
st.divider()

tabs = [st.expander("NIFTY historical returns", expanded=False)]
with tabs[0]:
    st.markdown("""<div style="font-size:clamp(24px,3vw,34px);font-weight:850;background:linear-gradient(90deg,#f59e0b,#f97316);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">NIFTY historical returns</div>""", unsafe_allow_html=True)
    st.caption("Calendar-year returns and monthly return heatmap, calculated from returned daily NIFTY 50 closing prices. Historical performance is not a prediction.")
    @st.cache_data(ttl=3600, show_spinner=False)
    def _nifty_long_history(ticker: str):
        return get_history(ticker, "10y", "1d")
    if st.button("Load / refresh NIFTY historical returns", key="load_nifty_historical_returns"):
        _nifty_long_history.clear()
        get_history.clear()
        st.session_state["nifty_historical_returns_loaded"] = True
    if st.session_state.get("nifty_historical_returns_loaded", False):
        with st.spinner("Loading up to 10 years of NIFTY 50 daily history…"):
            nifty_long, nifty_long_error = _nifty_long_history(INDEXES["NIFTY 50"])
        if nifty_long.empty or "Close" not in nifty_long.columns:
            st.warning(f"Long-history data unavailable: {nifty_long_error or 'No usable NIFTY 50 daily closes returned.'}")
        else:
            hist = nifty_long.copy()
            hist["Date"] = pd.to_datetime(hist["Date"], errors="coerce")
            hist = hist.dropna(subset=["Date", "Close"]).sort_values("Date")
            hist["Year"] = hist["Date"].dt.year
            hist["Month"] = hist["Date"].dt.month
            annual_closes = hist.groupby("Year", as_index=False).tail(1).sort_values("Year").set_index("Year")["Close"]
            annual_rows = []
            current_year = int(hist["Year"].max())
            for year in range(max(2018, int(hist["Year"].min()) + 1), current_year + 1):
                if year in annual_closes.index and (year - 1) in annual_closes.index:
                    ref_close = float(annual_closes.loc[year - 1])
                    end_close = float(annual_closes.loc[year])
                    annual_rows.append({"Year": f"{year} YTD" if year == current_year and hist["Date"].max().month < 12 else str(year), "Reference close": ref_close, "Year-end/latest close": end_close, "Return (%)": (end_close / ref_close - 1) * 100})
            if annual_rows:
                annual_df = pd.DataFrame(annual_rows)
                st.markdown("#### Comparative yearly performance")
                st.dataframe(annual_df.style.format({"Reference close": "{:,.2f}", "Year-end/latest close": "{:,.2f}", "Return (%)": "{:+.2f}"}).map(lambda v: "color: #22c55e; font-weight: 700" if isinstance(v, (int, float, np.number)) and v > 0 else "color: #ef4444; font-weight: 700" if isinstance(v, (int, float, np.number)) and v < 0 else "", subset=["Return (%)"]), use_container_width=True, hide_index=True)
                annual_fig = go.Figure(go.Bar(x=annual_df["Year"], y=annual_df["Return (%)"], marker_color=["#22c55e" if v >= 0 else "#ef4444" for v in annual_df["Return (%)"]], text=[f"{v:+.2f}%" for v in annual_df["Return (%)"]], textposition="outside", cliponaxis=False))
                annual_fig.add_hline(y=0, line_color="#9ca3af", line_width=1)
                annual_fig.update_layout(template="plotly_dark", title="NIFTY 50 Calendar-Year Returns (latest year is YTD)", yaxis_title="Return (%)", height=360, margin=dict(l=10, r=10, t=55, b=10), showlegend=False)
                st.plotly_chart(annual_fig, use_container_width=True, config={"displaylogo": False}, key="nifty_annual_returns")
            else:
                st.info("Not enough prior year-end data to calculate calendar-year returns.")
            month_ends = hist.groupby(["Year", "Month"], as_index=False).tail(1).sort_values("Date").copy()
            month_ends["Monthly return (%)"] = month_ends["Close"].pct_change() * 100
            month_ends.loc[month_ends.index[0], "Monthly return (%)"] = np.nan
            month_ends = month_ends[month_ends["Year"] >= 2018]
            years = list(range(2018, current_year + 1))
            heat = pd.DataFrame(index=years, columns=range(1, 13), dtype=float)
            for _, row in month_ends.iterrows():
                y, m, val = int(row["Year"]), int(row["Month"]), row["Monthly return (%)"]
                if y in heat.index and pd.notna(val):
                    heat.loc[y, m] = float(val)
            st.markdown("#### Monthly percentage returns heat map")
            st.caption("Each cell compares the month-end close with the previous available month-end close. Green = positive; red = negative; blank = unavailable. The first displayed month may use a prior-year reference close.")
            month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            z = heat.to_numpy(dtype=float)
            text_values = [[("" if pd.isna(v) else f"{v:.1f}") for v in row] for row in z]
            heat_fig = go.Figure(go.Heatmap(z=z, x=month_names, y=[str(y) for y in years], colorscale=[[0, "#991b1b"], [0.5, "#4b5563"], [1, "#047857"]], zmin=-10, zmax=10, zmid=0, text=text_values, texttemplate="%{text}", textfont={"size": 10}, colorbar={"title": "Monthly return (%)"}, hovertemplate="Year %{y}<br>%{x}: %{z:.2f}%<extra></extra>", xgap=2, ygap=2))
            heat_fig.update_layout(template="plotly_dark", title="Monthly NIFTY 50 Returns (%)", height=max(360, 38 * len(years) + 100), margin=dict(l=10, r=10, t=50, b=10), xaxis_title="", yaxis_title="Year")
            st.plotly_chart(heat_fig, use_container_width=True, config={"displaylogo": False}, key="nifty_monthly_return_heatmap")
            st.markdown("#### Interpretation and limitations")
            st.write("Returns are calculated from the daily closing prices returned by the public data source. The latest year is year-to-date when the calendar year is incomplete. Price returns exclude dividends and do not guarantee future performance.")
            st.caption(f"Source: Yahoo Finance via yfinance · Last returned daily bar: {hist['Date'].max():%d %b %Y} · app retrieval timestamp is not an exchange timestamp.")

st.divider()
st.caption("For research and monitoring only. Verify exchange timestamps and official sources before trading. Public widgets and public-source data may be delayed, incomplete or unavailable; this dashboard is not an execution system or investment advice.")

# Expanded research modules (13–22). Calculations use public-source data; personal journal data remains user-controlled.
extra_tabs = [st.expander("13 Regime & trend strength", expanded=False), st.expander("14 Options analytics", expanded=False)]

with extra_tabs[0]:
    st.caption("Market regime and trend strength are displayed in the main dashboard above.")

with extra_tabs[1]:
    st.markdown("""<div style="font-size:clamp(24px,3vw,34px);font-weight:850;background:linear-gradient(90deg,#22c55e,#14b8a6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Options analytics</div>""", unsafe_allow_html=True)
    st.write("Option-chain data for NIFTY-family indices and a broad list of F&O stocks is fetched from the authenticated FYERS Market Data API. Choose an underlying and expiry; availability depends on FYERS contract coverage.")
    # FYERS accepts the underlying equity symbol for stock-option chains.
    # Keep the list explicit so unsupported/expired contracts fail transparently rather than showing guessed data.
    option_underlying_labels = {
        "Index · NIFTY": "NSE:NIFTY50-INDEX",
        "Index · BANKNIFTY": "NSE:NIFTYBANK-INDEX",
        "Index · FINNIFTY": "NSE:FINNIFTY-INDEX",
        "Stock · 360ONE": "NSE:360ONE-EQ",
        "Stock · ABB": "NSE:ABB-EQ",
        "Stock · ABCAPITAL": "NSE:ABCAPITAL-EQ",
        "Stock · ABFRL": "NSE:ABFRL-EQ",
        "Stock · ACC": "NSE:ACC-EQ",
        "Stock · ADANIENSOL": "NSE:ADANIENSOL-EQ",
        "Stock · ADANIENT": "NSE:ADANIENT-EQ",
        "Stock · ADANIGREEN": "NSE:ADANIGREEN-EQ",
        "Stock · ADANIPORTS": "NSE:ADANIPORTS-EQ",
        "Stock · ALKEM": "NSE:ALKEM-EQ",
        "Stock · AMBER": "NSE:AMBER-EQ",
        "Stock · AMBUJACEM": "NSE:AMBUJACEM-EQ",
        "Stock · ANGELONE": "NSE:ANGELONE-EQ",
        "Stock · APLAPOLLO": "NSE:APLAPOLLO-EQ",
        "Stock · APOLLOHOSP": "NSE:APOLLOHOSP-EQ",
        "Stock · ASHOKLEY": "NSE:ASHOKLEY-EQ",
        "Stock · ASIANPAINT": "NSE:ASIANPAINT-EQ",
        "Stock · ASTRAL": "NSE:ASTRAL-EQ",
        "Stock · ATGL": "NSE:ATGL-EQ",
        "Stock · AUBANK": "NSE:AUBANK-EQ",
        "Stock · AUROPHARMA": "NSE:AUROPHARMA-EQ",
        "Stock · AXISBANK": "NSE:AXISBANK-EQ",
        "Stock · BAJAJ-AUTO": "NSE:BAJAJ-AUTO-EQ",
        "Stock · BAJAJFINSV": "NSE:BAJAJFINSV-EQ",
        "Stock · BAJFINANCE": "NSE:BAJFINANCE-EQ",
        "Stock · BALKRISIND": "NSE:BALKRISIND-EQ",
        "Stock · BANDHANBNK": "NSE:BANDHANBNK-EQ",
        "Stock · BANKBARODA": "NSE:BANKBARODA-EQ",
        "Stock · BANKINDIA": "NSE:BANKINDIA-EQ",
        "Stock · BEL": "NSE:BEL-EQ",
        "Stock · BHARATFORG": "NSE:BHARATFORG-EQ",
        "Stock · BHARTIARTL": "NSE:BHARTIARTL-EQ",
        "Stock · BHEL": "NSE:BHEL-EQ",
        "Stock · BIOCON": "NSE:BIOCON-EQ",
        "Stock · BPCL": "NSE:BPCL-EQ",
        "Stock · BRITANNIA": "NSE:BRITANNIA-EQ",
        "Stock · BSE": "NSE:BSE-EQ",
        "Stock · CANBK": "NSE:CANBK-EQ",
        "Stock · CANFINHOME": "NSE:CANFINHOME-EQ",
        "Stock · CDSL": "NSE:CDSL-EQ",
        "Stock · CGPOWER": "NSE:CGPOWER-EQ",
        "Stock · CHAMBLFERT": "NSE:CHAMBLFERT-EQ",
        "Stock · CHOLAFIN": "NSE:CHOLAFIN-EQ",
        "Stock · CIPLA": "NSE:CIPLA-EQ",
        "Stock · COALINDIA": "NSE:COALINDIA-EQ",
        "Stock · COFORGE": "NSE:COFORGE-EQ",
        "Stock · COLPAL": "NSE:COLPAL-EQ",
        "Stock · CONCOR": "NSE:CONCOR-EQ",
        "Stock · CROMPTON": "NSE:CROMPTON-EQ",
        "Stock · CUMMINSIND": "NSE:CUMMINSIND-EQ",
        "Stock · DABUR": "NSE:DABUR-EQ",
        "Stock · DALBHARAT": "NSE:DALBHARAT-EQ",
        "Stock · DELHIVERY": "NSE:DELHIVERY-EQ",
        "Stock · DEVYANI": "NSE:DEVYANI-EQ",
        "Stock · DIXON": "NSE:DIXON-EQ",
        "Stock · DLF": "NSE:DLF-EQ",
        "Stock · DMART": "NSE:DMART-EQ",
        "Stock · DRREDDY": "NSE:DRREDDY-EQ",
        "Stock · EICHERMOT": "NSE:EICHERMOT-EQ",
        "Stock · ETERNAL": "NSE:ETERNAL-EQ",
        "Stock · EXIDEIND": "NSE:EXIDEIND-EQ",
        "Stock · FEDERALBNK": "NSE:FEDERALBNK-EQ",
        "Stock · FORTIS": "NSE:FORTIS-EQ",
        "Stock · GAIL": "NSE:GAIL-EQ",
        "Stock · GLENMARK": "NSE:GLENMARK-EQ",
        "Stock · GMRINFRA": "NSE:GMRINFRA-EQ",
        "Stock · GMRAIRPORT": "NSE:GMRAIRPORT-EQ",
        "Stock · GNFC": "NSE:GNFC-EQ",
        "Stock · GODREJCP": "NSE:GODREJCP-EQ",
        "Stock · GODREJPROP": "NSE:GODREJPROP-EQ",
        "Stock · GRANULES": "NSE:GRANULES-EQ",
        "Stock · GRASIM": "NSE:GRASIM-EQ",
        "Stock · HAL": "NSE:HAL-EQ",
        "Stock · HAVELLS": "NSE:HAVELLS-EQ",
        "Stock · HCLTECH": "NSE:HCLTECH-EQ",
        "Stock · HDFCAMC": "NSE:HDFCAMC-EQ",
        "Stock · HDFCBANK": "NSE:HDFCBANK-EQ",
        "Stock · HDFCLIFE": "NSE:HDFCLIFE-EQ",
        "Stock · HEROMOTOCO": "NSE:HEROMOTOCO-EQ",
        "Stock · HINDALCO": "NSE:HINDALCO-EQ",
        "Stock · HINDCOPPER": "NSE:HINDCOPPER-EQ",
        "Stock · HINDPETRO": "NSE:HINDPETRO-EQ",
        "Stock · HINDUNILVR": "NSE:HINDUNILVR-EQ",
        "Stock · HINDZINC": "NSE:HINDZINC-EQ",
        "Stock · HUDCO": "NSE:HUDCO-EQ",
        "Stock · ICICIBANK": "NSE:ICICIBANK-EQ",
        "Stock · ICICIGI": "NSE:ICICIGI-EQ",
        "Stock · ICICIPRULI": "NSE:ICICIPRULI-EQ",
        "Stock · IDEA": "NSE:IDEA-EQ",
        "Stock · IDFCFIRSTB": "NSE:IDFCFIRSTB-EQ",
        "Stock · IEX": "NSE:IEX-EQ",
        "Stock · IIFL": "NSE:IIFL-EQ",
        "Stock · INDHOTEL": "NSE:INDHOTEL-EQ",
        "Stock · INDIANB": "NSE:INDIANB-EQ",
        "Stock · INDIGO": "NSE:INDIGO-EQ",
        "Stock · INDUSINDBK": "NSE:INDUSINDBK-EQ",
        "Stock · INDUSTOWER": "NSE:INDUSTOWER-EQ",
        "Stock · INFY": "NSE:INFY-EQ",
        "Stock · INOXWIND": "NSE:INOXWIND-EQ",
        "Stock · IOC": "NSE:IOC-EQ",
        "Stock · IRB": "NSE:IRB-EQ",
        "Stock · IRCTC": "NSE:IRCTC-EQ",
        "Stock · IREDA": "NSE:IREDA-EQ",
        "Stock · IRFC": "NSE:IRFC-EQ",
        "Stock · ITC": "NSE:ITC-EQ",
        "Stock · JINDALSTEL": "NSE:JINDALSTEL-EQ",
        "Stock · JIOFIN": "NSE:JIOFIN-EQ",
        "Stock · JKCEMENT": "NSE:JKCEMENT-EQ",
        "Stock · JSWENERGY": "NSE:JSWENERGY-EQ",
        "Stock · JSWSTEEL": "NSE:JSWSTEEL-EQ",
        "Stock · JUBLFOOD": "NSE:JUBLFOOD-EQ",
        "Stock · KALYANKJIL": "NSE:KALYANKJIL-EQ",
        "Stock · KEI": "NSE:KEI-EQ",
        "Stock · KFINTECH": "NSE:KFINTECH-EQ",
        "Stock · KOTAKBANK": "NSE:KOTAKBANK-EQ",
        "Stock · KPITTECH": "NSE:KPITTECH-EQ",
        "Stock · LALPATHLAB": "NSE:LALPATHLAB-EQ",
        "Stock · LAURUSLABS": "NSE:LAURUSLABS-EQ",
        "Stock · LICHSGFIN": "NSE:LICHSGFIN-EQ",
        "Stock · LICI": "NSE:LICI-EQ",
        "Stock · LODHA": "NSE:LODHA-EQ",
        "Stock · LT": "NSE:LT-EQ",
        "Stock · LTIM": "NSE:LTIM-EQ",
        "Stock · LTTS": "NSE:LTTS-EQ",
        "Stock · LUPIN": "NSE:LUPIN-EQ",
        "Stock · M&M": "NSE:M&M-EQ",
        "Stock · MANAPPURAM": "NSE:MANAPPURAM-EQ",
        "Stock · MANKIND": "NSE:MANKIND-EQ",
        "Stock · MARICO": "NSE:MARICO-EQ",
        "Stock · MARUTI": "NSE:MARUTI-EQ",
        "Stock · MAXHEALTH": "NSE:MAXHEALTH-EQ",
        "Stock · MAZDOC": "NSE:MAZDOC-EQ",
        "Stock · MCX": "NSE:MCX-EQ",
        "Stock · METROPOLIS": "NSE:METROPOLIS-EQ",
        "Stock · MFSL": "NSE:MFSL-EQ",
        "Stock · MOTHERSON": "NSE:MOTHERSON-EQ",
        "Stock · MPHASIS": "NSE:MPHASIS-EQ",
        "Stock · MRF": "NSE:MRF-EQ",
        "Stock · MUTHOOTFIN": "NSE:MUTHOOTFIN-EQ",
        "Stock · NATIONALUM": "NSE:NATIONALUM-EQ",
        "Stock · NAUKRI": "NSE:NAUKRI-EQ",
        "Stock · NBCC": "NSE:NBCC-EQ",
        "Stock · NCC": "NSE:NCC-EQ",
        "Stock · NESTLEIND": "NSE:NESTLEIND-EQ",
        "Stock · NHPC": "NSE:NHPC-EQ",
        "Stock · NMDC": "NSE:NMDC-EQ",
        "Stock · NTPC": "NSE:NTPC-EQ",
        "Stock · NYKAA": "NSE:NYKAA-EQ",
        "Stock · OBEROIRLTY": "NSE:OBEROIRLTY-EQ",
        "Stock · OFSS": "NSE:OFSS-EQ",
        "Stock · OIL": "NSE:OIL-EQ",
        "Stock · ONGC": "NSE:ONGC-EQ",
        "Stock · PAYTM": "NSE:PAYTM-EQ",
        "Stock · PEL": "NSE:PEL-EQ",
        "Stock · PERSISTENT": "NSE:PERSISTENT-EQ",
        "Stock · PETRONET": "NSE:PETRONET-EQ",
        "Stock · PFC": "NSE:PFC-EQ",
        "Stock · PGEL": "NSE:PGEL-EQ",
        "Stock · PHOENIXLTD": "NSE:PHOENIXLTD-EQ",
        "Stock · PIDILITIND": "NSE:PIDILITIND-EQ",
        "Stock · PIIND": "NSE:PIIND-EQ",
        "Stock · PNB": "NSE:PNB-EQ",
        "Stock · POLICYBZR": "NSE:POLICYBZR-EQ",
        "Stock · POLYCAB": "NSE:POLYCAB-EQ",
        "Stock · POONAWALLA": "NSE:POONAWALLA-EQ",
        "Stock · POWERGRID": "NSE:POWERGRID-EQ",
        "Stock · PRESTIGE": "NSE:PRESTIGE-EQ",
        "Stock · RBLBANK": "NSE:RBLBANK-EQ",
        "Stock · RECLTD": "NSE:RECLTD-EQ",
        "Stock · RELIANCE": "NSE:RELIANCE-EQ",
        "Stock · SAIL": "NSE:SAIL-EQ",
        "Stock · SBICARD": "NSE:SBICARD-EQ",
        "Stock · SBILIFE": "NSE:SBILIFE-EQ",
        "Stock · SBIN": "NSE:SBIN-EQ",
        "Stock · SHREECEM": "NSE:SHREECEM-EQ",
        "Stock · SHRIRAMFIN": "NSE:SHRIRAMFIN-EQ",
        "Stock · SIEMENS": "NSE:SIEMENS-EQ",
        "Stock · SONACOMS": "NSE:SONACOMS-EQ",
        "Stock · SRF": "NSE:SRF-EQ",
        "Stock · SUNPHARMA": "NSE:SUNPHARMA-EQ",
        "Stock · SUPREMEIND": "NSE:SUPREMEIND-EQ",
        "Stock · SYNGENE": "NSE:SYNGENE-EQ",
        "Stock · TATACONSUM": "NSE:TATACONSUM-EQ",
        "Stock · TATAELXSI": "NSE:TATAELXSI-EQ",
        "Stock · TATAMOTORS": "NSE:TATAMOTORS-EQ",
        "Stock · TATAPOWER": "NSE:TATAPOWER-EQ",
        "Stock · TATASTEEL": "NSE:TATASTEEL-EQ",
        "Stock · TCS": "NSE:TCS-EQ",
        "Stock · TECHM": "NSE:TECHM-EQ",
        "Stock · TIINDIA": "NSE:TIINDIA-EQ",
        "Stock · TITAGARH": "NSE:TITAGARH-EQ",
        "Stock · TITAN": "NSE:TITAN-EQ",
        "Stock · TORNTPHARM": "NSE:TORNTPHARM-EQ",
        "Stock · TORNTPOWER": "NSE:TORNTPOWER-EQ",
        "Stock · TRENT": "NSE:TRENT-EQ",
        "Stock · TVSMOTOR": "NSE:TVSMOTOR-EQ",
        "Stock · UBL": "NSE:UBL-EQ",
        "Stock · ULTRACEMCO": "NSE:ULTRACEMCO-EQ",
        "Stock · UNOMINDA": "NSE:UNOMINDA-EQ",
        "Stock · UPL": "NSE:UPL-EQ",
        "Stock · VBL": "NSE:VBL-EQ",
        "Stock · VEDL": "NSE:VEDL-EQ",
        "Stock · VOLTAS": "NSE:VOLTAS-EQ",
        "Stock · WIPRO": "NSE:WIPRO-EQ",
        "Stock · YESBANK": "NSE:YESBANK-EQ",
        "Stock · ZOMATO": "NSE:ZOMATO-EQ",
        "Stock · ZYDUSLIFE": "NSE:ZYDUSLIFE-EQ",
        "Stock · ADANIPOWER": "NSE:ADANIPOWER-EQ",
        "Stock · COCHINSHIP": "NSE:COCHINSHIP-EQ",
        "Stock · HYUNDAI": "NSE:HYUNDAI-EQ",
        "Stock · MOTILALOFS": "NSE:MOTILALOFS-EQ",
        "Stock · NAM-INDIA": "NSE:NAM-INDIA-EQ",
        "Stock · VMM": "NSE:VMM-EQ",
        "Stock · ARE&M": "NSE:ARE&M-EQ",
        "Stock · PATANJALI": "NSE:PATANJALI-EQ",
    }
    # Searchable selector with simple index/stock filters; the full mapping remains available.
    selector_col1, selector_col2 = st.columns([2, 1])
    option_search = selector_col1.text_input(
        "Search F&O stock or index",
        placeholder="Type a symbol, e.g. RELIANCE, HDFCBANK, NIFTY",
        key="option_chain_symbol_search",
    ).strip().upper()
    option_category = selector_col2.selectbox(
        "Instrument type",
        ["All", "Stocks only", "Indices only"],
        key="option_chain_instrument_filter",
    )
    filtered_underlyings = [
        label for label in option_underlying_labels
        if (not option_search or option_search in label.upper())
        and (
            option_category == "All"
            or (option_category == "Stocks only" and label.startswith("Stock · "))
            or (option_category == "Indices only" and label.startswith("Index · "))
        )
    ]
    if not filtered_underlyings:
        st.warning("No matching instrument. Try another symbol or select All.")
        st.stop()
    option_underlying = st.selectbox(
        f"Underlying ({len(filtered_underlyings)} matches)",
        filtered_underlyings,
        key="auto_opt_underlying_searchable",
    )
    if not st.session_state.get("fyers_access_token") or not fyers_credentials():
        st.warning("Connect FYERS in the sidebar to load the live option chain, PCR, open interest, max pain and Greeks. No values are estimated when disconnected.")
    else:
        app_id = fyers_credentials()[0]
        access_token = st.session_state["fyers_access_token"]
        with st.spinner("Fetching available expiries from FYERS…"):
            base_chain, base_error = fyers_option_chain(app_id, access_token, option_underlying_labels[option_underlying], strikecount=10)
        if base_error:
            st.error(f"FYERS option-chain data unavailable: {base_error}")
            st.caption("Check that your FYERS API app has market-data access and that your session token is still valid.")
        else:
            expiry_rows = base_chain.get("expiryData") or []
            expiry_labels = {f'{row.get("date", "Unknown expiry")} · {row.get("expiry", "")}': str(row.get("expiry", "")) for row in expiry_rows if row.get("expiry") is not None}
            if expiry_labels:
                selected_expiry_label = st.selectbox("Expiry", list(expiry_labels.keys()), key="fyers_option_expiry")
                selected_expiry = expiry_labels[selected_expiry_label]
                with st.spinner("Loading selected FYERS option chain…"):
                    chain_data, chain_error = fyers_option_chain(app_id, access_token, option_underlying_labels[option_underlying], strikecount=10, expiry_timestamp=selected_expiry)
            else:
                chain_data, chain_error = base_chain, None
                st.info("FYERS did not provide a separate expiry list; displaying the returned chain.")
            if chain_error:
                st.error(f"Could not load selected expiry: {chain_error}")
            else:
                chain_rows = chain_data.get("optionsChain") or []
                call_rows = [row for row in chain_rows if str(row.get("option_type", "")).upper() == "CE"]
                put_rows = [row for row in chain_rows if str(row.get("option_type", "")).upper() == "PE"]
                spot_row = next((row for row in chain_rows if row.get("option_type", "") == "" or float(row.get("strike_price", 0) or 0) < 0), {})
                calls_by_strike = {float(row["strike_price"]): row for row in call_rows if row.get("strike_price") is not None}
                puts_by_strike = {float(row["strike_price"]): row for row in put_rows if row.get("strike_price") is not None}
                strikes = sorted(set(calls_by_strike) | set(puts_by_strike))
                call_oi = chain_data.get("callOi")
                put_oi = chain_data.get("putOi")
                if call_oi is None:
                    call_oi = sum(float(row.get("oi") or 0) for row in call_rows)
                if put_oi is None:
                    put_oi = sum(float(row.get("oi") or 0) for row in put_rows)
                pcr = float(put_oi) / float(call_oi) if call_oi and float(call_oi) != 0 else np.nan
                spot = spot_row.get("ltp", spot_row.get("fp"))
                a, b, c, d = st.columns(4)
                a.metric("Underlying LTP", fmt(spot))
                b.metric("Put/Call OI ratio", fmt(pcr), help="Aggregate put open interest divided by aggregate call open interest for the returned chain.")
                c.metric("Call open interest", fmt(call_oi, 0))
                d.metric("Put open interest", fmt(put_oi, 0))
                # Max pain: settlement price that minimizes combined intrinsic payout using returned strike-level OI.
                pain_rows = []
                for settlement in strikes:
                    call_payout = sum(float(row.get("oi") or 0) * max(settlement - strike, 0) for strike, row in calls_by_strike.items())
                    put_payout = sum(float(row.get("oi") or 0) * max(strike - settlement, 0) for strike, row in puts_by_strike.items())
                    pain_rows.append({"Settlement candidate": settlement, "Combined payout": call_payout + put_payout})
                max_pain = min(pain_rows, key=lambda row: row["Combined payout"])["Settlement candidate"] if pain_rows else np.nan
                nearest_atm = min(strikes, key=lambda strike: abs(strike - float(spot))) if strikes and spot is not None else None
                atm_call = calls_by_strike.get(nearest_atm, {})
                atm_put = puts_by_strike.get(nearest_atm, {})
                straddle = float(atm_call.get("ltp") or 0) + float(atm_put.get("ltp") or 0) if nearest_atm is not None and (atm_call or atm_put) else np.nan
                e, f = st.columns(2)
                e.metric("Max pain (returned strikes)", fmt(max_pain))
                f.metric("ATM straddle premium", fmt(straddle), help="Call plus put LTP at the nearest returned strike to the underlying LTP.")
                table_rows = []
                for strike in strikes:
                    ce, pe = calls_by_strike.get(strike, {}), puts_by_strike.get(strike, {})
                    ce_g, pe_g = ce.get("greeks") or {}, pe.get("greeks") or {}
                    table_rows.append({
                        "Strike": strike,
                        "CE LTP": ce.get("ltp"), "CE Chg %": ce.get("ltpchp"), "CE OI": ce.get("oi"), "CE OI chg": ce.get("oich"), "CE IV": ce_g.get("iv"), "CE Delta": ce_g.get("delta"),
                        "PE LTP": pe.get("ltp"), "PE Chg %": pe.get("ltpchp"), "PE OI": pe.get("oi"), "PE OI chg": pe.get("oich"), "PE IV": pe_g.get("iv"), "PE Delta": pe_g.get("delta"),
                    })
                chain_frame = pd.DataFrame(table_rows)
                if nearest_atm is not None:
                    chain_frame["ATM"] = chain_frame["Strike"].eq(nearest_atm).map({True: "ATM", False: ""})
                    ordered = ["ATM", "Strike"] + [col for col in chain_frame.columns if col not in {"ATM", "Strike"}]
                    chain_frame = chain_frame[ordered]
                st.dataframe(chain_frame, use_container_width=True, hide_index=True)
                st.download_button("Download FYERS option chain CSV", chain_frame.to_csv(index=False).encode("utf-8"), f"{option_underlying.split(' · ')[-1].lower()}_fyers_option_chain.csv", "text/csv")
                st.caption(f"Source: FYERS Option Chain API · selected {option_underlying} ({option_underlying_labels[option_underlying]}) · returned strikes: {len(strikes)} · retrieved {datetime.now(ZoneInfo('Asia/Kolkata')):%d %b %Y %H:%M:%S IST}. Max pain is calculated only across the returned strikes, not the entire market chain.")
    st.markdown("[FYERS Option Chain API documentation](https://fyers.in/api-docs/v3/api/market-data/option-chain) · [Official NSE option chain](https://www.nseindia.com/option-chain)")

st.markdown("""<div style="font-size:clamp(28px,4vw,42px);font-weight:850;letter-spacing:-1.2px;line-height:1.15;margin:8px 0 6px;background:linear-gradient(90deg,#19e6a0,#29c7d6,#4aa8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Liquidity Board</div>""", unsafe_allow_html=True)
st.caption("Ranked daily-candle setups · green = bullish, red = bearish. Public data may be delayed.")
with st.expander("Scan bullish/bearish setups and liquidity sweeps", expanded=False):
    ob_lookback = st.selectbox("Structure lookback", ["20 sessions", "40 sessions", "60 sessions"], index=0, key="ob_lookback")
    ob_volume_mode = st.selectbox(
        "Volume confirmation",
        ["Off", "On — require at least 1.2× 20-day average"],
        index=0,
        key="ob_volume_mode_select",
        help="When On, liquidity-sweep signals are confirmed only when the latest volume is at least 1.2 times the previous 20-session average."
    )
    ob_volume_filter = ob_volume_mode.startswith("On")
    ob_scan = st.button("Scan NIFTY 50 liquid stocks", key="ob_scan_button", type="primary")
    if ob_scan:
        get_history.clear()
        ob_days = int(ob_lookback.split()[0])
        ob_symbols = [
            "SBIN.NS", "AXISBANK.NS", "TITAN.NS", "INFY.NS", "WIPRO.NS",
            "HCLTECH.NS", "HDFCBANK.NS", "ICICIBANK.NS", "RELIANCE.NS", "TCS.NS",
            "LT.NS", "BHARTIARTL.NS", "BAJFINANCE.NS", "TRENT.NS", "BEL.NS",
            "SUNPHARMA.NS", "MARUTI.NS", "TATAMOTORS.NS", "NTPC.NS", "ADANIPORTS.NS"
        ]
        ob_rows = []
        with st.spinner("Scanning daily candles and volume…"):
            for ob_symbol in ob_symbols:
                ob_df, ob_error = get_history(ob_symbol, "6mo", "1d")
                if ob_df.empty or len(ob_df) < max(ob_days + 2, 25) or not {"Open","High","Low","Close","Volume"}.issubset(ob_df.columns):
                    continue
                ob_df = ob_df.dropna(subset=["Open","High","Low","Close"]).copy()
                last = ob_df.iloc[-1]
                prior = ob_df.iloc[-ob_days-1:-1]
                if prior.empty:
                    continue
                prev_high = float(prior["High"].max())
                prev_low = float(prior["Low"].min())
                close = float(last["Close"])
                open_ = float(last["Open"])
                high = float(last["High"])
                low = float(last["Low"])
                prev_close = float(ob_df["Close"].iloc[-2])
                day_change = (close / prev_close - 1) * 100 if prev_close else np.nan
                avg_vol = pd.to_numeric(ob_df["Volume"].iloc[-21:-1], errors="coerce").mean()
                last_vol = pd.to_numeric(last.get("Volume"), errors="coerce")
                volume_ratio = float(last_vol / avg_vol) if pd.notna(last_vol) and pd.notna(avg_vol) and avg_vol > 0 else np.nan
                sweep_low = low < prev_low and close > prev_low
                sweep_high = high > prev_high and close < prev_high
                volume_ok = (pd.notna(volume_ratio) and volume_ratio >= 1.2) if ob_volume_filter else True
                # Heuristic demand/supply zones: last opposing candle before a 3-candle displacement.
                recent = ob_df.tail(8).reset_index(drop=True)
                demand_zone = None
                supply_zone = None
                for j in range(len(recent)-2, 0, -1):
                    candle = recent.iloc[j]
                    next_candle = recent.iloc[j+1]
                    if float(candle["Close"]) < float(candle["Open"]) and float(next_candle["Close"]) > float(candle["High"]):
                        demand_zone = (float(candle["Low"]), float(max(candle["Open"], candle["Close"])))
                        break
                for j in range(len(recent)-2, 0, -1):
                    candle = recent.iloc[j]
                    next_candle = recent.iloc[j+1]
                    if float(candle["Close"]) > float(candle["Open"]) and float(next_candle["Close"]) < float(candle["Low"]):
                        supply_zone = (float(min(candle["Open"], candle["Close"])), float(candle["High"]))
                        break
                if sweep_low and volume_ok:
                    bias, reason = "Bullish", "Sell-side liquidity sweep"
                elif sweep_high and volume_ok:
                    bias, reason = "Bearish", "Buy-side liquidity sweep"
                elif close > prev_high and day_change > 0:
                    bias, reason = "Bullish", f"Breakout above {ob_days}-session high"
                elif close < prev_low and day_change < 0:
                    bias, reason = "Bearish", f"Breakdown below {ob_days}-session low"
                elif close > open_ and day_change > 0:
                    bias, reason = "Bullish", "Positive daily candle"
                elif close < open_ and day_change < 0:
                    bias, reason = "Bearish", "Negative daily candle"
                else:
                    bias, reason = "Mixed", "No clear daily direction"
                ob_rows.append({
                    "Symbol": ob_symbol.removesuffix(".NS"),
                    "Bias": bias,
                    "Daily change (%)": day_change,
                    "Volume / 20D avg": volume_ratio,
                    "Liquidity event": "Sweep low · bullish reversal proxy" if sweep_low else "Sweep high · bearish reversal proxy" if sweep_high else "None detected",
                    "Setup": reason,
                    "Demand zone (approx.)": f"{demand_zone[0]:.2f}–{demand_zone[1]:.2f}" if demand_zone else "Not detected",
                    "Supply zone (approx.)": f"{supply_zone[0]:.2f}–{supply_zone[1]:.2f}" if supply_zone else "Not detected",
                    "Latest candle": last["Date"].strftime("%d %b %Y"),
                })
        ob_results = pd.DataFrame(ob_rows)
        if ob_results.empty:
            st.warning("No usable OHLCV data returned. Try again later; no sample values are substituted.")
        else:
            ob_results = ob_results.sort_values("Daily change (%)", ascending=False, na_position="last")
            st.session_state["ob_scan_results"] = ob_results
    if "ob_scan_results" in st.session_state:
        ob_results = st.session_state["ob_scan_results"]
        bull, bear = st.columns(2)
        bullish = ob_results[ob_results["Bias"] == "Bullish"].sort_values("Daily change (%)", ascending=False).head(3)
        bearish = ob_results[ob_results["Bias"] == "Bearish"].sort_values("Daily change (%)", ascending=True).head(3)
        with bull:
            st.markdown("""<div style="font-size:20px;font-weight:750;color:#20dda0;margin:8px 0 12px;">↗ Strongest bullish setups</div>""", unsafe_allow_html=True)
            if bullish.empty:
                st.info("No bullish setups detected.")
            else:
                for rank, (_, item) in enumerate(bullish.iterrows(), 1):
                    vol_label = f"{item['Volume / 20D avg']:.2f}× 20D avg volume" if pd.notna(item["Volume / 20D avg"]) else "Volume unavailable"
                    st.markdown(
                        f'<div style="background:#0d1219;border:1px solid #24313d;border-left:4px solid #20dda0;border-radius:18px;padding:14px 16px;margin:0 0 10px 0;">'
                        f'<div style="display:flex;justify-content:space-between;gap:8px;align-items:center;">'
                        f'<span style="font-size:17px;font-weight:750;color:#f3f6fa;">{rank}. {item["Symbol"]}</span>'
                        f'<span style="font-size:17px;font-weight:750;color:#20dda0;">{item["Daily change (%)"]:+.2f}%</span></div>'
                        f'<div style="color:#c5d0dc;font-size:13px;margin-top:8px;">{item["Setup"]}</div>'
                        f'<div style="color:#7eeac1;font-size:12px;margin-top:5px;">✓ {vol_label}</div></div>',
                        unsafe_allow_html=True,
                    )
        with bear:
            st.markdown("""<div style="font-size:20px;font-weight:750;color:#ff647c;margin:8px 0 12px;">↘ Strongest bearish setups</div>""", unsafe_allow_html=True)
            if bearish.empty:
                st.info("No bearish setups detected.")
            else:
                for rank, (_, item) in enumerate(bearish.iterrows(), 1):
                    vol_label = f"{item['Volume / 20D avg']:.2f}× 20D avg volume" if pd.notna(item["Volume / 20D avg"]) else "Volume unavailable"
                    st.markdown(
                        f'<div style="background:#0d1219;border:1px solid #24313d;border-left:4px solid #ff647c;border-radius:18px;padding:14px 16px;margin:0 0 10px 0;">'
                        f'<div style="display:flex;justify-content:space-between;gap:8px;align-items:center;">'
                        f'<span style="font-size:17px;font-weight:750;color:#f3f6fa;">{rank}. {item["Symbol"]}</span>'
                        f'<span style="font-size:17px;font-weight:750;color:#ff647c;">{item["Daily change (%)"]:+.2f}%</span></div>'
                        f'<div style="color:#c5d0dc;font-size:13px;margin-top:8px;">{item["Setup"]}</div>'
                        f'<div style="color:#ff9aaa;font-size:12px;margin-top:5px;">✓ {vol_label}</div></div>',
                        unsafe_allow_html=True,
                    )
        st.markdown("""<div style="font-size:clamp(26px,3.5vw,36px);font-weight:850;letter-spacing:-1px;margin:20px 0 10px;background:linear-gradient(90deg,#19e6a0,#29c7d6,#4aa8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Order Block Charts</div>""", unsafe_allow_html=True)
        zone_cols = st.columns(2)
        with zone_cols[0]:
            st.markdown("""<div style="background:#0d1219;border:1px solid #24313d;border-radius:16px;padding:13px 15px;margin-bottom:8px;"><span style="color:#20dda0;font-weight:750;">✓ Demand zones</span><div style="color:#c5d0dc;font-size:13px;margin-top:5px;">Potential support areas from bullish displacement candles</div></div>""", unsafe_allow_html=True)
        with zone_cols[1]:
            st.markdown("""<div style="background:#0d1219;border:1px solid #24313d;border-radius:16px;padding:13px 15px;margin-bottom:8px;"><span style="color:#ff647c;font-weight:750;">✓ Supply zones</span><div style="color:#c5d0dc;font-size:13px;margin-top:5px;">Potential resistance areas from bearish displacement candles</div></div>""", unsafe_allow_html=True)
        st.dataframe(ob_results, use_container_width=True, hide_index=True)
        st.download_button("Download liquidity scan (CSV)", ob_results.to_csv(index=False).encode("utf-8"), "liquidity_order_block_watchlist.csv", "text/csv", key="ob_scan_download")
        st.warning("Order blocks and liquidity sweeps here are simplified OHLCV heuristics, not confirmed institutional orders, market depth or guaranteed reversal signals. Data may be delayed. Use a stop-loss and verify prices with your broker.")


@st.cache_data(ttl=21600, show_spinner=False)
def get_turnaround_constituents():
    sources = {
        "Mid-cap": "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv",
        "Small-cap": "https://www.niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv",
    }
    rows = []
    errors = []
    for segment, url in sources.items():
        try:
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            response.raise_for_status()
            from io import StringIO
            frame = pd.read_csv(StringIO(response.text))
            symbol_col = next((c for c in frame.columns if str(c).strip().lower() == "symbol"), None)
            name_col = next((c for c in frame.columns if str(c).strip().lower() in ("company name", "companyname")), None)
            if symbol_col is None:
                errors.append(f"{segment} constituent list did not contain a Symbol column.")
                continue
            for _, row in frame.iterrows():
                symbol = str(row.get(symbol_col, "")).strip()
                if symbol and symbol.lower() != "nan":
                    rows.append({"Symbol": symbol, "Company": str(row.get(name_col, symbol)).strip() if name_col else symbol, "Segment": segment, "Ticker": symbol + ".NS"})
        except Exception as exc:
            errors.append(f"{segment} constituent list unavailable ({exc.__class__.__name__}).")
    if not rows:
        return pd.DataFrame(), "; ".join(errors) if errors else "No constituents returned."
    return pd.DataFrame(rows).drop_duplicates(subset=["Ticker"]), "; ".join(errors) if errors else None

@st.cache_data(ttl=21600, show_spinner=False)
def get_one_turnaround_fundamentals(ticker: str, segment: str, company: str):
    """Read quarterly income-statement data only; return no row when the source lacks enough data."""
    try:
        q = yf.Ticker(ticker).quarterly_income_stmt
        if q is None or q.empty or q.shape[1] < 2:
            return None
        q = q.copy()
        q.columns = pd.to_datetime(q.columns, errors="coerce")
        q = q.loc[:, ~q.columns.isna()].sort_index(axis=1, ascending=False)
        if q.shape[1] < 2:
            return None
        def metric(names, col):
            for name in names:
                if name in q.index:
                    try:
                        value = pd.to_numeric(q.loc[name, col], errors="coerce")
                        if pd.notna(value):
                            return float(value)
                    except Exception:
                        pass
            return np.nan
        rev_names = ["Total Revenue", "Operating Revenue", "Revenue"]
        profit_names = ["Net Income", "Net Income Common Stockholders", "Net Income From Continuing Operation Net Minority Interest"]
        op_names = ["Operating Income", "EBIT"]
        dates = list(q.columns)
        rev_now, rev_prev = metric(rev_names, dates[0]), metric(rev_names, dates[1])
        profit_now, profit_prev = metric(profit_names, dates[0]), metric(profit_names, dates[1])
        op_now = metric(op_names, dates[0])
        # Prefer year-on-year comparisons when a matching quarter four periods back exists.
        rev_base = metric(rev_names, dates[4]) if len(dates) >= 5 else np.nan
        profit_base = metric(profit_names, dates[4]) if len(dates) >= 5 else np.nan
        op_base = metric(op_names, dates[4]) if len(dates) >= 5 else np.nan
        if not np.isfinite(rev_now) or rev_now <= 0 or not np.isfinite(profit_now):
            return None
        rev_yoy = (rev_now / rev_base - 1) * 100 if np.isfinite(rev_base) and rev_base > 0 else np.nan
        if np.isfinite(profit_base) and profit_base != 0:
            profit_yoy = (profit_now / abs(profit_base) - 1) * 100 if profit_base > 0 else np.nan
        else:
            profit_yoy = np.nan
        margin_now = op_now / rev_now * 100 if np.isfinite(op_now) else np.nan
        margin_prev = metric(op_names, dates[1]) / rev_prev * 100 if np.isfinite(metric(op_names, dates[1])) and np.isfinite(rev_prev) and rev_prev > 0 else np.nan
        # A recovery from a loss to profit is explicitly flagged; growth rates across negative bases are not fabricated.
        loss_to_profit = np.isfinite(profit_base) and profit_base < 0 and profit_now > 0
        improving_profit = np.isfinite(profit_yoy) and profit_yoy >= 15
        improving_sales = np.isfinite(rev_yoy) and rev_yoy >= 10
        margin_improving = np.isfinite(margin_now) and np.isfinite(margin_prev) and margin_now > margin_prev
        signals = sum([improving_sales, improving_profit, margin_improving, loss_to_profit])
        if signals < 2:
            return None
        latest_date = dates[0].strftime("%Y-%m-%d") if hasattr(dates[0], "strftime") else str(dates[0])
        return {
            "Company": company, "Symbol": ticker.replace(".NS", ""), "Segment": segment,
            "Latest quarter": latest_date, "Sales growth YoY %": round(rev_yoy, 1) if np.isfinite(rev_yoy) else np.nan,
            "Profit growth YoY %": round(profit_yoy, 1) if np.isfinite(profit_yoy) else ("Loss → profit" if loss_to_profit else np.nan),
            "Operating margin %": round(margin_now, 1) if np.isfinite(margin_now) else np.nan,
            "Margin improved QoQ": bool(margin_improving) if np.isfinite(margin_now) and np.isfinite(margin_prev) else "Insufficient data",
            "Turnaround signals": int(signals),
            "Reason flagged": ", ".join([label for label, ok in [
                ("Sales growth ≥10% YoY", improving_sales),
                ("Profit growth ≥15% YoY", improving_profit),
                ("Loss-to-profit recovery", loss_to_profit),
                ("Operating margin improved QoQ", margin_improving),
            ] if ok]),
            "Source": "Yahoo Finance quarterly income statement",
        }
    except Exception:
        return None

def run_turnaround_scan(segments: tuple[str, ...]):
    universe, universe_error = get_turnaround_constituents()
    if universe.empty:
        return pd.DataFrame(), universe_error or "Index constituent data unavailable."
    universe = universe[universe["Segment"].isin(segments)]
    # A modest worker pool keeps the screen practical while covering the selected index constituents.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    found = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(get_one_turnaround_fundamentals, row["Ticker"], row["Segment"], row["Company"]) for _, row in universe.iterrows()]
        for future in as_completed(futures):
            try:
                item = future.result()
                if item:
                    found.append(item)
            except Exception:
                continue
    result = pd.DataFrame(found)
    if not result.empty:
        # Rank candidates using transparent, capped fundamentals signals so extreme
        # one-quarter growth does not dominate the entire watchlist.
        sales = pd.to_numeric(result["Sales growth YoY %"], errors="coerce").clip(lower=0, upper=50).fillna(0)
        profit = pd.to_numeric(result["Profit growth YoY %"], errors="coerce").clip(lower=0, upper=50).fillna(0)
        signals = pd.to_numeric(result["Turnaround signals"], errors="coerce").fillna(0).clip(0, 4)
        margin_improved = result["Margin improved QoQ"].map(lambda value: value is True or str(value).strip().lower() == "true").astype(int)
        loss_to_profit = result["Profit growth YoY %"].astype(str).str.contains("Loss", case=False, na=False).astype(int)
        result["Turnaround Score"] = (
            signals / 4 * 40
            + sales / 50 * 25
            + profit / 50 * 25
            + margin_improved * 10
            + loss_to_profit * 5
        ).clip(upper=100).round(1)
        result = result.sort_values(
            ["Turnaround Score", "Turnaround signals", "Sales growth YoY %"],
            ascending=[False, False, False], na_position="last"
        ).reset_index(drop=True)
    return result, universe_error

st.markdown("""<div style="font-size:clamp(28px,4vw,42px);font-weight:850;letter-spacing:-1.2px;line-height:1.15;margin:8px 0 6px;background:linear-gradient(90deg,#19e6a0,#29c7d6,#4aa8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Turnaround Board</div>""", unsafe_allow_html=True)
st.caption("Small-cap + mid-cap candidates · improving sales, profit recovery and operating margins. Public data may be delayed or incomplete.")
with st.expander("Scan small-cap / mid-cap turnaround stocks", expanded=False):
    st.write("Scans current Nifty Midcap 150 and Nifty Smallcap 250 constituents. Newly qualifying stocks can appear when the source data updates. Research watchlist only, not a buy signal.")
    segment_choice = st.multiselect("Segments to scan", ["Mid-cap", "Small-cap"], default=["Mid-cap", "Small-cap"], key="turnaround_segments")
    st.caption("Fundamentals: Yahoo Finance quarterly income statements · Constituents: Nifty Indices CSV lists · Cached for up to 6 hours. Initial scans may take a few minutes.")
    auto_turnaround_scan = st.checkbox(
        "Automatically refresh turnaround scan when dashboard refreshes",
        value=False,
        key="auto_turnaround_scan_enabled",
        help="Opt-in because a first scan can take several minutes. Fundamental source data is cached for up to 6 hours."
    )
    turnaround_scan_clicked = st.button("Scan / refresh turnaround stocks", type="primary", key="run_auto_turnaround_scan", disabled=not segment_choice)
    if turnaround_scan_clicked:
        get_turnaround_constituents.clear()
        get_one_turnaround_fundamentals.clear()
    if (turnaround_scan_clicked or auto_turnaround_scan) and segment_choice:
        with st.spinner("Scanning index constituents for improving fundamentals. This can take a few minutes…"):
            scan_df, scan_warning = run_turnaround_scan(tuple(sorted(segment_choice)))
        previous_scan = st.session_state.get("auto_turnaround_scan_results", pd.DataFrame())
        if not scan_df.empty:
            current_symbols = set(scan_df["Symbol"].astype(str))
            if isinstance(previous_scan, pd.DataFrame) and not previous_scan.empty and "Symbol" in previous_scan.columns:
                previous_symbols = set(previous_scan["Symbol"].astype(str))
                scan_df["Status"] = scan_df["Symbol"].astype(str).map(
                    lambda symbol: "🆕 NEW" if symbol not in previous_symbols else "Existing"
                )
            else:
                # First scan is the baseline; later scans can identify newly qualifying names.
                scan_df["Status"] = "Baseline"
        st.session_state["auto_turnaround_scan_results"] = scan_df
        st.session_state["auto_turnaround_scan_time"] = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S IST")
        st.session_state["auto_turnaround_scan_segments"] = ", ".join(segment_choice)
        st.session_state["auto_turnaround_scan_warning"] = scan_warning
    scan_df = st.session_state.get("auto_turnaround_scan_results", pd.DataFrame())
    if st.session_state.get("auto_turnaround_scan_time"):
        st.caption("Last scan: " + st.session_state["auto_turnaround_scan_time"] + " · Segments: " + st.session_state.get("auto_turnaround_scan_segments", ""))
    if st.session_state.get("auto_turnaround_scan_warning"):
        st.warning(st.session_state["auto_turnaround_scan_warning"])
    if isinstance(scan_df, pd.DataFrame) and not scan_df.empty:
        new_count = int(scan_df["Status"].eq("🆕 NEW").sum()) if "Status" in scan_df.columns else 0
        st.success(f"{len(scan_df)} potential turnaround candidate(s) matched the current rules · {new_count} newly qualifying since the previous scan.")
        display_cols = ["Status", "Turnaround Score"] + [col for col in scan_df.columns if col not in ("Status", "Turnaround Score")]
        st.dataframe(scan_df[display_cols], use_container_width=True, hide_index=True, height=520)
        st.download_button("Download turnaround watchlist CSV", scan_df.to_csv(index=False).encode("utf-8"), file_name="small_midcap_turnaround_watchlist.csv", mime="text/csv", key="download_auto_turnaround_csv")
        st.caption("Missing metrics remain blank. Profit growth across a negative comparison base is not calculated; loss-to-profit is flagged separately. Debt, cash flow and valuation are not screened in this first version because quarterly statement coverage is inconsistent.")
    elif "auto_turnaround_scan_time" in st.session_state:
        st.info("No candidates met the current rules, or the data provider returned insufficient quarterly data. This is not proof that no turnaround stocks exist.")
    else:
        st.info("Select segments and press Scan / refresh to generate the first watchlist. Re-run after market-data refresh to catch new qualifying stocks.")






