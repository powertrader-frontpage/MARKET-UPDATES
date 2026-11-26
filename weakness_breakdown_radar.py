from __future__ import annotations

import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


OFFICIAL_SECTOR_INDEX_FILES = {
    "Auto": "ind_niftyautolist.csv", "Bank": "ind_niftybanklist.csv",
    "Chemicals": "ind_niftyChemicals_list.csv", "Financial Services": "ind_niftyfinancelist.csv",
    "FMCG": "ind_niftyfmcglist.csv", "Healthcare": "ind_niftyhealthcarelist.csv",
    "IT": "ind_niftyitlist.csv", "Media": "ind_niftymedialist.csv",
    "Metal": "ind_niftymetallist.csv", "Pharma": "ind_niftypharmalist.csv",
    "Private Bank": "ind_nifty_privatebanklist.csv", "PSU Bank": "ind_niftypsubanklist.csv",
    "Realty": "ind_niftyrealtylist.csv", "Consumer Durables": "ind_niftyconsumerdurableslist.csv",
    "Oil & Gas": "ind_niftyoilgaslist.csv", "Power": "ind_niftypowerlist.csv",
    "Capital Goods": "ind_niftycapitalgoodslist.csv", "Cement": "ind_niftycementlist.csv",
    "Construction": "ind_niftyconstructionlist.csv", "Telecommunications": "ind_niftytelecomlist.csv",
    "Nifty500 Healthcare": "ind_nifty500Healthcare_list.csv",
    "MidSmall Financial Services": "ind_niftymidsmallfinancailservice_list.csv",
    "MidSmall Healthcare": "ind_niftymidsmallhealthcare_list.csv",
    "MidSmall IT & Telecom": "ind_niftymidsmallitAndtelecom_list.csv",
}


@st.cache_data(ttl=86400, show_spinner=False)
def _official_sector_universe():
    baskets, errors = {}, []
    base = "https://www.niftyindices.com/IndexConstituent/"
    for sector, filename in OFFICIAL_SECTOR_INDEX_FILES.items():
        try:
            response = requests.get(base + filename, headers={"User-Agent":"Mozilla/5.0"}, timeout=12)
            response.raise_for_status()
            frame = pd.read_csv(pd.io.common.BytesIO(response.content))
            col = next((c for c in frame.columns if str(c).strip().lower()=="symbol"), None)
            if col is None: raise ValueError("Symbol column missing")
            values = sorted({str(v).strip().upper()+".NS" for v in frame[col].dropna() if str(v).strip()})
            if values: baskets["Nifty "+sector] = values
            else: errors.append(sector+": empty file")
        except Exception as exc:
            errors.append(sector+": "+type(exc).__name__)
    return baskets, errors


@st.cache_data(ttl=900, show_spinner=False)
def _fyers_daily_history(app_id, access_token, fyers_symbol, days=1900):
    end = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    start = end - timedelta(days=days)
    try:
        response = requests.get("https://api-t1.fyers.in/data/history",
            params={"symbol":fyers_symbol,"resolution":"D","date_format":"1","range_from":str(start),"range_to":str(end),"cont_flag":"1"},
            headers={"Authorization":f"{app_id}:{access_token}"},timeout=18)
        payload=response.json()
        if not response.ok or payload.get("s")!="ok": return pd.DataFrame()
        candles=payload.get("candles") or []
        if not candles: return pd.DataFrame()
        frame=pd.DataFrame(candles,columns=["Epoch","Open","High","Low","Close","Volume"][:len(candles[0])])
        frame["Date"]=pd.to_datetime(frame["Epoch"],unit="s",utc=True).dt.tz_convert("Asia/Kolkata")
        return frame.set_index("Date")[["Open","High","Low","Close","Volume"]].apply(pd.to_numeric,errors="coerce").dropna(subset=["Close"])
    except Exception:
        return pd.DataFrame()


def _build_price_matrix(symbols, fyers_session):
    symbols=tuple(sorted(set(symbols)))
    prices={}
    failed=list(symbols)
    if fyers_session and fyers_session[0] and fyers_session[1]:
        app_id,token=fyers_session
        failed=[]
        with ThreadPoolExecutor(max_workers=5) as pool:
            jobs={pool.submit(_fyers_daily_history,app_id,token,"NSE:"+sym.removesuffix(".NS")+"-EQ"):sym for sym in symbols}
            for job in as_completed(jobs):
                sym=jobs[job]
                try:
                    frame=job.result()
                    if len(frame)>=210: prices[sym]=frame
                    else: failed.append(sym)
                except Exception: failed.append(sym)
    if failed:
        raw=_download_daily(tuple(failed))
        for sym in failed:
            close=_series(raw,sym,"Close")
            if close.empty: continue
            part=pd.DataFrame({"Close":close})
            part["Volume"]=_series(raw,sym,"Volume").reindex(part.index)
            prices[sym]=part
    return prices, len(symbols)-len(failed), len(failed)


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



def _style_signed_columns(frame, columns):
    """Red for negative performance, green for positive performance."""
    def shade(value):
        if pd.isna(value): return ""
        if value < 0: return "background-color: #5b2525; color: #ffdada;"
        if value > 0: return "background-color: #174b36; color: #d8ffea;"
        return ""
    valid = [col for col in columns if col in frame.columns]
    return frame.style.applymap(shade, subset=valid)


def _style_sector_table(frame):
    styled = _style_signed_columns(frame, [
        "Average 1W %", "Average 1M %", "Average 3M %",
        "1M RS vs NIFTY (pp)", "3M RS vs NIFTY (pp)", "1M RS vs NIFTY 500 (pp)"
    ])
    if "Weakness score" in frame.columns:
        styled = styled.background_gradient(subset=["Weakness score"], cmap="Reds")
    if "Below Daily 200 EMA %" in frame.columns:
        styled = styled.background_gradient(subset=["Below Daily 200 EMA %"], cmap="Oranges")
    if "Below Weekly 200 EMA %" in frame.columns:
        styled = styled.background_gradient(subset=["Below Weekly 200 EMA %"], cmap="Oranges")
    return styled


def _style_stock_table(frame):
    styled = _style_signed_columns(frame, [
        "1W %", "1M %", "3M %", "1M RS vs NIFTY (pp)",
        "1M RS vs NIFTY 500 (pp)", "1M RS vs sector basket (pp)",
        "3M RS vs NIFTY 500 (pp)", "Daily 200 EMA gap %", "Weekly 200 EMA gap %"
    ])
    if "Classification" in frame.columns:
        def classification_row(row):
            label = str(row.get("Classification", ""))
            if label.startswith("A"):
                color = "background-color: #662b2b; color: #ffe1e1;"
            elif label.startswith("C"):
                color = "background-color: #664b1f; color: #fff0c2;"
            else:
                color = ""
            return [color] * len(row)
        styled = styled.apply(classification_row, axis=1)
    if "Sector weak" in frame.columns:
        styled = styled.applymap(
            lambda value: "background-color: #662b2b; color: #ffe1e1; font-weight: bold;" if value is True else "",
            subset=["Sector weak"]
        )
    return styled


def render_weakness_breakdown_radar(sector_stocks, sector_tickers, fyers_session=None):
    st.markdown("## 📉 Weakness & Breakdown Radar")
    st.caption("Independent bearish research page · official Nifty sector-index constituents drive sector context · F&O shortlist is restricted to verified NSE F&O symbols.")
    fo, fo_error = _fo_symbols()
    sector_map = {str(k): tuple(sorted(set(v))) for k, v in sector_stocks.items()}
    official_map, official_errors = _official_sector_universe()
    sector_map.update(official_map)
    all_symbols = sorted({sym for basket in sector_map.values() for sym in basket})
    with st.expander("Universe & data status", expanded=False):
        st.write(f"Official Nifty sector baskets loaded: {len(official_map)} / {len(OFFICIAL_SECTOR_INDEX_FILES)}.")
        st.write(f"Unique mapped constituents: {len(all_symbols)} across {len(sector_map)} baskets.")
        st.write(f"Verified F&O symbols: {len(fo)}.")
        st.write("FYERS session: connected" if fyers_session and fyers_session[1] else "FYERS session: not connected · Yahoo fallback will be used.")
        if fo_error: st.warning(fo_error)
        if official_errors: st.caption("Official index files unavailable: " + "; ".join(official_errors[:8]))
        st.caption("Stock history uses FYERS first when authenticated; Yahoo fills failed or insufficient FYERS symbols. NIFTY benchmark history uses Yahoo Finance.")
    if not all_symbols:
        st.warning("No sector constituents are available; rankings are withheld.")
        return
    with st.spinner(f"Loading daily OHLCV for {len(all_symbols)} unique constituents…"):
        price_map, fyers_count, yahoo_count = _build_price_matrix(all_symbols, fyers_session)
        benchmark_raw = _download_daily(("^NSEI","^CRSLDX"))
    nifty = _series(benchmark_raw,"^NSEI","Close")
    nifty500 = _series(benchmark_raw,"^CRSLDX","Close")
    if not price_map or len(nifty)<65:
        st.error("Insufficient stock or NIFTY history. No rankings have been fabricated.")
        return
    def _returns(series):
        return {n:(float(series.iloc[-1])/float(series.iloc[-n-1])-1)*100 if len(series)>n and float(series.iloc[-n-1]) else np.nan for n in (5,21,63)}
    nr, n5r = _returns(nifty), _returns(nifty500)
    st.caption(f"Price coverage: FYERS primary {fyers_count} symbols · Yahoo fallback used/attempted for {yahoo_count} symbols · index benchmarks: Yahoo Finance.")
    stock_rows=[]
    for sector,basket in sector_map.items():
        for symbol in basket:
            frame=price_map.get(symbol,pd.DataFrame())
            close=pd.to_numeric(frame.get("Close",pd.Series(dtype=float)),errors="coerce").dropna().sort_index()
            volume=pd.to_numeric(frame.get("Volume",pd.Series(dtype=float)),errors="coerce").dropna().sort_index()
            m=_metrics(close,volume)
            if not m: continue
            m.update({"Stock":symbol.removesuffix(".NS"),"Sector":sector,
                "1M RS vs NIFTY (pp)":m["1M %"]-nr[21] if pd.notna(nr[21]) else np.nan,
                "3M RS vs NIFTY (pp)":m["3M %"]-nr[63] if pd.notna(nr[63]) else np.nan,
                "1M RS vs NIFTY 500 (pp)":m["1M %"]-n5r[21] if pd.notna(n5r[21]) and pd.notna(m["1M %"]) else np.nan,
                "3M RS vs NIFTY 500 (pp)":m["3M %"]-n5r[63] if pd.notna(n5r[63]) and pd.notna(m["3M %"]) else np.nan,
                "F&O eligible":symbol in fo})
            stock_rows.append(m)
    stocks=pd.DataFrame(stock_rows)
    if stocks.empty:
        st.warning("No constituents returned enough history for the 200-day EMA calculations.")
        return
    sector_return_lookup = stocks.groupby("Sector")["1M %"].mean().to_dict()
    stocks["1M RS vs sector basket (pp)"] = stocks.apply(
        lambda row: row["1M %"] - sector_return_lookup.get(row["Sector"], np.nan)
        if pd.notna(row["1M %"]) else np.nan, axis=1)
    sector_rows=[]
    for sector,frame in stocks.groupby("Sector"):
        sector_rows.append({"Sector":sector,"Stocks measured":frame["Stock"].nunique(),
            "Average 1W %":frame["1W %"].mean(),"Average 1M %":frame["1M %"].mean(),"Average 3M %":frame["3M %"].mean(),
            "1M RS vs NIFTY (pp)":frame["1M RS vs NIFTY (pp)"].mean(),"3M RS vs NIFTY (pp)":frame["3M RS vs NIFTY (pp)"].mean(),
            "1M RS vs NIFTY 500 (pp)":frame["1M RS vs NIFTY 500 (pp)"].mean(),
            "Below Daily 200 EMA %":frame["Below Daily 200 EMA"].mean()*100,
            "Below Weekly 200 EMA %":frame["Below Weekly 200 EMA"].mean()*100,
            "Fresh bearish crosses":int(frame["Fresh bearish 200 EMA cross"].sum())})
    sectors=pd.DataFrame(sector_rows)
    score_cols=[("Average 1M %",25,False),("Average 3M %",25,False),("Below Daily 200 EMA %",25,False),("Below Weekly 200 EMA %",25,False)]
    sectors["Weakness score"]=sum(sectors[col].rank(pct=True,ascending=asc)*weight for col,weight,asc in score_cols)
    sectors=sectors.sort_values("Weakness score",ascending=False).reset_index(drop=True)
    sectors.insert(0,"Rank",np.arange(1,len(sectors)+1))
    st.markdown("### 1. Weak Sector Ranking")
    st.caption("All available constituents of each official Nifty sector index contribute, regardless of F&O eligibility. Overlapping index membership is intentional.")
    st.dataframe(_style_sector_table(sectors).format({c:"{:.1f}" for c in sectors.select_dtypes(include="number").columns if c not in {"Rank","Stocks measured","Fresh bearish crosses"}}),use_container_width=True,hide_index=True)
    st.download_button("⬇ Download weak sector ranking",sectors.to_csv(index=False).encode("utf-8-sig"),"weak_sector_ranking.csv","text/csv",key="weak_sector_csv")
    st.markdown("### 2. Weak F&O Stocks")
    fo_stocks=stocks[stocks["F&O eligible"]].copy()
    sector_weak_cutoff = float(sectors["Weakness score"].median()) if not sectors.empty else np.nan
    sector_weak_lookup = dict(zip(sectors["Sector"], sectors["Weakness score"] >= sector_weak_cutoff)) if pd.notna(sector_weak_cutoff) else {}
    fo_stocks["Sector weak"] = fo_stocks["Sector"].map(sector_weak_lookup).fillna(False)
    if fo_stocks.empty:
        st.warning("No verified F&O stocks overlap the loaded sector universe, or the NSE F&O list is unavailable. Shortlists are withheld.")
        return
    fo_stocks["Bearish confirmations"]=fo_stocks[["Below Daily 200 EMA","Below Weekly 200 EMA","Fresh bearish 200 EMA cross","20D low breakdown"]].sum(axis=1)
    fo_stocks["Classification"]=np.select(
        [(fo_stocks["Sector weak"] & fo_stocks["Below Daily 200 EMA"] & fo_stocks["Below Weekly 200 EMA"] & (fo_stocks["Volume / 20D avg"]>=1.2) & (fo_stocks["1M RS vs NIFTY 500 (pp)"]<0)),
         (fo_stocks["Fresh bearish 200 EMA cross"] | fo_stocks["20D low breakdown"])],
        ["A · High-conviction weakness","C · Early breakdown watch"],default="B · Weak / developing")
    fo_stocks=fo_stocks.sort_values(["Bearish confirmations","1M RS vs NIFTY 500 (pp)","3M RS vs NIFTY 500 (pp)"],ascending=[False,True,True])
    display=["Stock","Sector","Sector weak","Classification","Price","1W %","1M %","3M %","1M RS vs NIFTY (pp)","1M RS vs NIFTY 500 (pp)","1M RS vs sector basket (pp)","3M RS vs NIFTY 500 (pp)","Daily 200 EMA gap %","Weekly 200 EMA gap %","Volume / 20D avg","Fresh bearish 200 EMA cross","20D low breakdown","Data date"]
    fo_display = fo_stocks[display].drop_duplicates(subset=["Stock"]).head(150)\n    st.dataframe(_style_stock_table(fo_display),use_container_width=True,hide_index=True)
    st.download_button("⬇ Download weak F&O stocks",fo_stocks.to_csv(index=False).encode("utf-8-sig"),"weak_fo_stocks.csv","text/csv",key="weak_fo_csv")
    st.markdown("### 3. Fresh Breakdown Watchlist")
    fresh=fo_stocks[fo_stocks["Fresh bearish 200 EMA cross"] | fo_stocks["20D low breakdown"]].copy()
    fresh_display = fresh[display].drop_duplicates(subset=["Stock"])\n    st.dataframe(_style_stock_table(fresh_display),use_container_width=True,hide_index=True)
    st.download_button("⬇ Download fresh breakdown watchlist",fresh.to_csv(index=False).encode("utf-8-sig"),"fresh_breakdown_watchlist.csv","text/csv",key="fresh_breakdown_csv")
    st.markdown("### 4. Shortlist for Analysis")
    shortlist=fo_stocks[fo_stocks["Classification"]=="A · High-conviction weakness"].drop_duplicates(subset=["Stock"]).head(20)
    st.dataframe(_style_stock_table(shortlist[display]),use_container_width=True,hide_index=True)
    st.download_button("⬇ Download analysis shortlist",shortlist.to_csv(index=False).encode("utf-8-sig"),"weakness_analysis_shortlist.csv","text/csv",key="weakness_shortlist_csv")
    st.info("Research filter only, not a short-entry instruction. Confirm liquidity, event risk, derivative constraints and price structure before acting. Extended declines can reverse sharply.")
