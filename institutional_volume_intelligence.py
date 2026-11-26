"""Streamlit page for the HVY/HVQ institutional futures-volume scanner."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import streamlit as st
import yfinance as yf

from institutional_volume_data import (
    load_nse_futures_master,
    fetch_continuous_futures_daily,
    volume_record_flags,
)


def _rsi(series: pd.Series, period: int = 9) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    return result.mask((loss == 0) & (gain > 0), 100.0)


def _cash_filters(frame: pd.DataFrame) -> dict:
    data = frame.dropna(subset=["Close", "Volume"]).copy()
    if len(data) < 50:
        return {}
    close = pd.to_numeric(data["Close"], errors="coerce")
    volume = pd.to_numeric(data["Volume"], errors="coerce")
    turnover = close * volume
    weekly_volume = volume.resample("W-FRI").sum().dropna()
    rsi = _rsi(close, 9)
    rsi_ema3 = rsi.ewm(span=3, adjust=False, min_periods=3).mean()
    cross = (
        len(rsi_ema3.dropna()) >= 2
        and rsi_ema3.iloc[-2] <= 50
        and rsi_ema3.iloc[-1] > 50
    )
    return {
        "Close": float(close.iloc[-1]),
        "Daily change %": float((close.iloc[-1] / close.iloc[-2] - 1) * 100) if len(close) > 1 else np.nan,
        "10W avg volume": float(weekly_volume.tail(10).mean()) if len(weekly_volume) >= 10 else np.nan,
        "50D avg turnover ₹": float(turnover.tail(50).mean()),
        "RSI EMA(3) cross above 50": bool(cross),
        "Close >= previous close": bool(close.iloc[-1] >= close.iloc[-2]),
    }


@st.cache_data(ttl=1800, show_spinner=False)
def _download_cash_history(tickers: tuple[str, ...]) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame()
    return yf.download(
        list(tickers), period="6mo", interval="1d", group_by="ticker",
        auto_adjust=True, progress=False, threads=True
    )


@st.cache_data(ttl=21600, show_spinner=False)
def _market_cap_crore(ticker: str) -> float:
    try:
        cap = yf.Ticker(ticker).fast_info.get("market_cap")
        if cap is None:
            cap = yf.Ticker(ticker).info.get("marketCap")
        return float(cap) / 1e7 if cap else np.nan
    except Exception:
        return np.nan


def _extract_cash_frame(raw: pd.DataFrame, ticker: str, single: bool) -> pd.DataFrame:
    try:
        if isinstance(raw.columns, pd.MultiIndex):
            if (ticker, "Close") in raw.columns:
                frame = raw[ticker].copy()
            elif ("Close", ticker) in raw.columns:
                frame = raw.xs(ticker, axis=1, level=1).copy()
            else:
                return pd.DataFrame()
        elif single:
            frame = raw.copy()
        else:
            return pd.DataFrame()
        frame.index = pd.to_datetime(frame.index)
        return frame.dropna(how="all")
    except Exception:
        return pd.DataFrame()


def render_institutional_volume_intelligence(app_id: str | None, access_token: str | None,
                                              micro_sectors: dict):
    st.markdown(
        """<div style="font-size:clamp(27px,3.7vw,40px);font-weight:850;letter-spacing:-1px;line-height:1.15;margin:8px 0 6px;background:linear-gradient(90deg,#19e6a0,#29c7d6,#4aa8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Institutional Volume Intelligence</div>""",
        unsafe_allow_html=True,
    )
    st.caption("NSE single-stock futures · HVY (252 sessions) and HVQ (63 sessions) · Research watchlist, not a buy/sell signal.")
    st.info("Volume records are calculated from FYERS continuous futures candles. Price, turnover and weekly liquidity filters use cash-equity data. The two volume series are never substituted for one another.")
    if not app_id or not access_token:
        st.warning("Connect FYERS from the Main Dashboard before running the futures-volume scan.")
        return

    membership = {}
    for micro_name, (sector_name, symbols) in micro_sectors.items():
        for symbol in symbols:
            key = str(symbol).replace(".NS", "").strip()
            membership.setdefault(key, {"Sectors": set(), "Microsectors": set()})
            membership[key]["Sectors"].add(str(sector_name))
            membership[key]["Microsectors"].add(str(micro_name))

    with st.expander("Scanner rules", expanded=False):
        st.markdown(
            "- HVY: latest completed futures-volume session equals the maximum of the trailing 252 sessions.\n"
            "- HVQ: latest completed futures-volume session equals the maximum of the trailing 63 sessions.\n"
            "- Cash filters: close ≥ ₹20; market cap ≥ ₹1,000 crore; 10-week average volume > 1,00,000; 50-day average turnover > ₹50 lakh.\n"
            "- Momentum confirmations are displayed separately and do not suppress raw HVY/HVQ events."
        )
    st.caption("FYERS history is requested only after you press Scan. The first full-universe scan can take several minutes; results are cached for 30 minutes.")
    scan = st.button("Run / refresh NSE futures scan", type="primary", key="run_institutional_volume_scan")
    if scan:
        _download_cash_history.clear()
        _market_cap_crore.clear()
        with st.spinner("Loading the current NSE futures universe…"):
            master, master_error = load_nse_futures_master()
        if master.empty:
            st.error(master_error or "No NSE futures contracts were returned.")
            return

        master["Cash ticker"] = master["_underlying"].astype(str).str.strip() + ".NS"
        cash_tickers = tuple(sorted(master["Cash ticker"].dropna().unique()))
        with st.spinner(f"Downloading cash-market liquidity data for {len(cash_tickers)} futures underlyings…"):
            raw = _download_cash_history(cash_tickers)
        cash_ok = []
        cash_rejections = {"No cash history / insufficient rows": 0, "Close below ₹20": 0, "10W average volume <= 100,000": 0, "50D average turnover <= ₹50 lakh": 0}
        for _, contract in master.iterrows():
            ticker = contract["Cash ticker"]
            cash = _extract_cash_frame(raw, ticker, len(cash_tickers) == 1)
            metrics = _cash_filters(cash)
            if not metrics:
                cash_rejections["No cash history / insufficient rows"] += 1
                continue
            if metrics["Close"] < 20:
                cash_rejections["Close below ₹20"] += 1
                continue
            if not np.isfinite(metrics["10W avg volume"]) or metrics["10W avg volume"] <= 100000:
                cash_rejections["10W average volume <= 100,000"] += 1
                continue
            if metrics["50D avg turnover ₹"] <= 5000000:
                cash_rejections["50D average turnover <= ₹50 lakh"] += 1
                continue
            cash_ok.append((contract, ticker, metrics))
        st.session_state["ivi_cash_candidates"] = [
            (str(c["_ticker"]), str(c["_underlying"]), str(t), m)
            for c, t, m in cash_ok
        ]
        st.session_state["ivi_master_count"] = len(master)
        st.session_state["ivi_cash_count"] = len(cash_ok)
        st.session_state["ivi_cash_rejections"] = cash_rejections
        st.session_state.pop("ivi_results", None)

        diagnostics = {"Market cap unavailable / below ₹1,000 Cr": 0, "FYERS history failed / empty": 0, "Insufficient HVY/HVQ history": 0, "History available, no HVY/HVQ record": 0, "HVY/HVQ record found": 0}
        target_symbols = {"BRITANNIA", "KFINTECH", "TORNTPHARM"}
        target_checks = []
        rows = []
        progress = st.progress(0.0, text="Checking market capitalisation and futures-volume records…")
        total = max(len(cash_ok), 1)
        for idx, (contract, ticker, metrics) in enumerate(cash_ok):
            cap_crore = _market_cap_crore(ticker)
            key = str(contract["_underlying"]).replace(".NS", "").upper()
            if not np.isfinite(cap_crore) or cap_crore < 1000:
                diagnostics["Market cap unavailable / below ₹1,000 Cr"] += 1
                progress.progress((idx + 1) / total, text=f"Market-cap filter {idx + 1}/{total}")
                continue
            candles, error = fetch_continuous_futures_daily(app_id, access_token, str(contract["_ticker"]))
            if candles.empty:
                diagnostics["FYERS history failed / empty"] += 1
                if key in target_symbols:
                    target_checks.append({"Symbol": key, "Check": "FYERS history failed / empty", "Detail": error or "No candles returned"})
                progress.progress((idx + 1) / total, text=f"Futures history {idx + 1}/{total}")
                continue
            flags = volume_record_flags(candles)
            if flags.get("HVY") is None or flags.get("HVQ") is None:
                diagnostics["Insufficient HVY/HVQ history"] += 1
            if flags.get("HVY") is not True and flags.get("HVQ") is not True:
                diagnostics["History available, no HVY/HVQ record"] += 1
                if key in target_symbols:
                    target_checks.append({"Symbol": key, "Check": "No HVY/HVQ record in FYERS futures", "Detail": f"Sessions: {flags.get(chr(104)+chr(105)+chr(115)+chr(116)+chr(111)+chr(114)+chr(121)+chr(95)+chr(115)+chr(101)+chr(115)+chr(115)+chr(105)+chr(111)+chr(110)+chr(115), 0)}; HVQ: {flags.get(chr(72)+chr(86)+chr(81)+chr(95)+chr(115)+chr(116)+chr(97)+chr(116)+chr(117)+chr(115))}"})
                progress.progress((idx + 1) / total, text=f"Volume records {idx + 1}/{total}")
                continue
            diagnostics["HVY/HVQ record found"] += 1
            if key in target_symbols:
                target_checks.append({"Symbol": key, "Check": "HVY/HVQ record found", "Detail": f"HVY: {flags.get(chr(72)+chr(86)+chr(89)+chr(95)+chr(115)+chr(116)+chr(97)+chr(116)+chr(117)+chr(115))}; HVQ: {flags.get(chr(72)+chr(86)+chr(81)+chr(95)+chr(115)+chr(116)+chr(97)+chr(116)+chr(117)+chr(115))}"})
            key = str(contract["_underlying"]).replace(".NS", "")
            mapped = membership.get(key, {"Sectors": set(), "Microsectors": set()})
            rows.append({
                "Symbol": key,
                "Volume signal": "HVY + HVQ" if flags.get("HVY") and flags.get("HVQ") else "HVY" if flags.get("HVY") else "HVQ",
                "Futures volume": int(candles["Volume"].iloc[-1]),
                "252D max volume": int(candles["Volume"].tail(252).max()) if len(candles) >= 252 else np.nan,
                "63D max volume": int(candles["Volume"].tail(63).max()) if len(candles) >= 63 else np.nan,
                "Cash close ₹": round(metrics["Close"], 2),
                "Daily change %": round(metrics["Daily change %"], 2),
                "Market cap ₹ Cr": round(cap_crore, 1),
                "10W avg volume": int(metrics["10W avg volume"]),
                "50D avg turnover ₹": round(metrics["50D avg turnover ₹"]),
                "Close >= prev close": metrics["Close >= previous close"],
                "RSI EMA3 cross >50": metrics["RSI EMA(3) cross above 50"],
                "Sector mapping": ", ".join(sorted(mapped["Sectors"])) or "Not mapped",
                "Microsector mapping": ", ".join(sorted(mapped["Microsectors"])) or "Not mapped",
                "Futures data source": "FYERS continuous futures",
                "Signal date": pd.to_datetime(candles["Date"].iloc[-1]).strftime("%Y-%m-%d"),
                "History sessions": flags["history_sessions"],
                "History warning": error or "",
            })
            progress.progress((idx + 1) / total, text=f"Futures records {idx + 1}/{total}")
        progress.empty()
        result = pd.DataFrame(rows)
        if not result.empty:
            result = result.sort_values(
                ["Volume signal", "Market cap ₹ Cr"], ascending=[True, False]
            ).reset_index(drop=True)
        st.session_state["ivi_results"] = result
        st.session_state["ivi_diagnostics"] = diagnostics
        st.session_state["ivi_target_checks"] = target_checks
        st.session_state["ivi_scan_time"] = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %H:%M:%S IST")

    result = st.session_state.get("ivi_results", pd.DataFrame())
    if st.session_state.get("ivi_scan_time"):
        st.caption(f"Last scan: {st.session_state['ivi_scan_time']} · Futures underlyings: {st.session_state.get('ivi_master_count', 0)} · Cash-liquidity candidates: {st.session_state.get('ivi_cash_count', 0)}")
    if "ivi_diagnostics" in st.session_state:
        st.subheader("Scan diagnostics")
        diag = st.session_state["ivi_diagnostics"]
        dcols = st.columns(5)
        dcols[0].metric("Universe", st.session_state.get("ivi_master_count", 0))
        dcols[1].metric("Cash filters passed", st.session_state.get("ivi_cash_count", 0))
        dcols[2].metric("Market cap rejected", diag.get("Market cap unavailable / below ₹1,000 Cr", 0))
        dcols[3].metric("FYERS history failed", diag.get("FYERS history failed / empty", 0))
        dcols[4].metric("Volume records", diag.get("HVY/HVQ record found", 0))
        st.dataframe(pd.DataFrame([{"Filter / outcome": k, "Stocks": v} for k, v in st.session_state.get("ivi_cash_rejections", {}).items()] + [{"Filter / outcome": k, "Stocks": v} for k, v in diag.items()]), use_container_width=True, hide_index=True)
        target_rows = st.session_state.get("ivi_target_checks", [])
        st.markdown("**Reference-stock checks: Britannia, KFin Technologies and Torrent Pharma**")
        if target_rows:
            st.dataframe(pd.DataFrame(target_rows), use_container_width=True, hide_index=True)
        else:
            st.caption("These reference stocks did not reach the FYERS volume check because they did not pass the cash-liquidity and market-cap gates. Their rejection reasons are shown in the diagnostics above where applicable.")
    if isinstance(result, pd.DataFrame) and not result.empty:
        hvy = int(result["Volume signal"].str.contains("HVY").sum())
        hvq = int(result["Volume signal"].str.contains("HVQ").sum())
        c1, c2, c3 = st.columns(3)
        c1.metric("HVY records", hvy)
        c2.metric("HVQ records", hvq)
        c3.metric("Qualifying stocks", len(result))
        st.dataframe(result, use_container_width=True, hide_index=True, height=560)
        st.download_button(
            "Download HVY / HVQ watchlist CSV", result.to_csv(index=False).encode("utf-8"),
            file_name="institutional_hvy_hvq_watchlist.csv", mime="text/csv",
            key="download_institutional_hvy_hvq",
        )
        st.caption("Sector and microsector columns show all existing universe memberships. Rotation-zone readings are not yet joined into this scan output; do not interpret membership alone as a strength confirmation.")
    elif "ivi_scan_time" in st.session_state:
        st.warning("No stocks matched all liquidity filters and HVY/HVQ records. Use the diagnostics above to identify the exclusion stage.")
    else:
        st.info("Press Run / refresh NSE futures scan to build the first HVY/HVQ watchlist.")
