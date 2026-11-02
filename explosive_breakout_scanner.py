from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from io import StringIO
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf


@st.cache_data(ttl=21600, show_spinner=False)
def _breakout_universe():
    sources = {
        "Mid-cap": "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv",
        "Small-cap": "https://www.niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv",
    }
    rows, errors = [], []
    for segment, url in sources.items():
        try:
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            response.raise_for_status()
            frame = pd.read_csv(StringIO(response.text))
            symbol_col = next((c for c in frame.columns if str(c).strip().lower() == "symbol"), None)
            name_col = next((c for c in frame.columns if str(c).strip().lower() in ("company name", "companyname")), None)
            sector_col = next((c for c in frame.columns if str(c).strip().lower() in ("industry", "sector")), None)
            if not symbol_col:
                errors.append(f"{segment}: Symbol column missing")
                continue
            for _, row in frame.iterrows():
                symbol = str(row.get(symbol_col, "")).strip()
                if symbol and symbol.lower() != "nan":
                    rows.append({
                        "Symbol": symbol, "Company": str(row.get(name_col, symbol)).strip() if name_col else symbol,
                        "Segment": segment, "Sector": str(row.get(sector_col, "—")).strip() if sector_col else "—",
                        "Ticker": symbol + ".NS",
                    })
        except Exception as exc:
            errors.append(f"{segment} constituent list unavailable ({exc.__class__.__name__})")
    if not rows:
        return pd.DataFrame(), "; ".join(errors) or "No constituents returned"
    return pd.DataFrame(rows).drop_duplicates("Ticker").reset_index(drop=True), "; ".join(errors) if errors else None


@st.cache_data(ttl=21600, show_spinner=False)
def _breakout_prices(tickers: tuple[str, ...]):
    frames = []
    for start in range(0, len(tickers), 80):
        batch = list(tickers[start:start + 80])
        try:
            raw = yf.download(batch, period="2y", interval="1d", auto_adjust=True,
                              group_by="ticker", threads=False, progress=False, actions=False)
            if raw is None or raw.empty:
                continue
            for ticker in batch:
                try:
                    part = raw[ticker].copy() if isinstance(raw.columns, pd.MultiIndex) and ticker in raw.columns.get_level_values(0) else raw.copy()
                    if "Close" not in part.columns:
                        continue
                    part = part.reset_index()
                    date_col = "Date" if "Date" in part.columns else "Datetime"
                    if date_col not in part.columns:
                        continue
                    part = part.rename(columns={date_col: "Date"})
                    part["Date"] = pd.to_datetime(part["Date"], errors="coerce")
                    for col in ["High", "Low", "Close", "Volume"]:
                        if col in part:
                            part[col] = pd.to_numeric(part[col], errors="coerce")
                    if "High" not in part or "Low" not in part or "Volume" not in part:
                        continue
                    part = part.dropna(subset=["Date", "High", "Low", "Close"])
                    if not part.empty:
                        part["Ticker"] = ticker
                        frames.append(part[["Date", "Ticker", "High", "Low", "Close", "Volume"]])
                except Exception:
                    continue
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _news_for_symbol(symbol: str, company: str):
    query = quote_plus(f'"{company}" OR {symbol} stock India')
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0 MarketIntelligenceTerminal/1.0"}, timeout=10)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        rows = []
        for item in root.findall(".//item")[:8]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            source = item.find("source")
            publisher = (source.text or "").strip() if source is not None else "Google News"
            if title and link:
                rows.append({"Headline": title, "Published": pub, "Publisher": publisher, "Source link": link})
        return rows
    except Exception:
        return []


@st.cache_data(ttl=21600, show_spinner=False)
def _breakout_news(shortlist: tuple[tuple[str, str], ...]):
    rows = []
    # News enrichment is deliberately limited to the highest-ranked technical candidates.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(_news_for_symbol, symbol, company): (symbol, company) for symbol, company in shortlist}
        for future in as_completed(futures):
            symbol, company = futures[future]
            try:
                for item in future.result():
                    rows.append({"Symbol": symbol, "Company": company, **item})
            except Exception:
                continue
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["_date"] = pd.to_datetime(frame["Published"], errors="coerce", utc=True)
    return frame.sort_values("_date", ascending=False, na_position="last").drop(columns="_date").reset_index(drop=True)


def _completed_weekly(group: pd.DataFrame):
    data = group.sort_values("Date").set_index("Date")
    weekly = pd.DataFrame({
        "Close": data["Close"].resample("W-FRI").last(),
        "High": data["High"].resample("W-FRI").max(),
        "Low": data["Low"].resample("W-FRI").min(),
        "Volume": data["Volume"].resample("W-FRI").sum(min_count=1),
    }).dropna(subset=["Close", "High", "Low"])
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    current_friday = now.date() + timedelta(days=(4 - now.weekday()) % 7)
    if len(weekly) and weekly.index[-1].date() == current_friday and (
        now.date() < current_friday or (now.date() == current_friday and now.time() < time(15, 45))
    ):
        weekly = weekly.iloc[:-1]
    if len(weekly) < 34:
        return None
    close, high, low = weekly["Close"], weekly["High"], weekly["Low"]
    sma = close.rolling(30, min_periods=30).mean()
    prev_close = close.shift(1)
    tr = pd.concat([(high-low), (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    slope = sma - sma.shift(2)
    flat = slope.abs() < atr * 0.05
    stage = pd.Series("Stage 1", index=weekly.index, dtype="object")
    stage.loc[(close > sma) & (slope > 0) & ~flat] = "Stage 2"
    stage.loc[(close < sma) & (slope < 0) & ~flat] = "Stage 4"
    stage.loc[(close > sma) & flat] = "Stage 1"
    other = ~(((close > sma) & (slope > 0) & ~flat) | ((close < sma) & (slope < 0) & ~flat) | ((close > sma) & flat))
    stage.loc[other] = "Stage 3"
    stage.loc[sma.isna() | atr.isna()] = "Unclassified"
    volume_avg = weekly["Volume"].shift(1).rolling(10, min_periods=8).mean()
    volume_ratio = weekly["Volume"] / volume_avg.replace(0, np.nan)
    prior_base_high = high.shift(1).rolling(4, min_periods=4).max()
    return weekly, stage, sma, slope, volume_ratio, prior_base_high


def run_explosive_breakout_scan():
    universe, warning = _breakout_universe()
    if universe.empty:
        return pd.DataFrame(), pd.DataFrame(), warning
    prices = _breakout_prices(tuple(universe["Ticker"].tolist()))
    if prices.empty:
        return pd.DataFrame(), pd.DataFrame(), warning or "Price history unavailable from Yahoo Finance."
    records = []
    by_ticker = {row["Ticker"]: row for _, row in universe.iterrows()}
    for ticker, group in prices.groupby("Ticker"):
        info = by_ticker.get(ticker)
        if info is None:
            continue
        calc = _completed_weekly(group)
        if calc is None:
            continue
        weekly, stage, sma, slope, volume_ratio, resistance = calc
        if len(weekly) < 2:
            continue
        i = -1
        close = float(weekly["Close"].iloc[i])
        prior_stage = stage.iloc[-2]
        current_stage = stage.iloc[-1]
        b1_transition = prior_stage != "Stage 2" and current_stage == "Stage 2"
        breakout_level = float(resistance.iloc[-1]) if pd.notna(resistance.iloc[-1]) else np.nan
        breakout = pd.notna(breakout_level) and close > breakout_level
        vol = float(volume_ratio.iloc[-1]) if pd.notna(volume_ratio.iloc[-1]) else np.nan
        ma30 = float(sma.iloc[-1]) if pd.notna(sma.iloc[-1]) else np.nan
        slope_pct = float((sma.iloc[-1] / sma.iloc[-3] - 1) * 100) if len(sma) >= 3 and pd.notna(sma.iloc[-3]) and sma.iloc[-3] else np.nan
        gap30 = (close / ma30 - 1) * 100 if pd.notna(ma30) and ma30 else np.nan
        daily = group.sort_values("Date")
        dclose = daily["Close"].dropna()
        daily_change = (float(dclose.iloc[-1]) / float(dclose.iloc[-2]) - 1) * 100 if len(dclose) >= 2 else np.nan
        ema10 = dclose.ewm(span=10, adjust=False).mean().iloc[-1]
        ema21 = dclose.ewm(span=21, adjust=False).mean().iloc[-1]
        ema55 = dclose.ewm(span=55, adjust=False).mean().iloc[-1]
        ema_aligned = close > ema10 > ema21 > ema55
        # Relative strength is ranked against the scanned mid/small-cap universe.
        ret13 = float(weekly["Close"].iloc[-1] / weekly["Close"].iloc[-14] - 1) * 100 if len(weekly) >= 14 else np.nan
        records.append({
            "Symbol": info["Symbol"], "Company": info["Company"], "Segment": info["Segment"], "Sector": info["Sector"],
            "Price": close, "Stage": current_stage, "Previous Stage": prior_stage,
            "Fresh B1": bool(b1_transition), "4W breakout": bool(breakout), "Breakout level": breakout_level,
            "Volume vs 10W Avg": vol, "13W return %": ret13, "30W slope %": slope_pct,
            "Gap vs 30W %": gap30, "EMA 10>21>55": bool(ema_aligned), "Daily change %": daily_change,
            "Weekly date": weekly.index[-1].strftime("%Y-%m-%d"),
        })
    result = pd.DataFrame(records)
    if result.empty:
        return result, pd.DataFrame(), warning
    rank = result["13W return %"].rank(pct=True) * 100
    breakout_pts = result["4W breakout"].astype(int) * 15
    stage_pts = result["Stage"].eq("Stage 2").astype(int) * 10
    vol_pts = pd.to_numeric(result["Volume vs 10W Avg"], errors="coerce").clip(0, 2).fillna(0) / 2 * 15
    slope_pts = pd.to_numeric(result["30W slope %"], errors="coerce").clip(0, 8).fillna(0) / 8 * 10
    ema_pts = result["EMA 10>21>55"].astype(int) * 10
    rs_pts = rank.fillna(0) * 0.20
    proximity = pd.to_numeric(result["Gap vs 30W %"], errors="coerce")
    extension_pts = np.where((proximity >= 0) & (proximity <= 15), 10, 0)
    result["Technical Score"] = (breakout_pts + stage_pts + vol_pts + slope_pts + ema_pts + rs_pts + extension_pts).round(1)
    result["Setup"] = np.select(
        [result["Fresh B1"] & result["4W breakout"], result["Fresh B1"], result["Stage"].eq("Stage 2") & result["4W breakout"]],
        ["Fresh Stage 2 + breakout", "Fresh Stage 2 transition", "Stage 2 breakout follow-through"],
        default="Stage 2 watch"
    )
    fresh = result[result["Fresh B1"] & result["4W breakout"]].copy()
    follow = result[result["Stage"].eq("Stage 2") & result["4W breakout"] & ~result["Fresh B1"]].copy()
    # Upcoming list is for Stage 2 stocks that have not yet cleared the recent 4-week resistance.
    # Keep candidates close to resistance with positive relative momentum; exclude completed breakouts.
    resistance_gap = (pd.to_numeric(result["Breakout level"], errors="coerce") / pd.to_numeric(result["Price"], errors="coerce") - 1) * 100
    result["Gap to breakout %"] = resistance_gap
    upcoming = result[
        result["Stage"].eq("Stage 2")
        & ~result["4W breakout"]
        & resistance_gap.between(0, 5)
        & (pd.to_numeric(result["13W return %"], errors="coerce") > 0)
    ].copy()
    for frame in (fresh, follow, upcoming):
        frame.sort_values(["Technical Score", "Volume vs 10W Avg", "13W return %"], ascending=False, inplace=True, na_position="last")
        frame.reset_index(drop=True, inplace=True)
    return fresh, follow, warning, upcoming, result


def render_explosive_breakout_scanner():
    st.markdown("""<div style="font-size:clamp(26px,3.5vw,38px);font-weight:850;letter-spacing:-1px;margin:10px 0 8px;background:linear-gradient(90deg,#19e6a0,#29c7d6,#4aa8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">🚀 Explosive Breakout Scanner</div>""", unsafe_allow_html=True)
    st.caption("Nifty Midcap 150 + Nifty Smallcap 250 · Weekly Weinstein stages · Fresh B1 breakouts · Volume and relative-strength ranking · Recent news enrichment")
    st.info("Scans the full selected index universe. A Fresh B1 is defined here as a completed-week transition into Stage 2 that also clears the recent 4-week high. Verify the chart and exchange filings before acting.")
    c1, c2 = st.columns([1, 3])
    with c1:
        run = st.button("Scan / refresh breakouts", type="primary", key="run_explosive_breakout_scan")
    with c2:
        st.caption("Price history and constituents are cached for 6 hours. News is fetched only for the top technical candidates to limit load.")
    if run or st.session_state.get("explosive_breakout_scan_loaded", False):
        if run or "explosive_breakout_scan_data" not in st.session_state:
            with st.spinner("Scanning mid-cap and small-cap weekly price history…"):
                output = run_explosive_breakout_scan()
            # Keep the scan result and the latest-run time in this browser session.
            if len(output) == 5:
                fresh, follow, warning, upcoming, all_rows = output
                st.session_state["explosive_breakout_scan_data"] = (fresh, follow, upcoming, all_rows)
                st.session_state["explosive_breakout_scan_warning"] = warning
                st.session_state["explosive_breakout_scan_time"] = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y %H:%M IST")
                previous = st.session_state.get("explosive_breakout_previous_symbols", set())
                current = set(all_rows["Symbol"].astype(str)) if not all_rows.empty else set()
                fresh["Status"] = fresh["Symbol"].astype(str).map(lambda x: "🆕 NEW" if x not in previous and previous else ("Baseline" if not previous else "Existing"))
                st.session_state["explosive_breakout_previous_symbols"] = current
            else:
                st.session_state["explosive_breakout_scan_data"] = (pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
                st.session_state["explosive_breakout_scan_warning"] = output[2] if len(output) > 2 else "Scan unavailable"
        st.session_state["explosive_breakout_scan_loaded"] = True
        if st.session_state.get("explosive_breakout_scan_warning"):
            st.warning(st.session_state["explosive_breakout_scan_warning"])
        fresh, follow, upcoming, all_rows = st.session_state.get("explosive_breakout_scan_data", (pd.DataFrame(),)*4)
        if st.session_state.get("explosive_breakout_scan_time"):
            st.caption("Last scan: " + st.session_state["explosive_breakout_scan_time"])
        if not fresh.empty:
            st.subheader("Fresh B1 · Stage 2 breakouts")
            display = ["Status", "Technical Score", "Symbol", "Company", "Segment", "Sector", "Price", "Breakout level", "Volume vs 10W Avg", "13W return %", "30W slope %", "Gap vs 30W %", "EMA 10>21>55", "Daily change %"]
            st.dataframe(fresh[[c for c in display if c in fresh.columns]], use_container_width=True, hide_index=True)
        else:
            st.info("No fresh B1 breakout met all current conditions. Check Upcoming Breakouts and Stage 2 follow-through below.")
        st.subheader("Upcoming Breakouts · Early watch")
        if upcoming.empty:
            st.caption("No Stage 1 / early-recovery candidates currently meet the proximity and relative-strength conditions.")
        else:
            st.dataframe(upcoming[["Technical Score", "Symbol", "Company", "Segment", "Sector", "Price", "Stage", "Gap vs 30W %", "13W return %", "Volume vs 10W Avg"]], use_container_width=True, hide_index=True)
        st.subheader("Strong B2+ · Breakout follow-through")
        if follow.empty:
            st.caption("No additional Stage 2 stocks currently clear the recent 4-week high.")
        else:
            st.dataframe(follow[["Technical Score", "Symbol", "Company", "Segment", "Sector", "Price", "Breakout level", "Volume vs 10W Avg", "13W return %", "Gap vs 30W %"]], use_container_width=True, hide_index=True)
        if not fresh.empty or not follow.empty:
            news_targets = pd.concat([fresh, follow]).sort_values("Technical Score", ascending=False).head(10)
            with st.expander("Recent news · Top technical candidates", expanded=False):
                if st.button("Load recent news for top 10", key="load_explosive_breakout_news"):
                    pairs = tuple((str(row["Symbol"]), str(row["Company"])) for _, row in news_targets.iterrows())
                    with st.spinner("Checking recent public headlines…"):
                        news = _breakout_news(pairs)
                    st.session_state["explosive_breakout_news"] = news
                news = st.session_state.get("explosive_breakout_news", pd.DataFrame())
                if not news.empty:
                    st.dataframe(news, use_container_width=True, hide_index=True, column_config={"Source link": st.column_config.LinkColumn("Source link")})
                else:
                    st.caption("News has not been loaded yet. Headlines are leads for verification, not proof of a catalyst.")
        st.download_button("Download full mid/small-cap scan (CSV)", all_rows.to_csv(index=False).encode("utf-8"), "explosive_breakout_mid_smallcap_scan.csv", "text/csv", key="download_explosive_breakout_scan")
        st.caption("Score is a technical ranking heuristic, not a probability or buy recommendation. News headlines are third-party RSS results; verify material announcements on NSE/BSE. Fresh B1 and upcoming lists use completed weekly bars; daily EMA alignment is a supporting filter.")
    else:
        st.info("Press Scan / refresh to evaluate the Midcap 150 and Smallcap 250 universe. The scan is separate from the NSE 500 Weinstein screener and Turnaround Board.")
