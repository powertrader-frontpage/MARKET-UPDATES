from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import streamlit as st
import yfinance as yf

IST = ZoneInfo("Asia/Kolkata")
HOURLY_TTL_SECONDS = 900
WEEKLY_STAGES = ["Stage 1 · Base", "Stage 2 · Uptrend", "Stage 3 · Top", "Stage 4 · Downtrend"]


@st.cache_data(ttl=900, show_spinner=False)
def _weekly_stage_lookup(fyers_app_id: str, fyers_access_token: str):
    """Use the exact weekly Weinstein classifications from the existing NSE 500 screener."""
    from nse500_screener import _calculate
    results = _calculate(fyers_app_id, fyers_access_token)[0]
    if results is None or results.empty or not {"Symbol", "Stage"}.issubset(results.columns):
        return {}
    return dict(zip(results["Symbol"].astype(str).str.replace(".NS", "", regex=False),
                    results["Stage"].astype(str)))


@st.cache_data(ttl=86400, show_spinner=False)
def _nse500_universe():
    from nse500_screener import _constituents
    return _constituents()


@st.cache_data(ttl=HOURLY_TTL_SECONDS, show_spinner=False)
def _download_hourly(tickers: tuple[str, ...]):
    """Yahoo 60-minute OHLCV. Fetch in modest batches to avoid oversized requests."""
    parts = []
    failures = []
    for start in range(0, len(tickers), 40):
        batch = list(tickers[start:start + 40])
        try:
            raw = yf.download(
                tickers=batch, period="30d", interval="60m",
                auto_adjust=False, group_by="ticker", threads=False,
                progress=False, timeout=30, prepost=False,
            )
            if raw is None or raw.empty:
                failures.extend(batch)
                continue
            if isinstance(raw.columns, pd.MultiIndex):
                level0 = set(map(str, raw.columns.get_level_values(0)))
                level1 = set(map(str, raw.columns.get_level_values(1)))
                for ticker in batch:
                    try:
                        frame = raw[ticker] if ticker in level0 else raw.xs(ticker, axis=1, level=1) if ticker in level1 else None
                        if frame is not None and {"Open", "High", "Low", "Close", "Volume"}.issubset(frame.columns):
                            frame = frame[["Open", "High", "Low", "Close", "Volume"]].copy()
                            frame["Ticker"] = ticker
                            parts.append(frame.reset_index().rename(columns={frame.index.name or "Datetime": "Datetime"}))
                        else:
                            failures.append(ticker)
                    except Exception:
                        failures.append(ticker)
            elif len(batch) == 1 and {"Open", "High", "Low", "Close", "Volume"}.issubset(raw.columns):
                frame = raw[["Open", "High", "Low", "Close", "Volume"]].copy()
                frame["Ticker"] = batch[0]
                parts.append(frame.reset_index().rename(columns={frame.index.name or "Datetime": "Datetime"}))
            else:
                failures.extend(batch)
        except Exception:
            failures.extend(batch)
    if not parts:
        return pd.DataFrame(), sorted(set(failures))
    data = pd.concat(parts, ignore_index=True)
    if "Datetime" not in data.columns:
        date_col = next((c for c in data.columns if str(c).lower() in {"datetime", "date"}), None)
        if date_col:
            data = data.rename(columns={date_col: "Datetime"})
    data["Datetime"] = pd.to_datetime(data["Datetime"], errors="coerce")
    if data["Datetime"].dt.tz is None:
        data["Datetime"] = data["Datetime"].dt.tz_localize("UTC").dt.tz_convert(IST)
    else:
        data["Datetime"] = data["Datetime"].dt.tz_convert(IST)
    return data.dropna(subset=["Datetime", "Close"]).sort_values(["Ticker", "Datetime"]), sorted(set(failures))


def _completed_bars(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty:
        return data
    now = pd.Timestamp(datetime.now(IST))
    # Yahoo labels 60-minute candles by their opening timestamp. Exclude any bar
    # whose full 60 minutes have not elapsed; this also excludes the live candle.
    out = data[(data["Datetime"] + pd.Timedelta(minutes=60) <= now) & (data["Datetime"].dt.strftime("%H:%M") != "15:15")].copy()
    return out


def _classify_symbol(frame: pd.DataFrame) -> dict | None:
    frame = frame.sort_values("Datetime").drop_duplicates("Datetime").tail(30).copy()
    if len(frame) < 12:
        return None
    for col in ("Open", "High", "Low", "Close", "Volume"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if len(frame) < 12:
        return None

    recent = frame.tail(12).copy()
    prior_vol = frame["Volume"].shift(1).rolling(20, min_periods=10).mean().iloc[-1]
    last = recent.iloc[-1]
    volume_ratio = float(last["Volume"] / prior_vol) if pd.notna(prior_vol) and prior_vol > 0 else np.nan

    bullish = recent[recent["Close"] > recent["Open"]]["Volume"].sum()
    bearish = recent[recent["Close"] < recent["Open"]]["Volume"].sum()
    bull_bear_ratio = float(bullish / bearish) if bearish > 0 else (float("inf") if bullish > 0 else np.nan)
    bullish_count = int((recent["Close"] > recent["Open"]).sum())
    bearish_count = int((recent["Close"] < recent["Open"]).sum())

    highs = recent["High"].tail(6).to_numpy(dtype=float)
    lows = recent["Low"].tail(6).to_numpy(dtype=float)
    higher_highs = bool(np.all(np.diff(highs[-4:]) > 0))
    higher_lows = bool(np.all(np.diff(lows[-4:]) > 0))
    ema9 = frame["Close"].ewm(span=9, adjust=False).mean().iloc[-1]
    ema20 = frame["Close"].ewm(span=20, adjust=False, min_periods=12).mean().iloc[-1]
    momentum = bool(last["Close"] > ema9 and ema9 > ema20)

    # Compare average volume on green and red candles, not raw totals alone.
    avg_bull_vol = float(bullish / bullish_count) if bullish_count else 0.0
    avg_bear_vol = float(bearish / bearish_count) if bearish_count else np.nan
    quiet_pullbacks = bool(avg_bear_vol < avg_bull_vol) if pd.notna(avg_bear_vol) else False
    structure = higher_highs and higher_lows
    volume_confirm = (pd.notna(volume_ratio) and volume_ratio >= 1.3) or (pd.notna(bull_bear_ratio) and bull_bear_ratio >= 1.5 and quiet_pullbacks)

    if structure and momentum and volume_confirm and quiet_pullbacks:
        signal = "Strong"
    elif momentum and (volume_confirm or (bullish_count >= 7 and quiet_pullbacks)):
        signal = "Emerging"
    elif momentum and not quiet_pullbacks:
        signal = "Weakening"
    else:
        signal = "Developing"

    return {
        "Price": round(float(last["Close"]), 2),
        "Last completed candle": last["Datetime"].strftime("%d %b %H:%M"),
        "1H Volume / Prior 20H Avg": round(volume_ratio, 2) if pd.notna(volume_ratio) else np.nan,
        "Bull / Bear Volume": round(bull_bear_ratio, 2) if np.isfinite(bull_bear_ratio) else (np.inf if bull_bear_ratio == np.inf else np.nan),
        "Bullish candles (12H)": bullish_count,
        "Quiet pullbacks": "Yes" if quiet_pullbacks else "No",
        "Higher highs & lows": "Yes" if structure else "No",
        "EMA momentum": "Yes" if momentum else "No",
        "Signal": signal,
    }


@st.cache_data(ttl=21600, show_spinner=False)
def _rotation_zone_lookup(sector_tickers: tuple, micro_members: tuple):
    """Reuse the daily/weekly rotation-zone definitions used by the terminal's rotation tabs."""
    micro_symbols = sorted({symbol for _, _, symbols in micro_members for symbol in symbols})
    all_tickers = sorted({ticker for _, ticker in sector_tickers} | set(micro_symbols) | {"^NSEI", "^CRSLDX"})
    try:
        raw = yf.download(all_tickers, period="6mo", interval="1d", auto_adjust=False,
                          group_by="ticker", threads=False, progress=False, timeout=35)
    except Exception:
        return {}, {}
    def close_for(ticker):
        try:
            frame = raw[ticker] if isinstance(raw.columns, pd.MultiIndex) and ticker in raw.columns.get_level_values(0) else raw.xs(ticker, axis=1, level=1) if isinstance(raw.columns, pd.MultiIndex) and ticker in raw.columns.get_level_values(1) else raw
            close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
            index = pd.DatetimeIndex(pd.to_datetime(close.index))
            if index.tz is not None:
                index = index.tz_localize(None)
            close.index = index.normalize()
            return close[~close.index.duplicated(keep="last")].sort_index()
        except Exception:
            return pd.Series(dtype=float)

    nifty = close_for("^NSEI")
    if nifty.empty:
        nifty = close_for("^CRSLDX")
    broad = {}
    for name, ticker in sector_tickers:
        sec = close_for(ticker)
        aligned = pd.concat([sec.rename("sector"), nifty.rename("nifty")], axis=1).dropna()
        if len(aligned) < 12:
            continue
        relative = aligned["sector"] / aligned["nifty"] * 100
        rs_mean = relative.rolling(20, min_periods=8).mean()
        rs_momentum = relative.pct_change(5) * 100
        if pd.isna(rs_mean.iloc[-1]) or pd.isna(rs_momentum.iloc[-1]):
            continue
        strong = relative.iloc[-1] >= rs_mean.iloc[-1]
        accelerating = rs_momentum.iloc[-1] >= 0
        broad[name] = ("Leading" if strong and accelerating else
                       "Weakening" if strong else "Improving" if accelerating else "Lagging")

    micro = {}
    for name, group, symbols in micro_members:
        constituent_returns = []
        for symbol in symbols:
            close = close_for(symbol)
            if len(close) < 22:
                continue
            last = float(close.iloc[-1])
            ret1 = (last / float(close.iloc[-min(22, len(close))]) - 1) * 100
            ret3 = (last / float(close.iloc[-min(64, len(close))]) - 1) * 100 if len(close) >= 64 else np.nan
            constituent_returns.append((ret1, ret3))
        if len(constituent_returns) < 3 or len(nifty) < 22:
            continue
        avg1 = float(np.nanmean([item[0] for item in constituent_returns]))
        avg3 = float(np.nanmean([item[1] for item in constituent_returns])) if any(pd.notna(item[1]) for item in constituent_returns) else np.nan
        bench1 = (float(nifty.iloc[-1]) / float(nifty.iloc[-22]) - 1) * 100
        bench3 = (float(nifty.iloc[-1]) / float(nifty.iloc[-min(64, len(nifty))]) - 1) * 100 if len(nifty) >= 64 else np.nan
        rel1 = avg1 - bench1
        rel3 = avg3 - bench3 if pd.notna(avg3) and pd.notna(bench3) else np.nan
        if pd.notna(rel1) and pd.notna(rel3):
            micro[name] = ("Leading" if rel3 >= 0 and rel1 >= 0 else
                           "Weakening" if rel3 >= 0 else
                           "Improving" if rel1 >= 0 else "Lagging")
    return broad, micro


def _sector_strength_columns(result: pd.DataFrame, sector_tickers: dict, micro_sectors: dict) -> pd.DataFrame:
    broad, micro = _rotation_zone_lookup(
        tuple(sorted(sector_tickers.items())),
        tuple((name, group, tuple(symbols)) for name, (group, symbols) in sorted(micro_sectors.items()))
    )
    symbol_to_micro = {}
    for name, (_, symbols) in micro_sectors.items():
        for symbol in symbols:
            symbol_to_micro.setdefault(symbol.replace(".NS", ""), []).append(name)
    group_to_broad = {
        "Pharma": "Pharma", "Technology": "IT", "Industrials": "Infrastructure",
        "Media & Services": "Media", "Financials": "Financial Services",
        "Consumer": "Consumption", "Consumer Durables": "Consumption",
        "Power": "Energy", "Auto": "Auto", "Construction": "Infrastructure",
        "Real Estate": "Realty", "Chemicals": None, "Healthcare": "Pharma",
        "Textiles": None, "Energy": "Energy", "Materials": "Metal",
        "Agri": None, "Services": None, "Transport": None, "Travel": None,
        "Defence": None, "Utilities": None, "Niche": None,
    }
    values = []
    for _, row in result.iterrows():
        symbol = str(row.get("Symbol", "")).strip()
        micro_names = symbol_to_micro.get(symbol, [])
        micro_zones = [micro[name] for name in micro_names if name in micro]
        if micro_zones:
            # Prefer strongest available existing microsector rotation zone for shared memberships.
            order = {"Leading": 0, "Improving": 1, "Weakening": 2, "Lagging": 3}
            zone = sorted(micro_zones, key=lambda value: order[value])[0]
            source = "Microsector rotation"
        else:
            sector = str(row.get("Sector", "")).strip()
            sector_aliases = {
                "Information Technology": "IT", "IT Services": "IT",
                "Pharmaceuticals": "Pharma", "Healthcare": "Pharma",
                "Automobile": "Auto", "Automobiles": "Auto",
                "Metals & Mining": "Metal", "Metals": "Metal",
                "Oil Gas & Consumable Fuels": "Energy", "Oil & Gas": "Energy",
                "FMCG": "FMCG", "Consumer Goods": "FMCG",
                "Real Estate": "Realty", "Financials": "Financial Services",
                "Media": "Media", "Infrastructure": "Infrastructure",
            }
            zone = broad.get(sector_aliases.get(sector, sector))
            source = "Sector rotation" if zone else "Unavailable"
        values.append((zone or "Unavailable", source if zone else "Unavailable"))
    result = result.copy()
    result["Sector strength"] = [item[0] for item in values]
    result["Strength source"] = [item[1] for item in values]
    return result


def render_hourly_strength_screener(sector_tickers: dict, micro_sectors: dict):
    st.title("1-Hour Price–Volume Strength Screener")
    st.caption("NSE 500 · Completed 60-minute candles only · Yahoo Finance intraday data · Research watchlist, not a buy signal")
    st.info("This page is independent of the weekly Stage 2 screener. It looks for hourly demand and momentum, including stocks that may still be in weekly Stage 1.")

    universe = _nse500_universe()
    tickers = tuple(universe["Ticker"].dropna().astype(str).unique())
    refresh = st.button("↻ Refresh hourly data", key="hourly_strength_refresh")
    if refresh:
        _download_hourly.clear()

    with st.spinner(f"Loading completed 1-hour candles for up to {len(tickers)} NSE 500 stocks…"):
        data, failures = _download_hourly(tickers)
    if data.empty:
        st.error("No usable hourly OHLCV data was returned. The source may be temporarily unavailable or throttling requests. No sample data is shown.")
        return

    completed = _completed_bars(data)
    if completed.empty:
        st.warning("No completed hourly candles are available yet. Check again after the next hourly candle closes.")
        return

    lookup = universe.set_index("Ticker")[["Symbol", "Company", "Sector", "Industry"]].to_dict("index")
    rows = []
    for ticker, group in completed.groupby("Ticker", sort=False):
        metrics = _classify_symbol(group)
        if metrics is None:
            continue
        meta = lookup.get(ticker, {})
        rows.append({"Symbol": meta.get("Symbol", ticker), "Company": meta.get("Company", ""),
                     "Sector": meta.get("Sector", "Unclassified"), "Industry": meta.get("Industry", "Unclassified"),
                     **metrics})
    result = pd.DataFrame(rows)
    if result.empty:
        st.warning("Hourly history was returned, but not enough completed candles were available to classify stocks.")
        return

    # Use existing rotation zones where available. A failure in this optional context
    # must never prevent the core hourly screener from rendering.
    try:
        result = _sector_strength_columns(result, sector_tickers, micro_sectors)
    except Exception as exc:
        result = result.copy()
        result["Sector strength"] = "Unavailable"
        result["Strength source"] = "Unavailable"
        st.warning(f"Sector-rotation context is temporarily unavailable; hourly stock signals are unaffected. ({type(exc).__name__})")

    # Attach the exact weekly Weinstein stage classification used by the NSE 500 screener.
    try:
        fyers_app_id = str(st.secrets.get("FYERS_APP_ID", "")).strip()
    except Exception:
        fyers_app_id = ""
    fyers_access_token = str(st.session_state.get("fyers_access_token", "") or "")
    try:
        stage_map = _weekly_stage_lookup(fyers_app_id, fyers_access_token)
    except Exception as exc:
        stage_map = {}
        st.warning(f"Weekly stage data is temporarily unavailable; stage filtering is not applied. ({type(exc).__name__})")
    result["Weekly Stage"] = result["Symbol"].astype(str).map(stage_map).fillna("Unavailable")

    latest = completed["Datetime"].max()
    st.caption(f"Latest completed candle in dataset: {latest.strftime('%d %b %Y, %I:%M %p IST')} · Stocks classified: {len(result)} / {len(tickers)} · Unavailable/failed tickers: {len(failures)}")
    if failures:
        st.caption("Some symbols did not return hourly history. Missing data is excluded, not replaced.")

    order = {"Strong": 0, "Emerging": 1, "Developing": 2, "Weakening": 3}
    result["_rank"] = result["Signal"].map(order).fillna(4)
    result = result.sort_values(["_rank", "1H Volume / Prior 20H Avg", "Bull / Bear Volume"], ascending=[True, False, False]).drop(columns="_rank")
    selected = st.multiselect("Signal categories", ["Strong", "Emerging", "Developing", "Weakening"], default=["Strong", "Emerging"], key="hourly_strength_categories")
    selected_stages = st.multiselect("Weekly Weinstein stages", WEEKLY_STAGES, default=WEEKLY_STAGES, format_func=lambda value: value.replace(" · ", " — "), key="hourly_strength_weekly_stages")
    view = result[result["Signal"].isin(selected) & result["Weekly Stage"].isin(selected_stages)].copy()

    c1, c2, c3, c4 = st.columns(4)
    for col, signal in zip((c1, c2, c3, c4), ("Strong", "Emerging", "Developing", "Weakening")):
        col.metric(signal, int((result["Signal"] == signal).sum()))
    st.caption("Strong = higher-high/higher-low structure, EMA momentum, stronger bullish participation and quieter pullbacks. Emerging = improving momentum/volume without every structural condition. These are heuristic labels, not calibrated probabilities.")
    if view.empty:
        st.info("No stocks match the selected hourly signal categories.")
    else:
        cols = ["Symbol", "Company", "Weekly Stage", "Sector", "Sector strength", "Strength source",
                "Price", "Signal", "1H Volume / Prior 20H Avg",
                "Bull / Bear Volume", "Bullish candles (12H)", "Quiet pullbacks",
                "Higher highs & lows", "EMA momentum", "Last completed candle"]
        def _sector_status_style(value):
            colors = {
                "Leading": "background-color: #14532d; color: #bbf7d0; font-weight: 700;",
                "Improving": "background-color: #164e63; color: #a5f3fc; font-weight: 700;",
                "Neutral": "background-color: #374151; color: #e5e7eb;",
                "Weakening": "background-color: #92400e; color: #fde68a; font-weight: 700;",
                "Lagging": "background-color: #7f1d1d; color: #fecaca; font-weight: 700;",
                "Unavailable": "background-color: #374151; color: #9ca3af;",
            }
            return colors.get(str(value), "")

        styled_view = view[cols].style.map(_sector_status_style, subset=["Sector strength"])
        st.dataframe(styled_view, use_container_width=True, hide_index=True, height=560)
        st.download_button("Download hourly strength watchlist (CSV)", view[cols].to_csv(index=False).encode("utf-8"),
                           "nse500_1h_price_volume_strength.csv", "text/csv", key="download_hourly_strength")
    sector_summary = result.groupby("Sector").agg(
        Stocks=("Symbol", "count"),
        Strong=("Signal", lambda x: int((x == "Strong").sum())),
        Emerging=("Signal", lambda x: int((x == "Emerging").sum())),
        Sector_strength=("Sector strength", "first"),
    ).reset_index().rename(columns={"Sector_strength": "Sector strength"})
    sector_summary = sector_summary.sort_values(["Strong", "Emerging", "Stocks"], ascending=False)
    st.caption("Sector strength is taken from the terminal's existing Sector Rotation or Micro Sector Rotation zone classifications. It is context only and does not filter stocks or change hourly stock rankings.")
    with st.expander("Sector participation in hourly strength", expanded=False):
        st.dataframe(sector_summary, use_container_width=True, hide_index=True)
