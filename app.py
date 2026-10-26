from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
import json
import os
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
            st.warning("Your FYERS access token has expired. Automatic token refresh is unavailable for this app, so reconnect your FYERS account to continue using live data.")
            st.link_button(
                "Reconnect FYERS account",
                fyers_login_url(fyers_app_id, fyers_redirect_uri, fyers_make_state(fyers_app_secret)),
                use_container_width=True,
            )
            st.caption("This opens FYERS authorization. Sign in and approve the connection; you should not need to enter your FYERS PIN into this dashboard.")
        else:
            st.link_button(
                "Connect FYERS account",
                fyers_login_url(fyers_app_id, fyers_redirect_uri, fyers_make_state(fyers_app_secret)),
                use_container_width=True,
            )
            st.caption("FYERS access tokens expire daily at 6:30 AM. If the token expires or the app restarts, reconnect FYERS when prompted.")

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
    # Show the FYERS live quote control and results directly on the dashboard, not inside a dropdown.
    # Keep Sensex directly below Nifty Bank in the live quote table.
    fyers_symbols = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "BSE:SENSEX-INDEX", "NSE:RELIANCE-EQ", "NSE:HDFCBANK-EQ", "NSE:INFY-EQ"]
    # Automatically fetch the latest FYERS quotes whenever the dashboard runs after connection.
    quote_rows, quote_error = fyers_live_quotes(fyers_app_id, st.session_state["fyers_access_token"], fyers_symbols)
    if quote_error:
        st.warning(f"FYERS quote data unavailable: {quote_error}")
    elif not quote_rows:
        st.info("FYERS returned no quote rows for the selected symbols.")
    else:
        normalized = []
        def _quote_num(value, decimals=2):
            number = pd.to_numeric(value, errors="coerce")
            return round(float(number), decimals) if pd.notna(number) and np.isfinite(number) else np.nan

        for item in quote_rows:
            values = item.get("v") or {}
            normalized.append({
                "Symbol": item.get("n", values.get("symbol", "Unknown")),
                "Last price": _quote_num(values.get("lp")),
                "Change": _quote_num(values.get("ch")),
                "Change %": _quote_num(values.get("chp")),
                "Open": _quote_num(values.get("open_price")),
                "High": _quote_num(values.get("high_price")),
                "Low": _quote_num(values.get("low_price")),
                "Previous close": _quote_num(values.get("prev_close_price")),
            })
        quote_df = pd.DataFrame(normalized)

        # Highlight both absolute and percentage changes by direction.
        def _fyers_change_style(value):
            try:
                number = float(value)
            except (TypeError, ValueError):
                return ""
            if not np.isfinite(number):
                return ""
            if number > 0:
                return "color: #22c55e; font-weight: 600;"
            if number < 0:
                return "color: #ef4444; font-weight: 600;"
            return "color: #9ca3af;"

        quote_format = {
            "Last price": "{:,.2f}",
            "Change": "{:+,.2f}",
            "Change %": "{:+.2f}%",
            "Open": "{:,.2f}",
            "High": "{:,.2f}",
            "Low": "{:,.2f}",
            "Previous close": "{:,.2f}",
        }
        styled_quotes = quote_df.style.format(quote_format, na_rep="—").map(_fyers_change_style, subset=["Change", "Change %"])
        st.dataframe(styled_quotes, use_container_width=True, hide_index=True)
        st.caption("FYERS quotes fetched automatically · " + datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S IST"))


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

# Headline cards: use FYERS quotes for NIFTY 50, BANK NIFTY and India VIX when connected.
# Other cards continue to use the labelled public historical-data source.
headline_names = ["NIFTY 50", "BANK NIFTY", "India VIX", "Brent Crude", "USD/INR"]
fyers_headline_quotes = {}
fyers_headline_error = None
if "fyers_access_token" in st.session_state and fyers_credentials():
    _fyers_app_id = fyers_credentials()[0]
    _live_symbols = ["NSE:NIFTY50-INDEX", "NSE:NIFTYBANK-INDEX", "NSE:INDIAVIX-INDEX"]
    _live_rows, fyers_headline_error = fyers_live_quotes(_fyers_app_id, st.session_state["fyers_access_token"], _live_symbols)
    for _item in (_live_rows or []):
        _values = _item.get("v") or {}
        _symbol = _item.get("n", _values.get("symbol", ""))
        if _symbol in _live_symbols:
            fyers_headline_quotes[_symbol] = _values

    # Prefer FYERS daily candles for NIFTY and India VIX when connected, rather than
    # silently calculating sentiment from stale Yahoo daily history.
    for _market_name, _fyers_symbol in [("NIFTY 50", "NSE:NIFTY50-INDEX"), ("India VIX", "NSE:INDIAVIX-INDEX")]:
        _fyers_daily, _fyers_daily_error = fyers_history(
            _fyers_app_id, st.session_state["fyers_access_token"], _fyers_symbol, "1D", 120
        )
        if not _fyers_daily.empty and {"Date", "High", "Low", "Close"}.issubset(_fyers_daily.columns):
            market_data[_market_name] = _fyers_daily.copy()
            market_errors[_market_name] = None
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
    _fyers_symbol = {"NIFTY 50": "NSE:NIFTY50-INDEX", "BANK NIFTY": "NSE:NIFTYBANK-INDEX", "India VIX": "NSE:INDIAVIX-INDEX"}.get(name)
    _live = fyers_headline_quotes.get(_fyers_symbol) if _fyers_symbol else None
    if _live and _live.get("lp") is not None:
        _live_price = pd.to_numeric(_live.get("lp"), errors="coerce")
        _live_change = pd.to_numeric(_live.get("ch"), errors="coerce")
        _live_pct = pd.to_numeric(_live.get("chp"), errors="coerce")
        _live_delta = f"{fmt(_live_change)} ({fmt(_live_pct)}%)" if pd.notna(_live_change) and pd.notna(_live_pct) else None
        if name == "India VIX":
            col.metric(name, fmt(_live_price), _live_delta, delta_color="inverse")
        else:
            col.metric(name, fmt(_live_price), _live_delta)
    elif snap:
        last, change, pct = snap
        # Invert colour semantics for instruments where a decline is favourable for India.
        if name == "USD/INR":
            # A falling USD/INR rate means the rupee appreciates.
            delta_text = f"{fmt(change)} ({fmt(pct)}%)"
            delta_text += " · INR stronger" if change < 0 else (" · INR weaker" if change > 0 else "")
            col.metric(name, fmt(last["Close"]), delta_text, delta_color="inverse")
        elif name == "Brent Crude":
            # Falling crude prices are generally favourable for India; show declines in green.
            col.metric(name, fmt(last["Close"]), f"{fmt(change)} ({fmt(pct)}%)", delta_color="inverse")
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
        # Weighted model: GIFT Nifty 60%, Brent 10%, USD/INR 5%, FII+DII combined 5%, Infosys ADR 10%, HDFC Bank ADR 10%.
        
        sentiment_parts.append((gift_score, 0.60))
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
_sentiment_change("Brent crude (inverse impact)", "BZ=F", 0.10, inverse=True)
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
for flow_name, flow_weight in [("FII/FPI", 0.025), ("DII", 0.025)]:
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
_sentiment_change("Infosys ADR", "INFY", 0.10, inverse=False)
_sentiment_change("Wipro ADR", "WIT", 0.0, inverse=False)
_sentiment_change("HDFC Bank ADR", "HDB", 0.10, inverse=False)
_sentiment_change("ICICI Bank ADR", "IBN", 0.0, inverse=False)
_sentiment_change("Reliance Industries GDR", "RIGD.L", 0.0, inverse=False)
# Do not publish a confident full-market direction when the highest-weight GIFT Nifty cue is missing.
# Partial score remains visible for transparency, but the headline is explicitly marked incomplete.
gift_available = bool(gift_loaded)  # A stale fallback row is display-only and must not count as live GIFT Nifty
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
        # Weekly EMA 200 is calculated for the instrument selected in the regime selector.
        # Use all available weekly history to reduce EMA initialization differences versus charting platforms.
        # Standard indices use their Yahoo ticker; NIFTY 500 constituents use their .NS ticker.
        weekly_ema200 = np.nan
        weekly_selected_close = np.nan
        weekly_selected_timestamp = "Unavailable"
        weekly_ema_error = None
        selected_yahoo_ticker = INDEXES.get(overview_regime_name, overview_regime_name + ".NS")
        try:
            weekly_selected = yf.download(selected_yahoo_ticker, period="max", interval="1wk", auto_adjust=False, progress=False, threads=False)
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

        # Critical trading metric: never silently present an old weekly close as a current price.
        # EMA remains calculated from weekly candles; the gap uses only a valid FYERS quote.
        weekly_close_source = "FYERS live"
        _regime_fyers_symbols = {
            "NIFTY 50": "NSE:NIFTY50-INDEX",
            "BANK NIFTY": "NSE:NIFTYBANK-INDEX",
            "India VIX": "NSE:INDIAVIX-INDEX",
        }
        _regime_symbol = _regime_fyers_symbols.get(overview_regime_name)
        _regime_quote = fyers_headline_quotes.get(_regime_symbol, {}) if _regime_symbol else {}
        _regime_live_price = pd.to_numeric(_regime_quote.get("lp"), errors="coerce") if _regime_quote else np.nan
        if pd.notna(_regime_live_price) and np.isfinite(_regime_live_price) and _regime_live_price > 0:
            weekly_selected_close = float(_regime_live_price)
        else:
            weekly_selected_close = np.nan
            weekly_close_source = "LIVE PRICE UNAVAILABLE"
            weekly_ema_error = weekly_ema_error or "No valid FYERS live quote for the selected instrument; gap withheld to avoid stale data."
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
        rsi_color = overview_rsi_color
        regime_cols[1].markdown(f'<div style="border:1px solid #343a46;border-left:4px solid {adx_color};border-radius:12px;padding:12px 14px;min-height:92px;"><div style="font-size:13px;color:#aeb6c2;margin-bottom:8px;">ADX 14 · trend strength</div><div style="font-size:clamp(20px,2.8vw,27px);font-weight:700;color:{adx_color};">{fmt(overview_adx_value)}</div><div style="font-size:12px;color:#aeb6c2;">{ "Strong trend" if pd.notna(overview_adx_value) and overview_adx_value >= 25 else "Trend building" if pd.notna(overview_adx_value) and overview_adx_value >= 20 else "Weak trend" }</div></div>', unsafe_allow_html=True)
        regime_cols[2].markdown(f'<div style="border:1px solid #343a46;border-left:4px solid {rsi_color};border-radius:12px;padding:12px 14px;min-height:92px;"><div style="font-size:13px;color:#aeb6c2;margin-bottom:8px;">RSI 14 · momentum</div><div style="font-size:clamp(20px,2.8vw,27px);font-weight:700;color:{rsi_color};">{fmt(overview_rsi_value)}</div><div style="font-size:12px;color:{rsi_color};margin-top:3px;">{overview_rsi_zone}</div></div>', unsafe_allow_html=True)
        weekly_close_text = f"{weekly_selected_close:,.2f}" if pd.notna(weekly_selected_close) else "—"
        weekly_ema_text = f"{weekly_ema200:,.2f}" if pd.notna(weekly_ema200) else "—"
        weekly_gap_text = f"{weekly_ema_gap_pct:+.2f}%" if pd.notna(weekly_ema_gap_pct) else "—"
        weekly_subtext = weekly_ema_status if pd.notna(weekly_ema_gap_pct) else (weekly_ema_error or "Live quote unavailable; gap not calculated.")
        regime_cols[3].markdown(f'<div style="border:1px solid #343a46;border-left:4px solid {weekly_ema_color};border-radius:12px;padding:12px 14px;min-height:92px;"><div style="font-size:13px;color:#aeb6c2;margin-bottom:8px;">{html.escape(overview_regime_name)} · Weekly EMA 200 gap</div><div style="font-size:clamp(20px,2.8vw,27px);font-weight:700;color:{weekly_ema_color};">{weekly_gap_text}</div><div style="font-size:12px;color:#aeb6c2;">{weekly_close_source} {weekly_close_text} · EMA {weekly_ema_text}</div><div style="font-size:12px;color:{weekly_ema_color};margin-top:4px;">{weekly_subtext}</div></div>', unsafe_allow_html=True)
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
            fear_component_details = []
            recent_rc = overview_rc.dropna(subset=["Close"]).copy()
            _fear_data_date = pd.to_datetime(recent_rc["Date"].iloc[-1]).date() if not recent_rc.empty else None
            _fear_today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
            _fear_data_is_fresh = _fear_data_date is not None and (_fear_today - _fear_data_date).days <= 3
            if len(recent_rc) >= 21 and _fear_data_is_fresh:
                # This is a transparent in-house proxy, not an official exchange sentiment index.
                rsi_series = recent_rc["RSI 14"] if "RSI 14" in recent_rc.columns else pd.Series(dtype=float)
                if not rsi_series.dropna().empty:
                    rsi_input = float(np.clip(rsi_series.dropna().iloc[-1], 0, 100))
                    fear_components.append((rsi_input, 0.35))
                    fear_component_details.append({"Input": "RSI 14", "Value": f"{rsi_input:.2f}", "Weight": "35%"})
                trend_parts = []
                for ema_col in ["EMA 20", "EMA 50"]:
                    if ema_col in recent_rc.columns and pd.notna(recent_rc[ema_col].iloc[-1]) and recent_rc[ema_col].iloc[-1] != 0:
                        gap_pct = (float(recent_rc["Close"].iloc[-1]) / float(recent_rc[ema_col].iloc[-1]) - 1) * 100
                        trend_parts.append(float(np.clip(50 + gap_pct * 12, 0, 100)))
                if trend_parts:
                    trend_input = float(np.mean(trend_parts))
                    fear_components.append((trend_input, 0.25))
                    fear_component_details.append({"Input": "Price vs EMA 20/50", "Value": f"{trend_input:.2f}", "Weight": "25%"})
                if len(recent_rc) >= 21 and recent_rc["Close"].iloc[-21] != 0:
                    ret20 = (float(recent_rc["Close"].iloc[-1]) / float(recent_rc["Close"].iloc[-21]) - 1) * 100
                    return_input = float(np.clip(50 + ret20 * 8, 0, 100))
                    fear_components.append((return_input, 0.15))
                    fear_component_details.append({"Input": "20-session return", "Value": f"{return_input:.2f}", "Weight": "15%"})
                vix_frame = market_data.get("India VIX", pd.DataFrame())
                vix_snap = quote_snapshot(vix_frame) if not vix_frame.empty else None
                if vix_snap:
                    vix_value = float(vix_snap[0]["Close"])
                    vix_input = float(np.clip(100 - vix_value * 3, 0, 100))
                    fear_components.append((vix_input, 0.25))
                    fear_component_details.append({"Input": f"India VIX ({vix_value:.2f})", "Value": f"{vix_input:.2f}", "Weight": "25%"})
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
                fear_col.caption("Dashboard-derived proxy · not an official index")
                fear_col.caption("Underlying daily candle: " + pd.to_datetime(recent_rc["Date"].iloc[-1]).strftime("%d %b %Y"))
                # Keep the dashboard compact: show the sentiment label without the input table.

            else:
                if len(recent_rc) >= 21 and not _fear_data_is_fresh:
                    fear_col.warning(f"Fear & Greed unavailable: latest daily candle is {_fear_data_date.strftime('%d %b %Y')}. Data is stale; no current sentiment is shown.")
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


# Automatic small-cap and mid-cap turnaround scanner.
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
        # Supplement turnaround signals with available annual fundamentals; never fill missing fields with estimates.
        info = {}
        debt_equity = pe_ratio = roe_pct = operating_cash_flow = np.nan
        try:
            stock_obj = yf.Ticker(ticker)
            info = stock_obj.get_info() or {}
            debt_raw = pd.to_numeric(info.get("debtToEquity"), errors="coerce")
            debt_equity = float(debt_raw) if pd.notna(debt_raw) else np.nan
            pe_raw = pd.to_numeric(info.get("trailingPE"), errors="coerce")
            pe_ratio = float(pe_raw) if pd.notna(pe_raw) else np.nan
            roe_raw = pd.to_numeric(info.get("returnOnEquity"), errors="coerce")
            roe_pct = float(roe_raw) * 100 if pd.notna(roe_raw) else np.nan
            cf = stock_obj.cashflow
            if isinstance(cf, pd.DataFrame) and not cf.empty:
                for cf_name in ["Operating Cash Flow", "OperatingCashFlow", "Total Cash From Operating Activities"]:
                    if cf_name in cf.index:
                        cf_raw = pd.to_numeric(cf.loc[cf_name].iloc[0], errors="coerce")
                        operating_cash_flow = float(cf_raw) if pd.notna(cf_raw) else np.nan
                        break
        except Exception:
            pass
        return {
            "Company": company, "Symbol": ticker.replace(".NS", ""), "Segment": segment,
            "Latest quarter": latest_date, "Sales growth YoY %": round(rev_yoy, 1) if np.isfinite(rev_yoy) else np.nan,
            "Profit growth YoY %": round(profit_yoy, 1) if np.isfinite(profit_yoy) else ("Loss → profit" if loss_to_profit else np.nan),
            "Operating margin %": round(margin_now, 1) if np.isfinite(margin_now) else np.nan,
            "Margin improved QoQ": bool(margin_improving) if np.isfinite(margin_now) and np.isfinite(margin_prev) else "Insufficient data",
            "ROE %": round(roe_pct, 1) if np.isfinite(roe_pct) else np.nan,
            "Debt/equity": round(debt_equity, 1) if np.isfinite(debt_equity) else np.nan,
            "Trailing P/E": round(pe_ratio, 1) if np.isfinite(pe_ratio) else np.nan,
            "Operating cash flow (annual, ₹)": operating_cash_flow,
            "Turnaround signals": int(signals),
            "Reason flagged": ", ".join([label for label, ok in [
                ("Sales growth ≥10% YoY", improving_sales),
                ("Profit growth ≥15% YoY", improving_profit),
                ("Loss-to-profit recovery", loss_to_profit),
                ("Operating margin improved QoQ", margin_improving),
            ] if ok]),
            "Source": "Yahoo Finance quarterly income statement; annual fundamentals where available",
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
    with ThreadPoolExecutor(max_workers=8) as pool:
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
        result["_sort"] = pd.to_numeric(result["Turnaround signals"], errors="coerce")
        result = result.sort_values(["_sort", "Sales growth YoY %"], ascending=[False, False], na_position="last").drop(columns=["_sort"])
    return result, universe_error

st.markdown("""<div style="font-size:clamp(28px,4vw,42px);font-weight:850;letter-spacing:-1.2px;line-height:1.15;margin:8px 0 6px;background:linear-gradient(90deg,#19e6a0,#29c7d6,#4aa8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Turnaround Board</div>""", unsafe_allow_html=True)
st.caption("Small-cap + mid-cap candidates · improving sales, profit recovery and operating margins. Public data may be delayed or incomplete.")
with st.expander("Scan small-cap / mid-cap turnaround stocks", expanded=False):
    st.write("Scans current Nifty Midcap 150 and Nifty Smallcap 250 constituents. Newly qualifying stocks can appear when the source data updates. Research watchlist only, not a buy signal.")
    segment_choice = st.multiselect("Segments to scan", ["Mid-cap", "Small-cap"], default=["Mid-cap", "Small-cap"], key="turnaround_segments")
    st.caption("Fundamentals: Yahoo Finance quarterly income statements · Constituents: Nifty Indices CSV lists · Cached for up to 6 hours. Initial scans may take a few minutes.")
    if st.button("Scan / refresh turnaround stocks", type="primary", key="run_auto_turnaround_scan", disabled=not segment_choice):
        previous_df = st.session_state.get("auto_turnaround_scan_results", pd.DataFrame())
        previous_segments = tuple(sorted(st.session_state.get("auto_turnaround_scan_segment_keys", ())))
        current_segments = tuple(sorted(segment_choice))
        history_path = "turnaround_scan_baseline.json"
        saved_symbols = set()
        saved_segments = ()
        try:
            with open(history_path, "r", encoding="utf-8") as history_file:
                saved_history = json.load(history_file)
            saved_segments = tuple(sorted(saved_history.get("segments", [])))
            saved_symbols = {str(symbol).upper() for symbol in saved_history.get("symbols", [])}
        except (OSError, ValueError, TypeError):
            pass
        with st.spinner("Scanning index constituents for improving fundamentals. This can take a few minutes…"):
            scan_df, scan_warning = run_turnaround_scan(current_segments)
        if isinstance(previous_df, pd.DataFrame) and not previous_df.empty and previous_segments == current_segments and "Symbol" in previous_df.columns:
            previous_symbols = set(previous_df["Symbol"].astype(str).str.upper())
            has_baseline = True
        elif saved_symbols and saved_segments == current_segments:
            previous_symbols = saved_symbols
            has_baseline = True
        else:
            previous_symbols = set()
            has_baseline = False
        if isinstance(scan_df, pd.DataFrame) and not scan_df.empty:
            scan_df = scan_df.copy()
            scan_df["Status"] = scan_df["Symbol"].astype(str).str.upper().map(
                lambda symbol: "🆕 NEW" if has_baseline and symbol not in previous_symbols else ("Previously detected" if has_baseline else "Baseline scan")
            )
            try:
                with open(history_path, "w", encoding="utf-8") as history_file:
                    json.dump({"segments": list(current_segments), "symbols": sorted(set(scan_df["Symbol"].astype(str).str.upper())), "saved_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()}, history_file)
            except OSError:
                pass
        st.session_state["auto_turnaround_scan_results"] = scan_df
        st.session_state["auto_turnaround_scan_time"] = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S IST")
        st.session_state["auto_turnaround_scan_segments"] = ", ".join(segment_choice)
        st.session_state["auto_turnaround_scan_segment_keys"] = current_segments
        st.session_state["auto_turnaround_scan_warning"] = scan_warning
    scan_df = st.session_state.get("auto_turnaround_scan_results", pd.DataFrame())
    if st.session_state.get("auto_turnaround_scan_time"):
        st.caption("Last scan: " + st.session_state["auto_turnaround_scan_time"] + " · Segments: " + st.session_state.get("auto_turnaround_scan_segments", ""))
    if st.session_state.get("auto_turnaround_scan_warning"):
        st.warning(st.session_state["auto_turnaround_scan_warning"])
    if isinstance(scan_df, pd.DataFrame) and not scan_df.empty:
        scan_df = scan_df.copy()
        # Rank new discoveries first, then rank the rest by a transparent fundamentals-quality score.
        status_series = scan_df.get("Status", pd.Series("Previously detected", index=scan_df.index)).astype(str)
        scan_df["_new_priority"] = status_series.eq("🆕 NEW").astype(int)
        score = pd.to_numeric(scan_df.get("Turnaround signals", pd.Series(0, index=scan_df.index)), errors="coerce").fillna(0) * 3
        sales = pd.to_numeric(scan_df.get("Sales growth YoY %", pd.Series(np.nan, index=scan_df.index)), errors="coerce")
        profit = pd.to_numeric(scan_df.get("Profit growth YoY %", pd.Series(np.nan, index=scan_df.index)), errors="coerce")
        roe = pd.to_numeric(scan_df.get("ROE %", pd.Series(np.nan, index=scan_df.index)), errors="coerce")
        debt = pd.to_numeric(scan_df.get("Debt/equity", pd.Series(np.nan, index=scan_df.index)), errors="coerce")
        pe = pd.to_numeric(scan_df.get("Trailing P/E", pd.Series(np.nan, index=scan_df.index)), errors="coerce")
        cash = pd.to_numeric(scan_df.get("Operating cash flow (annual, ₹)", pd.Series(np.nan, index=scan_df.index)), errors="coerce")
        margin_better = scan_df.get("Margin improved QoQ", pd.Series(False, index=scan_df.index)).map(lambda x: str(x).lower() == "true")
        score += sales.ge(10).fillna(False).astype(int) * 2
        score += sales.ge(25).fillna(False).astype(int)
        score += profit.ge(15).fillna(False).astype(int) * 2
        score += profit.ge(30).fillna(False).astype(int)
        score += margin_better.astype(int) * 2
        score += roe.ge(12).fillna(False).astype(int)
        score += debt.le(100).fillna(False).astype(int)
        score += pe.gt(0).fillna(False).astype(int) * pe.le(30).fillna(False).astype(int)
        score += cash.gt(0).fillna(False).astype(int)
        scan_df["Research rank score"] = score.astype(int)
        scan_df = scan_df.sort_values(
            ["_new_priority", "Research rank score", "Sales growth YoY %", "Profit growth YoY %"],
            ascending=[False, False, False, False],
            na_position="last",
            kind="stable",
        ).drop(columns=["_new_priority"]).reset_index(drop=True)
        scan_df.insert(0, "Rank", range(1, len(scan_df) + 1))
        new_count = int((scan_df.get("Status", pd.Series(dtype=str)) == "🆕 NEW").sum()) if "Status" in scan_df.columns else 0
        if new_count:
            st.success(f"🆕 {new_count} NEW turnaround stock(s) since your previous comparable scan.")
        elif "Status" in scan_df.columns and (scan_df["Status"] == "Baseline scan").all():
            st.info("Baseline saved. On your next scan with the same segments selected, newly qualifying stocks will be marked NEW.")
        else:
            st.success(f"{len(scan_df)} potential turnaround candidate(s) matched the current rules; no new stocks since the previous comparable scan.")
        if "Status" in scan_df.columns:
            def _turnaround_new_row_style(row):
                if str(row.get("Status", "")) == "🆕 NEW":
                    return ["background-color: #00c853; color: #07130b; font-weight: 800; border-top: 2px solid #b9f6ca; border-bottom: 2px solid #b9f6ca"] * len(row)
                return [""] * len(row)
            styled_scan = scan_df.style.apply(_turnaround_new_row_style, axis=1)
            st.dataframe(styled_scan, use_container_width=True, hide_index=True, height=520)
        else:
            st.dataframe(scan_df, use_container_width=True, hide_index=True, height=520)
        st.download_button("Download turnaround watchlist CSV", scan_df.to_csv(index=False).encode("utf-8"), file_name="small_midcap_turnaround_watchlist.csv", mime="text/csv", key="download_auto_turnaround_csv")
        st.caption("Ranking: newly detected 🆕 NEW stocks appear first; remaining stocks are ordered by a transparent research score using turnaround signals, sales/profit growth, margin improvement and available ROE, debt/equity, P/E and operating cash flow. Missing metrics are not treated as passing. This is a research ranking, not a buy recommendation.")
    elif "auto_turnaround_scan_time" in st.session_state:
        st.info("No candidates met the current rules, or the data provider returned insufficient quarterly data. This is not proof that no turnaround stocks exist.")
    else:
        st.info("Select segments and press Scan / refresh to generate the first watchlist. Re-run after market-data refresh to catch new qualifying stocks.")



st.markdown("### NIFTY 50 stock heatmap")
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
    raw = yf.download(list(symbols), period="5d", interval="1d", group_by="ticker", auto_adjust=False, progress=False, threads=True)
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
with st.spinner("Loading NIFTY 50 constituent performance…"):
    heatmap_df = _nifty50_heatmap_data(tuple(NIFTY50_HEATMAP_SYMBOLS))
if heatmap_df.empty:
    st.warning("NIFTY 50 heatmap data is temporarily unavailable from the public data source.")
else:
    # Use an explicit formatted custom-data field for tile labels. Plotly treemap's
    # %{color} token is not reliably populated in texttemplate and can render NaN%.
    heatmap_df["Direction"] = np.where(heatmap_df["Change (%)"] >= 0, "Positive", "Negative")
    heatmap_df["Display Change"] = heatmap_df["Change (%)"].map(
        lambda value: f"{value:+.2f}%" if pd.notna(value) and np.isfinite(value) else "N/A"
    )
    heatmap_fig = px.treemap(
        heatmap_df, path=["Direction", "Symbol"], values=np.ones(len(heatmap_df)),
        color="Change (%)", custom_data=["Display Change"],
        color_continuous_scale=["#ef4444", "#252a34", "#16a34a"],
        color_continuous_midpoint=0,
        hover_data={"Close": ":,.2f", "Change (%)": ":.2f", "Direction": False, "Display Change": False}
    )
    heatmap_fig.update_traces(
        texttemplate="%{label}<br>%{customdata[0]}",
        textfont_size=12,
        hovertemplate="%{label}<br>Daily change: %{customdata[0]}<extra></extra>"
    )
    heatmap_fig.update_layout(template="plotly_dark", height=520, margin=dict(l=4, r=4, t=8, b=4), coloraxis_colorbar_title="Daily %")
    st.plotly_chart(heatmap_fig, use_container_width=True, config={"displaylogo": False}, key="nifty50_only_treemap")
st.divider()

def make_expander_grid(labels, columns_per_row=1):
    """Render full-width expanders so opened sections never get trapped in narrow columns."""
    containers = []
    # Keep one full-width expander per row. Multi-column expanders squeeze wide charts,
    # tables and forms on phones and force horizontal scrolling.
    for label in labels:
        containers.append(st.expander(label, expanded=False))
    return containers

tabs = make_expander_grid(["Overview · market snapshot", "Live charts", "Technical analysis", "Sector rotation", "Global markets", "FII/DII & news", "Derivatives", "History & sources", "NIFTY historical returns"])
with tabs[0]:
        st.subheader("Market snapshot and historical source data")
        rows = []
        for name, df in market_data.items():
            snap = quote_snapshot(df)
            if snap:
                last, change, pct = snap
                rows.append({"Instrument": name, "Ticker": INDEXES[name], "Latest returned close": last["Close"], "Change vs previous bar": change, "Change %": pct, "Source bar timestamp (IST)": last["Date"].strftime("%Y-%m-%d %H:%M %Z"), "Source": "Yahoo Finance / yfinance"})
            else:
                rows.append({"Instrument": name, "Ticker": INDEXES[name], "Latest returned close": np.nan, "Change vs previous bar": np.nan, "Change %": np.nan, "Source bar timestamp (IST)": "Unavailable", "Source": market_errors[name] or "No data"})
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        snapshot_df = pd.DataFrame(rows)
        if not snapshot_df.empty and "Change %" in snapshot_df:
            snapshot_plot = snapshot_df.dropna(subset=["Change %"]).copy()
            if not snapshot_plot.empty:
                snapshot_plot["Direction"] = np.where(snapshot_plot["Change %"] >= 0, "Positive", "Negative")
                snap_fig = px.bar(snapshot_plot.sort_values("Change %"), x="Change %", y="Instrument", color="Direction", color_discrete_map={"Positive":"#22c55e","Negative":"#ef4444"}, orientation="h", template="plotly_dark", title="Market snapshot · change vs previous returned bar")
                snap_fig.update_layout(height=max(260, 36 * len(snapshot_plot)), margin=dict(l=10,r=10,t=45,b=10), showlegend=False, xaxis_title="Change (%)", yaxis_title="")
                st.plotly_chart(snap_fig, use_container_width=True, config={"displaylogo": False}, key="overview_snapshot_change_chart")

with tabs[1]:
        st.subheader("Market charts")
        st.caption("Clean, full-width charts built from the data already loaded by this dashboard. Source is Yahoo Finance via yfinance; daily bars can be delayed or stale. For FYERS-sourced charts, use the FYERS stock charts section below.")
        available_chart_names = [name for name in ["NIFTY 50", "BANK NIFTY", "India VIX", "Brent Crude", "USD/INR", "INFY", "WIPRO", "RELIANCE", "HDFCBANK"] if name in market_data and not market_data[name].empty]
        if not available_chart_names:
            st.warning("No chart history was returned by the source. Refresh market data or try again later.")
        else:
            chart_choice = st.selectbox("Choose instrument", available_chart_names, key="market_chart_choice")
            chart_frame = market_data[chart_choice].copy().sort_values("Date")
            chart_frame = chart_frame.dropna(subset=["Date", "Open", "High", "Low", "Close"])
            chart_window = st.selectbox("Chart history", ["1 month", "3 months", "6 months", "1 year", "All loaded"], index=2, key="market_chart_window")
            window_days = {"1 month": 31, "3 months": 92, "6 months": 183, "1 year": 365}
            if chart_window != "All loaded" and not chart_frame.empty:
                cutoff = chart_frame["Date"].max() - pd.Timedelta(days=window_days[chart_window])
                chart_frame = chart_frame[chart_frame["Date"] >= cutoff]
            if chart_frame.empty:
                st.info("No valid OHLC candles are available for this instrument.")
            else:
                chart_fig = go.Figure()
                chart_fig.add_trace(go.Candlestick(
                    x=chart_frame["Date"], open=chart_frame["Open"], high=chart_frame["High"],
                    low=chart_frame["Low"], close=chart_frame["Close"], name=chart_choice,
                    increasing_line_color="#22c55e", decreasing_line_color="#ef4444",
                    increasing_fillcolor="#22c55e", decreasing_fillcolor="#ef4444",
                ))
                if "Volume" in chart_frame and chart_frame["Volume"].notna().any():
                    chart_fig.add_trace(go.Bar(x=chart_frame["Date"], y=chart_frame["Volume"], name="Volume", yaxis="y2", marker_color="#64748b", opacity=0.28))
                chart_fig.update_layout(
                    template="plotly_dark", height=620, autosize=True,
                    title=f"{chart_choice} · OHLC candles",
                    margin=dict(l=12, r=18, t=55, b=18),
                    xaxis=dict(title="", rangeslider=dict(visible=False), showgrid=False),
                    yaxis=dict(title="Price", domain=[0.22, 1], showgrid=True, gridcolor="rgba(148,163,184,.12)"),
                    yaxis2=dict(title="Volume", overlaying="y", side="right", showgrid=False, rangemode="tozero", domain=[0, .18]),
                    legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1),
                    hovermode="x unified",
                )
                st.plotly_chart(chart_fig, use_container_width=True, config={"displaylogo": False, "scrollZoom": True}, key="market_candlestick_chart")
                latest_snap = quote_snapshot(chart_frame)
                if latest_snap:
                    latest_bar, latest_change, latest_pct = latest_snap
                    st.caption(f"Latest returned bar: {latest_bar['Date']:%d %b %Y, %H:%M IST} · Change vs previous bar: {latest_change:,.2f} ({latest_pct:,.2f}%) · Not guaranteed live.")

with tabs[2]:
        st.subheader("Technical analysis from returned OHLC data")
        technical_symbols = {}
        for instrument_name, ticker in INDEXES.items():
            technical_symbols[instrument_name] = ("yahoo", instrument_name, ticker)
        fyers_symbols_for_technicals = []
        if st.session_state.get("fyers_access_token") and fyers_credentials():
            fyers_symbols_for_technicals, fyers_symbols_error = fyers_nse_equity_symbols()
            for symbol, company_name in fyers_symbols_for_technicals:
                label = f"{company_name or symbol} · {symbol}"
                technical_symbols[label] = ("fyers", symbol, symbol)
        technical_choice = st.selectbox(
            "Instrument for technicals",
            list(technical_symbols.keys()),
            index=0,
            key="tech_symbol",
            help="Connect FYERS in the sidebar to unlock the full NSE cash-equity symbol list.",
        )
        source_kind, source_key, source_ticker = technical_symbols[technical_choice]
        source_df = pd.DataFrame()
        source_error = None
        if source_kind == "yahoo":
            source_df = market_data[source_key]
            source_error = market_errors.get(source_key)
        elif not st.session_state.get("fyers_access_token"):
            st.info("Connect your FYERS account in the sidebar to load technicals for NSE stocks.")
        else:
            @st.cache_data(ttl=300, show_spinner=False)
            def _cached_fyers_technicals(app_id: str, access_token: str, symbol: str):
                return fyers_history(app_id, access_token, symbol, "1D", 365)
            fyers_config_for_technicals = fyers_credentials()
            with st.spinner(f"Loading daily FYERS candles for {source_ticker}..."):
                source_df, source_error = _cached_fyers_technicals(
                    fyers_config_for_technicals[0], st.session_state["fyers_access_token"], source_ticker
                )
        if source_error:
            st.warning(f"Technical data unavailable: {source_error}")
        elif source_df.empty:
            st.info("No technical data returned for this selection.")
        else:
            source_df = source_df.copy()
            if "Date" not in source_df.columns and "Datetime" in source_df.columns:
                source_df = source_df.rename(columns={"Datetime": "Date"})
            source_df["Date"] = pd.to_datetime(source_df["Date"], errors="coerce")
            source_df = source_df.dropna(subset=["Date", "Close"]).sort_values("Date")
            if len(source_df) < 30:
                st.warning("Fewer than 30 source candles were returned; longer-period indicators may be unavailable.")
            calc = indicators(source_df)
            last = calc.iloc[-1]
            metric_cols = st.columns(5)
            for col, label in zip(metric_cols, ["Close", "RSI 14", "SMA 20", "SMA 50", "ADR 20"]):
                val = last.get(label, np.nan)
                col.metric(label, fmt(val))
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=calc["Date"], y=calc["Close"], name="Close", line={"width": 2}))
            for ma in ["SMA 20", "SMA 50", "EMA 20", "EMA 50"]:
                if ma in calc and calc[ma].notna().any():
                    fig.add_trace(go.Scatter(x=calc["Date"], y=calc[ma], name=ma, line={"width": 1.3}))
            fig.update_layout(template="plotly_dark", height=470, margin={"l": 10, "r": 10, "t": 25, "b": 10}, xaxis_title="Source candle time", yaxis_title="Price")
            st.plotly_chart(fig, use_container_width=True)
            rsi_fig = go.Figure()
            rsi_fig.add_hrect(y0=0, y1=30, fillcolor="rgba(239,68,68,.16)", line_width=0)
            rsi_fig.add_hrect(y0=70, y1=100, fillcolor="rgba(34,197,94,.16)", line_width=0)
            rsi_fig.add_hrect(y0=30, y1=70, fillcolor="rgba(245,158,11,.06)", line_width=0)
            rsi_fig.add_trace(go.Scatter(x=calc["Date"], y=calc["RSI 14"], name="RSI 14", line={"color":"#60a5fa","width":2.5}))
            rsi_fig.add_hline(y=70, line_dash="dash", line_color="#22c55e", annotation_text="Overbought > 70")
            rsi_fig.add_hline(y=30, line_dash="dash", line_color="#ef4444", annotation_text="Oversold < 30")
            rsi_fig.update_layout(template="plotly_dark", height=280, yaxis={"range": [0,100]}, margin={"l":10,"r":10,"t":25,"b":10}, title="RSI 14 · green > 70, red < 30")
            st.plotly_chart(rsi_fig, use_container_width=True)
            pivot = None
            if len(calc) >= 2 and {"High", "Low", "Close"}.issubset(calc.columns):
                prev = calc.iloc[-2]
                h, l, c = prev["High"], prev["Low"], prev["Close"]
                if pd.notna(h) and pd.notna(l) and pd.notna(c):
                    p = (h+l+c)/3
                    pivot = pd.DataFrame([("Previous candle high",h),("R2",p+h-l),("R1",2*p-l),("Pivot",p),("S1",2*p-h),("S2",p-h+l),("Previous candle low",l)],columns=["Level","Value"])
            st.markdown("#### Classic pivot levels (calculated from previous returned candle)")
            if pivot is not None:
                st.dataframe(pivot.style.format({"Value":"{:,.2f}"}), use_container_width=True, hide_index=True)
            else:
                st.info("Pivot levels unavailable: source data lacks sufficient OHLC rows.")
            source_label = "FYERS historical data" if source_kind == "fyers" else "Yahoo Finance / yfinance"
            st.caption(f"Source: {source_label} · {len(calc):,} returned candles. Last source candle: {last['Date']:%d %b %Y, %H:%M IST}. Indicators depend on source coverage and are not guaranteed live signals.")

with tabs[3]:
        st.subheader("Sector rotation — best-effort public index history")
        st.caption("Only sectors that return usable source rows are plotted. Relative strength is computed against NIFTY 50 over the selected lookback; it is not institutional money-flow data.")
        sector_rows = []
        with st.spinner("Checking available sector index symbols…"):
            for sector, ticker in SECTOR_TICKERS.items():
                sdf, err = get_history(ticker, period, "1d")
                if not sdf.empty and len(sdf) >= 2:
                    close = sdf["Close"].dropna()
                    perf = (close.iloc[-1] / close.iloc[0] - 1) * 100 if close.iloc[0] else np.nan
                    base_df = market_data["NIFTY 50"]["Close"].dropna()
                    base_perf = (base_df.iloc[-1] / base_df.iloc[0] - 1) * 100 if len(base_df) >= 2 and base_df.iloc[0] else np.nan
                    sector_rows.append({"Sector": sector, "Ticker": ticker, "Period return %": perf, "NIFTY return %": base_perf, "Relative return vs NIFTY (pp)": perf-base_perf if pd.notna(perf) and pd.notna(base_perf) else np.nan, "Last source bar": sdf.iloc[-1]["Date"].strftime("%Y-%m-%d %H:%M IST")})
        if sector_rows:
            sec = pd.DataFrame(sector_rows).sort_values("Relative return vs NIFTY (pp)", ascending=False)
            st.dataframe(sec, use_container_width=True, hide_index=True)
            sec["Direction"] = np.where(sec["Relative return vs NIFTY (pp)"] >= 0, "Outperforming", "Underperforming")
            fig = px.bar(sec.sort_values("Relative return vs NIFTY (pp)"), x="Relative return vs NIFTY (pp)", y="Sector", color="Direction", color_discrete_map={"Outperforming":"#22c55e","Underperforming":"#ef4444"}, orientation="h", template="plotly_dark", title="Sector relative performance vs NIFTY")
            fig.update_layout(height=440)
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.warning("No sector index series were returned by the public source. No values have been substituted.")

with tabs[4]:
        st.subheader("Global markets and commodities")
        tv_widget("market-overview", {"colorTheme":"dark","dateRange":"1D","showChart":True,"locale":"en","width":"100%","height":440,"isTransparent":False,"showSymbolLogo":True,"tabs":[{"title":"Global indices & commodities","symbols":[{"s":"TVC:SPX"},{"s":"NASDAQ:NDX"},{"s":"TVC:UKOIL"},{"s":"TVC:USOIL"},{"s":"TVC:DXY"},{"s":"FX:USDINR"},{"s":"TVC:GOLD"}]}]}, 450)
        st.markdown("**Public sources**")
        st.markdown("- [Reuters World](https://www.reuters.com/world/) · [Associated Press World](https://apnews.com/world-news) · [BBC World](https://www.bbc.com/news/world)")
        st.markdown("- [U.S. Energy Information Administration](https://www.eia.gov/) · [FRED economic data](https://fred.stlouisfed.org/)")

with tabs[5]:
        st.subheader("FII/DII and macro data")
        st.info("This free edition does not scrape or invent FII/DII values. Use the official reports below; values are not shown as current unless a source has actually been connected and timestamped.")
        st.markdown("- [NSE FII/DII reports](https://www.nseindia.com/reports/fii-dii) — official published data")
        st.markdown("- [NSE all reports](https://www.nseindia.com/all-reports)")
        st.markdown("- [RBI statistics and publications](https://www.rbi.org.in/)")
        st.markdown("- [SEBI](https://www.sebi.gov.in/)")
        tv_widget("news", {"feedMode":"market","colorTheme":"dark","isTransparent":False,"displayMode":"regular","width":"100%","height":420,"locale":"en"}, 430)

with tabs[6]:
        st.subheader("Derivatives and options")
        st.warning("A verified, timestamped NSE option chain, put-call ratio, max pain, option Greeks, and open-interest change are not available from this free public-data setup. These fields are intentionally not populated with guesses.")
        st.markdown("- [NSE option chain](https://www.nseindia.com/option-chain)")
        st.markdown("- [NSE derivatives reports](https://www.nseindia.com/all-reports-derivatives)")
        st.markdown("If you download an option-chain CSV from a source you are permitted to use, the next version can calculate PCR, OI changes, strike concentration and max pain from that actual file.")

with tabs[7]:
        st.subheader("History & provenance")
        st.markdown("**Data behaviour**")
        st.write("- Market history: Yahoo Finance via the open-source `yfinance` package; this is not an official Yahoo API product and Yahoo describes its data access as intended for personal use.")
        st.write("- Interactive charts, market overview, heatmap and news: embedded TradingView widgets. Coverage and delay are set by TradingView and the underlying exchange/data provider.")
        st.write("- Technical indicators: calculated locally from returned daily OHLC bars. No sample data is generated.")
        st.write("- FII/DII and options: official links only until a permitted, dependable data source is connected.")
        st.markdown("**Documentation**")
        st.markdown("- [TradingView widgets](https://www.tradingview.com/widget/) · [TradingView data coverage and widget FAQ](https://www.tradingview.com/widget/)")
        st.markdown("- [yfinance documentation and disclaimer](https://ranaroussi.github.io/yfinance/)")
        st.markdown("- [Yahoo Finance coverage and delays](https://help.yahoo.com/kb/SLN2310.html)")
        st.markdown("- [Nifty Indices historical data](https://www.niftyindices.com/reports/historical-data)")
        st.markdown("- [NSE reports](https://www.nseindia.com/all-reports)")
        st.markdown("- [NSE FII/DII reports](https://www.nseindia.com/reports/fii-dii)")
        st.markdown("**Download fetched daily data**")
        dl_symbol = st.selectbox("Instrument", list(INDEXES.keys()), key="dl_symbol")
        dldf = market_data[dl_symbol]
        if not dldf.empty:
            st.download_button("Download CSV", dldf.to_csv(index=False).encode("utf-8"), file_name=f"{dl_symbol.lower().replace(' ','_')}_source_data.csv", mime="text/csv")
            st.dataframe(dldf.tail(20), use_container_width=True, hide_index=True)
        else:
            st.info("No data to export for this instrument.")

with tabs[8]:
        st.subheader("NIFTY 50 historical returns")
        st.caption("Calendar-year returns and monthly return heatmap, calculated from returned daily NIFTY 50 closing prices. Historical performance is not a prediction.")
        @st.cache_data(ttl=3600, show_spinner=False)
        def _nifty_long_history(ticker: str):
            return get_history(ticker, "10y", "1d")
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
    
with st.expander("Chartink scanners & alert inbox", expanded=False):
    st.subheader("Chartink scanner workspace")
    st.write("Use Chartink’s free stock screeners here, then bring a scan snapshot into this dashboard for review. Your current free account does not include Chartink Alerts or webhook delivery, so this inbox cannot receive automatic alert pushes until the Chartink plan supports alerts.")
    st.markdown("[Open Chartink Stock Screener](https://chartink.com/screener/) · [Explore popular scans](https://chartink.com/screener/) · [Chartink alert feature details](https://chartink.com/articles/alerts/alerts-guide/)")
    chartink_scan_url = st.text_input("Optional: paste the URL of your saved/public Chartink scan", placeholder="https://chartink.com/screener/your-scan-name", key="chartink_scan_url")
    if chartink_scan_url.strip():
        if chartink_scan_url.strip().startswith("https://chartink.com/"):
            st.link_button("Open my Chartink scan", chartink_scan_url.strip(), use_container_width=False)
        else:
            st.warning("For safety, use a URL beginning with https://chartink.com/.")
    st.markdown("#### Import a scan snapshot")
    st.caption("Paste the JSON body of a Chartink webhook payload if you already have one, or paste a simple JSON object with stocks and trigger_prices. This is a manual import only; it does not connect to Chartink or poll for alerts.")
    chartink_payload = st.text_area(
        "Chartink payload JSON",
        placeholder='{"stocks":"RELIANCE,TCS,INFY","trigger_prices":"1450,3800,1600","triggered_at":"10:30 am","scan_name":"Example scan","scan_url":"example-scan"}',
        height=130,
        key="chartink_payload_json",
    )
    if st.button("Parse scan snapshot", key="chartink_parse_payload"):
        try:
            payload = json.loads(chartink_payload)
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object.")
            stocks_raw = payload.get("stocks", payload.get("stock", ""))
            prices_raw = payload.get("trigger_prices", payload.get("trigger_price", ""))
            stocks = [str(x).strip() for x in stocks_raw] if isinstance(stocks_raw, list) else [x.strip() for x in str(stocks_raw).split(",") if x.strip()]
            prices = [str(x).strip() for x in prices_raw] if isinstance(prices_raw, list) else [x.strip() for x in str(prices_raw).split(",") if x.strip()]
            rows = []
            for i, stock in enumerate(stocks):
                price = prices[i] if i < len(prices) else ""
                try:
                    price = float(price.replace(",", "")) if price else np.nan
                except (TypeError, ValueError):
                    price = np.nan
                rows.append({"Stock": stock, "Trigger price": price})
            if not rows:
                st.session_state["chartink_import_rows"] = []
                st.warning("No stocks were found. Check that the payload contains a 'stocks' field.")
            else:
                st.session_state["chartink_import_rows"] = rows
                st.session_state["chartink_import_meta"] = {
                    "scan_name": str(payload.get("scan_name", payload.get("alert_name", "Imported scan"))),
                    "triggered_at": str(payload.get("triggered_at", "Not provided")),
                    "scan_url": str(payload.get("scan_url", "")),
                }
        except Exception as exc:
            st.error(f"Could not parse JSON: {exc}")
    imported_rows = st.session_state.get("chartink_import_rows", [])
    if imported_rows:
        meta = st.session_state.get("chartink_import_meta", {})
        st.markdown(f"**Scan:** {meta.get('scan_name', 'Imported scan')}  ·  **Triggered at:** {meta.get('triggered_at', 'Not provided')}")
        if meta.get("scan_url"):
            st.caption("Scan slug: " + meta["scan_url"])
        st.dataframe(pd.DataFrame(imported_rows), use_container_width=True, hide_index=True)
        csv_data = pd.DataFrame(imported_rows).to_csv(index=False).encode("utf-8")
        st.download_button("Download imported scan as CSV", data=csv_data, file_name="chartink_scan_snapshot.csv", mime="text/csv", key="chartink_download_csv")
    st.info("To enable automatic delivery later, Chartink requires a paid Alerts subscription with webhook support, plus a separate public HTTPS endpoint and persistent storage. This Streamlit page alone is not a public webhook receiver. No broker order placement is included.")

st.divider()
st.caption("For research and monitoring only. Verify exchange timestamps and official sources before trading. Public widgets and public-source data may be delayed, incomplete or unavailable; this dashboard is not an execution system or investment advice.")

# Expanded research modules (13–22). Calculations use public-source data; personal journal data remains user-controlled.
extra_tabs = make_expander_grid(["13 Regime & trend strength", "14 Options analytics", "15 Risk & position sizing", "16 Strategy backtest", "17 Market breadth", "18 Events & gap risk", "19 Trade journal", "20 Data quality", "21 Cross-asset performance", "22 Technical signals"])

with extra_tabs[0]:
        st.caption("Market regime and trend strength are displayed in the main dashboard above.")

with extra_tabs[1]:
        st.subheader("14 · Options analytics")
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

with extra_tabs[2]:
        st.subheader("15 · Risk and position-sizing calculator")
        c1,c2,c3 = st.columns(3)
        capital = c1.number_input("Trading capital (₹)", min_value=0.0, value=500000.0, step=25000.0)
        risk_pct = c2.number_input("Risk per trade (%)", min_value=0.1, max_value=10.0, value=1.0, step=0.1)
        entry = c3.number_input("Entry price", min_value=0.0, value=100.0, step=1.0)
        c4,c5,c6 = st.columns(3)
        stop = c4.number_input("Stop-loss price", min_value=0.0, value=95.0, step=1.0)
        lot_qty = c5.number_input("Quantity / lot size", min_value=1, value=1, step=1)
        target_rr = c6.number_input("Target R multiple", min_value=0.1, value=1.5, step=0.1)
        risk_budget = capital*risk_pct/100
        per_unit = abs(entry-stop)
        max_qty = int(risk_budget/per_unit) if per_unit > 0 else 0
        max_lots = max_qty//int(lot_qty)
        actual_qty = max_lots*int(lot_qty)
        st.metric("Risk budget", f"₹{risk_budget:,.2f}")
        st.metric("Estimated lots within risk budget", f"{max_lots:,}")
        st.write(f"Estimated quantity: {actual_qty:,} · Estimated loss at stop: ₹{actual_qty*per_unit:,.2f} · Target price at {target_rr:.2f}R: {entry + (per_unit*target_rr if entry>=stop else -per_unit*target_rr):,.2f}")
        st.caption("Excludes slippage, brokerage, taxes, gaps, margin rules and option-specific nonlinear risk. This is a sizing aid, not a margin calculator.")

with extra_tabs[3]:
        st.subheader("16 · Simple historical strategy test")
        st.write("A transparent long-only moving-average crossover test using returned daily closes. It does not include intraday execution or derivatives.")
        bt_name = st.selectbox("Instrument", list(INDEXES.keys()), key="bt_symbol")
        fast_n = st.number_input("Fast SMA", min_value=2, max_value=100, value=20, step=1)
        slow_n = st.number_input("Slow SMA", min_value=3, max_value=250, value=50, step=1)
        cost_bps = st.number_input("Estimated round-trip costs (basis points per position change)", min_value=0.0, value=10.0, step=1.0)
        bdf = market_data[bt_name].copy()
        if not bdf.empty and len(bdf) > int(slow_n)+2 and fast_n < slow_n:
            bdf["Fast"] = bdf["Close"].rolling(int(fast_n)).mean(); bdf["Slow"] = bdf["Close"].rolling(int(slow_n)).mean()
            bdf["Signal"] = (bdf["Fast"] > bdf["Slow"]).astype(float)
            bdf["Position"] = bdf["Signal"].shift(1).fillna(0)
            bdf["Asset return"] = bdf["Close"].pct_change().fillna(0)
            bdf["Strategy return"] = bdf["Position"]*bdf["Asset return"] - bdf["Position"].diff().abs().fillna(0)*cost_bps/10000
            bdf["Equity"] = (1+bdf["Strategy return"]).cumprod(); bdf["Buy & hold"]=(1+bdf["Asset return"]).cumprod()
            total=(bdf["Equity"].iloc[-1]-1)*100; bh=(bdf["Buy & hold"].iloc[-1]-1)*100
            peak=bdf["Equity"].cummax(); mdd=((bdf["Equity"]/peak)-1).min()*100
            wins=bdf.loc[bdf["Strategy return"]!=0,"Strategy return"]
            a,b,c,d=st.columns(4); a.metric("Strategy return",f"{total:.2f}%"); b.metric("Buy & hold",f"{bh:.2f}%"); c.metric("Max drawdown",f"{mdd:.2f}%"); d.metric("Non-zero return bars",f"{len(wins)}")
            fig=go.Figure(); fig.add_trace(go.Scatter(x=bdf["Date"],y=bdf["Equity"],name="Strategy equity")); fig.add_trace(go.Scatter(x=bdf["Date"],y=bdf["Buy & hold"],name="Buy & hold")); fig.update_layout(template="plotly_dark",height=400,yaxis_title="Growth of ₹1 (before tax; costs approximate)")
            st.plotly_chart(fig,use_container_width=True)
            st.download_button("Download backtest rows", bdf.to_csv(index=False).encode(), file_name="backtest_results.csv", mime="text/csv")
            st.warning("Illustrative backtest only. It is not walk-forward validation and may be distorted by source data, index construction, costs and execution assumptions.")
        else: st.info("Insufficient history or invalid periods. Choose fast SMA below slow SMA and provide enough returned daily bars.")

with extra_tabs[4]:
        st.subheader("17 · Automatic market breadth")
        st.write("Uses the dashboard's automatically retrieved large-cap stock history. This is a sample universe, not the full NSE market.")
        wide=get_auto_universe(period="2y")
        if wide.empty:
            st.warning("Automatic breadth unavailable: Yahoo Finance returned no usable stock history. No breadth values are fabricated.")
        else:
            wide["Date"]=pd.to_datetime(wide["Date"],errors="coerce")
            wide["Close"]=pd.to_numeric(wide["Close"],errors="coerce")
            wide=wide.dropna(subset=["Date","Symbol","Close"]).sort_values(["Symbol","Date"])
            for n in [20,50,200]:
                wide[f"SMA{n}"]=wide.groupby("Symbol")["Close"].transform(lambda x:x.rolling(n,min_periods=n).mean())
            latest_date=wide["Date"].max()
            last=wide[wide["Date"]==latest_date].copy()
            x,y,z=st.columns(3)
            for col,ma,label in [(x,"SMA20","Above 20-day SMA"),(y,"SMA50","Above 50-day SMA"),(z,"SMA200","Above 200-day SMA")]:
                valid=last.dropna(subset=[ma])
                col.metric(label,f"{(valid['Close']>valid[ma]).mean()*100:.1f}%" if not valid.empty else "Unavailable",f"{len(valid)} symbols")
            last["Above SMA20"]=last["Close"]>last["SMA20"]
            last["Above SMA50"]=last["Close"]>last["SMA50"]
            last["Above SMA200"]=last["Close"]>last["SMA200"]
            st.caption(f"Latest source date: {latest_date:%Y-%m-%d} · {last['Symbol'].nunique()} symbols returned · Yahoo Finance daily history, cached for up to 6 hours.")
            breadth_view = last[["Date","Symbol","Industry","Close","SMA20","SMA50","SMA200","Above SMA20","Above SMA50","Above SMA200"]].sort_values("Symbol")
            def breadth_bool_color(value):
                if pd.isna(value): return ""
                return "background-color: #dcfce7; color: #166534; font-weight: 600" if bool(value) else "background-color: #fee2e2; color: #991b1b; font-weight: 600"
            st.dataframe(breadth_view.style.applymap(breadth_bool_color, subset=["Above SMA20","Above SMA50","Above SMA200"]),use_container_width=True,hide_index=True)
            ind=last.dropna(subset=["Above SMA200"]).groupby("Industry").agg(Symbols=("Symbol","nunique"),Above200Pct=("Above SMA200",lambda x:100*x.mean())).reset_index().sort_values("Above200Pct",ascending=False)
            if not ind.empty:
                st.markdown("#### Breadth by mapped industry")
                st.dataframe(ind.style.format({"Above200Pct":"{:.1f}%"}),use_container_width=True,hide_index=True)
            st.download_button("Download automatic breadth data",last.to_csv(index=False).encode("utf-8"),"automatic_market_breadth.csv","text/csv")

with extra_tabs[5]:
        st.subheader("18 · Automatic economic and gap-risk monitor")
        st.write("Automatically searches public news headlines about scheduled central-bank and macroeconomic events. This is a headline monitor, not a verified release-time calendar.")
        query=st.selectbox("News focus",["India RBI inflation GDP policy","US Federal Reserve CPI jobs economic data","Crude oil OPEC EIA inventory","India US geopolitical market risk"],key="event_news_query")
        news_query={"India RBI inflation GDP policy":"India RBI inflation GDP monetary policy","US Federal Reserve CPI jobs economic data":"Federal Reserve CPI employment economic data","Crude oil OPEC EIA inventory":"OPEC EIA crude oil inventory","India US geopolitical market risk":"India US geopolitical market risk markets"}[query]
        with st.spinner("Fetching public news headlines…"):
            headlines,news_error=get_news_rss(news_query,15)
        if news_error: st.warning(news_error)
        elif not headlines.empty:
            # Keep the headline table readable on mobile; hide long raw Google News redirect URLs.
            display_headlines = headlines.drop(columns=["Source link"], errors="ignore")
            st.dataframe(display_headlines,use_container_width=True,hide_index=True)
            st.download_button("Download event-risk headlines",display_headlines.to_csv(index=False).encode("utf-8"),"event_risk_headlines.csv","text/csv")
        gap_name=st.selectbox("Instrument for previous-bar gap check",["NIFTY 50","BANK NIFTY","INFY","WIPRO"],key="gap_symbol")
        gdf=market_data[gap_name]
        if len(gdf)>=2:
            prev=float(gdf.iloc[-2]["Close"]); latest=float(gdf.iloc[-1]["Open"]) if "Open" in gdf else np.nan
            st.metric("Latest bar open vs prior close",f"{((latest/prev)-1)*100:.2f}%" if pd.notna(latest) and prev else "Unavailable")
            st.caption(f"Uses returned bars only. Latest bar timestamp: {gdf.iloc[-1]['Date']:%d %b %Y %H:%M IST}; may not be today's opening gap.")
        else: st.info("Insufficient bars for gap calculation.")

with extra_tabs[6]:
        st.subheader("19 · Trade journal and performance review")
        journal_cols=["Date","Instrument","Strategy/setup","Direction","Entry","Stop","Exit","Quantity","Gross P&L","Costs","Net P&L","Notes","Followed rules?"]
        journal_file=st.file_uploader("Load prior journal CSV (optional)",type=["csv"],key="journal_csv")
        journal=pd.read_csv(journal_file) if journal_file is not None else pd.DataFrame(columns=journal_cols)
        journal_edit=st.data_editor(journal,use_container_width=True,num_rows="dynamic",key="journal_editor")
        if "Net P&L" in journal_edit.columns:
            net=pd.to_numeric(journal_edit["Net P&L"],errors="coerce").dropna()
            if len(net):
                a,b,c=st.columns(3); a.metric("Net P&L total",f"₹{net.sum():,.2f}"); b.metric("Win rate",f"{(net>0).mean()*100:.1f}%"); c.metric("Trades logged",str(len(net)))
        st.download_button("Download trade journal CSV",journal_edit.to_csv(index=False).encode(),file_name="trade_journal.csv",mime="text/csv")
        st.caption("Export your journal regularly; the hosted app does not promise permanent storage of edits between sessions.")

with extra_tabs[7]:
        st.subheader("20 · Data quality and provenance monitor")
        dq=[]
        for name,df in market_data.items():
            err=market_errors.get(name); last_date=df.iloc[-1]["Date"] if not df.empty else pd.NaT
            dq.append({"Instrument":name,"Ticker":INDEXES[name],"Rows":len(df),"Latest source candle (IST)":last_date.strftime("%Y-%m-%d %H:%M %Z") if pd.notna(last_date) else "Unavailable","Status":"OK · source returned rows" if not df.empty else "UNAVAILABLE","Error/source note":err or "Yahoo Finance via yfinance; verify delay/coverage"})
        dqdf=pd.DataFrame(dq); st.dataframe(dqdf,use_container_width=True,hide_index=True)
        st.metric("Instruments with returned data",f"{sum(not d.empty for d in market_data.values())}/{len(market_data)}")
        st.caption(f"App checked/viewed at {datetime.now(ZoneInfo('Asia/Kolkata')):%Y-%m-%d %H:%M:%S IST}. This is not the exchange timestamp. Staleness thresholds are not used to relabel exchange data without verified exchange metadata.")
        st.download_button("Download data quality report",dqdf.to_csv(index=False).encode(),file_name="data_quality_report.csv",mime="text/csv")

with extra_tabs[8]:
        st.subheader("21 · Cross-asset relative performance")
        cross_tickers={"NIFTY 50":"^NSEI","BANK NIFTY":"^NSEBANK","India VIX":"^INDIAVIX","Brent crude":"BZ=F","Gold":"GC=F","US Dollar Index":"DX-Y.NYB","USD/INR":"INR=X","S&P 500":"^GSPC","Nasdaq 100":"^NDX"}
        cross_rows=[]
        with st.spinner("Fetching best-effort cross-asset histories…"):
            for nm,tkr in cross_tickers.items():
                xdf,xerr=get_history(tkr,period,"1d")
                if len(xdf)>=2:
                    xclose=xdf["Close"].dropna(); ret=(xclose.iloc[-1]/xclose.iloc[0]-1)*100 if xclose.iloc[0] else np.nan
                    cross_rows.append({"Asset":nm,"Ticker":tkr,"Lookback return %":ret,"Last source candle":xdf.iloc[-1]["Date"].strftime("%Y-%m-%d %H:%M IST")})
        if cross_rows:
            cross=pd.DataFrame(cross_rows).sort_values("Lookback return %",ascending=False); st.dataframe(cross,use_container_width=True,hide_index=True)
            fig=px.bar(cross,x="Asset",y="Lookback return %",color="Lookback return %",template="plotly_dark"); fig.update_layout(height=420); st.plotly_chart(fig,use_container_width=True)
        else: st.warning("No cross-asset data returned. No values substituted.")
        st.caption("Cross-asset correlations are intentionally not shown unless overlapping timestamped history is available for the selected assets.")

with extra_tabs[9]:
        st.subheader("22 · Transparent technical setup scanner")
        st.write("Screen the instruments already configured in this dashboard. This is a small watchlist, not a full NSE universe scanner.")
        min_rsi=st.slider("Bullish RSI minimum",0,100,50); max_rsi=st.slider("Bearish RSI maximum",0,100,45)
        signal_rows=[]
        for nm,df in market_data.items():
            if df.empty: continue
            si=indicators(df); last=si.iloc[-1]
            close=last.get("Close",np.nan); e20=last.get("EMA 20",np.nan); e50=last.get("EMA 50",np.nan); rsi=last.get("RSI 14",np.nan)
            if pd.isna(e20) or pd.isna(e50) or pd.isna(rsi): signal="Insufficient history"
            elif close>e20>e50 and rsi>=min_rsi: signal="Bullish alignment"
            elif close<e20<e50 and rsi<=max_rsi: signal="Bearish alignment"
            else: signal="No aligned setup"
            signal_rows.append({"Instrument":nm,"Close":close,"EMA 20":e20,"EMA 50":e50,"RSI 14":rsi,"Rule result":signal,"Last source candle":last["Date"].strftime("%Y-%m-%d %H:%M IST")})
        if signal_rows: st.dataframe(pd.DataFrame(signal_rows),use_container_width=True,hide_index=True)
        else: st.info("No source history available for the scanner.")
        st.caption("Signals are mechanical rule checks on latest returned daily bars, not recommendations. No automated alerts or trade execution are enabled.")
    
    
# Additional research modules inspired by the user's reference dashboard.
research_tabs = [st.expander(label, expanded=False) for label in ["23 Industry intelligence", "24 Delivery & money-flow proxies", "25 Daily briefing"]]

with research_tabs[0]:
    st.subheader("23 · Industry intelligence and rotation")
    st.write("Automatically fetches a broad large-cap stock universe from Yahoo Finance, with optional CSV override. Public data may be delayed or unavailable.")
    industry_file = st.file_uploader("Optional: override with your own stock history CSV", type=["csv"], key="industry_history_csv")
    auto_industry = st.button("↻ Fetch latest industry data", key="auto_industry_fetch")
    if True:  # Auto-fetch by default; cached for six hours. CSV remains an optional override.
        try:
            ind = pd.read_csv(industry_file) if industry_file is not None else get_auto_universe(period="2y")
            ind.columns = [str(c).strip() for c in ind.columns]
            required = {"Date", "Symbol", "Industry", "Close"}
            if not required.issubset(ind.columns):
                st.error("Required columns: Date, Symbol, Industry, Close.")
            else:
                ind["Date"] = pd.to_datetime(ind["Date"], errors="coerce")
                ind["Close"] = pd.to_numeric(ind["Close"], errors="coerce")
                ind = ind.dropna(subset=["Date", "Symbol", "Industry", "Close"])
                ind = ind.sort_values(["Symbol", "Date"]).drop_duplicates(["Symbol", "Date"], keep="last")
                ind["Return 1M %"] = ind.groupby("Symbol")["Close"].transform(lambda x: (x / x.shift(21) - 1) * 100)
                ind["Return 3M %"] = ind.groupby("Symbol")["Close"].transform(lambda x: (x / x.shift(63) - 1) * 100)
                ind["SMA50"] = ind.groupby("Symbol")["Close"].transform(lambda x: x.rolling(50, min_periods=50).mean())
                ind["SMA200"] = ind.groupby("Symbol")["Close"].transform(lambda x: x.rolling(200, min_periods=200).mean())
                latest_date = ind["Date"].max()
                latest = ind[ind["Date"] == latest_date].copy()
                agg = latest.groupby("Industry").agg(
                    Stocks=("Symbol", "nunique"),
                    Mean_1M=("Return 1M %", "mean"),
                    Mean_3M=("Return 3M %", "mean"),
                    Median_1M=("Return 1M %", "median"),
                    Valid_1M=("Return 1M %", "count"),
                    Valid_3M=("Return 3M %", "count"),
                ).reset_index()
                breadth = latest.assign(
                    Above50=latest["Close"] > latest["SMA50"],
                    Above200=latest["Close"] > latest["SMA200"],
                ).groupby("Industry").agg(
                    Above_50DMA_pct=("Above50", lambda x: x.dropna().mean() * 100 if x.notna().any() else np.nan),
                    Above_200DMA_pct=("Above200", lambda x: x.dropna().mean() * 100 if x.notna().any() else np.nan),
                ).reset_index()
                agg = agg.merge(breadth, on="Industry", how="left").sort_values("Mean_1M", ascending=False)
                st.caption(f"Latest data date: {latest_date:%Y-%m-%d}. Returns are equal-weighted across available stocks, not market-cap weighted.")
                display = agg.rename(columns={"Mean_1M":"Average 1M return %","Mean_3M":"Average 3M return %","Median_1M":"Median 1M return %","Valid_1M":"Stocks with 1M history","Valid_3M":"Stocks with 3M history"})
                st.dataframe(display.style.format({"Average 1M return %":"{:.2f}","Average 3M return %":"{:.2f}","Median 1M return %":"{:.2f}","Above_50DMA_pct":"{:.1f}","Above_200DMA_pct":"{:.1f}"}), use_container_width=True, hide_index=True)
                plot_df = agg.dropna(subset=["Mean_1M"]).head(15)
                if not plot_df.empty:
                    fig = px.bar(plot_df.sort_values("Mean_1M"), x="Mean_1M", y="Industry", orientation="h", color="Mean_1M", template="plotly_dark", title="Top industries by equal-weighted 1-month return")
                    fig.update_layout(height=max(360, 28 * len(plot_df)), xaxis_title="Return (%)")
                    st.plotly_chart(fig, use_container_width=True)
                st.download_button("Download industry rankings", agg.to_csv(index=False).encode("utf-8"), file_name="industry_intelligence.csv", mime="text/csv")
        except Exception as exc:
            st.error(f"Could not calculate industry analytics: {type(exc).__name__}: {exc}")
    else:
        st.info("Automatic history was unavailable from Yahoo Finance. No industry values are substituted; retry later or optionally upload your own genuine CSV.")
    st.caption("Automatic universe: selected large-cap NSE-listed stocks. Industry labels are broad groupings, not official index constituents. Source: Yahoo Finance via yfinance; verify freshness and coverage.")

with research_tabs[1]:
    st.subheader("24 · Delivery activity and money-flow proxies")
    st.write("The dashboard does not scrape NSE reports. NSE publishes downloadable delivery reports, but no authorized free machine-readable API is configured here.")
    st.warning("Automatic delivery percentage and exchange turnover are unavailable until a permitted data feed is connected. The app will not invent delivery data or ask you to upload files.")
    st.markdown("[Open official NSE All Reports](https://www.nseindia.com/all-reports) · Look for “Full Bhavcopy and Security Deliverable data” or “Security-wise Delivery Positions”.")
    st.markdown("**What remains automatic:** price history, market breadth, industry performance, technical indicators, stock deep-dive, strategy backtests, and public news headlines.")
    st.caption("Delivery percentage, participant money flow, and institutional delivery signals require genuine source fields. Price × volume is not presented as exchange turnover or delivery activity.")

with research_tabs[2]:
    st.subheader("25 · Daily market briefing")
    st.write("Generated from data already retrieved by the dashboard. It does not invent news headlines or claim to know why a market moved.")
    nifty_df = market_data.get("NIFTY 50", pd.DataFrame())
    if not nifty_df.empty:
        ni = indicators(nifty_df)
        nl = ni.iloc[-1]
        prev_close = float(ni.iloc[-2]["Close"]) if len(ni) > 1 else np.nan
        ret = (float(nl["Close"]) / prev_close - 1) * 100 if pd.notna(prev_close) and prev_close else np.nan
        a,b,c,d = st.columns(4)
        a.metric("NIFTY 50 latest returned close", fmt(nl["Close"]), f"{fmt(ret)}% vs previous bar" if pd.notna(ret) else None)
        b.metric("RSI 14", fmt(nl.get("RSI 14")))
        c.metric("EMA 20", fmt(nl.get("EMA 20")))
        d.metric("EMA 50", fmt(nl.get("EMA 50")))
        close, e20, e50 = nl["Close"], nl.get("EMA 20", np.nan), nl.get("EMA 50", np.nan)
        regime_text = ("bullish EMA alignment" if close > e20 > e50 else "bearish EMA alignment" if close < e20 < e50 else "mixed EMA alignment") if pd.notna(e20) and pd.notna(e50) else "insufficient moving-average history"
        st.markdown(f"**Technical regime:** {regime_text}. This is a rule-based description, not a forecast.")
        brief_rows=[]
        for nm,df in market_data.items():
            snap=quote_snapshot(df)
            if snap:
                last,change,pct=snap
                brief_rows.append({"Instrument":nm,"Latest returned close":last["Close"],"Change vs previous bar":change,"Change %":pct,"Source candle (IST)":last["Date"].strftime("%Y-%m-%d %H:%M IST"),"Frequency":"Daily","Source":"Yahoo Finance via yfinance"})
        st.dataframe(pd.DataFrame(brief_rows),use_container_width=True,hide_index=True)
        st.markdown("#### Known limitations")
        st.write("- This is a daily-bar summary, not a live intraday signal.")
        st.write("- Market moves are not attributed to geopolitical or economic news without verified event sources.")
        st.write("- FII/DII flows, derivatives positioning and delivery activity are unavailable unless separately sourced or uploaded.")
        st.download_button("Download daily briefing data",pd.DataFrame(brief_rows).to_csv(index=False).encode("utf-8"),file_name="daily_market_briefing_data.csv",mime="text/csv")
    else:
        st.warning("No market history returned. A briefing cannot be generated until a real source returns data.")


# Additional modules completing the major workflows shown in the reference screenshots.
advanced_tabs = [st.expander(label, expanded=False) for label in [
    "26 Macro & event monitor",
    "27 Breadth & stock scanner",
    "28 Derivatives lab",
    "29 Stock deep-dive",
    "30 Strategy backtest",
    "31 Data quality"
]]

with advanced_tabs[0]:
    st.subheader("26 · Automatic macro & geopolitical event monitor")
    st.write("Public news headlines are fetched automatically and linked to their publishers. Headlines are not proof of causation and are not used to fabricate market-impact statistics.")
    topic=st.selectbox("Headline category",["India macro & RBI","Global macro & central banks","Geopolitics and energy","Markets and policy"],key="auto_event_topic")
    queries={"India macro & RBI":"India RBI inflation GDP economy","Global macro & central banks":"Federal Reserve ECB Bank of Japan inflation rates economy","Geopolitics and energy":"geopolitics oil energy markets conflict","Markets and policy":"India markets policy regulation economy"}
    with st.spinner("Fetching public headline feed…"):
        ev,ev_error=get_news_rss(queries[topic],20)
    if ev_error: st.warning(ev_error)
    elif not ev.empty:
        st.dataframe(ev,use_container_width=True,hide_index=True,column_config={"Source link":st.column_config.LinkColumn("Open source")})
        st.download_button("Download headline log",ev.to_csv(index=False).encode("utf-8"),"macro_geopolitical_headlines.csv","text/csv")
    st.markdown("#### Live macro market proxies")
    macro_rows=[]
    for name,ticker in [("NIFTY 50","^NSEI"),("BANK NIFTY","^NSEBANK"),("India VIX","^INDIAVIX"),("Brent Crude","BZ=F"),("USD/INR","INR=X"),("Gold","GC=F")]:
        if name in market_data and not market_data[name].empty:
            df=market_data[name]
        else:
            df,_=get_history(ticker,period,"1d")
        if not df.empty:
            snap=quote_snapshot(df)
            if snap:
                last,change,pct=snap
                macro_rows.append({"Instrument":name,"Latest returned close":last["Close"],"Change %":pct,"Source candle":last["Date"].strftime("%Y-%m-%d %H:%M IST"),"Source":"Yahoo Finance via yfinance"})
        else: macro_rows.append({"Instrument":name,"Latest returned close":np.nan,"Change %":np.nan,"Source candle":"Unavailable","Source":"No data returned"})
    st.dataframe(pd.DataFrame(macro_rows),use_container_width=True,hide_index=True)
    st.caption("Macro proxies are public market history, not official economic indicators. RBI rates, inflation releases, FII/DII flows and event impact are not inferred from prices.")
    st.markdown("[Official sources: NSE reports](https://www.nseindia.com/all-reports) · [RBI](https://www.rbi.org.in/) · [MOSPI](https://mospi.gov.in/) · [SEBI](https://www.sebi.gov.in/)")

with advanced_tabs[1]:
    st.subheader("27 · Automatic market breadth and technical stock scanner")
    st.write("Automatically fetches a fixed large-cap OHLCV universe by default. Optional CSV override is available, but not required.")
    breadth_file = st.file_uploader("Optional: override with your own OHLCV CSV", type=["csv"], key="breadth_universe")
    auto_breadth = st.button("↻ Fetch latest stock universe", key="auto_breadth_fetch")
    st.caption("Automatic history is fetched from Yahoo Finance. Public source coverage, delays and availability can vary.")
    if True:  # Auto-fetch by default; cached for six hours. CSV remains an optional override.
        try:
            univ = pd.read_csv(breadth_file) if breadth_file is not None else get_auto_universe(period="2y")
            univ.columns = [str(c).strip() for c in univ.columns]
            if not {"Date", "Symbol", "Close"}.issubset(univ.columns):
                st.error("Required columns: Date, Symbol, Close.")
            else:
                univ["Date"] = pd.to_datetime(univ["Date"], errors="coerce")
                for c in ["Close", "Open", "High", "Low", "Volume"]:
                    if c in univ: univ[c] = pd.to_numeric(univ[c], errors="coerce")
                univ = univ.dropna(subset=["Date", "Symbol", "Close"]).sort_values(["Symbol", "Date"]).drop_duplicates(["Symbol", "Date"], keep="last")
                results = []
                for sym, sdf in univ.groupby("Symbol"):
                    sdf = sdf.copy()
                    close = sdf["Close"]
                    sdf["SMA20"] = close.rolling(20, min_periods=20).mean()
                    sdf["SMA50"] = close.rolling(50, min_periods=50).mean()
                    sdf["SMA200"] = close.rolling(200, min_periods=200).mean()
                    sdf["EMA20"] = close.ewm(span=20, adjust=False, min_periods=20).mean()
                    delta = close.diff()
                    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
                    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False, min_periods=14).mean()
                    rs = gain / loss.replace(0, np.nan)
                    sdf["RSI14"] = 100 - 100/(1+rs)
                    sdf.loc[(loss == 0) & gain.notna(), "RSI14"] = 100
                    last = sdf.iloc[-1]
                    prior = sdf.iloc[-2] if len(sdf) > 1 else None
                    results.append({
                        "Symbol": sym, "Industry": last.get("Industry", ""),
                        "Date": last["Date"], "Close": last["Close"],
                        "1D %": ((last["Close"]/prior["Close"]-1)*100 if prior is not None and prior["Close"] else np.nan),
                        "SMA20": last["SMA20"], "SMA50": last["SMA50"], "SMA200": last["SMA200"],
                        "RSI14": last["RSI14"],
                        "Above SMA20": bool(last["Close"] > last["SMA20"]) if pd.notna(last["SMA20"]) else None,
                        "Above SMA50": bool(last["Close"] > last["SMA50"]) if pd.notna(last["SMA50"]) else None,
                        "Above SMA200": bool(last["Close"] > last["SMA200"]) if pd.notna(last["SMA200"]) else None,
                        "EMA trend": ("Bullish" if last["Close"] > last["EMA20"] else "Bearish") if pd.notna(last["EMA20"]) else "Insufficient history",
                    })
                scan = pd.DataFrame(results)
                valid200 = scan["Above SMA200"].dropna()
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Symbols uploaded", f"{scan['Symbol'].nunique():,}")
                m2.metric("Above 50DMA", f"{100*scan['Above SMA50'].dropna().mean():.1f}%" if scan["Above SMA50"].notna().any() else "Unavailable")
                m3.metric("Above 200DMA", f"{100*valid200.mean():.1f}%" if len(valid200) else "Insufficient history")
                m4.metric("Breadth sample", f"{len(valid200):,} with 200DMA" )
                st.markdown("#### Filters")
                f1, f2, f3 = st.columns(3)
                trend_filter = f1.selectbox("Trend", ["All", "Bullish", "Bearish", "Insufficient history"], key="scanner_trend")
                rsi_filter = f2.selectbox("RSI 14", ["All", "Below 30", "30–50", "50–70", "Above 70"], key="scanner_rsi")
                dma_filter = f3.selectbox("200DMA", ["All", "Above", "Below", "Insufficient history"], key="scanner_dma")
                filtered = scan.copy()
                if trend_filter != "All": filtered = filtered[filtered["EMA trend"] == trend_filter]
                if rsi_filter == "Below 30": filtered = filtered[filtered["RSI14"] < 30]
                elif rsi_filter == "30–50": filtered = filtered[filtered["RSI14"].between(30, 50, inclusive="left")]
                elif rsi_filter == "50–70": filtered = filtered[filtered["RSI14"].between(50, 70, inclusive="left")]
                elif rsi_filter == "Above 70": filtered = filtered[filtered["RSI14"] >= 70]
                if dma_filter == "Above": filtered = filtered[filtered["Above SMA200"] == True]
                elif dma_filter == "Below": filtered = filtered[filtered["Above SMA200"] == False]
                elif dma_filter == "Insufficient history": filtered = filtered[filtered["Above SMA200"].isna()]
                st.dataframe(filtered.sort_values("1D %", ascending=False), use_container_width=True, hide_index=True)
                if "Industry" in scan and scan["Industry"].astype(str).str.len().gt(0).any():
                    ind_breadth = scan.dropna(subset=["Above SMA200"]).groupby("Industry").agg(Stocks=("Symbol","nunique"), Above200Pct=("Above SMA200",lambda x:100*x.mean())).reset_index().sort_values("Above200Pct",ascending=False)
                    st.markdown("#### Industry breadth")
                    st.dataframe(ind_breadth.style.format({"Above200Pct":"{:.1f}%"}), use_container_width=True, hide_index=True)
                st.download_button("Download scanner results", filtered.to_csv(index=False).encode("utf-8"), "technical_scanner_results.csv", "text/csv")
        except Exception as exc:
            st.error(f"Could not calculate breadth/scanner: {type(exc).__name__}: {exc}")
    else:
        st.info("Automatic stock history is unavailable from the public source. No breadth values are fabricated; retry later.")

with advanced_tabs[2]:
    st.subheader("28 · Derivatives lab")
    st.write("This module does not scrape NSE or fabricate derivatives data.")
    st.info("Options analytics now use the authenticated FYERS Option Chain API when connected. Futures-chain OI and participant-level FII/DII positioning are separate data sets and may remain unavailable.")
    st.markdown("[Official NSE option chain](https://www.nseindia.com/option-chain) · [NSE derivatives reports](https://www.nseindia.com/all-reports-derivatives)")
    st.caption("Read-only FYERS market-data access is used for option-chain analysis; this module does not place orders. FII/DII participant positioning is not inferred from option-chain data.")

with advanced_tabs[3]:
    st.subheader("29 · Automatic stock deep-dive")
    deep_univ=get_auto_universe(period="2y")
    if deep_univ.empty:
        st.warning("Stock deep-dive unavailable because public OHLCV history could not be retrieved.")
    else:
        symbols=sorted(deep_univ["Symbol"].dropna().unique())
        symbol=st.selectbox("Stock from automatic research universe",symbols,key="deep_auto_symbol")
        sdf=deep_univ[deep_univ["Symbol"]==symbol].copy().sort_values("Date")
        calc=indicators(sdf)
        fig=go.Figure(go.Candlestick(x=calc["Date"],open=calc["Open"],high=calc["High"],low=calc["Low"],close=calc["Close"],name=symbol))
        for col in ["SMA 20","SMA 50","SMA 200","EMA 20","EMA 50"]:
            if col in calc and calc[col].notna().any(): fig.add_trace(go.Scatter(x=calc["Date"],y=calc[col],name=col,mode="lines"))
        fig.update_layout(template="plotly_dark",height=560,xaxis_rangeslider_visible=False,title=f"{symbol} · daily historical candles")
        st.plotly_chart(fig,use_container_width=True)
        last=calc.iloc[-1]
        a,b,c,d=st.columns(4)
        a.metric("Latest close",fmt(last["Close"])); b.metric("RSI 14",fmt(last.get("RSI 14"))); c.metric("ATR 14",fmt(last.get("ATR 14"))); d.metric("Latest source date",last["Date"].strftime("%Y-%m-%d"))
        st.caption(f"Source: Yahoo Finance via yfinance · {len(calc)} returned bars · Latest candle is not guaranteed live.")
        st.dataframe(calc.tail(30),use_container_width=True,hide_index=True)
        st.download_button("Download stock history and indicators",calc.to_csv(index=False).encode("utf-8"),f"{symbol.lower()}_deep_dive.csv","text/csv")

with advanced_tabs[4]:
    st.subheader("30 · Automatic strategy backtest")
    st.write("Long-only SMA crossover test using the dashboard's retrieved daily OHLCV history. Includes an assumed transaction cost; does not model slippage, taxes, corporate actions or intraday execution.")
    bt_univ=get_auto_universe(period="5y")
    if bt_univ.empty:
        st.warning("Backtest unavailable because public historical data could not be retrieved.")
    else:
        bt_symbol=st.selectbox("Stock",sorted(bt_univ["Symbol"].dropna().unique()),key="bt_auto_symbol")
        bt=bt_univ[bt_univ["Symbol"]==bt_symbol].copy().sort_values("Date").dropna(subset=["Close"])
        fast=st.slider("Fast SMA",2,100,20,key="bt_auto_fast")
        slow=st.slider("Slow SMA",5,250,50,key="bt_auto_slow")
        cost_bps=st.number_input("Transaction cost per position change (basis points)",min_value=0.0,max_value=200.0,value=5.0,step=1.0,key="bt_auto_cost")
        if fast>=slow: st.warning("Fast SMA must be shorter than slow SMA.")
        elif len(bt)<=slow: st.warning(f"Need more than {slow} returned daily observations; current rows: {len(bt)}.")
        else:
            bt["SMA_fast"]=bt["Close"].rolling(fast,min_periods=fast).mean()
            bt["SMA_slow"]=bt["Close"].rolling(slow,min_periods=slow).mean()
            bt["Signal"]=(bt["SMA_fast"]>bt["SMA_slow"]).astype(int)
            bt["Position"]=bt["Signal"].shift(1).fillna(0)
            bt["Market return"]=bt["Close"].pct_change().fillna(0)
            bt["Turnover"]=bt["Position"].diff().abs().fillna(bt["Position"].abs())
            bt["Strategy return"]=bt["Position"]*bt["Market return"]-bt["Turnover"]*(cost_bps/10000)
            bt["Equity curve"]=(1+bt["Strategy return"]).cumprod()
            bt["Buy & hold"]=(1+bt["Market return"]).cumprod()
            bt["Drawdown"]=bt["Equity curve"]/bt["Equity curve"].cummax()-1
            total=(bt["Equity curve"].iloc[-1]-1)*100; bh=(bt["Buy & hold"].iloc[-1]-1)*100; dd=bt["Drawdown"].min()*100
            vol=bt["Strategy return"].std(); sharpe=(bt["Strategy return"].mean()/vol*np.sqrt(252)) if pd.notna(vol) and vol>0 else np.nan
            a,b,c,d=st.columns(4)
            a.metric("Strategy return",f"{total:.2f}%"); b.metric("Buy & hold",f"{bh:.2f}%"); c.metric("Max drawdown",f"{dd:.2f}%"); d.metric("Annualised Sharpe (rough)",fmt(sharpe))
            fig=go.Figure(); fig.add_trace(go.Scatter(x=bt["Date"],y=bt["Equity curve"],name="SMA strategy")); fig.add_trace(go.Scatter(x=bt["Date"],y=bt["Buy & hold"],name="Buy & hold"))
            fig.update_layout(template="plotly_dark",height=420,title="Growth of ₹1 (approximate costs applied)")
            st.plotly_chart(fig,use_container_width=True)
            st.caption(f"Source: Yahoo Finance · {len(bt)} daily bars · last candle {bt['Date'].max():%Y-%m-%d}. Illustrative research only, not a recommendation.")
            st.dataframe(bt.tail(30),use_container_width=True,hide_index=True)
            st.download_button("Download automatic backtest results",bt.to_csv(index=False).encode("utf-8"),f"{bt_symbol.lower()}_sma_backtest.csv","text/csv")

with advanced_tabs[5]:
    st.subheader("31 · Data quality and source audit")
    st.write("Audit source coverage, timestamps, missing rows and indicator readiness for the public market series currently loaded.")
    quality_rows = []
    for name, ticker in INDEXES.items():
        df = market_data.get(name, pd.DataFrame())
        err = market_errors.get(name)
        if df.empty:
            quality_rows.append({"Instrument":name,"Ticker":ticker,"Rows":0,"Latest source candle":"Unavailable","Age (days)":np.nan,"Duplicate timestamps":0,"OHLC complete %":np.nan,"Status":err or "No data"})
        else:
            date_max = df["Date"].max()
            age = (pd.Timestamp.now(tz="Asia/Kolkata") - date_max).total_seconds()/86400 if date_max.tzinfo else (pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None)-date_max).total_seconds()/86400
            cols = [c for c in ["Open","High","Low","Close"] if c in df.columns]
            complete = df[cols].notna().all(axis=1).mean()*100 if cols else np.nan
            dupes = int(df["Date"].duplicated().sum())
            status = "OK (daily source bar)" if age < 4 else "Potentially stale — verify source" 
            if complete < 99.9: status += "; incomplete OHLC"
            quality_rows.append({"Instrument":name,"Ticker":ticker,"Rows":len(df),"Latest source candle":date_max.strftime("%Y-%m-%d %H:%M %Z"),"Age (days)":age,"Duplicate timestamps":dupes,"OHLC complete %":complete,"Status":status})
    qdf = pd.DataFrame(quality_rows)
    st.dataframe(qdf.style.format({"Age (days)":"{:.2f}","OHLC complete %":"{:.1f}"}),use_container_width=True,hide_index=True)
    st.download_button("Download source audit",qdf.to_csv(index=False).encode("utf-8"),"market_data_quality_audit.csv","text/csv")
    st.markdown("#### What this dashboard does not claim")
    st.write("- Yahoo Finance data is best-effort, not guaranteed live exchange data.")
    st.write("- TradingView embedded charts are a separate provider and may have exchange-specific delays.")
    st.write("- Public daily price history does not provide real option-chain OI, implied volatility, max pain, FII/DII flows or verified geopolitical causes.")
    st.write("- Calculations are only as sound as the dates, adjustments, universe membership and source rows supplied.")


# FYERS chart workspace: all NSE cash-market equity symbols from FYERS Symbol Master.
if "fyers_access_token" in st.session_state and fyers_credentials():
    st.divider()
    st.subheader("FYERS stock charts")
    st.caption("Choose any symbol from FYERS' NSE capital-market symbol master. Candles are from FYERS Historical Data; the current quote is fetched separately. This is not a continuously updating WebSocket chart.")
    symbols_list, symbols_error = fyers_nse_equity_symbols()
    if symbols_error:
        st.warning(symbols_error)
    elif not symbols_list:
        st.warning("The FYERS symbol master returned no NSE equity symbols.")
    else:
        symbol_labels = {f"{name or symbol} · {symbol}": symbol for symbol, name in symbols_list}
        selected_label = st.selectbox("Search/select NSE stock", list(symbol_labels.keys()), index=0, key="fyers_chart_symbol")
        # Stock Intelligence Card: use FYERS daily candles and live quote when connected.
        analysis_symbol = symbol_labels[selected_label]
        analysis_cache = st.session_state.get("stock_intelligence_cache", {})
        analysis_item = analysis_cache.get(analysis_symbol, {})
        analysis_age = (datetime.now(ZoneInfo("Asia/Kolkata")).timestamp() - analysis_item.get("loaded_epoch", 0)) if analysis_item else float("inf")
        if not analysis_item or analysis_age > 300:
            with st.spinner(f"Loading FYERS stock intelligence for {analysis_symbol}..."):
                analysis_candles, analysis_error = fyers_history(fyers_credentials()[0], st.session_state["fyers_access_token"], analysis_symbol, "1D", 365)
                analysis_quote_rows, analysis_quote_error = fyers_live_quotes(fyers_credentials()[0], st.session_state["fyers_access_token"], [analysis_symbol])
                analysis_quote = {}
                for _qrow in (analysis_quote_rows or []):
                    _qvalues = _qrow.get("v") or {}
                    if _qrow.get("n", _qvalues.get("symbol", "")) == analysis_symbol:
                        analysis_quote = _qvalues
                        break
                analysis_item = {
                    "candles": analysis_candles.to_json(date_format="iso", orient="split") if analysis_candles is not None and not analysis_candles.empty else None,
                    "error": analysis_error,
                    "quote": analysis_quote,
                    "quote_error": analysis_quote_error,
                    "loaded_epoch": datetime.now(ZoneInfo("Asia/Kolkata")).timestamp(),
                    "loaded_at": datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y %H:%M:%S IST"),
                }
                analysis_cache[analysis_symbol] = analysis_item
                st.session_state["stock_intelligence_cache"] = analysis_cache

        st.markdown("### Stock Intelligence")
        if not analysis_item.get("candles"):
            st.warning("FYERS historical candles are unavailable for this stock. " + str(analysis_item.get("error") or "No candle data returned."))
        else:
            intelligence_df = pd.read_json(analysis_item["candles"], orient="split")
            intelligence_df["Date"] = pd.to_datetime(intelligence_df["Date"], errors="coerce")
            intelligence_df = intelligence_df.dropna(subset=["Date", "Open", "High", "Low", "Close"]).sort_values("Date")
            intelligence_close = intelligence_df["Close"].astype(float)
            intelligence_last = float(intelligence_close.iloc[-1])
            intelligence_quote = analysis_item.get("quote") or {}
            intelligence_ltp = pd.to_numeric(intelligence_quote.get("lp"), errors="coerce")
            if pd.isna(intelligence_ltp) or not np.isfinite(intelligence_ltp) or intelligence_ltp <= 0:
                intelligence_ltp = intelligence_last
                intelligence_price_source = "Last FYERS daily close"
            else:
                intelligence_ltp = float(intelligence_ltp)
                intelligence_price_source = "FYERS quote"
            intelligence_change_pct = pd.to_numeric(intelligence_quote.get("chp"), errors="coerce")
            if pd.isna(intelligence_change_pct) and len(intelligence_close) > 1:
                intelligence_change_pct = (intelligence_close.iloc[-1] / intelligence_close.iloc[-2] - 1) * 100
            intelligence_ema5 = intelligence_close.ewm(span=5, adjust=False).mean()
            intelligence_ema20 = intelligence_close.ewm(span=20, adjust=False).mean()
            intelligence_ema50 = intelligence_close.ewm(span=50, adjust=False).mean()
            intelligence_ema100 = intelligence_close.ewm(span=100, adjust=False).mean()
            intelligence_delta = intelligence_close.diff()
            intelligence_gain = intelligence_delta.clip(lower=0).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
            intelligence_loss = (-intelligence_delta.clip(upper=0)).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
            intelligence_rs = intelligence_gain / intelligence_loss.replace(0, np.nan)
            intelligence_rsi = (100 - 100 / (1 + intelligence_rs)).iloc[-1]
            intelligence_ma_bull = intelligence_ltp > intelligence_ema20.iloc[-1] > intelligence_ema50.iloc[-1]
            intelligence_ma_bear = intelligence_ltp < intelligence_ema20.iloc[-1] < intelligence_ema50.iloc[-1]
            if intelligence_ma_bull and pd.notna(intelligence_rsi) and intelligence_rsi >= 55:
                intelligence_signal, intelligence_signal_color = "BULLISH MOMENTUM", "#16a34a"
            elif intelligence_ma_bear and pd.notna(intelligence_rsi) and intelligence_rsi <= 45:
                intelligence_signal, intelligence_signal_color = "BEARISH MOMENTUM", "#dc2626"
            elif intelligence_ltp > intelligence_ema20.iloc[-1]:
                intelligence_signal, intelligence_signal_color = "MIXED · ABOVE EMA 20", "#d97706"
            else:
                intelligence_signal, intelligence_signal_color = "NEUTRAL / WEAK", "#64748b"
            intelligence_atr = (intelligence_df["High"] - intelligence_df["Low"]).rolling(14, min_periods=14).mean().iloc[-1]
            if pd.isna(intelligence_atr) or intelligence_atr <= 0:
                intelligence_atr = intelligence_ltp * 0.02
            intelligence_entry_low = intelligence_ltp - 0.25 * intelligence_atr
            intelligence_entry_high = intelligence_ltp + 0.25 * intelligence_atr
            intelligence_stop = intelligence_ltp - 1.5 * intelligence_atr
            intelligence_target = intelligence_ltp + 2.0 * intelligence_atr
            intelligence_days = [5, 20, 50]
            intelligence_performance = {}
            for _days in intelligence_days:
                if len(intelligence_close) > _days and intelligence_close.iloc[-_days-1] != 0:
                    intelligence_performance[_days] = (intelligence_close.iloc[-1] / intelligence_close.iloc[-_days-1] - 1) * 100
            st.markdown(f"""
            <div style="border:1px solid #334155;border-radius:18px;padding:20px;background:linear-gradient(135deg,#101923,#111827);">
              <div style="font-size:12px;color:#94a3b8;letter-spacing:1px;">FYERS STOCK SNAPSHOT</div>
              <div style="font-size:26px;font-weight:750;margin:4px 0 12px;">{selected_label.split(' · ')[0]}</div>
              <div style="font-size:30px;font-weight:750;">₹{intelligence_ltp:,.2f}</div>
              <div style="color:{'#16a34a' if pd.notna(intelligence_change_pct) and intelligence_change_pct >= 0 else '#dc2626'};font-weight:650;">{f'{float(intelligence_change_pct):+.2f}%' if pd.notna(intelligence_change_pct) else 'Daily change unavailable'}</div>
              <div style="display:inline-block;margin-top:12px;padding:7px 12px;border-radius:999px;background:{intelligence_signal_color}22;color:{intelligence_signal_color};font-weight:750;">{intelligence_signal}</div>
              <div style="margin-top:10px;font-size:12px;color:#94a3b8;">Price source: {intelligence_price_source} · Data fetched: {analysis_item.get('loaded_at','unknown')}</div>
            </div>
            """, unsafe_allow_html=True)
            card_left, card_right = st.columns([1, 1])
            with card_left:
                st.markdown("#### Technical snapshot")
                st.dataframe(pd.DataFrame([
                    {"Indicator":"EMA 5","Value":f"₹{intelligence_ema5.iloc[-1]:,.2f}","Signal":"Above" if intelligence_ltp > intelligence_ema5.iloc[-1] else "Below"},
                    {"Indicator":"EMA 20","Value":f"₹{intelligence_ema20.iloc[-1]:,.2f}","Signal":"Above" if intelligence_ltp > intelligence_ema20.iloc[-1] else "Below"},
                    {"Indicator":"EMA 50","Value":f"₹{intelligence_ema50.iloc[-1]:,.2f}","Signal":"Above" if intelligence_ltp > intelligence_ema50.iloc[-1] else "Below"},
                    {"Indicator":"EMA 100","Value":f"₹{intelligence_ema100.iloc[-1]:,.2f}","Signal":"Above" if intelligence_ltp > intelligence_ema100.iloc[-1] else "Below"},
                    {"Indicator":"RSI 14","Value":f"{float(intelligence_rsi):.1f}" if pd.notna(intelligence_rsi) else "—","Signal":"Overbought" if pd.notna(intelligence_rsi) and intelligence_rsi >= 70 else "Oversold" if pd.notna(intelligence_rsi) and intelligence_rsi <= 30 else "Neutral"},
                ]), use_container_width=True, hide_index=True)
            with card_right:
                st.markdown("#### Trade levels · ATR-based estimate")
                st.metric("Potential entry zone", f"₹{intelligence_entry_low:,.2f} – ₹{intelligence_entry_high:,.2f}")
                st.metric("Illustrative target (+2 ATR)", f"₹{intelligence_target:,.2f}")
                st.metric("Illustrative stop (−1.5 ATR)", f"₹{intelligence_stop:,.2f}")
                st.caption("Mechanical volatility-based reference, not a guaranteed target or personalized recommendation.")
            st.markdown("#### Momentum and recent performance")
            if intelligence_performance:
                performance_cols = st.columns(len(intelligence_performance))
                for _col, (_period, _perf) in zip(performance_cols, intelligence_performance.items()):
                    _col.metric(f"{_period}-day change", f"{_perf:+.2f}%")
            intelligence_chart = go.Figure()
            intelligence_chart.add_trace(go.Scatter(x=intelligence_df["Date"], y=intelligence_close, name="Close", line=dict(width=2)))
            intelligence_chart.add_trace(go.Scatter(x=intelligence_df["Date"], y=intelligence_ema20, name="EMA 20", line=dict(width=1.3)))
            intelligence_chart.add_trace(go.Scatter(x=intelligence_df["Date"], y=intelligence_ema50, name="EMA 50", line=dict(width=1.3)))
            intelligence_chart.update_layout(template="plotly_dark", height=300, margin=dict(l=15,r=15,t=30,b=15), xaxis_title=None, yaxis_title="₹", legend=dict(orientation="h", y=1.12))
            st.plotly_chart(intelligence_chart, use_container_width=True, config={"displaylogo":False}, key="stock_intelligence_price_chart")
            st.caption("Technical analysis uses FYERS daily historical candles; live quote is shown when available. Data refreshes automatically on selection and is cached for up to 5 minutes.")
        # The Stock Intelligence Card now provides the selected-stock overview and analysis.
        # Start a single FYERS Data Socket for the currently selected chart symbol.
        st.markdown("**Live market stream**")
        live_symbol = symbol_labels[selected_label]
        stream_symbol = st.session_state.get("fyers_stream_symbol")
        stream_active = bool(st.session_state.get("fyers_stream_active"))
        live_col1, live_col2 = st.columns(2)
        with live_col1:
            if not stream_active and st.button("▶ Start live updates", key="fyers_start_stream"):
                try:
                    import queue
                    import threading
                    from fyers_apiv3.FyersWebsocket import data_ws

                    tick_queue = queue.Queue(maxsize=2000)
                    app_id = fyers_credentials()[0]
                    access_token = st.session_state["fyers_access_token"]
                    def _push_tick(message):
                        if isinstance(message, dict) and message.get("symbol") and message.get("ltp") is not None:
                            try:
                                tick_queue.put_nowait(dict(message, received_at=datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()))
                            except Exception:
                                pass
                    def _socket_error(message):
                        try:
                            tick_queue.put_nowait({"_error": str(message), "received_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()})
                        except Exception:
                            pass
                    socket_holder = {"socket": None}
                    def _on_connect():
                        connected_socket = socket_holder.get("socket")
                        if connected_socket:
                            connected_socket.subscribe(symbols=[live_symbol], data_type="SymbolUpdate")
                            connected_socket.keep_running()
                    socket_ref = data_ws.FyersDataSocket(
                        access_token=f"{app_id}:{access_token}",
                        log_path="",
                        litemode=False,
                        write_to_file=False,
                        reconnect=True,
                        reconnect_retry=10,
                        on_connect=_on_connect,
                        on_close=_socket_error,
                        on_error=_socket_error,
                        on_message=_push_tick,
                    )
                    socket_holder["socket"] = socket_ref
                    st.session_state["fyers_tick_queue"] = tick_queue
                    st.session_state["fyers_socket_ref"] = socket_ref
                    st.session_state["fyers_stream_symbol"] = live_symbol
                    st.session_state["fyers_stream_active"] = True
                    thread = threading.Thread(target=socket_ref.connect, daemon=True)
                    st.session_state["fyers_socket_thread"] = thread
                    thread.start()
                    st.success(f"Started FYERS live stream for {live_symbol}.")
                except Exception as exc:
                    st.session_state["fyers_stream_active"] = False
                    st.error(f"Could not start FYERS live stream ({exc.__class__.__name__}). Check app deployment and FYERS session.")
        with live_col2:
            if stream_active and st.button("■ Stop live updates", key="fyers_stop_stream"):
                try:
                    socket_ref = st.session_state.get("fyers_socket_ref")
                    if socket_ref:
                        socket_ref.close_connection()
                except Exception:
                    pass
                st.session_state["fyers_stream_active"] = False
                st.session_state.pop("fyers_socket_ref", None)
                st.session_state.pop("fyers_socket_thread", None)
                st.session_state.pop("fyers_tick_queue", None)
                st.session_state.pop("fyers_live_tick", None)
                st.rerun()

        @st.fragment(run_every=2)
        def _fyers_live_chart_fragment():
            import queue as _queue
            tick_queue = st.session_state.get("fyers_tick_queue")
            if tick_queue is not None:
                while True:
                    try:
                        tick = tick_queue.get_nowait()
                    except _queue.Empty:
                        break
                    if tick.get("_error"):
                        st.session_state["fyers_stream_error"] = tick["_error"]
                    elif tick.get("symbol") == st.session_state.get("fyers_stream_symbol"):
                        st.session_state["fyers_live_tick"] = tick
                        st.session_state["fyers_stream_error"] = None

            tick = st.session_state.get("fyers_live_tick")
            if tick and tick.get("symbol") == st.session_state.get("fyers_stream_symbol"):
                tick_time = tick.get("exch_feed_time") or tick.get("last_traded_time")
                try:
                    tick_time_text = datetime.fromtimestamp(float(tick_time), ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S IST") if tick_time else tick.get("received_at", "time unavailable")
                except Exception:
                    tick_time_text = tick.get("received_at", "time unavailable")
                m1, m2, m3 = st.columns(3)
                with m1:
                    st.metric("FYERS live LTP", f"{float(tick['ltp']):,.2f}", f"{float(tick.get('chp', 0)):+.2f}%" if tick.get("chp") is not None else None)
                with m2:
                    st.metric("Day high", f"{float(tick['high_price']):,.2f}" if tick.get("high_price") is not None else "—")
                with m3:
                    st.metric("Day low", f"{float(tick['low_price']):,.2f}" if tick.get("low_price") is not None else "—")
                st.caption(f"WebSocket feed time: {tick_time_text} · Last received by app: {tick.get('received_at', 'unknown')}")
            elif st.session_state.get("fyers_stream_error"):
                st.warning("FYERS stream message: " + str(st.session_state["fyers_stream_error"])[:300])
            elif st.session_state.get("fyers_stream_active"):
                st.info("Connecting to FYERS Data Socket; waiting for the first market update.")
            else:
                st.caption("Live stream is stopped. Start it during market hours to receive updates.")

            chart_json = st.session_state.get("fyers_chart_data")
            if chart_json:
                chart_symbol_loaded = st.session_state.get("fyers_chart_symbol_loaded", live_symbol)
                candles = pd.read_json(chart_json, orient="split")
                candles["Date"] = pd.to_datetime(candles["Date"], errors="coerce")
                candles = candles.dropna(subset=["Date", "Open", "High", "Low", "Close"]).sort_values("Date")
                if not candles.empty:
                    tick = st.session_state.get("fyers_live_tick")
                    current_ltp = tick.get("ltp") if tick and tick.get("symbol") == chart_symbol_loaded else None
                    fig = go.Figure(data=[go.Candlestick(x=candles["Date"], open=candles["Open"], high=candles["High"], low=candles["Low"], close=candles["Close"], name=chart_symbol_loaded)])
                    for span, enabled in [(21, show_ema21), (50, show_ema50), (200, show_ema200)]:
                        if enabled:
                            ema_values = candles["Close"].ewm(span=span, adjust=False).mean()
                            fig.add_trace(go.Scatter(x=candles["Date"], y=ema_values, name="EMA " + str(span), mode="lines", line=dict(width=1.4)))
                    if show_volume and "Volume" in candles:
                        volume_fig = go.Figure(go.Bar(x=candles["Date"], y=candles["Volume"], name="Volume"))
                        volume_fig.update_layout(title="Volume", template="plotly_dark", height=220, margin=dict(l=20,r=20,t=40,b=20), xaxis_title="Time (IST)")
                        st.plotly_chart(volume_fig, use_container_width=True, config={"displaylogo": False}, key="fyers_volume_plot")
                    if current_ltp is not None:
                        fig.add_hline(y=float(current_ltp), line_dash="dot", annotation_text=f"Live FYERS LTP: {float(current_ltp):,.2f}")
                    fig.update_layout(title=f"{chart_symbol_loaded} · FYERS candles", xaxis_title="Time (IST)", yaxis_title="Price", xaxis_rangeslider_visible=False, height=650, hovermode="x unified", margin=dict(l=20, r=20, t=60, b=20))
                    fig.update_xaxes(type="date", tickformat="%d %b %H:%M" if st.session_state.get("fyers_chart_resolution_loaded") != "1D" else "%d %b %Y", showspikes=True, spikemode="across", spikesnap="cursor")
                    fig.update_yaxes(fixedrange=False)
                    st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True, "displaylogo": False, "modeBarButtonsToAdd": ["drawline", "drawopenpath", "drawclosedpath", "drawcircle", "drawrect", "eraseshape"]}, key="fyers_live_chart_plot")
                    st.caption(f"Historical candles: FYERS · fetched: {st.session_state.get('fyers_chart_loaded_at', 'unknown')} · live quote overlay refreshes every 2 seconds while connected.")
                    delta = candles["Close"].diff()
                    gain = delta.clip(lower=0).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
                    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
                    rs = gain / loss.replace(0, np.nan)
                    rsi = (100 - (100 / (1 + rs))).fillna(50)
                    if show_rsi:
                        rsi_fig = go.Figure(go.Scatter(x=candles["Date"], y=rsi, name="RSI 14", line=dict(width=1.5)))
                        rsi_fig.add_hline(y=70, line_dash="dash")
                        rsi_fig.add_hline(y=30, line_dash="dash")
                        rsi_fig.update_layout(title="RSI (14)", template="plotly_dark", height=240, margin=dict(l=20,r=20,t=45,b=20), yaxis=dict(range=[0,100]), xaxis_title="Time (IST)")
                        st.plotly_chart(rsi_fig, use_container_width=True, config={"scrollZoom": True, "displaylogo": False}, key="fyers_live_rsi_plot")
                    if show_stoch_rsi:
                        rsi_low = rsi.rolling(14, min_periods=14).min()
                        rsi_high = rsi.rolling(14, min_periods=14).max()
                        stoch = ((rsi-rsi_low)/(rsi_high-rsi_low).replace(0, np.nan))*100
                        stoch_k = stoch.rolling(3, min_periods=3).mean()
                        stoch_d = stoch_k.rolling(3, min_periods=3).mean()
                        sf = go.Figure()
                        sf.add_trace(go.Scatter(x=candles["Date"], y=stoch_k, name="Stoch RSI %K"))
                        sf.add_trace(go.Scatter(x=candles["Date"], y=stoch_d, name="Stoch RSI %D"))
                        sf.add_hline(y=80, line_dash="dash")
                        sf.add_hline(y=20, line_dash="dash")
                        sf.update_layout(title="Stochastic RSI (14,14,3,3)", template="plotly_dark", height=240, margin=dict(l=20,r=20,t=45,b=20), yaxis=dict(range=[0,100]), xaxis_title="Time (IST)")
                        st.plotly_chart(sf, use_container_width=True, config={"scrollZoom": True, "displaylogo": False}, key="fyers_live_stoch_rsi_plot")

        _fyers_live_chart_fragment()

        if st.session_state.get("fyers_stream_active") and st.session_state.get("fyers_stream_symbol") != live_symbol:
            st.warning("The active stream is subscribed to a different symbol. Stop it, then start again for the selected chart symbol.")
        st.info("FYERS WebSocket updates the live quote overlay and redraws the displayed chart every 2 seconds. Historical candles remain the last fetched candle set; the latest price line is live. Stream is read-only and does not place orders.")
with st.expander("Important: Public Yahoo Finance and market-data limitations", expanded=False):
    st.warning("Yahoo Finance data accessed through yfinance is best-effort and intended for personal use. Intraday values may be delayed, stale or unavailable; this is not a guaranteed real-time trading feed.")
    st.write("TradingView embeds, where used elsewhere in the app, have their own exchange coverage and delay rules. Always check the source candle timestamp and verify important prices with your broker or official exchange before trading.")
    st.markdown("[Yahoo Finance data coverage and delays](https://help.yahoo.com/kb/SLN2310.html) · [TradingView widgets and data coverage](https://www.tradingview.com/widget/)")






if refresh:
    st.cache_data.clear()
