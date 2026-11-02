from __future__ import annotations

from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf


BENCHMARK_STOCKS = {
    "CUPID": "Cupid Limited",
    "STLTECH": "Sterlite Technologies",
    "HFCL": "HFCL Limited",
    "LAURUSLABS": "Laurus Labs",
    "ATHERENERG": "Ather Energy",
    "WELCORP": "Welspun Corp",
}


@st.cache_data(ttl=21600, show_spinner=False)
def _history_5y(symbols: tuple[str, ...]) -> pd.DataFrame:
    frames = []
    tickers = [f"{symbol}.NS" for symbol in symbols] + ["^CRSLDX"]
    for start in range(0, len(tickers), 80):
        batch = tickers[start:start + 80]
        try:
            raw = yf.download(
                batch, period="5y", interval="1d", auto_adjust=True,
                group_by="ticker", threads=False, progress=False, actions=False,
            )
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
                    for col in ["Open", "High", "Low", "Close", "Volume"]:
                        if col in part:
                            part[col] = pd.to_numeric(part[col], errors="coerce")
                    if not {"High", "Low", "Close"}.issubset(part.columns):
                        continue
                    part = part.dropna(subset=["Date", "High", "Low", "Close"])
                    if not part.empty:
                        part["Ticker"] = ticker
                        frames.append(part[["Date", "Ticker", "High", "Low", "Close", "Volume"]])
                except Exception:
                    continue
        except Exception:
            continue
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def _weekly_stage_and_b1(daily: pd.DataFrame):
    data = daily.sort_values("Date").set_index("Date")
    weekly = pd.DataFrame({
        "Close": data["Close"].resample("W-FRI").last(),
        "High": data["High"].resample("W-FRI").max(),
        "Low": data["Low"].resample("W-FRI").min(),
        "Volume": data["Volume"].resample("W-FRI").sum(min_count=1) if "Volume" in data else np.nan,
    }).dropna(subset=["Close", "High", "Low"])
    if len(weekly) < 34:
        return weekly, pd.Series(dtype="object"), [], None
    # Exclude the still-forming weekly candle using the same 15:45 IST cutoff.
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    friday = now.date() + timedelta(days=(4 - now.weekday()) % 7)
    if len(weekly) and weekly.index[-1].date() == friday and (
        now.date() < friday or (now.date() == friday and now.time() < time(15, 45))
    ):
        weekly = weekly.iloc[:-1]
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

    # Exact Pine-port rules used by the live NSE 500 screener: Stage 2 reset,
    # stage-low invalidation, >=4 weeks (or >=2 weeks with >=90% range),
    # and >=10% advance from the prior breakout price.
    count = 0
    stage2_low = base_low = base_high = last_breakout = np.nan
    base_start = None
    b1_events = []
    for i in range(len(weekly)):
        c, h, l = close.iloc[i], high.iloc[i], low.iloc[i]
        if stage.iloc[i] != "Stage 2" or any(pd.isna(v) for v in (c, h, l)):
            count = 0
            stage2_low = base_low = base_high = last_breakout = np.nan
            base_start = None
            continue
        if count == 0:
            count = 1
            stage2_low, base_low, base_high, last_breakout = l, l, h, h
            base_start = i
            continue
        if l < stage2_low:
            count = 1
            stage2_low, base_low, base_high, last_breakout = l, l, h, h
            base_start = i
            continue
        prior_high = base_high
        base_low, base_high = min(base_low, l), max(base_high, h)
        duration = i - base_start
        range_pct = (base_high - base_low) / base_low * 100 if base_low > 0 else 0
        duration_ok = duration >= 4 or (duration >= 2 and range_pct >= 90)
        advance_ok = pd.notna(last_breakout) and last_breakout > 0 and (c / last_breakout - 1) * 100 >= 10
        if c > prior_high and duration_ok and advance_ok:
            prior_count = count
            count += 1
            if prior_count == 1 and count == 2:
                b1_events.append({
                    "date": weekly.index[i],
                    "price": float(c),
                    "resistance": float(prior_high),
                    "base_weeks": int(duration),
                    "base_range_pct": float(range_pct),
                    "volume_vs_10w": float(weekly["Volume"].iloc[i] / weekly["Volume"].shift(1).rolling(10, min_periods=8).mean().iloc[i])
                        if pd.notna(weekly["Volume"].shift(1).rolling(10, min_periods=8).mean().iloc[i])
                        and weekly["Volume"].shift(1).rolling(10, min_periods=8).mean().iloc[i] > 0 else np.nan,
                })
            base_low, base_high, last_breakout, base_start = l, h, c, i
    return weekly, stage, b1_events, sma


@st.cache_data(ttl=21600, show_spinner=False)
def _official_filings(symbol: str, event_date: str):
    """Retrieve dated NSE filings around the historical B1 event; filing availability varies."""
    try:
        event = pd.Timestamp(event_date).date()
        start, end = event - timedelta(days=180), event + timedelta(days=180)
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
        }
        session.get("https://www.nseindia.com", headers=headers, timeout=8)
        response = session.get(
            "https://www.nseindia.com/api/corporate-announcements",
            params={"index": "equities", "symbol": symbol,
                    "from": start.strftime("%d-%m-%Y"), "to": end.strftime("%d-%m-%Y")},
            headers=headers, timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            payload = payload.get("data", [])
        rows = []
        for item in payload if isinstance(payload, list) else []:
            category = str(item.get("desc") or "").strip()
            subject = str(item.get("subject") or "").strip()
            attachment_text = str(item.get("attchmntText") or "").strip()
            headline = " · ".join(dict.fromkeys(x for x in (subject, attachment_text, category) if x))
            raw_date = str(item.get("an_dt") or item.get("broadcastDateTime") or item.get("dt") or "").strip()
            link = str(item.get("attchmntFile") or item.get("fileName") or "").strip()
            if link.startswith("/"):
                link = "https://www.nseindia.com" + link
            if headline and raw_date:
                parsed = pd.to_datetime(raw_date, errors="coerce", dayfirst=True)
                if pd.notna(parsed):
                    rows.append({"Symbol": symbol, "Filing date": parsed.strftime("%Y-%m-%d"),
                                 "Days from B1": int((parsed.date() - event).days),
                                 "NSE filing": headline, "Official source": link})
        return rows, None
    except Exception as exc:
        return [], f"{symbol}: NSE filing history unavailable ({exc.__class__.__name__})."


def _performance_metrics(daily: pd.DataFrame, event_date: pd.Timestamp, event_price: float, benchmark: pd.DataFrame):
    data = daily[daily["Date"] >= event_date].sort_values("Date").copy()
    if data.empty:
        return {}
    data = data.set_index("Date")
    out = {}
    for weeks in (13, 26, 52):
        target = event_date + pd.Timedelta(weeks=weeks)
        future = data[data.index >= target]
        out[f"{weeks}W return %"] = round((float(future["Close"].iloc[0]) / event_price - 1) * 100, 1) if not future.empty else np.nan
    one_year = data[data.index <= event_date + pd.Timedelta(weeks=52)]
    if not one_year.empty:
        peak_date = one_year["Close"].idxmax()
        peak_price = float(one_year.loc[peak_date, "Close"])
        out["52W peak %"] = round((peak_price / event_price - 1) * 100, 1)
        out["Peak date"] = peak_date.strftime("%Y-%m-%d")
        out["Max drawdown from B1 %"] = round((float(one_year["Close"].min()) / event_price - 1) * 100, 1)
    else:
        out["52W peak %"], out["Peak date"], out["Max drawdown from B1 %"] = np.nan, "", np.nan
    if not benchmark.empty:
        b = benchmark.set_index("Date")["Close"].sort_index()
        b0 = b[b.index >= event_date]
        b1 = b[b.index >= event_date + pd.Timedelta(weeks=52)]
        if not b0.empty and not b1.empty:
            out["NIFTY 500 52W return %"] = round((float(b1.iloc[0]) / float(b0.iloc[0]) - 1) * 100, 1)
            out["Excess vs NIFTY 500 (pp)"] = round(out["52W return %"] - out["NIFTY 500 52W return %"], 1) if pd.notna(out.get("52W return %")) else np.nan
    return out


def run_historical_breakout_postmortem():
    prices = _history_5y(tuple(BENCHMARK_STOCKS))
    if prices.empty:
        return pd.DataFrame(), pd.DataFrame(), "Yahoo Finance returned no 5-year price history."
    benchmark = prices[prices["Ticker"] == "^CRSLDX"].copy()
    summary, filings, warnings = [], [], []
    for symbol, company in BENCHMARK_STOCKS.items():
        ticker = f"{symbol}.NS"
        daily = prices[prices["Ticker"] == ticker].copy()
        if daily.empty:
            warnings.append(f"{symbol}: no 5-year daily history returned.")
            continue
        weekly, stages, events, _ = _weekly_stage_and_b1(daily)
        if weekly.empty or stages.empty:
            warnings.append(f"{symbol}: insufficient weekly history for the 30-week stage model.")
            continue
        stage2_positions = np.flatnonzero(stages.eq("Stage 2").to_numpy())
        first_stage2 = weekly.index[stage2_positions[0]] if len(stage2_positions) else pd.NaT
        first_b1 = events[0] if events else None
        row = {"Symbol": symbol, "Company": company,
               "First Stage 2 date (5Y)": first_stage2.strftime("%Y-%m-%d") if pd.notna(first_stage2) else "Not found"}
        if first_b1:
            event_date = pd.Timestamp(first_b1["date"])
            row.update({
                "First valid B1 date": event_date.strftime("%Y-%m-%d"),
                "B1 close": round(first_b1["price"], 2),
                "Prior base resistance": round(first_b1["resistance"], 2),
                "Base duration (weeks)": first_b1["base_weeks"],
                "Base range %": round(first_b1["base_range_pct"], 1),
                "Breakout volume / 10W avg": round(first_b1["volume_vs_10w"], 2) if pd.notna(first_b1["volume_vs_10w"]) else np.nan,
            })
            row.update(_performance_metrics(daily, event_date, first_b1["price"], benchmark))
            found, warning = _official_filings(symbol, event_date.strftime("%Y-%m-%d"))
            filings.extend(found)
            if warning:
                warnings.append(warning)
        else:
            row.update({"First valid B1 date": "Not found in 5Y sample"})
        summary.append(row)
    summary_df = pd.DataFrame(summary)
    filing_df = pd.DataFrame(filings)
    if not filing_df.empty:
        filing_df = filing_df.sort_values(["Symbol", "Filing date"])
    return summary_df, filing_df, " · ".join(warnings)


def render_historical_breakout_postmortem():
    st.subheader("Historical Breakout Post-Mortem · Six NSE 500 leaders")
    st.caption("Five-year replay of CUPID, STLTECH, HFCL, LAURUSLABS, ATHERENERG and WELCORP. Uses the same completed-week Weinstein 30W stage model and Pine base/B1 rules as the NSE 500 screener. Adjusted daily prices are used for corporate-action continuity.")
    st.info("This is a six-stock case study, not a statistically validated strategy. A dated filing near a breakout is evidence to investigate, not proof that it caused the move.")
    if st.button("Run / refresh 5-year post-mortem", key="run_six_stock_postmortem"):
        with st.spinner("Replaying five years of weekly price history and checking NSE filings…"):
            result = run_historical_breakout_postmortem()
        st.session_state["six_stock_postmortem_data"] = result
        st.session_state["six_stock_postmortem_version"] = "six-stock-b1-postmortem-v1"
    if st.session_state.get("six_stock_postmortem_version") == "six-stock-b1-postmortem-v1":
        summary, filings, warning = st.session_state.get("six_stock_postmortem_data", (pd.DataFrame(), pd.DataFrame(), ""))
        if warning:
            st.warning(warning)
        if not summary.empty:
            st.dataframe(summary, use_container_width=True, hide_index=True)
            st.download_button("Download six-stock post-mortem (CSV)", summary.to_csv(index=False).encode("utf-8"),
                               "six_stock_historical_b1_postmortem.csv", "text/csv", key="download_six_stock_postmortem")
        if not filings.empty:
            st.markdown("**Official NSE filings within ±180 days of each first B1**")
            st.dataframe(filings, use_container_width=True, hide_index=True,
                         column_config={"Official source": st.column_config.LinkColumn("Official source")})
            st.download_button("Download historical NSE filings (CSV)", filings.to_csv(index=False).encode("utf-8"),
                               "six_stock_b1_nse_filings.csv", "text/csv", key="download_six_stock_filings")
        elif not summary.empty:
            st.caption("No dated NSE filings were returned for the selected event windows. This does not mean no catalyst existed; older filing coverage or NSE access may be incomplete.")
    else:
        st.caption("Run this separately from the live NSE 500 screener. It does not change any live scanner filters, scores or watchlists.")
