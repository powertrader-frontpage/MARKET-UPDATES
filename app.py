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
from urllib.parse import urlencode

st.set_page_config(page_title="Market Intelligence Terminal", page_icon="📈", layout="wide")
st.markdown("""
<style>
.block-container{padding-top:1rem;padding-bottom:2rem}
[data-testid="stMetric"]{background:rgba(100,120,145,.08);border:1px solid rgba(120,140,160,.25);padding:12px;border-radius:12px}
.small-note{font-size:.85rem;opacity:.8}\n[data-testid='stDataFrame']{max-width:100%}\n@media(max-width:700px){.block-container{padding-left:.7rem;padding-right:.7rem}[data-testid='stMetricValue']{font-size:1.35rem}h1{font-size:1.7rem}h2{font-size:1.35rem}}
</style>
""", unsafe_allow_html=True)

st.title("📈 Market Intelligence Terminal")
st.caption("Free hybrid edition · No broker API · Source-labelled data · Missing data stays missing")
st.warning("Important: public Yahoo Finance data is best-effort and intended for personal use; intraday values may be delayed, stale or unavailable. TradingView embeds have their own exchange coverage and delay rules. This is not a guaranteed real-time trading feed.")

INDEXES = {
    "NIFTY 50": "^NSEI",
    "BANK NIFTY": "^NSEBANK",
    "India VIX": "^INDIAVIX",
    "Brent Crude": "BZ=F",
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
            return payload["access_token"], None
        return None, str(payload.get("message", "FYERS did not return an access token."))
    except Exception as exc:
        return None, f"FYERS authentication request failed: {exc.__class__.__name__}"


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

with st.sidebar:
    st.divider()
    st.subheader("FYERS live data")
    fyers_config = fyers_credentials()
    if not fyers_config:
        st.warning("Add FYERS_APP_ID, FYERS_APP_SECRET and FYERS_REDIRECT_URI in Streamlit Cloud Secrets to enable the connection.")
    else:
        fyers_app_id, fyers_app_secret, fyers_redirect_uri = fyers_config
        if st.session_state.get("fyers_access_token"):
            st.success("FYERS session connected for this app session.")
            if st.button("Disconnect FYERS session", use_container_width=True):
                st.session_state.pop("fyers_access_token", None)
                st.session_state.pop("fyers_connected_at", None)
                st.rerun()
        else:
            st.link_button(
                "Connect FYERS account",
                fyers_login_url(fyers_app_id, fyers_redirect_uri, fyers_make_state(fyers_app_secret)),
                use_container_width=True,
            )
            st.caption("Read-only market data. You may need to authorize again when the daily access token expires.")

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
            token, token_error = fyers_exchange_auth_code(fyers_config[0], fyers_config[1], auth_code)
        if token:
            st.session_state["fyers_access_token"] = token
            st.session_state["fyers_connected_at"] = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()
            st.query_params.clear()
            st.success("FYERS connected. Access token is held in this Streamlit session only.")
            st.rerun()
        else:
            st.error(f"FYERS authorization was not completed: {token_error}")
            st.query_params.clear()
    else:
        st.error("FYERS returned without an authorization code. Please retry the connection.")
        st.query_params.clear()

if "fyers_access_token" in st.session_state and fyers_credentials():
    fyers_app_id = fyers_credentials()[0]
    st.subheader("FYERS live market snapshot")
    st.caption("Source: FYERS Quotes API · values are shown only when returned by FYERS; this is a quote snapshot, not a continuous WebSocket stream.")
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
        col_res, col_period = st.columns(2)
        with col_res:
            chart_resolution = st.selectbox("Candle interval", ["1", "5", "15", "30", "60", "1D"], index=1, key="fyers_chart_resolution")
        with col_period:
            chart_days_label = st.selectbox("History window", ["5 days", "1 month", "3 months", "6 months", "1 year"], index=2, key="fyers_chart_days")
        chart_days = {"5 days": 5, "1 month": 31, "3 months": 92, "6 months": 183, "1 year": 365}[chart_days_label]
        if chart_resolution in {"1", "5"} and chart_days > 5:
            st.warning("For 1-minute and 5-minute candles, the chart automatically limits history to the most recent 5 calendar days to avoid oversized or unsupported requests.")
            chart_days = 5
        st.markdown("**Chart tools**")
        ind1, ind2, ind3, ind4 = st.columns(4)
        with ind1:
            show_ema9 = st.checkbox("EMA 9", value=True, key="fyers_ema9")
        with ind2:
            show_ema21 = st.checkbox("EMA 21", value=True, key="fyers_ema21")
        with ind3:
            show_sma50 = st.checkbox("SMA 50", value=False, key="fyers_sma50")
        with ind4:
            show_bbands = st.checkbox("Bollinger Bands", value=False, key="fyers_bbands")
        show_rsi = st.checkbox("Show RSI (14) panel", value=False, key="fyers_rsi")
        st.caption("After the chart loads, use the Plotly toolbar to draw lines, rectangles, circles, and annotations. These drawings are temporary and reset when the chart is rebuilt.")
        if st.button("Load / refresh FYERS chart", key="fyers_load_chart"):
            chart_symbol = symbol_labels[selected_label]
            with st.spinner(f"Fetching FYERS candles for {chart_symbol}..."):
                candles, candle_error = fyers_history(fyers_credentials()[0], st.session_state["fyers_access_token"], chart_symbol, chart_resolution, chart_days)
            if candle_error:
                st.warning(f"Chart data unavailable: {candle_error}")
            else:
                candles = candles.copy()
                candles["Date"] = pd.to_datetime(candles["Date"], errors="coerce")
                candles = candles.dropna(subset=["Date", "Open", "High", "Low", "Close"]).sort_values("Date")
                if candles.empty:
                    st.warning("FYERS returned no valid OHLC candles for this selection.")
                else:
                    st.session_state["fyers_chart_data"] = candles.to_json(date_format="iso", orient="split")
                    st.session_state["fyers_chart_symbol_loaded"] = chart_symbol
                    st.session_state["fyers_chart_resolution_loaded"] = chart_resolution
                    st.session_state["fyers_chart_loaded_at"] = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S IST")
                    st.session_state.pop("fyers_chart_error", None)

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
                    candles["EMA 9"] = candles["Close"].ewm(span=9, adjust=False).mean()
                    candles["EMA 21"] = candles["Close"].ewm(span=21, adjust=False).mean()
                    candles["SMA 50"] = candles["Close"].rolling(50, min_periods=1).mean()
                    fig = go.Figure(data=[go.Candlestick(x=candles["Date"], open=candles["Open"], high=candles["High"], low=candles["Low"], close=candles["Close"], name=chart_symbol_loaded)])
                    if show_ema9:
                        fig.add_trace(go.Scatter(x=candles["Date"], y=candles["EMA 9"], name="EMA 9", mode="lines", line=dict(width=1.4)))
                    if show_ema21:
                        fig.add_trace(go.Scatter(x=candles["Date"], y=candles["EMA 21"], name="EMA 21", mode="lines", line=dict(width=1.4)))
                    if show_sma50:
                        fig.add_trace(go.Scatter(x=candles["Date"], y=candles["SMA 50"], name="SMA 50", mode="lines", line=dict(width=1.4)))
                    if show_bbands:
                        bb_mid = candles["Close"].rolling(20, min_periods=1).mean()
                        bb_std = candles["Close"].rolling(20, min_periods=2).std().fillna(0)
                        fig.add_trace(go.Scatter(x=candles["Date"], y=bb_mid + 2 * bb_std, name="BB upper", line=dict(width=1, dash="dot")))
                        fig.add_trace(go.Scatter(x=candles["Date"], y=bb_mid - 2 * bb_std, name="BB lower", line=dict(width=1, dash="dot"), fill="tonexty", fillcolor="rgba(100,120,150,0.08)"))
                    if current_ltp is not None:
                        fig.add_hline(y=float(current_ltp), line_dash="dot", annotation_text=f"Live FYERS LTP: {float(current_ltp):,.2f}")
                    fig.update_layout(title=f"{chart_symbol_loaded} · FYERS candles", xaxis_title="Time (IST)", yaxis_title="Price", xaxis_rangeslider_visible=False, height=650, hovermode="x unified", margin=dict(l=20, r=20, t=60, b=20))
                    fig.update_xaxes(type="date", tickformat="%d %b %H:%M" if st.session_state.get("fyers_chart_resolution_loaded") != "1D" else "%d %b %Y", showspikes=True, spikemode="across", spikesnap="cursor")
                    fig.update_yaxes(fixedrange=False)
                    st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True, "displaylogo": False, "modeBarButtonsToAdd": ["drawline", "drawopenpath", "drawclosedpath", "drawcircle", "drawrect", "eraseshape"]}, key="fyers_live_chart_plot")
                    st.caption(f"Historical candles: FYERS · fetched: {st.session_state.get('fyers_chart_loaded_at', 'unknown')} · live quote overlay refreshes every 2 seconds while connected.")
                    if show_rsi:
                        delta = candles["Close"].diff()
                        gain = delta.clip(lower=0).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
                        loss = (-delta.clip(upper=0)).ewm(alpha=1/14, min_periods=14, adjust=False).mean()
                        rs = gain / loss.replace(0, np.nan)
                        rsi = (100 - (100 / (1 + rs))).fillna(50)
                        rsi_fig = go.Figure()
                        rsi_fig.add_trace(go.Scatter(x=candles["Date"], y=rsi, name="RSI 14", line=dict(width=1.5)))
                        rsi_fig.add_hline(y=70, line_dash="dash")
                        rsi_fig.add_hline(y=30, line_dash="dash")
                        rsi_fig.update_layout(title="Relative Strength Index (14)", height=240, margin=dict(l=20, r=20, t=45, b=20), yaxis=dict(range=[0, 100]), xaxis_title="Time (IST)")
                        st.plotly_chart(rsi_fig, use_container_width=True, config={"scrollZoom": True, "displaylogo": False}, key="fyers_live_rsi_plot")

        _fyers_live_chart_fragment()

        if st.session_state.get("fyers_stream_active") and st.session_state.get("fyers_stream_symbol") != live_symbol:
            st.warning("The active stream is subscribed to a different symbol. Stop it, then start again for the selected chart symbol.")
        st.info("FYERS WebSocket updates the live quote overlay and redraws the displayed chart every 2 seconds. Historical candles remain the last fetched candle set; the latest price line is live. Stream is read-only and does not place orders.")
if refresh:
    st.cache_data.clear()

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
            if title and link:
                rows.append({"Published (RSS)":pub,"Headline":title,"Publisher":publisher,"Source link":link})
        if not rows:
            return pd.DataFrame(), "RSS returned no headline rows."
        return pd.DataFrame(rows), None
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

def advanced_chart(symbol: str, height: int = 550):
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
st.caption(f"Dashboard refreshed/viewed: {now_ist:%d %b %Y, %H:%M:%S IST} · Yahoo Finance via yfinance for calculations; TradingView for embedded charts. This timestamp is the app refresh time, not an exchange timestamp.")

# Headline metrics from the latest returned historical bar (not described as real-time).
cols = st.columns(4)
for col, name in zip(cols, ["NIFTY 50", "BANK NIFTY", "India VIX", "Brent Crude"]):
    df = market_data[name]
    snap = quote_snapshot(df)
    if snap:
        last, change, pct = snap
        col.metric(name, fmt(last["Close"]), f"{fmt(change)} ({fmt(pct)}%)")
        col.caption(f"Last source bar: {last['Date']:%d %b %Y, %H:%M IST}")
    else:
        col.metric(name, "Unavailable")
        col.caption(market_errors[name] or "No data returned")

st.caption("Headline values are the latest returned daily bar, not guaranteed live quotes. For current market viewing, use the embedded TradingView widgets and check the exchange/data-status label there.")

tabs = st.tabs(["Overview", "Live charts", "Technical analysis", "Sector rotation", "Global markets", "FII/DII & news", "Derivatives", "History & sources"])
with tabs[0]:
    st.subheader("Market regime & trend strength")
    overview_regime_name = st.selectbox("Regime instrument", list(INDEXES.keys()), index=0, key="overview_regime_symbol")
    overview_regime_df = market_data[overview_regime_name]
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
            overview_vol = overview_rc["Return %"].rolling(20).std().iloc[-1] * np.sqrt(252)
            regime_cols = st.columns(4)
            regime_cols[0].metric("Regime framework", overview_regime)
            regime_cols[1].metric("ADX 14", fmt(overview_adx_value))
            regime_cols[2].metric("RSI 14", fmt(overview_rsi_value))
            regime_cols[3].metric("Realized vol 20d annualized", f"{overview_vol:.2%}" if pd.notna(overview_vol) else "Unavailable")
            st.caption(f"Calculated from {overview_regime_name} historical OHLC data · last source bar: {overview_last['Date']:%d %b %Y, %H:%M IST} · not a live signal.")
    st.divider()
    st.subheader("Market snapshot")
    rows = []
    for name, df in market_data.items():
        snap = quote_snapshot(df)
        if snap:
            last, change, pct = snap
            rows.append({"Instrument": name, "Ticker": INDEXES[name], "Latest returned close": last["Close"], "Change vs previous bar": change, "Change %": pct, "Source bar timestamp (IST)": last["Date"].strftime("%Y-%m-%d %H:%M %Z"), "Source": "Yahoo Finance / yfinance"})
        else:
            rows.append({"Instrument": name, "Ticker": INDEXES[name], "Latest returned close": np.nan, "Change vs previous bar": np.nan, "Change %": np.nan, "Source bar timestamp (IST)": "Unavailable", "Source": market_errors[name] or "No data"})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.markdown("#### Global market and news widgets")
    tv_widget("market-overview", {"colorTheme": "dark", "dateRange": "1D", "showChart": True, "locale": "en", "width": "100%", "height": 420, "isTransparent": False, "showSymbolLogo": True, "showFloatingTooltip": False, "tabs": [{"title": "Indices", "symbols": [{"s": "NSE:NIFTY"}, {"s": "NSE:BANKNIFTY"}, {"s": "TVC:VIX"}, {"s": "TVC:UKOIL"}, {"s": "NASDAQ:NDX"}, {"s": "SP:SPX"}]}]}, 430)
with tabs[1]:
    st.subheader("Embedded interactive charts")
    chart_choice = st.selectbox("Choose chart", ["NIFTY 50", "BANK NIFTY", "INFY", "WIPRO", "Brent Crude", "India VIX"])
    tv_symbols = {"NIFTY 50": "NSE:NIFTY", "BANK NIFTY": "NSE:BANKNIFTY", "INFY": "NSE:INFY", "WIPRO": "NSE:WIPRO", "Brent Crude": "TVC:UKOIL", "India VIX": "NSE:INDIAVIX"}
    advanced_chart(tv_symbols[chart_choice], 600)
    st.caption("TradingView supplies the chart data independently. Availability and delay vary by symbol/exchange; this app does not override those settings.")
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**NIFTY 50**")
        advanced_chart("NSE:NIFTY", 440)
    with c2:
        st.markdown("**BANK NIFTY**")
        advanced_chart("NSE:BANKNIFTY", 440)

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
        rsi_fig = go.Figure(go.Scatter(x=calc["Date"], y=calc["RSI 14"], name="RSI 14"))
        rsi_fig.add_hline(y=70, line_dash="dash")
        rsi_fig.add_hline(y=30, line_dash="dash")
        rsi_fig.update_layout(template="plotly_dark", height=260, yaxis={"range": [0,100]}, margin={"l":10,"r":10,"t":20,"b":10})
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
        fig = px.bar(sec, x="Sector", y="Relative return vs NIFTY (pp)", color="Relative return vs NIFTY (pp)", template="plotly_dark", title="Relative sector performance over selected lookback")
        fig.update_layout(height=440)
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.warning("No sector index series were returned by the public source. No values have been substituted.")
    st.markdown("#### TradingView stock heatmap (global coverage; exchange coverage may vary)")
    tv_widget("stock-heatmap", {"dataSource": "SPX500", "blockSize": "market_cap_basic", "blockColor": "change", "grouping": "sector", "locale": "en", "colorTheme": "dark", "hasTopBar": False, "isDataSetEnabled": False, "isZoomEnabled": True, "hasSymbolTooltip": True, "isMonoSize": False, "width": "100%", "height": 520}, 530)

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

st.divider()
st.caption("For research and monitoring only. Verify exchange timestamps and official sources before trading. Public widgets and public-source data may be delayed, incomplete or unavailable; this dashboard is not an execution system or investment advice.")

# Expanded research modules (13–22). Calculations use public-source data; personal journal data remains user-controlled.
extra_tabs = st.tabs([
    "13 Regime", "14 Options status", "15 Risk", "16 Backtest", "17 Breadth",
    "18 Events", "19 Trade journal", "20 Data quality", "21 Cross-asset", "22 Signals"
])

with extra_tabs[0]:
    st.subheader("13 · Market regime & trend strength")
    regime_name = st.selectbox("Instrument", list(INDEXES.keys()), key="regime_symbol")
    rdf = market_data[regime_name]
    if rdf.empty or len(rdf) < 30:
        st.warning("Insufficient returned OHLC history to assess regime. No regime label generated.")
    else:
        rc = indicators(rdf).copy()
        if {"High", "Low", "Close"}.issubset(rc.columns):
            prevc = rc["Close"].shift(1)
            tr = pd.concat([(rc["High"]-rc["Low"]), (rc["High"]-prevc).abs(), (rc["Low"]-prevc).abs()], axis=1).max(axis=1)
            up = rc["High"].diff(); down = -rc["Low"].diff()
            plus_dm = up.where((up > down) & (up > 0), 0.0)
            minus_dm = down.where((down > up) & (down > 0), 0.0)
            atr14 = tr.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
            pdi = 100 * plus_dm.ewm(alpha=1/14, min_periods=14, adjust=False).mean() / atr14.replace(0, np.nan)
            mdi = 100 * minus_dm.ewm(alpha=1/14, min_periods=14, adjust=False).mean() / atr14.replace(0, np.nan)
            dx = 100 * (pdi-mdi).abs() / (pdi+mdi).replace(0, np.nan)
            rc["ADX 14"] = dx.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
            rc["Realized volatility 20d %"] = rc["Return %"].rolling(20).std() * np.sqrt(252)
            last = rc.iloc[-1]
            adx = last.get("ADX 14", np.nan); rsi = last.get("RSI 14", np.nan)
            close = last["Close"]; ema20 = last.get("EMA 20", np.nan); ema50 = last.get("EMA 50", np.nan)
            regime = "Insufficient indicator history"
            if pd.notna(adx) and pd.notna(ema20) and pd.notna(ema50):
                if adx >= 25 and close > ema20 > ema50: regime = "Trending bullish"
                elif adx >= 25 and close < ema20 < ema50: regime = "Trending bearish"
                elif adx < 20: regime = "Range / weak trend"
                else: regime = "Transition / mixed"
            a,b,c,d = st.columns(4)
            a.metric("Regime framework", regime)
            b.metric("ADX 14", fmt(adx))
            c.metric("RSI 14", fmt(rsi))
            d.metric("Realized vol 20d annualized", fmt(last.get("Realized volatility 20d %")) + "%")
            st.caption(f"Based on {len(rc)} source bars; last source candle {last['Date']:%d %b %Y %H:%M IST}. Thresholds are heuristic, not predictions.")
            st.dataframe(rc[["Date","Close","EMA 20","EMA 50","RSI 14","ADX 14","ATR 14","Realized volatility 20d %"]].tail(30), use_container_width=True, hide_index=True)

with extra_tabs[1]:
    st.subheader("14 · Options analytics")
    st.write("PCR, max pain, open interest and implied volatility require a genuine option-chain snapshot.")
    st.selectbox("Underlying",["NIFTY","BANKNIFTY","FINNIFTY"],key="auto_opt_underlying")
    st.warning("No authorized free machine-readable NSE option-chain API is configured. Scripted scraping is not used, and no option values are fabricated.")
    st.markdown("[Open official NSE option chain](https://www.nseindia.com/option-chain) · [NSE derivatives reports](https://www.nseindia.com/all-reports-derivatives)")
    st.caption("Price history and technical modules continue to work automatically. Derivatives analytics require an explicitly permitted source; no broker API is used.")

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
        st.dataframe(last[["Date","Symbol","Industry","Close","SMA20","SMA50","SMA200","Above SMA20","Above SMA50","Above SMA200"]].sort_values("Symbol"),use_container_width=True,hide_index=True)
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
        st.dataframe(headlines,use_container_width=True,hide_index=True,column_config={"Source link":st.column_config.LinkColumn("Source link")})
        st.download_button("Download event-risk headlines",headlines.to_csv(index=False).encode("utf-8"),"event_risk_headlines.csv","text/csv")
    st.markdown("**Official release calendars and primary sources**")
    st.markdown("- [RBI](https://www.rbi.org.in/) · [MOSPI](https://mospi.gov.in/) · [Federal Reserve](https://www.federalreserve.gov/monetarypolicy.htm) · [US BLS release schedule](https://www.bls.gov/schedule/news_release/) · [EIA](https://www.eia.gov/petroleum/supply/weekly/)")
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
research_tabs = st.tabs(["23 Industry intelligence", "24 Delivery & money-flow proxies", "25 Daily briefing"])

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
advanced_tabs = st.tabs([
    "26 Macro & event log",
    "27 Breadth & stock scanner",
    "28 Derivatives lab",
    "29 Stock deep-dive",
    "30 Strategy backtest",
    "31 Data quality"
])

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
    st.warning("No authorized free machine-readable NSE derivatives API is configured. Option-chain PCR, max pain, IV, futures OI and participant positioning are therefore unavailable automatically.")
    st.markdown("[Official NSE option chain](https://www.nseindia.com/option-chain) · [NSE derivatives reports](https://www.nseindia.com/all-reports-derivatives)")
    st.caption("Market prices and technical indicators continue to load automatically. Derivatives data requires an explicitly permitted source; no broker API is used.")

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
