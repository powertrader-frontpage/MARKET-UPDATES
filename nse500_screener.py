from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from io import StringIO

import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf


@st.cache_data(ttl=86400, show_spinner=False)
def _constituents():
    url = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"
    response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=25)
    response.raise_for_status()
    frame = pd.read_csv(StringIO(response.text))
    cols = {str(c).strip().lower(): c for c in frame.columns}
    symbol_col = cols.get("symbol")
    company_col = cols.get("company name") or cols.get("companyname")
    sector_col = cols.get("industry") or cols.get("sector")
    if symbol_col is None:
        raise ValueError("Official Nifty 500 file has no Symbol column.")
    symbols = frame[symbol_col].astype(str).str.strip()
    out = pd.DataFrame({
        "Symbol": symbols,
        "Company": frame[company_col].astype(str).str.strip() if company_col else symbols,
        "Sector": frame[sector_col].astype(str).str.strip() if sector_col else "Unclassified",
    })
    out = out[out["Symbol"].ne("") & out["Symbol"].str.lower().ne("nan")].drop_duplicates("Symbol")
    out["Ticker"] = out["Symbol"] + ".NS"
    return out.reset_index(drop=True)


@st.cache_data(ttl=86400, show_spinner=False)
def _daily_prices(tickers):
    batches = []
    for start in range(0, len(tickers), 60):
        batch = list(tickers[start:start + 60])
        raw = yf.download(
            tickers=batch, period="2y", interval="1d", auto_adjust=True,
            group_by="ticker", threads=True, progress=False, timeout=25,
        )
        if raw is None or raw.empty:
            continue
        if isinstance(raw.columns, pd.MultiIndex):
            level0 = set(map(str, raw.columns.get_level_values(0)))
            level1 = set(map(str, raw.columns.get_level_values(1)))
            for ticker in batch:
                if ticker in level0 and "Close" in level1:
                    batches.append(raw[ticker][["Close"]].rename(columns={"Close": ticker}))
                elif ticker in level1 and "Close" in level0:
                    batches.append(raw["Close"][ticker].to_frame(name=ticker))
        elif len(batch) == 1 and "Close" in raw.columns:
            batches.append(raw[["Close"]].rename(columns={"Close": batch[0]}))
    if not batches:
        return pd.DataFrame()
    return pd.concat(batches, axis=1).loc[:, lambda d: ~d.columns.duplicated()].sort_index()


def _calculate():
    universe = _constituents()
    prices = _daily_prices(tuple(universe["Ticker"].tolist()))
    if prices.empty or len(prices.index) < 160:
        raise ValueError("Insufficient daily history returned for a full-universe scan.")
    prices = prices.apply(pd.to_numeric, errors="coerce").dropna(axis=1, how="all")
    weekly = prices.resample("W-FRI").last().dropna(how="all")
    if len(weekly) < 32:
        raise ValueError("At least 32 weekly closes are required for Weinstein staging.")

    latest = weekly.iloc[-1]
    def prior(n):
        return weekly.iloc[-(n + 1)] if len(weekly) > n else weekly.iloc[0]

    r3 = latest / prior(13) - 1
    r6 = latest / prior(26) - 1
    r12 = latest / prior(52) - 1
    composite = 0.25 * r3 + 0.50 * r6 + 0.25 * r12
    ranks = (composite.rank(pct=True, method="average") * 99).round().clip(1, 99)
    ma30 = weekly.rolling(30, min_periods=30).mean()
    ma30_now = ma30.iloc[-1]
    ma30_four = ma30.iloc[-5]
    ma10_now = weekly.rolling(10, min_periods=10).mean().iloc[-1]
    slope = (ma30_now / ma30_four - 1) * 100
    high52 = weekly.tail(52).max()
    daily_now, daily_prev = prices.iloc[-1], prices.iloc[-2]

    records = []
    for _, item in universe.iterrows():
        ticker = item["Ticker"]
        if ticker not in prices.columns or pd.isna(latest.get(ticker)) or pd.isna(ma30_now.get(ticker)):
            continue
        price = float(latest[ticker])
        avg30 = float(ma30_now[ticker])
        avg10 = ma10_now.get(ticker)
        ma_slope = slope.get(ticker)
        rank = ranks.get(ticker)
        if pd.isna(avg10) or pd.isna(ma_slope) or pd.isna(rank):
            continue
        gap = (price / avg30 - 1) * 100
        high = high52.get(ticker)
        high_gap = (price / high - 1) * 100 if pd.notna(high) and high else np.nan

        # Stage 2 is intentionally strict: price and 10W average above a rising 30W average.
        if price > avg30 and float(avg10) > avg30 and float(ma_slope) >= 1.0:
            stage = "Stage 2 · Uptrend"
        elif price < avg30 and float(ma_slope) <= -1.0:
            stage = "Stage 4 · Downtrend"
        elif abs(float(ma_slope)) < 1.0 and abs(gap) <= 5.0:
            stage = "Stage 1 · Base"
        elif price < avg30 or float(ma_slope) < 1.0:
            stage = "Stage 3 · Top"
        else:
            stage = "Unclassified"

        extension = (
            "Extended" if gap >= 10 else
            "Near highs" if pd.notna(high_gap) and high_gap >= -5 else
            "Slipping" if price < avg30 and float(ma_slope) < 0 else
            "Near 30W average"
        )
        d0, d1 = daily_now.get(ticker), daily_prev.get(ticker)
        day_change = (float(d0) / float(d1) - 1) * 100 if pd.notna(d0) and pd.notna(d1) and float(d1) else np.nan
        records.append({
            "Symbol": item["Symbol"], "Company": item["Company"], "Sector": item["Sector"],
            "Price": price, "RS Rank": int(rank), "Stage": stage, "30W Avg": avg30,
            "Gap vs 30W %": gap, "52W High Gap %": high_gap,
            "Extension": extension, "30W Slope %": float(ma_slope), "Daily Change %": day_change,
        })

    result = pd.DataFrame(records)
    if result.empty:
        raise ValueError("No NSE 500 symbols had enough usable weekly price history.")
    result = result.sort_values(["RS Rank", "Gap vs 30W %"], ascending=[False, False]).reset_index(drop=True)

    sector_rows = []
    for sector, group in result.groupby("Sector", dropna=False):
        changes = pd.to_numeric(group["Daily Change %"], errors="coerce").dropna()
        sector_rows.append({
            "Sector": sector,
            "Median RS": round(float(group["RS Rank"].median()), 1),
            "Advancing Stocks %": round(float((changes > 0).mean() * 100), 1) if len(changes) else np.nan,
            "Stocks": len(group),
        })
    sectors = pd.DataFrame(sector_rows).sort_values(
        ["Median RS", "Advancing Stocks %"], ascending=False
    )
    return result, sectors, len(universe), len(prices.columns)


def render_nse500_screener():
    with st.expander("NSE 500 · Relative Strength & Weinstein Stage Screener", expanded=False):
        st.markdown("**Find market leaders, confirmed Stage 2 trends and sector participation across the full NSE 500.**")
        st.caption(
            "RS is a 1–99 percentile rank using a 3M/6M/12M momentum blend versus the scanned NSE 500 universe. "
            "Weinstein stages use weekly closes, 10-week and 30-week averages, and the 30-week average slope. "
            "This is a transparent rules-based screen, not discretionary chart analysis."
        )
        run_scan = st.button("Run / refresh NSE 500 scan", type="primary", key="run_nse500_weinstein")
        if run_scan or "nse500_rs_stage_results" not in st.session_state:
            with st.spinner("Loading official NSE 500 constituents and calculating weekly relative strength. The first scan may take a few minutes…"):
                try:
                    results, sectors, universe_count, priced_count = _calculate()
                    st.session_state["nse500_rs_stage_results"] = results
                    st.session_state["nse500_rs_stage_sectors"] = sectors
                    st.session_state["nse500_rs_stage_time"] = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
                    st.session_state["nse500_rs_stage_counts"] = (universe_count, priced_count)
                except Exception as exc:
                    st.warning(f"NSE 500 screener could not complete: {exc.__class__.__name__}: {exc}")

        results = st.session_state.get("nse500_rs_stage_results", pd.DataFrame())
        sectors = st.session_state.get("nse500_rs_stage_sectors", pd.DataFrame())
        if isinstance(results, pd.DataFrame) and not results.empty:
            universe_count, priced_count = st.session_state.get("nse500_rs_stage_counts", (len(results), len(results)))
            st.caption(
                f"Last calculated: {st.session_state.get('nse500_rs_stage_time', '—')} · "
                f"Official constituents: {universe_count} · With usable prices: {priced_count} · "
                "Sources: Nifty Indices + Yahoo Finance adjusted daily history"
            )
            counts = results["Stage"].value_counts()
            cols = st.columns(4)
            stages = [
                ("Stage 1 · Base", "#f59e0b"), ("Stage 2 · Uptrend", "#22c55e"),
                ("Stage 3 · Top", "#fb923c"), ("Stage 4 · Downtrend", "#ef4444"),
            ]
            for col, (stage, color) in zip(cols, stages):
                col.markdown(
                    f'<div style="border:1px solid #343a46;border-left:4px solid {color};border-radius:10px;padding:9px 11px;">'
                    f'<div style="font-size:11px;color:#aeb6c2;">{stage}</div>'
                    f'<div style="font-size:23px;font-weight:800;color:{color};">{int(counts.get(stage, 0))}</div></div>',
                    unsafe_allow_html=True,
                )
            st.markdown("#### Sector strength · median RS and advancing participation")
            st.dataframe(sectors, use_container_width=True, hide_index=True, height=230)
            st.markdown("#### Full NSE 500 stock screener")
            c1, c2, c3 = st.columns([1.2, 1, 1])
            with c1:
                query = st.text_input("Search symbol / company", key="nse500_search")
            with c2:
                stage_filter = st.multiselect(
                    "Weinstein stage",
                    [x[0] for x in stages] + ["Unclassified"],
                    default=["Stage 2 · Uptrend"],
                    key="nse500_stage_filter",
                )
            with c3:
                sector_filter = st.selectbox(
                    "Sector", ["All sectors"] + sorted(results["Sector"].dropna().astype(str).unique()),
                    key="nse500_sector_filter",
                )
            filtered = results.copy()
            if query.strip():
                q = query.strip().lower()
                filtered = filtered[
                    filtered["Symbol"].str.lower().str.contains(q, na=False)
                    | filtered["Company"].str.lower().str.contains(q, na=False)
                ]
            if stage_filter:
                filtered = filtered[filtered["Stage"].isin(stage_filter)]
            if sector_filter != "All sectors":
                filtered = filtered[filtered["Sector"].astype(str) == sector_filter]
            columns = ["Symbol", "Company", "Sector", "Price", "RS Rank", "Stage", "Gap vs 30W %", "Extension", "Daily Change %"]
            st.dataframe(filtered[columns], use_container_width=True, hide_index=True, height=520)
            st.download_button(
                "Download filtered NSE 500 screener (CSV)",
                filtered[columns].to_csv(index=False).encode("utf-8"),
                "nse500_relative_strength_weinstein_stage.csv", "text/csv",
                key="download_nse500_rs_stage",
            )
            st.caption(
                "Stage 2 requires price > rising 30W average and 10W average > 30W average. "
                "Extended is a price-distance label and can coexist with Stage 2. "
                "The data cache is 24 hours; refresh the scan after the daily close."
            )
        else:
            st.info("Run the scan to calculate the full NSE 500 universe. The first run may take several minutes; no illustrative stock scores are substituted.")
        st.markdown(
            '<div style="display:flex;justify-content:center;margin:14px 0 4px;">'
            '<a href="https://www.niftyindices.com/indices/equity/broad-based-indices/Nifty-500" target="_blank" '
            'style="display:inline-block;background:#16a34a;color:white;text-decoration:none;font-weight:750;'
            'border-radius:9px;padding:10px 20px;">Explore the screener free ↗</a></div>',
            unsafe_allow_html=True,
        )
