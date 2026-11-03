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


@st.cache_data(ttl=21600, show_spinner=False)
def _daily_ohlc(tickers):
    """Fetch adjusted daily OHLC once per batch; shared by RS and Weinstein calculations."""
    close_parts, high_parts, low_parts, volume_parts = [], [], [], []
    for start in range(0, len(tickers), 80):
        batch = list(tickers[start:start + 80])
        raw = yf.download(
            tickers=batch, period="2y", interval="1d", auto_adjust=True,
            group_by="ticker", threads=False, progress=False, timeout=25,
        )
        if raw is None or raw.empty:
            continue
        if isinstance(raw.columns, pd.MultiIndex):
            level0 = set(map(str, raw.columns.get_level_values(0)))
            level1 = set(map(str, raw.columns.get_level_values(1)))
            for ticker in batch:
                if ticker in level0 and {"Close", "High", "Low", "Volume"}.issubset(level1):
                    part = raw[ticker]
                    close_parts.append(part["Close"].rename(ticker))
                    high_parts.append(part["High"].rename(ticker))
                    low_parts.append(part["Low"].rename(ticker))
                    volume_parts.append(part["Volume"].rename(ticker))
                elif ticker in level1 and {"Close", "High", "Low", "Volume"}.issubset(level0):
                    close_parts.append(raw["Close"][ticker].rename(ticker))
                    high_parts.append(raw["High"][ticker].rename(ticker))
                    low_parts.append(raw["Low"][ticker].rename(ticker))
                    volume_parts.append(raw["Volume"][ticker].rename(ticker))
        elif len(batch) == 1 and {"Close", "High", "Low", "Volume"}.issubset(raw.columns):
            close_parts.append(raw["Close"].rename(batch[0]))
            high_parts.append(raw["High"].rename(batch[0]))
            low_parts.append(raw["Low"].rename(batch[0]))
            volume_parts.append(raw["Volume"].rename(batch[0]))
    def combine(parts):
        return pd.concat(parts, axis=1).loc[:, lambda d: ~d.columns.duplicated()].sort_index() if parts else pd.DataFrame()
    return combine(close_parts), combine(high_parts), combine(low_parts), combine(volume_parts)


def _pine_rma(series, length):
    """TradingView ta.rma(): SMA seed followed by Wilder's recursive moving average."""
    values = pd.to_numeric(series, errors="coerce")
    out = pd.Series(np.nan, index=values.index, dtype="float64")
    valid = values.dropna()
    if len(valid) < length:
        return out
    seed_pos = values.index.get_loc(valid.index[length - 1])
    seed = float(valid.iloc[:length].mean())
    out.iloc[seed_pos] = seed
    alpha = 1.0 / float(length)
    for pos in range(seed_pos + 1, len(values)):
        current = values.iloc[pos]
        previous = out.iloc[pos - 1]
        if pd.notna(current) and pd.notna(previous):
            out.iloc[pos] = previous + alpha * (float(current) - previous)
    return out


def _pine_stage_series(close, high, low, ma_length=30, atr_length=14):
    """Match the supplied Pine engine: weekly SMA, ta.atr/RMA flatness and its four stage branches."""
    sma = close.rolling(ma_length, min_periods=ma_length).mean()
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = _pine_rma(tr, atr_length)
    slope = sma - sma.shift(2)
    flat_flag = slope.abs() < (atr * 0.05)

    # Pine starts each bar at Stage 1, then evaluates these branches in order.
    stage = pd.Series(STAGE_1, index=close.index, dtype="object")
    stage.loc[(close > sma) & (slope > 0) & ~flat_flag] = STAGE_2
    stage.loc[(close < sma) & (slope < 0) & ~flat_flag] = STAGE_4
    stage.loc[(close > sma) & flat_flag] = STAGE_1
    other = ~(
        ((close > sma) & (slope > 0) & ~flat_flag)
        | ((close < sma) & (slope < 0) & ~flat_flag)
        | ((close > sma) & flat_flag)
    )
    stage.loc[other] = STAGE_3
    stage.loc[sma.isna() | atr.isna() | close.isna()] = "Unclassified"
    return stage, sma, atr, flat_flag


def _pine_base_progress(close, high, low, stage_series, min_advance_pct=10.0, min_base_weeks=4):
    """Port of Pine base counting: duration, breakout, advance and explosive-base exception."""
    count = 0
    stage2_low = np.nan
    base_low = np.nan
    base_high = np.nan
    last_breakout = np.nan
    base_start = None
    counts = []
    fresh_b2 = []
    for i in range(len(close)):
        c, h, l = close.iloc[i], high.iloc[i], low.iloc[i]
        in_stage2 = stage_series.iloc[i] == STAGE_2
        if not in_stage2 or any(pd.isna(v) for v in (c, h, l)):
            count = 0
            stage2_low = base_low = base_high = last_breakout = np.nan
            base_start = None
            counts.append(0)
            fresh_b2.append(False)
            continue
        prior_count = count
        if count == 0:
            count = 1
            stage2_low = l
            base_low, base_high = l, h
            last_breakout = h
            base_start = i
            counts.append(count)
            fresh_b2.append(False)
            continue
        if l < stage2_low:
            count = 1
            stage2_low = l
            base_low, base_high = l, h
            last_breakout = h
            base_start = i
            counts.append(count)
            fresh_b2.append(False)
            continue
        prior_base_high = base_high
        base_low = min(base_low, l)
        base_high = max(base_high, h)
        duration = i - base_start
        explosive_range_pct = ((base_high - base_low) / base_low * 100) if base_low > 0 else 0.0
        duration_ok = duration >= min_base_weeks or (duration >= 2 and explosive_range_pct >= 90.0)
        advance_ok = (
            pd.notna(last_breakout) and last_breakout > 0
            and ((c / last_breakout - 1) * 100) >= min_advance_pct
        )
        broke_out = c > prior_base_high
        if broke_out and duration_ok and advance_ok:
            count += 1
            base_low, base_high = l, h
            last_breakout = c
            base_start = i
        counts.append(count)
        fresh_b2.append(prior_count == 1 and count == 2)
    return (
        pd.Series(counts, index=close.index, dtype="int64"),
        pd.Series(fresh_b2, index=close.index, dtype="bool"),
    )


def _stage_at(weekly, ticker, position, weekly_high, weekly_low):
    if position < 30 or ticker not in weekly.columns or ticker not in weekly_high.columns or ticker not in weekly_low.columns:
        return "Unclassified"
    stage_series, _, _, _ = _pine_stage_series(weekly[ticker], weekly_high[ticker], weekly_low[ticker])
    return stage_series.iloc[position]

@st.cache_data(ttl=21600, show_spinner=False)
def _calculate():
    universe = _constituents()
    tickers = tuple(universe["Ticker"].tolist())
    prices, daily_high, daily_low, daily_volume = _daily_ohlc(tickers)
    if prices.empty or len(prices.index) < 160:
        raise ValueError("Insufficient daily history returned for a full-universe scan.")
    prices = prices.apply(pd.to_numeric, errors="coerce").dropna(axis=1, how="all")
    weekly_all = prices.resample("W-FRI").last().dropna(how="all")
    weekly_high_all = daily_high.resample("W-FRI").max().reindex(weekly_all.index) if not daily_high.empty else pd.DataFrame(index=weekly_all.index)
    weekly_low_all = daily_low.resample("W-FRI").min().reindex(weekly_all.index) if not daily_low.empty else pd.DataFrame(index=weekly_all.index)
    weekly_volume_all = daily_volume.resample("W-FRI").sum(min_count=1).reindex(weekly_all.index) if not daily_volume.empty else pd.DataFrame(index=weekly_all.index)
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
        weekly_volume = weekly_volume_all.reindex(weekly.index)
    else:
        weekly = weekly_all.copy()
        weekly_high = weekly_high_all.reindex(weekly.index)
        weekly_low = weekly_low_all.reindex(weekly.index)
        weekly_volume = weekly_volume_all.reindex(weekly.index)
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
    ma30_now = ma30.iloc[-1]
    ma10_all = weekly.rolling(10, min_periods=10).mean()
    ma10_now = ma10_all.iloc[-1]
    ma10_prev = ma10_all.iloc[-2]
    avg10_volume = weekly_volume.shift(1).rolling(10, min_periods=8).mean().iloc[-1]
    latest_volume = weekly_volume.iloc[-1]
    stage_series_by_ticker = {}
    base_series_by_ticker = {}
    fresh_b2_series_by_ticker = {}
    for ticker in weekly.columns:
        if ticker in weekly_high.columns and ticker in weekly_low.columns:
            stage_s, _, _, _ = _pine_stage_series(weekly[ticker], weekly_high[ticker], weekly_low[ticker])
            base_s, fresh_s = _pine_base_progress(weekly[ticker], weekly_high[ticker], weekly_low[ticker], stage_s, min_advance_pct=10.0, min_base_weeks=4)
            stage_series_by_ticker[ticker] = stage_s
            base_series_by_ticker[ticker] = base_s
            fresh_b2_series_by_ticker[ticker] = fresh_s
    stage_frame = pd.DataFrame(stage_series_by_ticker, index=weekly.index)
    base_frame = pd.DataFrame(base_series_by_ticker, index=weekly.index)
    fresh_b2_frame = pd.DataFrame(fresh_b2_series_by_ticker, index=weekly.index)
    slope = (ma30_now / ma30.shift(2).iloc[-1] - 1) * 100
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
        prev_avg10 = ma10_prev.get(ticker)
        ma_slope = slope.get(ticker)
        rank = ranks.get(ticker)
        if pd.isna(avg10) or pd.isna(ma_slope) or pd.isna(rank):
            continue
        gap = (price / avg30 - 1) * 100
        prior_4w = weekly[ticker].iloc[-5] if len(weekly[ticker].dropna()) >= 5 else np.nan
        prior_12w = weekly[ticker].iloc[-13] if len(weekly[ticker].dropna()) >= 13 else np.nan
        return_4w = (price / float(prior_4w) - 1) * 100 if pd.notna(prior_4w) and float(prior_4w) > 0 else np.nan
        return_12w = (price / float(prior_12w) - 1) * 100 if pd.notna(prior_12w) and float(prior_12w) > 0 else np.nan
        recent5_volume = weekly_volume[ticker].tail(5).mean() if ticker in weekly_volume.columns else np.nan
        prior10_volume = weekly_volume[ticker].iloc[-15:-5].mean() if ticker in weekly_volume.columns and len(weekly_volume) >= 15 else np.nan
        volume_contraction = (
            float(recent5_volume / prior10_volume)
            if pd.notna(recent5_volume) and pd.notna(prior10_volume) and float(prior10_volume) > 0
            else np.nan
        )
        recent4_high = weekly_high[ticker].tail(4).max() if ticker in weekly_high.columns else np.nan
        recent4_low = weekly_low[ticker].tail(4).min() if ticker in weekly_low.columns else np.nan
        range4_pct = (
            (float(recent4_high) / float(recent4_low) - 1) * 100
            if pd.notna(recent4_high) and pd.notna(recent4_low) and float(recent4_low) > 0
            else np.nan
        )
        vol_now = latest_volume.get(ticker)
        vol_avg = avg10_volume.get(ticker)
        volume_ratio = float(vol_now / vol_avg) if pd.notna(vol_now) and pd.notna(vol_avg) and float(vol_avg) > 0 else np.nan
        high = high52.get(ticker)
        high_gap = (price / high - 1) * 100 if pd.notna(high) and high else np.nan
        stage = stage_frame[ticker].iloc[-1] if ticker in stage_frame.columns else "Unclassified"
        previous_stage = stage_frame[ticker].iloc[-2] if ticker in stage_frame.columns else "Unclassified"
        newly_stage2 = previous_stage == STAGE_1 and stage == STAGE_2
        reclaimed_10w = (
            pd.notna(prev_avg10) and float(weekly[ticker].iloc[-2]) <= float(prev_avg10)
            and price > float(avg10)
        )
        gap_vs_10w = (price / float(avg10) - 1) * 100 if float(avg10) else np.nan
        base_count = int(base_frame[ticker].iloc[-1]) if ticker in base_frame.columns else 0
        fresh_b2 = bool(fresh_b2_frame[ticker].iloc[-1]) if ticker in fresh_b2_frame.columns else False

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
            "New Stage 2": newly_stage2, "4W Return %": return_4w, "12W Return %": return_12w,
            "5W / Prior 10W Volume": volume_contraction, "4W Range %": range4_pct,
            "Reclaimed 10W": reclaimed_10w,
            "Gap vs 10W %": gap_vs_10w, "Base Count": base_count,
            "Fresh B2": fresh_b2, "Volume vs 10W Avg": volume_ratio,
            "30W Avg": avg30,
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
    sector_rs_map = sectors.set_index("Sector")["Median RS"].to_dict()
    result["Sector Median RS"] = result["Sector"].map(sector_rs_map).fillna(0)
    # Transparent 100-point quality score: relative strength 25, 30W slope 20,
    # breakout-week volume 20, sector strength 15, base development 10, extension discipline 10.
    rs_points = (result["RS Rank"].clip(0, 99) / 99) * 25
    slope_points = (result["30W Slope %"].clip(0, 8) / 8) * 20
    volume_points = (result["Volume vs 10W Avg"].clip(0, 2) / 2) * 20
    sector_points = (result["Sector Median RS"].clip(0, 99) / 99) * 15
    base_points = result["Base Count"].clip(0, 2) / 2 * 10
    extension_points = np.select(
        [result["Gap vs 30W %"].between(0, 10), result["Gap vs 30W %"].between(10, 15)],
        [10, 5], default=0
    )
    result["Breakout Quality"] = (rs_points + slope_points + volume_points + sector_points + base_points + extension_points).round(1)
    # Strict list requires strong RS/trend/volume/sector confirmation and avoids extended entries.
    # v8 historical setup resemblance, applied to ALL current Stage 2 stocks.
    # Not a calibrated probability; no changes to Weinstein stages or existing quality rules.
    benchmark_series = weekly["^CRSLDX"] if "^CRSLDX" in weekly else pd.Series(dtype=float)
    benchmark_13w = ((benchmark_series.iloc[-1] / benchmark_series.iloc[-14] - 1) * 100) if len(benchmark_series) >= 14 and pd.notna(benchmark_series.iloc[-14]) and benchmark_series.iloc[-14] > 0 else np.nan
    prior_52w_high = weekly.shift(1).tail(52).max()
    score_map = {}
    for ticker in weekly.columns:
        if ticker not in stage_frame or len(weekly[ticker].dropna()) < 30:
            continue
        c = weekly[ticker]
        last = c.iloc[-1]
        if pd.isna(last) or last <= 0:
            continue
        prior12 = c.iloc[-13] if len(c) >= 13 else np.nan
        prior13 = c.iloc[-14] if len(c) >= 14 else np.nan
        mom12 = (last / prior12 - 1) * 100 if pd.notna(prior12) and prior12 > 0 else np.nan
        excess13 = ((last / prior13 - 1) * 100 - benchmark_13w) if pd.notna(prior13) and prior13 > 0 and pd.notna(benchmark_13w) else np.nan
        ma = ma30_now.get(ticker)
        slope_value = slope.get(ticker)
        prior_high = prior_52w_high.get(ticker)
        high_gap = (last / prior_high - 1) * 100 if pd.notna(prior_high) and prior_high > 0 else np.nan
        # Duration proxy: consecutive completed weeks in Stage 2, capped at 16.
        stages = stage_frame[ticker]
        duration = 0
        for value in reversed(stages.tolist()):
            if value != STAGE_2:
                break
            duration += 1
        points = [
            20 * np.clip(mom12 / 50, 0, 1) if pd.notna(mom12) else 0,
            25 * np.clip(excess13 / 40, 0, 1) if pd.notna(excess13) else 0,
            15 * np.clip(((last / ma - 1) * 100) / 40, 0, 1) if pd.notna(ma) and ma > 0 else 0,
            15 * np.clip(slope_value / 3, 0, 1) if pd.notna(slope_value) else 0,
            15 * np.clip((high_gap + 20) / 20, 0, 1) if pd.notna(high_gap) else 0,
            10 * np.clip(min(duration / 4, (16 - duration) / 8), 0, 1),
        ]
        score_map[ticker] = (round(float(sum(points))), round(float(mom12), 1) if pd.notna(mom12) else np.nan, round(float(excess13), 1) if pd.notna(excess13) else np.nan)
    result["v8 Setup Score"] = result["Symbol"].map(lambda x: score_map.get(f"{x}.NS", (np.nan, np.nan, np.nan))[0])
    result["Prior 12W %"] = result["Symbol"].map(lambda x: score_map.get(f"{x}.NS", (np.nan, np.nan, np.nan))[1])
    result["13W Excess vs NIFTY500 (pp)"] = result["Symbol"].map(lambda x: score_map.get(f"{x}.NS", (np.nan, np.nan, np.nan))[2])
    result["Strict Quality"] = (
        (result["Stage"] == STAGE_2) & (result["RS Rank"] >= 80)
        & (result["30W Slope %"] > 0) & (result["Volume vs 10W Avg"] >= 1.5)
        & (result["Sector Median RS"] >= 60) & result["Gap vs 30W %"].between(0, 15)
    )
    return result, sectors, len(universe), len(prices.columns), current_week_label, previous_week_label


def render_nse500_screener():
    with st.expander("NSE 500 · Relative Strength & Weinstein Stage Screener", expanded=False):
        st.markdown("""<div style="font-size:clamp(24px,3vw,34px);font-weight:850;background:linear-gradient(90deg,#f97316,#facc15,#22c55e);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">NSE 500 · Relative Strength & Weinstein Stage Screener</div>""", unsafe_allow_html=True)
        st.markdown("**Find market leaders, fresh Stage 2 breakouts and sector participation across the full NSE 500.**")
        st.caption(
            "RS is a 1–99 percentile rank using a 3M/6M/12M momentum blend versus the scanned NSE 500 universe. "
            "Weinstein stages match the supplied weekly Pine engine: 30-week SMA, two-week SMA slope and ATR(14) flatness. "
            "A fresh Stage 2 entry means the stock was Stage 1 in the previous completed week and is Stage 2 now."
        )
        # Automatically calculate the screener when the section is first opened.
        # The calculation is cached, so reruns and repeat visits reuse recent results.
        if "nse500_rs_stage_results" not in st.session_state:
            with st.spinner("Loading NSE 500 screener for the first time…"):
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
            strict_stage2 = results[results["Strict Quality"]].copy().sort_values(["Breakout Quality", "RS Rank"], ascending=[False, False]).head(20)
            ranked_stage2 = current_stage2.sort_values(["Breakout Quality", "RS Rank"], ascending=[False, False])

            st.markdown("""<div style="font-size:23px;font-weight:850;background:linear-gradient(90deg,#22c55e,#38bdf8);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Stage 2 · Breakout Quality Radar</div>""", unsafe_allow_html=True)
            st.caption("A ranking aid, not a prediction. Strict mode applies hard confirmation filters and shows up to 20 strongest candidates; Broad mode ranks every current Stage 2 stock.")
            quality_mode = st.radio("Stage 2 view", ["Strict · Top 20", "Broad · All Stage 2"], horizontal=True, key="nse500_quality_mode")
            quality_frame = strict_stage2 if quality_mode == "Strict · Top 20" else ranked_stage2
            quality_cols = ["Symbol", "Company", "Sector", "Price", "RS Rank", "Breakout Quality", "Volume vs 10W Avg", "Sector Median RS", "30W Slope %", "Gap vs 30W %", "Base Count"]
            if quality_frame.empty:
                st.info("No stocks currently meet all strict breakout-quality filters. Switch to Broad mode to inspect all Stage 2 stocks.")
            else:
                st.dataframe(quality_frame[quality_cols], use_container_width=True, hide_index=True, height=min(520, 45 + 38 * len(quality_frame)))
                st.download_button("Download Stage 2 quality list (CSV)", quality_frame[quality_cols].to_csv(index=False).encode("utf-8"), "nse500_stage2_breakout_quality.csv", "text/csv", key="download_nse500_quality")

            st.markdown("#### v8 · Current Stage 2 Winner-Profile Watchlist")
            st.caption("All current Stage 2 stocks ranked by historical winner-profile resemblance. Uses 12W momentum, 13W excess vs NIFTY 500, 30W position/slope, prior 52W high and a current Stage 2 duration proxy (not historical B1 base duration). Research ranking only; not a probability or buy signal. Missing benchmark data earns no relative-strength points.")
            v8_frame = current_stage2.sort_values(["v8 Setup Score", "RS Rank"], ascending=[False, False])
            v8_cols = ["Symbol", "Company", "Sector", "Price", "v8 Setup Score", "Prior 12W %", "13W Excess vs NIFTY500 (pp)", "RS Rank", "30W Slope %", "Gap vs 30W %", "Base Count", "New Stage 2"]
            st.dataframe(v8_frame[v8_cols], use_container_width=True, hide_index=True, height=400)
            st.download_button("Download v8 Stage 2 watchlist (CSV)", v8_frame[v8_cols].to_csv(index=False).encode("utf-8"), "nse500_v8_current_stage2.csv", "text/csv", key="download_nse500_v8_stage2")

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

            # Strong-momentum subset of the exact same Pine-confirmed fresh Stage 2 transitions.
            # Thresholds are intentionally explicit; missing values do not qualify.
            fresh_momentum = fresh[
                (fresh["RS Rank"] >= 70)
                & (fresh["12W Return %"] >= 15)
                & (fresh["4W Return %"] > 0)
                & (fresh["30W Slope %"] > 0)
                & (fresh["Gap vs 30W %"].between(0, 15))
                & (fresh["Volume vs 10W Avg"] >= 1.2)
            ].copy().sort_values(
                ["RS Rank", "12W Return %", "Volume vs 10W Avg"],
                ascending=[False, False, False]
            )

            st.markdown("#### 🚀 Fresh Stage 2 · Strong Momentum")
            st.caption(
                "Subset of confirmed Stage 1 → Stage 2 transitions, requiring RS ≥70, "
                "12W return ≥15%, positive 4W return, rising 30W SMA, price 0–15% above "
                "the 30W SMA and latest completed-week volume ≥1.2× the prior 10-week average. "
                "Strict research filter; no matches means no stock currently meets every condition."
            )
            if fresh_momentum.empty:
                st.info("No fresh Stage 2 entry currently meets all strong-momentum filters.")
            else:
                momentum_cols = [
                    "Symbol", "Company", "Sector", "Price", "RS Rank",
                    "4W Return %", "12W Return %", "30W Slope %",
                    "Gap vs 30W %", "Volume vs 10W Avg", "52W High Gap %"
                ]
                st.dataframe(
                    fresh_momentum[momentum_cols],
                    use_container_width=True, hide_index=True,
                    height=min(360, 42 + 38 * len(fresh_momentum))
                )
                st.download_button(
                    "Download Fresh Stage 2 strong-momentum list (CSV)",
                    fresh_momentum[momentum_cols].to_csv(index=False).encode("utf-8"),
                    "nse500_fresh_stage2_strong_momentum.csv", "text/csv",
                    key="download_nse500_fresh_stage2_momentum",
                )

            # Early accumulation candidates: still Stage 1, with positive momentum,
            # price above a gently rising 30W average, and contraction in range/volume.
            early_momentum = results[
                (results["Stage"] == STAGE_1)
                & (results["RS Rank"] >= 70)
                & (results["4W Return %"] > 0)
                & (results["12W Return %"] >= 15)
                & (results["30W Slope %"] > 0)
                & (results["Gap vs 30W %"].between(0, 10))
                & (results["5W / Prior 10W Volume"] <= 1.0)
                & (results["4W Range %"] <= 15)
            ].copy().sort_values(
                ["RS Rank", "12W Return %", "4W Range %"],
                ascending=[False, False, True]
            )

            st.markdown("#### 🟡 Early Momentum · Stage 1 to Stage 2 Candidates")
            st.caption(
                "Pre-transition watchlist: current Stage 1 stocks with RS ≥70, positive 4W return, "
                "12W return ≥15%, positive 30W SMA slope, price 0–10% above the 30W SMA, "
                "5W average volume no higher than the preceding 10W average, and 4W range ≤15%. "
                "These are potential accumulation setups, not confirmed Stage 2 entries."
            )
            if early_momentum.empty:
                st.info("No Stage 1 stock currently meets all early-momentum and contraction conditions.")
            else:
                early_cols = [
                    "Symbol", "Company", "Sector", "Price", "RS Rank",
                    "4W Return %", "12W Return %", "30W Slope %",
                    "Gap vs 30W %", "5W / Prior 10W Volume", "4W Range %", "52W High Gap %"
                ]
                st.dataframe(
                    early_momentum[early_cols],
                    use_container_width=True, hide_index=True,
                    height=min(420, 42 + 38 * len(early_momentum))
                )
                st.download_button(
                    "Download Early Momentum Stage 1 candidates (CSV)",
                    early_momentum[early_cols].to_csv(index=False).encode("utf-8"),
                    "nse500_early_momentum_stage1_candidates.csv", "text/csv",
                    key="download_nse500_early_momentum_stage1",
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

            reclaimed = results[results["Reclaimed 10W"]].copy().sort_values(["RS Rank", "Gap vs 10W %"], ascending=[False, False])
            st.markdown("""<div style="font-size:22px;font-weight:800;background:linear-gradient(90deg,#2dd4bf,#60a5fa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Reclaimed 10-week · Early recovery watchlist</div>""", unsafe_allow_html=True)
            st.caption("Stocks that closed at or below their 10-week SMA in the previous completed week and closed above it in the latest completed week. This is an early recovery signal, not a Weinstein Stage 2 confirmation.")
            if reclaimed.empty:
                st.info("No confirmed 10-week reclaim signals in the latest completed week.")
            else:
                reclaimed_cols = ["Symbol", "Company", "Sector", "Price", "RS Rank", "30W Avg", "Gap vs 10W %", "Stage", "Daily Change %"]
                st.dataframe(reclaimed[reclaimed_cols], use_container_width=True, hide_index=True, height=min(360, 42 + 38 * len(reclaimed)))
                st.download_button(
                    "Download reclaimed 10-week stocks (CSV)",
                    reclaimed[reclaimed_cols].to_csv(index=False).encode("utf-8"),
                    "nse500_reclaimed_10_week.csv", "text/csv",
                    key="download_nse500_reclaimed_10w",
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

            st.markdown("""<div style="font-size:22px;font-weight:800;background:linear-gradient(90deg,#34d399,#60a5fa);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Sector strength · median RS and advancing participation</div>""", unsafe_allow_html=True)
            st.dataframe(sectors, use_container_width=True, hide_index=True, height=230)

            # Sector-wise Stage 2 participation: fresh Stage 2 entries and B2 base stocks.
            fresh_sector_stocks = results[
                (results["Stage"] == STAGE_2) & results["New Stage 2"]
            ].copy()
            b2_sector_stocks = results[
                (results["Stage"] == STAGE_2) & (results["Base Count"] == 2)
            ].copy()

            def _stage2_sector_summary(frame):
                if frame.empty:
                    return pd.DataFrame(columns=["Sector", "Stocks", "Symbols"])
                summary = frame.groupby("Sector", dropna=False).agg(
                    Stocks=("Symbol", "count"),
                    Symbols=("Symbol", lambda values: ", ".join(sorted(map(str, values)))),
                ).reset_index()
                return summary.sort_values(["Stocks", "Sector"], ascending=[False, True])

            fresh_sector_summary = _stage2_sector_summary(fresh_sector_stocks)
            b2_sector_summary = _stage2_sector_summary(b2_sector_stocks)
            st.markdown("""<div style="font-size:22px;font-weight:800;background:linear-gradient(90deg,#22c55e,#38bdf8);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Stage 2 sectors · Fresh entries (B1)</div>""", unsafe_allow_html=True)
            st.caption("Sectors ranked by the number of stocks that moved from Stage 1 to Stage 2 in the latest completed week.")
            if fresh_sector_summary.empty:
                st.info("No fresh Stage 2 entries are currently confirmed.")
            else:
                st.dataframe(fresh_sector_summary, use_container_width=True, hide_index=True, height=min(320, 45 + 38 * len(fresh_sector_summary)))

            st.markdown("""<div style="font-size:22px;font-weight:800;background:linear-gradient(90deg,#f59e0b,#f472b6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Stage 2 sectors · B2 stocks</div>""", unsafe_allow_html=True)
            st.caption("Sectors ranked by the number of current Stage 2 stocks that have completed their second confirmed base.")
            if b2_sector_summary.empty:
                st.info("No current Stage 2 B2 stocks are detected.")
            else:
                st.dataframe(b2_sector_summary, use_container_width=True, hide_index=True, height=min(320, 45 + 38 * len(b2_sector_summary)))

            st.markdown('<div id="full-nse-500-stock-screener"></div>', unsafe_allow_html=True)
            st.markdown("""<div style="font-size:22px;font-weight:800;background:linear-gradient(90deg,#fbbf24,#fb7185);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Full NSE 500 stock screener</div>""", unsafe_allow_html=True)
            stage_selection = st.selectbox(
                "Select Stage",
                ["All Stage 2", "Fresh Stage 2 (B1)", "Stage 2 – B2 Stocks (Fresh)", "Stage 2 – B2 Stocks"],
                key="nse500_base_stage_filter",
            )
            c1, c2, c3 = st.columns([1.2, 1, 1])
            with c1:
                query = st.text_input("Search symbol / company", key="nse500_search")
            with c2:
                stage_filter = st.multiselect("Weinstein stage", STAGES, default=[STAGE_2], key="nse500_stage_filter")
            with c3:
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
            columns = ["Symbol", "Company", "Sector", "Price", "RS Rank", "Stage", "Previous Stage", "New Stage 2", "Reclaimed 10W", "Gap vs 10W %", "Base Count", "Fresh B2", "Breakout Quality", "Volume vs 10W Avg", "Gap vs 30W %", "Extension", "Daily Change %"]
            st.dataframe(filtered[columns], use_container_width=True, hide_index=True, height=520)
            st.download_button(
                "Download filtered NSE 500 screener (CSV)",
                filtered[columns].to_csv(index=False).encode("utf-8"),
                "nse500_relative_strength_weinstein_stage.csv", "text/csv",
                key="download_nse500_rs_stage",
            )
            st.caption(
                "Stage 2 follows the supplied Pine rules: close above the 30W SMA, positive two-week SMA slope and non-flat ATR(14) slope. "
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
