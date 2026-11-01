from __future__ import annotations

from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo
from io import StringIO

import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf


STAGE_1 = "Stage 1 · Base"
STAGE_2 = "Stage 2 · Uptrend"
STAGE_3 = "Stage 3 · Top"
STAGE_4 = "Stage 4 · Downtrend"
STAGES = [STAGE_1, STAGE_2, STAGE_3, STAGE_4, "Unclassified"]


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



@st.cache_data(ttl=86400, show_spinner=False)
def _daily_high_low(tickers):
    """Fetch daily highs/lows for Weinstein base detection."""
    high_parts, low_parts = [], []
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
                if ticker in level0 and {"High", "Low"}.issubset(level1):
                    high_parts.append(raw[ticker]["High"].rename(ticker))
                    low_parts.append(raw[ticker]["Low"].rename(ticker))
                elif ticker in level1 and {"High", "Low"}.issubset(level0):
                    high_parts.append(raw["High"][ticker].rename(ticker))
                    low_parts.append(raw["Low"][ticker].rename(ticker))
        elif len(batch) == 1 and {"High", "Low"}.issubset(raw.columns):
            high_parts.append(raw["High"].rename(batch[0]))
            low_parts.append(raw["Low"].rename(batch[0]))
    if not high_parts or not low_parts:
        return pd.DataFrame(), pd.DataFrame()
    highs = pd.concat(high_parts, axis=1).loc[:, lambda d: ~d.columns.duplicated()].sort_index()
    lows = pd.concat(low_parts, axis=1).loc[:, lambda d: ~d.columns.duplicated()].sort_index()
    return highs, lows


def _base_progress(close, high, low, min_advance_pct=9.0, min_base_weeks=4):
    """Return weekly B-count series following the supplied Weinstein base rules."""
    count = 0
    stage2_low = np.nan
    base_low = np.nan
    base_high = np.nan
    last_breakout = np.nan
    base_start = None
    counts = []
    for i in range(len(close)):
        c, h, l = close.iloc[i], high.iloc[i], low.iloc[i]
        if any(pd.isna(v) for v in (c, h, l)):
            counts.append(0)
            continue
        stage2_now = False
        if i >= 30:
            ma30 = close.iloc[:i + 1].rolling(30, min_periods=30).mean().iloc[-1]
            ma30_4 = close.iloc[:i - 3].rolling(30, min_periods=30).mean().iloc[-1] if i >= 33 else np.nan
            ma10 = close.iloc[:i + 1].rolling(10, min_periods=10).mean().iloc[-1]
            stage2_now = pd.notna(ma30) and pd.notna(ma30_4) and pd.notna(ma10) and ma30_4 != 0 and c > ma30 and ma10 > ma30 and (ma30 / ma30_4 - 1) * 100 >= 1.0
        if not stage2_now:
            count = 0
            stage2_low = base_low = base_high = last_breakout = np.nan
            base_start = None
            counts.append(0)
            continue
        if count == 0:
            count = 1
            stage2_low = l
            base_low, base_high = l, h
            last_breakout = h
            base_start = i
            counts.append(count)
            continue
        if l < stage2_low:
            count = 1
            stage2_low = l
            base_low, base_high = l, h
            last_breakout = h
            base_start = i
            counts.append(count)
            continue
        prior_base_high = base_high
        base_low = min(base_low, l)
        base_high = max(base_high, h)
        duration = i - base_start
        range_pct = ((base_high - base_low) / base_low * 100) if base_low > 0 else 0
        duration_ok = duration >= min_base_weeks or (duration >= 2 and range_pct >= 90)
        advance_ok = pd.notna(last_breakout) and last_breakout > 0 and (c / last_breakout - 1) * 100 >= min_advance_pct
        if c > prior_base_high and duration_ok and advance_ok:
            count += 1
            base_low, base_high = l, h
            last_breakout = c
            base_start = i
        counts.append(count)
    return pd.Series(counts, index=close.index, dtype="int64")

def _stage_at(weekly, ticker, position):
    """Classify one completed weekly bar using the same strict Weinstein rules."""
    if position < 30 or ticker not in weekly.columns:
        return "Unclassified"
    close = weekly.iloc[position].get(ticker)
    ma30_series = weekly[ticker].rolling(30, min_periods=30).mean()
    ma10_series = weekly[ticker].rolling(10, min_periods=10).mean()
    ma30_now = ma30_series.iloc[position]
    ma30_four = ma30_series.iloc[position - 4]
    ma10_now = ma10_series.iloc[position]
    if any(pd.isna(x) for x in (close, ma30_now, ma30_four, ma10_now)) or not ma30_four:
        return "Unclassified"
    slope_pct = (ma30_now / ma30_four - 1) * 100
    gap_pct = (close / ma30_now - 1) * 100
    if close > ma30_now and ma10_now > ma30_now and slope_pct >= 1.0:
        return STAGE_2
    if close < ma30_now and slope_pct <= -1.0:
        return STAGE_4
    if abs(slope_pct) < 1.0 and abs(gap_pct) <= 5.0:
        return STAGE_1
    if close < ma30_now or slope_pct < 1.0:
        return STAGE_3
    return "Unclassified"


def _calculate():
    universe = _constituents()
    tickers = tuple(universe["Ticker"].tolist())
    prices = _daily_prices(tickers)
    daily_high, daily_low = _daily_high_low(tickers)
    if prices.empty or len(prices.index) < 160:
        raise ValueError("Insufficient daily history returned for a full-universe scan.")
    prices = prices.apply(pd.to_numeric, errors="coerce").dropna(axis=1, how="all")
    weekly_all = prices.resample("W-FRI").last().dropna(how="all")
    weekly_high_all = daily_high.resample("W-FRI").max().reindex(weekly_all.index) if not daily_high.empty else pd.DataFrame(index=weekly_all.index)
    weekly_low_all = daily_low.resample("W-FRI").min().reindex(weekly_all.index) if not daily_low.empty else pd.DataFrame(index=weekly_all.index)
    if len(weekly_all) < 34:
        raise ValueError("At least 34 weekly closes are required for current and prior Weinstein stages.")

    # Exclude a still-forming current week so a Friday intraday move cannot create a false Stage 2 entry.
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    current_friday = now.date() + timedelta(days=(4 - now.weekday()) % 7)
    if weekly_all.index[-1].date() == current_friday and (
        now.date() < current_friday or (now.date() == current_friday and now.time() < time(15, 45))
    ):
        weekly = weekly_all.iloc[:-1].copy()
        weekly_high = weekly_high_all.reindex(weekly.index)
        weekly_low = weekly_low_all.reindex(weekly.index)
    else:
        weekly = weekly_all.copy()
        weekly_high = weekly_high_all.reindex(weekly.index)
        weekly_low = weekly_low_all.reindex(weekly.index)
    if len(weekly) < 34:
        raise ValueError("Not enough completed weekly bars after excluding the in-progress week.")

    latest = weekly.iloc[-1]
    def prior(n):
        return weekly.iloc[-(n + 1)] if len(weekly) > n else weekly.iloc[0]

    r3 = latest / prior(13) - 1
    r6 = latest / prior(26) - 1
    r12 = latest / prior(52) - 1
    composite = 0.25 * r3 + 0.50 * r6 + 0.25 * r12
    ranks = (composite.rank(pct=True, method="average") * 99).round().clip(1, 99)

    ma30 = weekly.rolling(30, min_periods=30).mean()
    ma30_now, ma30_four = ma30.iloc[-1], ma30.iloc[-5]
    ma10_now = weekly.rolling(10, min_periods=10).mean().iloc[-1]
    slope = (ma30_now / ma30_four - 1) * 100
    high52 = weekly.tail(52).max()
    daily_now, daily_prev = prices.iloc[-1], prices.iloc[-2]
    current_week_label = weekly.index[-1]
    previous_week_label = weekly.index[-2]

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
        stage = _stage_at(weekly, ticker, len(weekly) - 1)
        previous_stage = _stage_at(weekly, ticker, len(weekly) - 2)
        newly_stage2 = previous_stage == STAGE_1 and stage == STAGE_2
        if ticker in weekly_high.columns and ticker in weekly_low.columns:
            base_counts = _base_progress(weekly[ticker], weekly_high[ticker], weekly_low[ticker], min_advance_pct=9.0)
            base_count = int(base_counts.iloc[-1])
            previous_base_count = int(base_counts.iloc[-2]) if len(base_counts) > 1 else 0
        else:
            base_count, previous_base_count = 0, 0
        fresh_b2 = stage == STAGE_2 and previous_base_count == 1 and base_count == 2

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
            "Price": price, "RS Rank": int(rank), "Stage": stage, "Previous Stage": previous_stage,
            "New Stage 2": newly_stage2, "Base Count": base_count,
            "Fresh B2": fresh_b2, "30W Avg": avg30,
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
    return result, sectors, len(universe), len(prices.columns), current_week_label, previous_week_label


def render_nse500_screener():
    with st.expander("NSE 500 · Relative Strength & Weinstein Stage Screener", expanded=False):
        st.markdown("**Find market leaders, fresh Stage 2 breakouts and sector participation across the full NSE 500.**")
        st.caption(
            "RS is a 1–99 percentile rank using a 3M/6M/12M momentum blend versus the scanned NSE 500 universe. "
            "Weinstein stages use completed weekly closes, 10-week and 30-week averages, and the 30-week average slope. "
            "A fresh Stage 2 entry means the stock was Stage 1 in the previous completed week and is Stage 2 now."
        )
        run_scan = st.button("Run / refresh NSE 500 scan", type="primary", key="run_nse500_weinstein")
        if run_scan:
            with st.spinner("Loading official NSE 500 constituents and calculating weekly relative strength and stage transitions. The first scan may take a few minutes…"):
                try:
                    results, sectors, universe_count, priced_count, current_week, previous_week = _calculate()
                    st.session_state["nse500_rs_stage_results"] = results
                    st.session_state["nse500_rs_stage_sectors"] = sectors
                    st.session_state["nse500_rs_stage_time"] = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p IST")
                    st.session_state["nse500_rs_stage_counts"] = (universe_count, priced_count)
                    st.session_state["nse500_rs_stage_weeks"] = (current_week.strftime("%d %b %Y"), previous_week.strftime("%d %b %Y"))
                except Exception as exc:
                    st.warning(f"NSE 500 screener could not complete: {exc.__class__.__name__}: {exc}")

        results = st.session_state.get("nse500_rs_stage_results", pd.DataFrame())
        sectors = st.session_state.get("nse500_rs_stage_sectors", pd.DataFrame())
        if isinstance(results, pd.DataFrame) and not results.empty:
            universe_count, priced_count = st.session_state.get("nse500_rs_stage_counts", (len(results), len(results)))
            current_week, previous_week = st.session_state.get("nse500_rs_stage_weeks", ("—", "—"))
            st.caption(
                f"Last calculated: {st.session_state.get('nse500_rs_stage_time', '—')} · "
                f"Completed week: {current_week} · Compared with: {previous_week} · "
                f"Official constituents: {universe_count} · With usable prices: {priced_count}"
            )

            fresh = results[results["New Stage 2"]].copy().sort_values(["RS Rank", "Gap vs 30W %"], ascending=[False, False])
            current_stage2 = results[results["Stage"] == STAGE_2].copy().sort_values(["RS Rank", "Gap vs 30W %"], ascending=[False, False])

            # Visual market overview: compact cards first, detailed tables remain below.
            counts = results["Stage"].value_counts()
            stage_colors = [(STAGE_1, "#d6a84f"), (STAGE_2, "#3ba77a"), (STAGE_3, "#d98a45"), (STAGE_4, "#c85b62")]
            metric_cols = st.columns(4)
            for metric_col, (stage_name, stage_color) in zip(metric_cols, stage_colors):
                metric_col.markdown(
                    f'<div style="background:#171d1b;border:1px solid #35433c;border-top:3px solid {stage_color};border-radius:12px;padding:12px 14px;">'
                    f'<div style="font-size:11px;color:#a8b5ad;">{stage_name}</div>'
                    f'<div style="font-size:27px;font-weight:800;color:{stage_color};">{int(counts.get(stage_name, 0))}</div></div>',
                    unsafe_allow_html=True,
                )

            leaders = results.sort_values(["RS Rank", "Gap vs 30W %"], ascending=[False, False]).head(6)
            daily_climbers = results.sort_values("Daily Change %", ascending=False).head(6)
            daily_fallers = results.sort_values("Daily Change %", ascending=True).head(6)
            board_left, board_right = st.columns(2, gap="medium")
            with board_left:
                st.markdown("**RS LEADERS · NSE 500**")
                for pos, (_, stock) in enumerate(leaders.iterrows(), 1):
                    st.markdown(
                        f'<div style="display:flex;justify-content:space-between;gap:10px;padding:9px 11px;border-bottom:1px solid #34413a;background:rgba(255,255,255,.025);">'
                        f'<span><b>#{pos} {stock["Symbol"]}</b><small style="display:block;color:#aab6ae;">{stock["Sector"]} · {stock["Stage"]}</small></span>'
                        f'<span style="text-align:right;color:#e5c46d;font-weight:800;">RS {int(stock["RS Rank"])}<small style="display:block;color:#aab6ae;">{float(stock["Gap vs 30W %"]):+.1f}% vs 30W</small></span></div>',
                        unsafe_allow_html=True,
                    )
            with board_right:
                st.markdown("**TODAY’S BIGGEST MOVERS**")
                mover_mode = st.segmented_control("Show movers", ["Climbers", "Fallers"], default="Climbers", key="nse500_mover_mode", label_visibility="collapsed")
                mover_frame = daily_climbers if mover_mode == "Climbers" else daily_fallers
                for pos, (_, stock) in enumerate(mover_frame.iterrows(), 1):
                    move = float(stock["Daily Change %"]) if pd.notna(stock["Daily Change %"]) else 0.0
                    move_color = "#3ba77a" if move >= 0 else "#d46b72"
                    st.markdown(
                        f'<div style="display:flex;justify-content:space-between;gap:10px;padding:9px 11px;border-bottom:1px solid #34413a;background:rgba(255,255,255,.025);">'
                        f'<span><b>#{pos} {stock["Symbol"]}</b><small style="display:block;color:#aab6ae;">{stock["Sector"]} · RS {int(stock["RS Rank"])}</small></span>'
                        f'<span style="color:{move_color};font-weight:800;">{move:+.2f}%</span></div>',
                        unsafe_allow_html=True,
                    )
            st.markdown(
                f'<div style="margin:12px 0 18px;padding:11px 14px;border:1px solid #6a5934;border-radius:10px;background:rgba(190,150,70,.08);">'
                f'<b style="color:#e5c46d;">FRESH STAGE 2 ENTRIES</b> · {len(fresh)} confirmed Stage 1 → Stage 2 transitions this completed week · '
                f'<b style="color:#e5c46d;">{len(current_stage2)}</b> stocks currently in Stage 2</div>',
                unsafe_allow_html=True,
            )

            st.markdown("#### 🟢 Fresh Stage 2 entries · Stage 1 → Stage 2")
            st.caption("Only stocks whose completed weekly classification changed from Stage 1 last week to Stage 2 this week appear here.")
            if fresh.empty:
                st.info("No confirmed Stage 1 → Stage 2 transitions were detected in the latest completed week.")
            else:
                fresh_cols = ["Symbol", "Company", "Sector", "Price", "RS Rank", "Gap vs 30W %", "Extension", "Daily Change %"]
                st.dataframe(fresh[fresh_cols], use_container_width=True, hide_index=True, height=min(360, 42 + 38 * len(fresh)))
                st.download_button(
                    "Download fresh Stage 2 entries (CSV)",
                    fresh[fresh_cols].to_csv(index=False).encode("utf-8"),
                    "nse500_fresh_stage2_entries.csv", "text/csv",
                    key="download_nse500_fresh_stage2",
                )

            st.markdown("#### Current Stage 2 watchlist · all confirmed uptrends")
            st.caption(f"{len(current_stage2)} NSE 500 stocks currently meet the Stage 2 rules; fresh entries are flagged separately above.")
            stage2_cols = ["Symbol", "Company", "Sector", "Price", "RS Rank", "Gap vs 30W %", "Extension", "New Stage 2"]
            st.dataframe(current_stage2[stage2_cols], use_container_width=True, hide_index=True, height=360)
            st.download_button(
                "Download all Stage 2 stocks (CSV)",
                current_stage2[stage2_cols].to_csv(index=False).encode("utf-8"),
                "nse500_current_stage2_watchlist.csv", "text/csv",
                key="download_nse500_all_stage2",
            )

            st.markdown("#### Sector strength · median RS and advancing participation")
            st.dataframe(sectors, use_container_width=True, hide_index=True, height=230)

            st.markdown('<div id="full-nse-500-stock-screener"></div>', unsafe_allow_html=True)
            st.markdown("#### Full NSE 500 stock screener")
            c1, c2, c3, c4 = st.columns([1.2, 1, 1.15, 1])
            with c1:
                query = st.text_input("Search symbol / company", key="nse500_search")
            with c2:
                stage_filter = st.multiselect("Weinstein stage", STAGES, default=[STAGE_2], key="nse500_stage_filter")
            with c3:
                stage_selection = st.selectbox(
                    "Select Stage",
                    ["All Stage 2", "Fresh Stage 2 (B1)", "Stage 2 – B2 Stocks (Fresh)", "Stage 2 – B2 Stocks"],
                    key="nse500_base_stage_filter",
                )
            with c4:
                sector_filter = st.selectbox("Sector", ["All sectors"] + sorted(results["Sector"].dropna().astype(str).unique()), key="nse500_sector_filter")
            filtered = results.copy()
            if query.strip():
                q = query.strip().lower()
                filtered = filtered[
                    filtered["Symbol"].str.lower().str.contains(q, na=False)
                    | filtered["Company"].str.lower().str.contains(q, na=False)
                ]
            if stage_filter:
                filtered = filtered[filtered["Stage"].isin(stage_filter)]
            if stage_selection == "All Stage 2":
                filtered = filtered[filtered["Stage"] == STAGE_2]
            elif stage_selection == "Fresh Stage 2 (B1)":
                filtered = filtered[(filtered["Stage"] == STAGE_2) & (filtered["New Stage 2"])]
            elif stage_selection == "Stage 2 – B2 Stocks (Fresh)":
                filtered = filtered[(filtered["Stage"] == STAGE_2) & filtered["Fresh B2"]]
            elif stage_selection == "Stage 2 – B2 Stocks":
                filtered = filtered[(filtered["Stage"] == STAGE_2) & (filtered["Base Count"] == 2)]
            if sector_filter != "All sectors":
                filtered = filtered[filtered["Sector"].astype(str) == sector_filter]
            columns = ["Symbol", "Company", "Sector", "Price", "RS Rank", "Stage", "Previous Stage", "New Stage 2", "Base Count", "Fresh B2", "Gap vs 30W %", "Extension", "Daily Change %"]
            st.dataframe(filtered[columns], use_container_width=True, hide_index=True, height=520)
            st.download_button(
                "Download filtered NSE 500 screener (CSV)",
                filtered[columns].to_csv(index=False).encode("utf-8"),
                "nse500_relative_strength_weinstein_stage.csv", "text/csv",
                key="download_nse500_rs_stage",
            )
            st.caption(
                "Stage 2 requires price > rising 30W average and 10W average > 30W average. "
                "Fresh entries compare the two latest completed weekly bars; an unfinished current week is excluded. "
                "Extended is a price-distance label and can coexist with Stage 2. Data cache: 24 hours."
            )
        else:
            st.info("Run the scan to calculate the full NSE 500 universe. The first run may take several minutes; no illustrative stock scores are substituted.")
        st.markdown(
            '<div style="display:flex;justify-content:center;margin:14px 0 4px;">'
            '<a href="#full-nse-500-stock-screener" '
            'style="display:inline-block;background:#16a34a;color:white;text-decoration:none;font-weight:750;'
            'border-radius:9px;padding:10px 20px;">Explore the screener free ↗</a></div>',
            unsafe_allow_html=True,
        )
