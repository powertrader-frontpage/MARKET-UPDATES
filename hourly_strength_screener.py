from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import streamlit as st
import yfinance as yf

IST = ZoneInfo("Asia/Kolkata")
HOURLY_TTL_SECONDS = 900


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
    all_tickers = sorted(set(sector_tickers) | set(micro_symbols) | {"^NSEI", "^CRSLDX"})
    try:
        raw = yf.download(all_tickers, period="6mo", interval="1d", auto_adjust=False,
                          group_by="ticker", threads=False, progress=False, timeout=35)
    except Exception:
        return {}, {}
    def close_for(ticker):
        try:
            frame = raw[ticker] if isinstance(raw.columns, pd.MultiIndex) and ticker in raw.columns.get_level_values(0) else raw.xs(ticker, axis=1, level=1) if isinstance(raw.columns, pd.MultiIndex) and ticker in raw.columns.get_level_values(1) else raw
            close = pd.to_numeric(frame["Close"], errors="coerce").dropna()
            close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
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
        series = [close_for(symbol) for symbol in symbols]
        series = [item for item in series if len(item) >= 22]
        if len(series) < 3 or len(nifty) < 22:
            continue
        constituent = pd.concat(series, axis=1).dropna(how="all")
        basket = constituent.pct_change().add(1).cumprod().mean(axis=1).dropna()
        aligned = pd.concat([basket.rename("micro"), nifty.rename("nifty")], axis=1).dropna()
        if len(aligned) < 22:
            continue
        rel1 = ((aligned["micro"].iloc[-1] / aligned["micro"].iloc[-22] - 1) -
                (aligned["nifty"].iloc[-1] / aligned["nifty"].iloc[-22] - 1)) * 100
        rel3 = ((aligned["micro"].iloc[-1] / aligned["micro"].iloc[-min(64, len(aligned))] - 1) -
                (aligned["nifty"].iloc[-1] / aligned["nifty"].iloc[-min(64, len(aligned))] - 1)) * 100 if len(aligned) >= 64 else np.nan
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
            sector = str(row.get("Sector", ""))
            zone = broad.get(sector)
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

    # Use the existing terminal Sector Rotation / Micro Sector Rotation zone definitions.
    result = _sector_strength_columns(result, sector_tickers, micro_sectors)

    latest = completed["Datetime"].max()
    st.caption(f"Latest completed candle in dataset: {latest.strftime('%d %b %Y, %I:%M %p IST')} · Stocks classified: {len(result)} / {len(tickers)} · Unavailable/failed tickers: {len(failures)}")
    if failures:
        st.caption("Some symbols did not return hourly history. Missing data is excluded, not replaced.")

    order = {"Strong": 0, "Emerging": 1, "Developing": 2, "Weakening": 3}
    result["_rank"] = result["Signal"].map(order).fillna(4)
    result = result.sort_values(["_rank", "1H Volume / Prior 20H Avg", "Bull / Bear Volume"], ascending=[True, False, False]).drop(columns="_rank")
    selected = st.multiselect("Signal categories", ["Strong", "Emerging", "Developing", "Weakening"], default=["Strong", "Emerging"], key="hourly_strength_categories")
    view = result[result["Signal"].isin(selected)].copy()

    c1, c2, c3, c4 = st.columns(4)
    for col, signal in zip((c1, c2, c3, c4), ("Strong", "Emerging", "Developing", "Weakening")):
        col.metric(signal, int((result["Signal"] == signal).sum()))
    st.caption("Strong = higher-high/higher-low structure, EMA momentum, stronger bullish participation and quieter pullbacks. Emerging = improving momentum/volume without every structural condition. These are heuristic labels, not calibrated probabilities.")
    if view.empty:
        st.info("No stocks match the selected hourly signal categories.")
    else:
        cols = ["Symbol", "Company", "Sector", "Sector strength", "Strength source",
                "Price", "Signal", "1H Volume / Prior 20H Avg",
                "Bull / Bear Volume", "Bullish candles (12H)", "Quiet pullbacks",
                "Higher highs & lows", "EMA momentum", "Last completed candle"]
        def _sector_status_style(value):
            colors = {
                "Leading": "background-color: #14532d; color: #bbf7d0; font-weight: 700;",
                "Improving": "background-color: #164e63; color: #a5f3fc; font-weight: 700;",
                "Neutral": "background-color: #374151; color: #e5e7eb;",
                "Weakening": "background-color: #7f1d1d; color: #fecaca; font-weight: 700;",
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
