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
import streamlit.components.v1 as components
import yfinance as yf

import hashlib
import hmac
import secrets
import time
import re
from urllib.parse import urlencode

st.set_page_config(page_title="Market Intelligence Terminal", page_icon="📈", layout="wide")

# Refresh the dashboard page during NSE cash-market hours. Market-data functions
# retain their own cache TTLs (typically 5 minutes), so this does not promise live ticks.
components.html("""
<script>
(() => {
  const scheduleNextRefresh = () => {
    const now = new Date();
    const day = now.getDay();
    const minutes = now.getHours() * 60 + now.getMinutes();
    const isWeekday = day >= 1 && day <= 5;
    const isMarketHours = minutes >= (9 * 60 + 15) && minutes < (15 * 60 + 30);
    if (isWeekday && isMarketHours) {
      window.setTimeout(() => {
        try { window.parent.location.reload(); }
        catch (_) { window.location.reload(); }
      }, 5 * 60 * 1000);
    }
  };
  scheduleNextRefresh();
})();
</script>
""", height=0, width=0)
st.markdown("""
<style>
.block-container{padding-top:1rem;padding-bottom:2rem}
[data-testid="stMetric"]{border-left:4px solid #64748b}
.signal-good [data-testid="stMetric"]{border-left-color:#22c55e}
.signal-bad [data-testid="stMetric"]{border-left-color:#ef4444}
.signal-neutral [data-testid="stMetric"]{border-left-color:#f59e0b}
div[data-testid="stExpander"]{border:1px solid rgba(148,163,184,.3);border-radius:12px;overflow:hidden}
div[data-testid="stExpander"] summary{font-weight:650}
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

with st.sidebar:
    st.header("Dashboard controls")
    period = st.selectbox("Historical lookback", ["1mo", "3mo", "6mo", "1y", "2y", "5y"], index=2)
    interval = st.selectbox("Intraday bar interval", ["5m", "15m", "30m", "60m"], index=0)
    st.caption("Intraday Yahoo Finance history is limited by the upstream source. If it is unavailable, the app will show an error instead of substituting sample values.")
    refresh = st.button("↻ Refresh market data", use_container_width=True)
    st.divider()
    st.markdown("**Public source links**")
    st.markdown("- [NSE reports](https://www.nseindia.com/all-reports)")
    st.markdown("- [Nifty Indices historical data](https://www.niftyindices.com/reports/historical-data)")
    st.markdown("- [NSE FII/DII reports](https://www.nseindia.com/reports/fii-dii)")
    st.markdown("- [RBI](https://www.rbi.org.in/)")
    st.markdown("- [TradingView widgets](https://www.tradingview.com/widget/)")
    st.markdown("- [Yahoo Finance data coverage/delays](https://help.yahoo.com/kb/SLN2310.html)")


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
                    st.error(f"Token refresh failed: {refresh_error}. The refresh token may have expired; reconnect FYERS if needed.")
        else:
            st.link_button(
                "Connect FYERS account",
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


@st.cache_data(ttl=300, show_spinner=False)
def get_history(ticker: str, period: str, interval: str) -> tuple[pd.DataFrame, str | None]:
    """Fetch best-effort market history from Yahoo Finance via yfinance; never synthesize rows."""
    try:
        data = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=False, actions=False, raise_errors=True)
        if data is None or data.empty:
            return pd.DataFrame(), "Source returned no rows for this ticker/period/interval."
        data = data.reset_index()
        date_col = "Datetime" if "Datetime" in data.columns else "Date"
        data = data.rename(columns={date_col: "Date"})
        data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
        if data["Date"].dt.tz is None:
            data["Date"] = data["Date"].dt.tz_localize("UTC").dt.tz_convert("Asia/Kolkata")
        else:
            data["Date"] = data["Date"].dt.tz_convert("Asia/Kolkata")
        for c in ["Open", "High", "Low", "Close", "Volume"]:
            if c in data.columns:
                data[c] = pd.to_numeric(data[c], errors="coerce")
        data = data.dropna(subset=["Date", "Close"]).sort_values("Date").drop_duplicates("Date", keep="last")
        return data.reset_index(drop=True), None
    except Exception as exc:
        return pd.DataFrame(), f"Fetch failed: {type(exc).__name__}: {str(exc)[:240]}"

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
                          threads=True, progress=False, actions=False)
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
    change = float(last["Close"] - prev["Close"]) if prev is not None else np.nan
    pct = change / float(prev["Close"]) * 100 if prev is not None and float(prev["Close"]) != 0 else np.nan
    return last, change, pct

# Pull key instruments. The sidebar interval controls intraday bars; daily history is used for indicators.
market_data: dict[str, pd.DataFrame] = {}
market_errors: dict[str, str | None] = {}
with st.spinner("Loading best-effort public market data…"):
    for name, ticker in INDEXES.items():
        # For equities and indices, fetch daily bars for stable technical calculations.
        hist, err = get_history(ticker, period, "1d")
        market_data[name], market_errors[name] = hist, err

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
        # For USD/INR, a falling rate means the rupee appreciates; reverse the colour semantics.
        if name == "USD/INR":
            delta_text = f"{fmt(change)} ({fmt(pct)}%)"
            delta_color = "inverse"
            delta_text += " · INR stronger" if change < 0 else (" · INR weaker" if change > 0 else "")
            col.metric(name, fmt(last["Close"]), delta_text, delta_color=delta_color)
        else:
            col.metric(name, fmt(last["Close"]), f"{fmt(change)} ({fmt(pct)}%)")
        # Source-bar timestamp retained in data, but hidden from the compact headline display.
    else:
        col.metric(name, "Unavailable")
        # Error details remain available in market_errors; avoid displaying extra helper text here.

# Hidden headline explanation retained here in code; display headline metrics without extra notes.

# Daily global-cue sentiment panel is intentionally placed immediately above Market Regime.
st.subheader("Today's Market Direction · Global Sentiment")
# Hidden sentiment explanation retained here in code; display the main heading and sentiment output.
sentiment_parts = []
sentiment_details = []
sentiment_errors = []
def _sentiment_change(label, ticker, weight, inverse=False):
    try:
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
        sentiment_details.append({"Factor": label, "Latest change": f"{pct:+.2f}%", "Signal": sentiment_signal, "Source bar": last["Date"].strftime("%d %b %Y")})
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
        gift_match = re.search(
            r"([+-]?\d[\d,]*\.\d{1,2})\s*\(\s*([+-]?\d+\.\d{1,2})\s*%\s*\)",
            gift_search_text,
        )
        if not gift_match:
            # Fallback: inspect raw HTML/embedded JSON where whitespace or tags may differ.
            gift_raw = html.unescape(gift_html).replace("\\u002d", "-").replace("\\/", "/")
            gift_match = re.search(
                r"([+-]?\d[\d,]*\.\d{1,2})[^0-9]{0,100}\(\s*([+-]?\d+\.\d{1,2})\s*%\s*\)",
                gift_raw,
                flags=re.IGNORECASE,
            )
        if not gift_match:
            # Some templates render the point change and percentage in separate adjacent elements.
            gift_match = re.search(
                r"(?:GIFT NIFTY.{0,1200}?)([+-]\d[\d,]*\.\d{1,2}).{0,100}?([+-]\d+\.\d{1,2})\s*%",
                gift_text,
                flags=re.IGNORECASE,
            )
        if not gift_match:
            gift_failures.append(f"{gift_source_name}: quote pattern not found")
            continue
        gift_change_points = float(gift_match.group(1).replace(",", ""))
        gift_change_pct = float(gift_match.group(2))
        gift_score = 1 if gift_change_pct > 0 else -1 if gift_change_pct < 0 else 0
        sentiment_parts.append((gift_score, 0.40))
        gift_asof_match = re.search(r"Last updated:\s*([^·]{1,80})", gift_search_text, flags=re.IGNORECASE)
        gift_asof = gift_asof_match.group(1).strip()[:24] if gift_asof_match else "Timestamp unavailable"
        sentiment_details.append({
            "Factor": f"GIFT Nifty · {gift_source_name}",
            "Latest change": f"{gift_change_pct:+.2f}% ({gift_change_points:+,.2f} pts)",
            "Signal": "Positive" if gift_score > 0 else "Negative" if gift_score < 0 else "Flat",
            "Source bar": gift_asof,
        })
        gift_loaded = True
        break
    except Exception as exc:
        gift_failures.append(f"{gift_source_name}: {exc.__class__.__name__}")
if not gift_loaded:
    # Last visibly confirmed GiftCityNifty.com snapshot supplied by the user.
    # Show it transparently as stale context only; do not add it to the current weighted score.
    sentiment_details.append({
        "Factor": "GIFT Nifty · GiftCityNifty.com (last confirmed snapshot; stale)",
        "Latest change": "-0.32% (-72.50 pts)",
        "Signal": "Negative",
        "Source bar": "24 Oct 2026 · stale",
    })
    sentiment_errors.append("GIFT Nifty live quote unavailable; showing the last confirmed 24 Oct snapshot for reference only (" + "; ".join(gift_failures) + ")")
_sentiment_change("Brent crude (inverse impact)", "BZ=F", 0.20, inverse=True)
_sentiment_change("USD/INR (inverse impact)", "INR=X", 0.10, inverse=True)

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
        for value in result.values():
            value.pop("_parsed_date", None)
        return result if "FII/FPI" in result and "DII" in result else {}

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
    return {}

fii_dii_flows = _nse_fii_dii_latest()
for flow_name, flow_weight in [("FII/FPI", 0.075), ("DII", 0.075)]:
    flow = fii_dii_flows.get(flow_name)
    if flow is None:
        sentiment_errors.append(f"{flow_name} cash-market flow: unavailable from NSE")
        sentiment_details.append({"Factor": f"{flow_name} net flow (NSE cash market)", "Latest change": "Unavailable", "Signal": "Unavailable", "Source bar": "NSE data not retrieved"})
        continue
    net_flow = flow["net"]
    flow_score = 1 if net_flow > 0 else -1 if net_flow < 0 else 0
    sentiment_parts.append((flow_score, flow_weight))
    sentiment_details.append({
        "Factor": f"{flow_name} net flow (NSE cash market)",
        "Latest change": f"₹{net_flow:+,.2f} crore",
        "Signal": "Positive" if flow_score > 0 else "Negative" if flow_score < 0 else "Flat",
        "Source bar": flow["date"],
    })
_sentiment_change("Infosys ADR", "INFY", 0.15, inverse=False)
_sentiment_change("Wipro ADR", "WIT", 0.10, inverse=False)
_sentiment_change("HDFC Bank ADR", "HDB", 0.075, inverse=False)
_sentiment_change("ICICI Bank ADR", "IBN", 0.075, inverse=False)
_sentiment_change("Reliance Industries GDR", "RIGD.L", 0.075, inverse=False)
# Do not publish a confident full-market direction when the highest-weight GIFT Nifty cue is missing.
# Partial score remains visible for transparency, but the headline is explicitly marked incomplete.
gift_available = any(str(item.get("Factor", "")).startswith("GIFT Nifty") for item in sentiment_details)
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
    sentiment_df = pd.DataFrame(sentiment_details)

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

    # Split all factors into two balanced tables, five rows on each side.
    sentiment_left, sentiment_right = st.columns(2, gap="small")
    for target_col, start_idx in [(sentiment_left, 0), (sentiment_right, 5)]:
        with target_col:
            subset = sentiment_df.iloc[start_idx:start_idx + 5].copy()
            if not subset.empty:
                styled_subset = subset.style.applymap(_sentiment_cell_style, subset=["Signal"]).apply(_sentiment_change_style, axis=1)
                st.dataframe(styled_subset, use_container_width=True, hide_index=True)
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
        # Weekly EMA 200 is calculated for the instrument selected in the regime selector.
        # Standard indices use their Yahoo ticker; NIFTY 500 constituents use their .NS ticker.
        weekly_ema200 = np.nan
        weekly_selected_close = np.nan
        weekly_selected_timestamp = "Unavailable"
        weekly_ema_error = None
        selected_yahoo_ticker = INDEXES.get(overview_regime_name, overview_regime_name + ".NS")
        try:
            weekly_selected = yf.download(selected_yahoo_ticker, period="10y", interval="1wk", auto_adjust=False, progress=False, threads=False)
            if not weekly_selected.empty:
                if isinstance(weekly_selected.columns, pd.MultiIndex):
                    weekly_selected.columns = weekly_selected.columns.get_level_values(0)
                weekly_selected = weekly_selected.rename_axis("Date").reset_index()
                weekly_selected.columns = [str(c).strip().title() for c in weekly_selected.columns]
                if "Close" in weekly_selected.columns:
                    weekly_selected["Close"] = pd.to_numeric(weekly_selected["Close"], errors="coerce")
                    weekly_selected = weekly_selected.dropna(subset=["Close"]).sort_values("Date")
                    if len(weekly_selected) >= 200:
                        weekly_selected_close = float(weekly_selected["Close"].iloc[-1])
                        weekly_ema200 = float(weekly_selected["Close"].ewm(span=200, adjust=False, min_periods=200).mean().iloc[-1])
                        weekly_selected_timestamp = pd.to_datetime(weekly_selected["Date"].iloc[-1]).strftime("%d %b %Y")
                    else:
                        weekly_ema_error = f"Fewer than 200 weekly closes returned for {overview_regime_name}"
                else:
                    weekly_ema_error = "Weekly Close column unavailable"
            else:
                weekly_ema_error = f"No weekly data returned for {overview_regime_name}"
        except Exception as exc:
            weekly_ema_error = f"Weekly data unavailable ({exc.__class__.__name__})"
        weekly_ema_gap_pct = ((weekly_selected_close / weekly_ema200) - 1) * 100 if pd.notna(weekly_selected_close) and pd.notna(weekly_ema200) and weekly_ema200 != 0 else np.nan

        # PCR is instrument-specific and only shown when FYERS returns a valid option chain.
        selected_fyers_symbol = None
        if overview_regime_name == "NIFTY 50":
            selected_fyers_symbol = "NSE:NIFTY50-INDEX"
        elif overview_regime_name == "BANK NIFTY":
            selected_fyers_symbol = "NSE:NIFTYBANK-INDEX"
        elif overview_regime_name in INDEXES:
            selected_fyers_symbol = None  # Do not request an option chain for non-optionable indicators/commodities/FX.
        else:
            selected_fyers_symbol = "NSE:" + overview_regime_name.upper() + "-EQ"
        nifty_pcr = np.nan
        nifty_pcr_status = f"PCR unavailable for {overview_regime_name}"
        nifty_pcr_source_time = "Unavailable"
        if selected_fyers_symbol and st.session_state.get("fyers_access_token") and fyers_credentials():
            try:
                pcr_app_id = fyers_credentials()[0]
                pcr_token = st.session_state["fyers_access_token"]
                pcr_chain, pcr_error = fyers_option_chain(pcr_app_id, pcr_token, selected_fyers_symbol, strikecount=10)
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
                        nifty_pcr_status = "Low put/call OI ratio" if nifty_pcr < 0.6 else "High put/call OI ratio" if nifty_pcr > 1.4 else "Neutral PCR range"
                        nifty_pcr_source_time = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y %H:%M IST")
                    else:
                        nifty_pcr_status = "PCR unavailable: call OI is zero or missing"
                else:
                    nifty_pcr_status = f"FYERS option chain unavailable for {overview_regime_name}"
            except Exception:
                nifty_pcr_status = f"FYERS option chain unavailable for {overview_regime_name}"
        elif selected_fyers_symbol:
            nifty_pcr_status = "Connect FYERS for option-chain PCR"
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
        rsi_color = "#ef4444" if pd.notna(overview_rsi_value) and overview_rsi_value < 30 else "#22c55e" if pd.notna(overview_rsi_value) and overview_rsi_value > 70 else "#f59e0b" if pd.notna(overview_rsi_value) and (overview_rsi_value >= 60 or overview_rsi_value <= 40) else "#94a3b8"
        regime_cols[1].markdown(f'<div style="border:1px solid #343a46;border-left:4px solid {adx_color};border-radius:12px;padding:12px 14px;min-height:92px;"><div style="font-size:13px;color:#aeb6c2;margin-bottom:8px;">ADX 14 · trend strength</div><div style="font-size:clamp(20px,2.8vw,27px);font-weight:700;color:{adx_color};">{fmt(overview_adx_value)}</div><div style="font-size:12px;color:#aeb6c2;">{ "Strong trend" if pd.notna(overview_adx_value) and overview_adx_value >= 25 else "Trend building" if pd.notna(overview_adx_value) and overview_adx_value >= 20 else "Weak trend" }</div></div>', unsafe_allow_html=True)
        regime_cols[2].markdown(f'<div style="border:1px solid #343a46;border-left:4px solid {rsi_color};border-radius:12px;padding:12px 14px;min-height:92px;"><div style="font-size:13px;color:#aeb6c2;margin-bottom:8px;">RSI 14 · momentum</div><div style="font-size:clamp(20px,2.8vw,27px);font-weight:700;color:{rsi_color};">{fmt(overview_rsi_value)}</div><div style="font-size:12px;color:#aeb6c2;">{ "Oversold" if pd.notna(overview_rsi_value) and overview_rsi_value < 30 else "Overbought" if pd.notna(overview_rsi_value) and overview_rsi_value > 70 else "Bullish momentum" if pd.notna(overview_rsi_value) and overview_rsi_value >= 60 else "Bearish momentum" if pd.notna(overview_rsi_value) and overview_rsi_value <= 40 else "Neutral" }</div></div>', unsafe_allow_html=True)
        weekly_close_text = f"{weekly_selected_close:,.2f}" if pd.notna(weekly_selected_close) else "—"
        weekly_ema_text = f"{weekly_ema200:,.2f}" if pd.notna(weekly_ema200) else "—"
        weekly_gap_text = f"{weekly_ema_gap_pct:+.2f}%" if pd.notna(weekly_ema_gap_pct) else "—"
        weekly_subtext = weekly_ema_status if pd.notna(weekly_ema_gap_pct) else weekly_ema_error or weekly_ema_status
        regime_cols[3].markdown(f'<div style="border:1px solid #343a46;border-left:4px solid {weekly_ema_color};border-radius:12px;padding:12px 14px;min-height:92px;"><div style="font-size:13px;color:#aeb6c2;margin-bottom:8px;">{html.escape(overview_regime_name)} · Weekly EMA 200 gap</div><div style="font-size:clamp(20px,2.8vw,27px);font-weight:700;color:{weekly_ema_color};">{weekly_gap_text}</div><div style="font-size:12px;color:#aeb6c2;">Close {weekly_close_text} · EMA {weekly_ema_text}</div><div style="font-size:12px;color:{weekly_ema_color};margin-top:4px;">{weekly_subtext}</div></div>', unsafe_allow_html=True)
        nifty_pcr_text = f"{nifty_pcr:.2f}" if pd.notna(nifty_pcr) else "—"
        regime_cols[4].markdown(f'<div style="border:1px solid #343a46;border-left:4px solid {nifty_pcr_color};border-radius:12px;padding:12px 14px;min-height:92px;"><div style="font-size:13px;color:#aeb6c2;margin-bottom:8px;">{html.escape(overview_regime_name)} · PCR (OI)</div><div style="font-size:clamp(20px,2.8vw,27px);font-weight:700;color:{nifty_pcr_color};">{nifty_pcr_text}</div><div style="font-size:12px;color:{nifty_pcr_color};margin-top:4px;">{nifty_pcr_status}</div><div style="font-size:11px;color:#aeb6c2;margin-top:4px;">Source: FYERS · {nifty_pcr_source_time}</div></div>', unsafe_allow_html=True)
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
        with fear_col:
            fear_score = np.nan
            fear_components = []
            recent_rc = overview_rc.dropna(subset=["Close"]).copy()
            if len(recent_rc) >= 20:
                # RSI contribution: RSI 50 maps to neutral; 0..100 maps to the same scale.
                rsi_series = recent_rc["RSI 14"] if "RSI 14" in recent_rc.columns else pd.Series(dtype=float)
                if not rsi_series.dropna().empty:
                    fear_components.append((float(np.clip(rsi_series.dropna().iloc[-1], 0, 100)), 0.35))
                # Trend contribution: close vs EMA 20 and EMA 50, mapped around neutral.
                trend_parts = []
                for ema_col in ["EMA 20", "EMA 50"]:
                    if ema_col in recent_rc.columns and pd.notna(recent_rc[ema_col].iloc[-1]) and recent_rc[ema_col].iloc[-1] != 0:
                        gap_pct = (float(recent_rc["Close"].iloc[-1]) / float(recent_rc[ema_col].iloc[-1]) - 1) * 100
                        trend_parts.append(float(np.clip(50 + gap_pct * 12, 0, 100)))
                if trend_parts:
                    fear_components.append((float(np.mean(trend_parts)), 0.40))
                # Medium-term momentum: 20-session return mapped around neutral.
                if len(recent_rc) >= 21 and recent_rc["Close"].iloc[-21] != 0:
                    ret20 = (float(recent_rc["Close"].iloc[-1]) / float(recent_rc["Close"].iloc[-21]) - 1) * 100
                    fear_components.append((float(np.clip(50 + ret20 * 8, 0, 100)), 0.15))
                # India VIX: higher volatility tilts the proxy toward fear.
                vix_frame = market_data.get("India VIX", pd.DataFrame())
                vix_snap = quote_snapshot(vix_frame) if not vix_frame.empty else None
                if vix_snap:
                    vix_value = float(vix_snap[0]["Close"])
                    fear_components.append((float(np.clip(100 - vix_value * 3, 0, 100)), 0.10))
                if fear_components:
                    fear_weight = sum(weight for _, weight in fear_components)
                    fear_score = sum(value * weight for value, weight in fear_components) / fear_weight
            if pd.notna(fear_score):
                fear_score = int(round(float(np.clip(fear_score, 0, 100))))
                fear_label = "Extreme Fear" if fear_score < 20 else "Fear" if fear_score < 40 else "Neutral" if fear_score < 60 else "Greed" if fear_score < 80 else "Extreme Greed"
                fear_color = "#ef4444" if fear_score < 20 else "#f97316" if fear_score < 40 else "#f59e0b" if fear_score < 60 else "#84cc16" if fear_score < 80 else "#16a34a"
                gauge_fig = go.Figure(go.Indicator(
                    mode="gauge+number",
                    value=fear_score,
                    number={"font": {"size": 42, "color": fear_color}, "suffix": "/100"},
                    title={"text": "Fear & Greed", "font": {"size": 18}},
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
                fear_col.markdown(f'<div style="text-align:center;font-weight:800;color:{fear_color};font-size:26px;line-height:1.2;margin-top:-2px;padding:4px 0 8px;">{fear_label.upper()}</div>', unsafe_allow_html=True)

            else:
                fear_col.info("Fear & Greed proxy unavailable: insufficient indicator data.")

# Compact support/resistance follows the currently selected regime instrument.
# Classic floor pivots use the latest completed daily candle returned for that instrument.
sr_display_name = overview_regime_name
sr_yahoo_ticker = INDEXES.get(sr_display_name, sr_display_name + ".NS")
st.markdown(f"#### {html.escape(sr_display_name)} · Support & Resistance")
sr_history_df = market_data.get(sr_display_name, pd.DataFrame()).copy()
# Market headline history may be short/intraday; fetch daily bars for a consistent pivot source.
if sr_history_df.empty or not {"High", "Low", "Close"}.issubset(sr_history_df.columns):
    sr_history_df, sr_history_error = get_history(sr_yahoo_ticker, "3mo", "1d")
sr_prev = None
if not sr_history_df.empty and {"High", "Low", "Close"}.issubset(sr_history_df.columns):
    sr_history = sr_history_df.dropna(subset=["High", "Low", "Close"]).copy()
    if "Date" in sr_history.columns:
        sr_history["Date"] = pd.to_datetime(sr_history["Date"], errors="coerce")
        sr_history = sr_history.dropna(subset=["Date"]).sort_values("Date")
    if not sr_history.empty:
        latest_sr_day = sr_history.iloc[-1]["Date"].date() if "Date" in sr_history.columns else None
        today_ist = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        # Avoid using a potentially incomplete candle dated today.
        if latest_sr_day is not None and latest_sr_day >= today_ist and len(sr_history) >= 2:
            sr_prev = sr_history.iloc[-2]
        else:
            sr_prev = sr_history.iloc[-1]
if sr_prev is not None:
    sr_high, sr_low, sr_close = float(sr_prev["High"]), float(sr_prev["Low"]), float(sr_prev["Close"])
    sr_pivot = (sr_high + sr_low + sr_close) / 3.0
    sr_levels = {
        "R2": sr_pivot + (sr_high - sr_low),
        "R1": 2 * sr_pivot - sr_low,
        "Pivot": sr_pivot,
        "S1": 2 * sr_pivot - sr_high,
        "S2": sr_pivot - (sr_high - sr_low),
    }
    sr_cols = st.columns(5)
    sr_colors = {"R2": "#ef4444", "R1": "#f97316", "Pivot": "#f59e0b", "S1": "#22c55e", "S2": "#16a34a"}
    for sr_col, sr_name in zip(sr_cols, ["R2", "R1", "Pivot", "S1", "S2"]):
        sr_col.markdown(
            f'<div style="border:1px solid #343a46;border-top:3px solid {sr_colors[sr_name]};border-radius:8px;padding:7px 9px;text-align:center;">'
            f'<div style="font-size:12px;color:#aeb6c2;">{sr_name}</div>'
            f'<div style="font-size:16px;font-weight:650;color:{sr_colors[sr_name]};">{sr_levels[sr_name]:,.2f}</div></div>',
            unsafe_allow_html=True,
        )
    sr_date = sr_prev["Date"].strftime("%d %b %Y") if "Date" in sr_prev.index and pd.notna(sr_prev["Date"]) else "latest available daily candle"
    st.caption(f"Classic pivot levels for {sr_display_name}, calculated from {sr_date} OHLC · Informational only; source: returned daily history for {sr_yahoo_ticker}.")
else:
    st.caption(f"Support/resistance unavailable for {sr_display_name}: insufficient valid daily OHLC data from {sr_yahoo_ticker}.")

# Leading sectors: rank recent 1-week performance against NIFTY 50, independent of sidebar lookback.
st.markdown("### Leading sectors vs NIFTY 50")
st.caption("Top 3 sectors ranked by relative performance versus NIFTY 50 over the latest 5 trading sessions. Cards are ranked by weekly outperformance; 1-month relative performance is shown for confirmation.")
leading_sector_rows = []
with st.spinner("Checking weekly sector momentum…"):
    weekly_nifty_df, weekly_nifty_err = get_history("^NSEI", "3mo", "1d")
    weekly_nifty_close = weekly_nifty_df["Close"].dropna() if not weekly_nifty_df.empty else pd.Series(dtype=float)
    if len(weekly_nifty_close) >= 6:
        nifty_week_return = (float(weekly_nifty_close.iloc[-1]) / float(weekly_nifty_close.iloc[-6]) - 1) * 100
        nifty_month_return = (float(weekly_nifty_close.iloc[-1]) / float(weekly_nifty_close.iloc[-22]) - 1) * 100 if len(weekly_nifty_close) >= 22 else np.nan
        for sector, ticker in SECTOR_TICKERS.items():
            sector_df, sector_err = get_history(ticker, "3mo", "1d")
            if not sector_df.empty:
                sector_close = sector_df["Close"].dropna()
                if len(sector_close) >= 6 and len(weekly_nifty_close) >= 6:
                    sector_week_return = (float(sector_close.iloc[-1]) / float(sector_close.iloc[-6]) - 1) * 100
                    sector_month_return = (float(sector_close.iloc[-1]) / float(sector_close.iloc[-22]) - 1) * 100 if len(sector_close) >= 22 else np.nan
                    relative_week = sector_week_return - nifty_week_return
                    relative_month = sector_month_return - nifty_month_return if pd.notna(sector_month_return) and pd.notna(nifty_month_return) else np.nan
                    if pd.notna(relative_week):
                        leading_sector_rows.append({
                            "Sector": sector,
                            "Sector 1W (%)": sector_week_return,
                            "NIFTY 1W (%)": nifty_week_return,
                            "1W outperformance (pp)": relative_week,
                            "1M outperformance (pp)": relative_month,
                        })
if leading_sector_rows:
    leading_df = pd.DataFrame(leading_sector_rows)
    ranked_sectors = leading_df.sort_values("1W outperformance (pp)", ascending=False).copy()
    leaders = ranked_sectors[ranked_sectors["1W outperformance (pp)"] > 0].head(3).copy()
    current_top_names = leaders["Sector"].tolist()
    previous_top_names = st.session_state.get("previous_top_3_sectors")
    if previous_top_names is not None:
        entered = [name for name in current_top_names if name not in previous_top_names]
        exited = [name for name in previous_top_names if name not in current_top_names]
        if entered or exited:
            alert_parts = []
            if entered:
                alert_parts.append("New sector(s) entering top 3: " + ", ".join(entered))
            if exited:
                alert_parts.append("Sector(s) leaving top 3: " + ", ".join(exited))
            st.warning("🔔 Sector leadership changed · " + " · ".join(alert_parts))
    st.session_state["previous_top_3_sectors"] = current_top_names
    if leaders.empty:
        st.info("No sector is outperforming NIFTY 50 over the latest 5 trading sessions.")
    else:
        leader_cols = st.columns(len(leaders))
        for leader_col, (_, leader) in zip(leader_cols, leaders.iterrows()):
            month_confirmation = f'{leader["1M outperformance (pp)"]:+.2f} pp' if pd.notna(leader["1M outperformance (pp)"]) else "N/A"
            leader_col.markdown(
                f'<div style="border:1px solid #166534;border-left:4px solid #22c55e;border-radius:12px;padding:12px 14px;min-height:118px;background:rgba(34,197,94,0.08);">'
                f'<div style="font-size:13px;color:#86efac;margin-bottom:8px;">{leader["Sector"]}</div>'
                f'<div style="font-size:clamp(20px,2.8vw,27px);font-weight:700;color:#22c55e;">+{leader["1W outperformance (pp)"]:.2f} pp</div>'
                f'<div style="font-size:12px;color:#a7f3d0;">1W: sector {leader["Sector 1W (%)"]:+.2f}% · NIFTY {leader["NIFTY 1W (%)"]:+.2f}%</div>'
                f'<div style="font-size:12px;color:#a7f3d0;">1M relative strength: {month_confirmation}</div>'
                f'</div>',
                unsafe_allow_html=True,
            )
else:
    st.info("Weekly sector momentum is temporarily unavailable because usable 3-month daily history was not returned.")

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




if refresh:
    st.cache_data.clear()
