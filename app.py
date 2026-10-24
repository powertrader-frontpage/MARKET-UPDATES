from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
import json
import html

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import requests
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf

st.set_page_config(page_title="Market Intelligence Terminal", page_icon="📈", layout="wide")
st.markdown("""
<style>
.block-container{padding-top:1rem;padding-bottom:2rem}
[data-testid="stMetric"]{background:rgba(100,120,145,.08);border:1px solid rgba(120,140,160,.25);padding:12px;border-radius:12px}
.small-note{font-size:.85rem;opacity:.8}
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
    st.markdown("#### Market news feed (provided by TradingView)")
    tv_widget("news", {"feedMode": "market", "colorTheme": "dark", "isTransparent": False, "displayMode": "regular", "width": "100%", "height": 450, "locale": "en"}, 460)

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
    st.subheader("Technical analysis from returned daily OHLC data")
    chosen = st.selectbox("Instrument for technicals", list(INDEXES.keys()), index=0, key="tech_symbol")
    source_df = market_data[chosen]
    if source_df.empty:
        st.error(f"No technicals calculated: {market_errors[chosen] or 'no source rows'}")
    else:
        calc = indicators(source_df)
        last = calc.iloc[-1]
        metric_cols = st.columns(5)
        for col, label in zip(metric_cols, ["Close", "RSI 14", "SMA 20", "SMA 50", "ADR 20"]):
            val = last.get(label, np.nan)
            col.metric(label, fmt(val))
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=calc["Date"], y=calc["Close"], name="Close", line={"width": 2}))
        for ma in ["SMA 20", "SMA 50", "EMA 20", "EMA 50"]:
            if ma in calc and calc[ma].notna().any(): fig.add_trace(go.Scatter(x=calc["Date"], y=calc[ma], name=ma, line={"width": 1.3}))
        fig.update_layout(template="plotly_dark", height=470, margin={"l": 10, "r": 10, "t": 25, "b": 10}, xaxis_title="Source candle time", yaxis_title="Price")
        st.plotly_chart(fig, use_container_width=True)
        rsi_fig = go.Figure(go.Scatter(x=calc["Date"], y=calc["RSI 14"], name="RSI 14"))
        rsi_fig.add_hline(y=70, line_dash="dash"); rsi_fig.add_hline(y=30, line_dash="dash")
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
        if pivot is not None: st.dataframe(pivot.style.format({"Value":"{:,.2f}"}), use_container_width=True, hide_index=True)
        else: st.info("Pivot levels unavailable: source data lacks sufficient OHLC rows.")
        st.caption(f"Calculations use {len(calc):,} returned daily bars. Last source candle: {last['Date']:%d %b %Y, %H:%M IST}. Indicators are only as reliable as the source data and its coverage.")

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

# Expanded research modules (13–22). All calculated values come from fetched or uploaded data.
extra_tabs = st.tabs([
    "13 Regime", "14 Options CSV", "15 Risk", "16 Backtest", "17 Breadth",
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
    st.subheader("14 · Options analytics from an uploaded option-chain CSV")
    st.write("Upload a CSV from a source you are permitted to use. Column names differ by provider, so map them below. Nothing is fetched or inferred if the file is absent.")
    opt_file = st.file_uploader("Option-chain CSV", type=["csv"], key="opt_csv")
    if opt_file is not None:
        try:
            odf = pd.read_csv(opt_file)
            st.caption("Detected columns: " + ", ".join(map(str, odf.columns)))
            strike_col = st.selectbox("Strike column", list(odf.columns), key="opt_strike")
            ce_oi_col = st.selectbox("Call OI column (or choose blank if absent)", ["(not available)"] + list(odf.columns), key="opt_ceoi")
            pe_oi_col = st.selectbox("Put OI column (or choose blank if absent)", ["(not available)"] + list(odf.columns), key="opt_peoi")
            ce_ltp_col = st.selectbox("Call LTP column (optional)", ["(not available)"] + list(odf.columns), key="opt_celtp")
            pe_ltp_col = st.selectbox("Put LTP column (optional)", ["(not available)"] + list(odf.columns), key="opt_peltp")
            work = pd.DataFrame({"Strike": pd.to_numeric(odf[strike_col], errors="coerce")})
            for label, col in [("Call OI",ce_oi_col),("Put OI",pe_oi_col),("Call LTP",ce_ltp_col),("Put LTP",pe_ltp_col)]:
                work[label] = pd.to_numeric(odf[col], errors="coerce") if col != "(not available)" else np.nan
            work = work.dropna(subset=["Strike"])
            if work.empty: st.warning("No valid strike values found.")
            else:
                if work["Call OI"].notna().any() and work["Put OI"].notna().any():
                    call_total=work["Call OI"].sum(min_count=1); put_total=work["Put OI"].sum(min_count=1)
                    st.metric("PCR (uploaded OI totals)", fmt(put_total/call_total) if pd.notna(call_total) and call_total else "Unavailable")
                    st.caption("PCR is calculated from the uploaded file only. Confirm whether OI is current or change-in-OI and note its timestamp.")
                    st.dataframe(work.sort_values("Strike"), use_container_width=True, hide_index=True)
                else: st.info("PCR unavailable: map both Call OI and Put OI columns.")
                if work["Call LTP"].notna().any() and work["Put LTP"].notna().any():
                    # Expiry max-pain proxy: payout sum at each candidate settlement strike.
                    candidates=work["Strike"].dropna().unique()
                    payouts=[]
                    for settlement in candidates:
                        call_pay=((settlement-work["Strike"]).clip(lower=0)*work["Call OI"].fillna(0)).sum()
                        put_pay=((work["Strike"]-settlement).clip(lower=0)*work["Put OI"].fillna(0)).sum()
                        payouts.append((settlement, call_pay+put_pay))
                    if payouts: st.metric("Max-pain estimate (uploaded strikes/OI)", fmt(min(payouts, key=lambda x:x[1])[0]))
                    st.caption("Max pain is a simple calculation from supplied strikes/OI, not a forecast. Missing OI can invalidate it.")
        except Exception as exc: st.error(f"Could not read uploaded file: {type(exc).__name__}: {exc}")
    else:
        st.info("Options analytics are inactive until a real option-chain CSV is uploaded. Live PCR, Greeks and IV are not available in this no-API edition.")
    st.markdown("[Official NSE option chain](https://www.nseindia.com/option-chain) · [NSE derivatives reports](https://www.nseindia.com/all-reports-derivatives)")

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
    st.subheader("17 · Market breadth")
    st.write("Upload a genuine constituent-price CSV to calculate breadth. Expected format: Date, Symbol, Close; optional Open, High, Low, Volume. Use daily rows per symbol.")
    breadth_file=st.file_uploader("Constituent history CSV",type=["csv"],key="breadth_csv")
    if breadth_file is not None:
        try:
            wide=pd.read_csv(breadth_file); wide.columns=[str(c).strip() for c in wide.columns]
            needed={"Date","Symbol","Close"}
            if not needed.issubset(wide.columns): st.error("Required columns missing: Date, Symbol, Close.")
            else:
                wide["Date"]=pd.to_datetime(wide["Date"],errors="coerce"); wide["Close"]=pd.to_numeric(wide["Close"],errors="coerce"); wide=wide.dropna(subset=["Date","Symbol","Close"]).sort_values("Date")
                wide["SMA20"]=wide.groupby("Symbol")["Close"].transform(lambda x:x.rolling(20).mean()); wide["SMA50"]=wide.groupby("Symbol")["Close"].transform(lambda x:x.rolling(50).mean()); wide["SMA200"]=wide.groupby("Symbol")["Close"].transform(lambda x:x.rolling(200).mean())
                latest_date=wide["Date"].max(); last=wide[wide["Date"]==latest_date]
                x,y,z=st.columns(3)
                for col,ma,label in [(x,"SMA20","Above 20-day SMA"),(y,"SMA50","Above 50-day SMA"),(z,"SMA200","Above 200-day SMA")]:
                    valid=last.dropna(subset=[ma]); col.metric(label, f"{(valid['Close']>valid[ma]).mean()*100:.1f}%" if not valid.empty else "Unavailable", f"{len(valid)} symbols")
                st.caption(f"Uploaded dataset latest date: {latest_date:%Y-%m-%d}; this is not automatically live.")
        except Exception as exc: st.error(f"Could not calculate breadth: {exc}")
    else: st.info("Breadth metrics are not shown until a constituent dataset is uploaded. No constituent values are guessed.")

with extra_tabs[5]:
    st.subheader("18 · Economic events and gap-risk monitor")
    st.write("Use this editable checklist to record upcoming events from official calendars. It is user-entered, not a live calendar feed.")
    event_template=pd.DataFrame(columns=["Date/time (IST)","Event","Source URL","Expected risk","Completed/checked"])
    event_file=st.file_uploader("Optional event calendar CSV",type=["csv"],key="event_csv")
    events=pd.read_csv(event_file) if event_file is not None else event_template
    edited=st.data_editor(events,use_container_width=True,num_rows="dynamic",key="event_editor")
    st.download_button("Download event calendar",edited.to_csv(index=False).encode(),file_name="market_event_calendar.csv",mime="text/csv")
    st.markdown("Sources: [RBI](https://www.rbi.org.in/) · [MOSPI](https://mospi.gov.in/) · [US Federal Reserve](https://www.federalreserve.gov/monetarypolicy.htm) · [US BLS](https://www.bls.gov/schedule/news_release/) · [EIA](https://www.eia.gov/petroleum/supply/weekly/)")
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
