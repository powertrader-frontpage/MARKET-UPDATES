from __future__ import annotations

import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf


@st.cache_data(ttl=900, show_spinner=False)
def _fo_symbols():
    """Read NSE's published futures market-lot file; fail closed if it cannot be verified."""
    url = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        response.raise_for_status()
        frame = pd.read_csv(pd.io.common.BytesIO(response.content))
        symbol_col = next((c for c in frame.columns if str(c).strip().lower() in {"symbol", "underlying"}), None)
        if symbol_col is None:
            return set(), "NSE F&O market-lot file did not expose a recognised symbol column."
        values = {str(x).strip().upper() + ".NS" for x in frame[symbol_col].dropna() if str(x).strip()}
        return values, None
    except Exception as exc:
        return set(), f"NSE F&O universe unavailable ({type(exc).__name__}); F&O-only ranking withheld."


@st.cache_data(ttl=900, show_spinner=False)
def _download_daily(symbols):
    symbols = tuple(sorted(set(symbols)))
    if not symbols:
        return pd.DataFrame()
    try:
        return yf.download(list(symbols), period="5y", interval="1d", group_by="ticker",
                           auto_adjust=True, progress=False, threads=False)
    except Exception:
        return pd.DataFrame()


def _series(raw, symbol, field):
    try:
        if isinstance(raw.columns, pd.MultiIndex):
            if (symbol, field) in raw.columns:
                out = raw[(symbol, field)]
            elif (field, symbol) in raw.columns:
                out = raw[(field, symbol)]
            else:
                return pd.Series(dtype=float)
        else:
            if field not in raw.columns:
                return pd.Series(dtype=float)
            out = raw[field]
        return pd.to_numeric(out, errors="coerce").dropna().sort_index()
    except Exception:
        return pd.Series(dtype=float)


def _metrics(close, volume):
    close = pd.to_numeric(close, errors="coerce").dropna().sort_index()
    if len(close) < 210:
        return None
    daily200 = close.ewm(span=200, adjust=False, min_periods=200).mean()
    weekly = close.resample("W-FRI").last().dropna()
    weekly200 = weekly.ewm(span=200, adjust=False, min_periods=200).mean()
    last, prev = float(close.iloc[-1]), float(close.iloc[-2])
    d200, p200 = float(daily200.iloc[-1]), float(daily200.iloc[-2])
    w200 = float(weekly200.iloc[-1]) if len(weekly200) and pd.notna(weekly200.iloc[-1]) else np.nan
    ret = lambda n: (last / float(close.iloc[-n-1]) - 1) * 100 if len(close) > n and close.iloc[-n-1] else np.nan
    vol_avg = volume.shift(1).rolling(20, min_periods=20).mean().iloc[-1] if len(volume) else np.nan
    vol_ratio = float(volume.iloc[-1] / vol_avg) if len(volume) and pd.notna(vol_avg) and vol_avg > 0 else np.nan
    below_d = last < d200
    below_w = pd.notna(w200) and last < w200
    cross = prev >= p200 and last < d200
    prior20_low = float(close.iloc[-21:-1].min()) if len(close) >= 21 else np.nan
    breakdown = last < prior20_low if pd.notna(prior20_low) else False
    return {"Price":last, "1W %":ret(5), "1M %":ret(21), "3M %":ret(63),
            "Daily 200 EMA gap %":(last/d200-1)*100, "Weekly 200 EMA gap %":(last/w200-1)*100 if pd.notna(w200) and w200 else np.nan,
            "Below Daily 200 EMA":below_d, "Below Weekly 200 EMA":below_w,
            "Fresh bearish 200 EMA cross":cross, "20D low breakdown":breakdown,
            "Volume / 20D avg":vol_ratio, "Data date":close.index[-1].strftime("%d %b %Y")}


def render_weakness_breakdown_radar(sector_stocks, sector_tickers):
    st.markdown("## 📉 Weakness & Breakdown Radar")
    st.caption("Independent bearish research page · sector context uses the mapped NSE sector baskets · stock trade candidates are restricted to the verified NSE F&O universe.")
    fo, fo_error = _fo_symbols()
    sector_map = {str(k): tuple(sorted(set(v))) for k, v in sector_stocks.items()}
    all_symbols = sorted({s for basket in sector_map.values() for s in basket})
    benchmark_symbols = ["^NSEI", "^CRSLDX"]
    with st.expander("Universe & data status", expanded=False):
        st.write(f"Mapped sector constituents: {len(all_symbols)} across {len(sector_map)} sector baskets.")
        st.write(f"Verified F&O symbols: {len(fo)}.")
        if fo_error:
            st.warning(fo_error)
        st.caption("This first version uses Yahoo Finance adjusted daily history for the bulk constituent matrix. It does not substitute missing values; FYERS-validated stock history integration is a follow-up before describing this page as FYERS-primary.")
    if not all_symbols:
        st.warning("No sector constituent mapping was supplied; radar is withheld.")
        return
    symbols = list(dict.fromkeys(all_symbols + benchmark_symbols))
    with st.spinner(f"Loading daily history for {len(symbols)} symbols…"):
        raw = _download_daily(tuple(symbols))
    nifty = _series(raw, "^NSEI", "Close")
    if raw.empty or len(nifty) < 65:
        st.error("Market history is unavailable or too short. No rankings have been fabricated.")
        return
    nifty_ret = {n: (float(nifty.iloc[-1]) / float(nifty.iloc[-n-1]) - 1) * 100 if len(nifty) > n else np.nan for n in (5,21,63)}
    stock_rows = []
    for sector, basket in sector_map.items():
        for symbol in basket:
            close, volume = _series(raw, symbol, "Close"), _series(raw, symbol, "Volume")
            m = _metrics(close, volume)
            if not m:
                continue
            m["Stock"] = symbol.removesuffix(".NS")
            m["Sector"] = sector
            m["1M RS vs NIFTY (pp)"] = m["1M %"] - nifty_ret[21] if pd.notna(m["1M %"]) else np.nan
            m["3M RS vs NIFTY (pp)"] = m["3M %"] - nifty_ret[63] if pd.notna(m["3M %"]) else np.nan
            m["F&O eligible"] = symbol in fo
            stock_rows.append(m)
    stocks = pd.DataFrame(stock_rows)
    if stocks.empty:
        st.warning("No mapped constituents returned sufficient 200-session history.")
        return
    # Sector strength and breadth are computed from all mapped constituents, not just F&O names.
    sector_rows = []
    for sector, frame in stocks.groupby("Sector"):
        sector_rows.append({
            "Sector":sector, "Stocks measured":len(frame),
            "Average 1W %":frame["1W %"].mean(), "Average 1M %":frame["1M %"].mean(),
            "Average 3M %":frame["3M %"].mean(),
            "1M RS vs NIFTY (pp)":frame["1M RS vs NIFTY (pp)"].mean(),
            "3M RS vs NIFTY (pp)":frame["3M RS vs NIFTY (pp)"].mean(),
            "Below Daily 200 EMA %":frame["Below Daily 200 EMA"].mean()*100,
            "Below Weekly 200 EMA %":frame["Below Weekly 200 EMA"].mean()*100,
            "Fresh bearish crosses":int(frame["Fresh bearish 200 EMA cross"].sum()),
        })
    sectors = pd.DataFrame(sector_rows)
    sectors["Weakness score"] = (
        (sectors["Average 1M %"].rank(pct=True, ascending=True)*25) +
        (sectors["Average 3M %"].rank(pct=True, ascending=True)*25) +
        (sectors["Below Daily 200 EMA %"].rank(pct=True)*25) +
        (sectors["Below Weekly 200 EMA %"].rank(pct=True)*25)
    )
    sectors = sectors.sort_values("Weakness score", ascending=False).reset_index(drop=True)
    sectors.insert(0,"Rank",np.arange(1,len(sectors)+1))
    st.markdown("### 1. Weak Sector Ranking")
    st.caption("All available mapped constituents contribute to sector breadth. A sector score ranks relative weakness within this displayed universe; it is not a probability of further decline.")
    st.dataframe(sectors.style.format({c:"{:.1f}" for c in sectors.select_dtypes(include="number").columns if c not in {"Rank","Stocks measured","Fresh bearish crosses"}}),use_container_width=True,hide_index=True)
    st.download_button("⬇ Download weak sector ranking",sectors.to_csv(index=False).encode("utf-8-sig"),"weak_sector_ranking.csv","text/csv",key="weak_sector_csv")
    st.markdown("### 2. Weak F&O Stocks")
    fo_stocks = stocks[stocks["F&O eligible"]].copy()
    if fo_stocks.empty:
        st.warning("The verified F&O universe is unavailable or no mapped stocks overlap it. F&O shortlist is withheld rather than inferred.")
    else:
        fo_stocks["Bearish confirmations"] = fo_stocks[["Below Daily 200 EMA","Below Weekly 200 EMA","Fresh bearish 200 EMA cross","20D low breakdown"]].sum(axis=1)
        fo_stocks["Volume confirmation"] = fo_stocks["Volume / 20D avg"] >= 1.2
        fo_stocks["Classification"] = np.select(
            [(fo_stocks["Below Daily 200 EMA"] & fo_stocks["Below Weekly 200 EMA"] & (fo_stocks["Volume / 20D avg"] >= 1.2) & (fo_stocks["1M RS vs NIFTY (pp)"] < 0)),
             (fo_stocks["Fresh bearish 200 EMA cross"] | fo_stocks["20D low breakdown"])],
            ["A · High-conviction weakness","C · Early breakdown watch"], default="B · Weak / developing")
        fo_stocks = fo_stocks.sort_values(["Bearish confirmations","1M RS vs NIFTY (pp)","3M RS vs NIFTY (pp)"],ascending=[False,True,True])
        st.dataframe(fo_stocks[["Stock","Sector","Classification","Price","1W %","1M %","3M %","1M RS vs NIFTY (pp)","3M RS vs NIFTY (pp)","Daily 200 EMA gap %","Weekly 200 EMA gap %","Volume / 20D avg","Fresh bearish 200 EMA cross","20D low breakdown","Data date"]].head(100),use_container_width=True,hide_index=True)
        st.download_button("⬇ Download weak F&O stocks",fo_stocks.to_csv(index=False).encode("utf-8-sig"),"weak_fo_stocks.csv","text/csv",key="weak_fo_csv")
        st.markdown("### 3. Fresh Breakdown Watchlist")
        fresh = fo_stocks[fo_stocks["Fresh bearish 200 EMA cross"] | fo_stocks["20D low breakdown"]].copy()
        st.dataframe(fresh[["Stock","Sector","Classification","1M %","1M RS vs NIFTY (pp)","Daily 200 EMA gap %","Weekly 200 EMA gap %","Volume / 20D avg","Fresh bearish 200 EMA cross","20D low breakdown","Data date"]],use_container_width=True,hide_index=True)
        st.download_button("⬇ Download fresh breakdown watchlist",fresh.to_csv(index=False).encode("utf-8-sig"),"fresh_breakdown_watchlist.csv","text/csv",key="fresh_breakdown_csv")
        st.markdown("### 4. Shortlist for Analysis")
        shortlist = fo_stocks[(fo_stocks["Classification"]=="A · High-conviction weakness") & (fo_stocks["Volume / 20D avg"]>=1.2)].head(20)
        st.dataframe(shortlist[["Stock","Sector","Classification","1M RS vs NIFTY (pp)","3M RS vs NIFTY (pp)","Daily 200 EMA gap %","Weekly 200 EMA gap %","Volume / 20D avg","Data date"]],use_container_width=True,hide_index=True)
        st.download_button("⬇ Download analysis shortlist",shortlist.to_csv(index=False).encode("utf-8-sig"),"weakness_analysis_shortlist.csv","text/csv",key="weakness_shortlist_csv")
    st.info("Research filter only, not a short-entry instruction. Confirm liquidity, event risk, borrow/derivative constraints and price structure before acting. An extended decline can reverse sharply.")
