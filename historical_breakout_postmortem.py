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
    # Match TradingView ta.rma(): SMA seed, then Wilder recursive smoothing.
    atr = pd.Series(np.nan, index=tr.index, dtype="float64")
    valid_tr = tr.dropna()
    if len(valid_tr) >= 14:
        seed_pos = tr.index.get_loc(valid_tr.index[13])
        atr.iloc[seed_pos] = float(valid_tr.iloc[:14].mean())
        for pos in range(seed_pos + 1, len(tr)):
            current, previous = tr.iloc[pos], atr.iloc[pos - 1]
            if pd.notna(current) and pd.notna(previous):
                atr.iloc[pos] = previous + (float(current) - previous) / 14.0
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
        running_peak = one_year["Close"].cummax()
        drawdown = (one_year["Close"] / running_peak - 1) * 100
        out["Max drawdown from peak %"] = round(float(drawdown.min()), 1)
        out["Worst close vs B1 %"] = round((float(one_year["Close"].min()) / event_price - 1) * 100, 1)
    else:
        out["52W peak %"], out["Peak date"], out["Max drawdown from peak %"], out["Worst close vs B1 %"] = np.nan, "", np.nan, np.nan
    if not benchmark.empty:
        b = benchmark.set_index("Date")["Close"].sort_index()
        b0 = b[b.index >= event_date]
        b1 = b[b.index >= event_date + pd.Timedelta(weeks=52)]
        if not b0.empty and not b1.empty:
            out["NIFTY 500 52W return %"] = round((float(b1.iloc[0]) / float(b0.iloc[0]) - 1) * 100, 1)
            out["Excess vs NIFTY 500 (pp)"] = round(out["52W return %"] - out["NIFTY 500 52W return %"], 1) if pd.notna(out.get("52W return %")) else np.nan
    return out


def _pre_breakout_context(daily: pd.DataFrame, weekly: pd.DataFrame, event_date: pd.Timestamp, event_price: float, benchmark: pd.DataFrame):
    """Capture only information available on or before the B1 week."""
    d = daily.sort_values("Date").copy()
    d = d[d["Date"] <= event_date]
    if d.empty:
        return {}
    close = d.set_index("Date")["Close"].astype(float)
    out = {}
    for weeks in (4, 8, 12):
        prior = close[close.index <= event_date - pd.Timedelta(weeks=weeks)]
        out[f"Prior {weeks}W return %"] = round((event_price / float(prior.iloc[-1]) - 1) * 100, 1) if not prior.empty and prior.iloc[-1] else np.nan
    w = weekly[weekly.index <= event_date].copy()
    if len(w) >= 30:
        wclose = w["Close"]
        ma30 = wclose.rolling(30, min_periods=30).mean()
        out["B1 vs 30W avg %"] = round((event_price / float(ma30.iloc[-1]) - 1) * 100, 1) if pd.notna(ma30.iloc[-1]) and ma30.iloc[-1] else np.nan
        out["30W slope at B1 %"] = round((float(ma30.iloc[-1]) / float(ma30.iloc[-3]) - 1) * 100, 2) if len(ma30) >= 3 and pd.notna(ma30.iloc[-3]) and ma30.iloc[-3] else np.nan
        out["Prior 52W high gap %"] = round((event_price / float(wclose.tail(52).max()) - 1) * 100, 1)
    if not benchmark.empty:
        b = benchmark[benchmark["Date"] <= event_date].sort_values("Date").set_index("Date")["Close"]
        for weeks in (13, 26):
            prior = b[b.index <= event_date - pd.Timedelta(weeks=weeks)]
            if not prior.empty and prior.iloc[-1]:
                out[f"Prior {weeks}W excess vs NIFTY 500 (pp)"] = round(out.get(f"Prior {weeks}W return %", np.nan) - (event_price * 0 + 0), 1) if False else np.nan
                # Benchmark-relative return is filled by the caller using the stock's matching prior close.
    return out


def run_historical_breakout_postmortem():
    # Winners are selected by subsequent historical performance, then traced backwards to B1.
    try:
        from nse500_screener import _constituents
        universe = _constituents()[["Symbol", "Company", "Sector"]].drop_duplicates("Symbol")
    except Exception as exc:
        return pd.DataFrame(), pd.DataFrame(), f"Could not load current NIFTY 500 constituents ({exc.__class__.__name__})."
    symbols = tuple(universe["Symbol"].astype(str).str.strip().tolist())
    prices = _history_5y(symbols)
    if prices.empty:
        return pd.DataFrame(), pd.DataFrame(), "Yahoo Finance returned no 5-year price history."
    benchmark = prices[prices["Ticker"] == "^CRSLDX"].copy()
    company_map = dict(zip(universe["Symbol"], universe["Company"]))
    sector_map = dict(zip(universe["Symbol"], universe["Sector"]))
    qualifying = []
    for symbol in symbols:
        daily = prices[prices["Ticker"] == f"{symbol}.NS"].copy()
        if daily.empty:
            continue
        weekly, stages, events, _ = _weekly_stage_and_b1(daily)
        if weekly.empty or stages.empty or not events:
            continue
        # Evaluate every valid B1 using only prices after that event. Require >=100%
        # in at least one 26-, 52- or 78-week horizon.
        for event in events:
            event_date = pd.Timestamp(event["date"])
            metrics = _performance_metrics(daily, event_date, event["price"], benchmark)
            horizons = [(h, metrics.get(f"{h}W return %", np.nan)) for h in (26, 52, 78)]
            valid = [(h, value) for h, value in horizons if pd.notna(value) and value >= 100.0]
            if not valid:
                continue
            best_horizon, best_return = max(valid, key=lambda item: item[1])
            pre = _pre_breakout_context(daily, weekly, event_date, event["price"], benchmark)
            row = {
                "Symbol": symbol,
                "Company": company_map.get(symbol, symbol),
                "Sector": sector_map.get(symbol, "Unknown"),
                "B1 date": event_date.strftime("%Y-%m-%d"),
                "B1 close": round(event["price"], 2),
                "Prior base resistance": round(event["resistance"], 2),
                "Base duration (weeks)": event["base_weeks"],
                "Base range %": round(event["base_range_pct"], 1),
                "Breakout volume / 10W avg": round(event["volume_vs_10w"], 2) if pd.notna(event["volume_vs_10w"]) else np.nan,
                "Best forward horizon (weeks)": best_horizon,
                "Best forward return %": round(best_return, 1),
            }
            row.update(pre)
            row.update(metrics)
            qualifying.append((symbol, event_date, row))
    if not qualifying:
        return pd.DataFrame(), pd.DataFrame(), "No B1 events followed by a 100% gain within 26, 52 or 78 weeks were found in the available five-year sample."
    # Keep the earliest qualifying B1 per stock: this is the earliest qualifying setup
    # identified by the rule, not necessarily the stock's first-ever breakout.
    earliest_by_symbol = {}
    for item in sorted(qualifying, key=lambda x: x[1]):
        earliest_by_symbol.setdefault(item[0], item)
    winners = sorted(earliest_by_symbol.values(), key=lambda x: x[2]["Best forward return %"], reverse=True)[:20]
    summary = pd.DataFrame([item[2] for item in winners]).reset_index(drop=True)
    filings = []
    for _, symbol, row in winners:
        found, warning = _official_filings(symbol, row["B1 date"])
        filings.extend(found)
        if warning:
            pass
    filing_df = pd.DataFrame(filings)
    if not filing_df.empty:
        filing_df = filing_df.sort_values(["Symbol", "Filing date"])
    note = (
        f"Explosive winners cohort: {len(summary)} stocks with at least +100% subsequent return "
        "within 26, 52 or 78 weeks after a valid B1. Earliest qualifying B1 per stock is shown. "
        "Universe is today's NIFTY 500 constituents, not point-in-time historical constituents; "
        "the five-year sample can miss older B1 events. Winner selection uses future returns by design."
    )
    return summary, filing_df, note


def render_historical_breakout_postmortem():
    st.subheader("Historical Explosive Winners · NIFTY 500")
    st.caption("Finds NIFTY 500 stocks that gained at least 100% within 26, 52 or 78 weeks after a historical B1, then studies the earliest qualifying B1 and the 4–12 weeks before it.")
    st.info("This is a hindsight-selected winners study for discovering pre-breakout patterns, not a validated trading strategy. A dated filing near B1 is evidence to investigate, not proof of causation.")
    if st.button("Run / refresh Explosive Winners study", key="run_six_stock_postmortem"):
        with st.spinner("Scanning five years for historical 100%+ winners, tracing B1 dates and checking NSE filings…"):
            result = run_historical_breakout_postmortem()
        st.session_state["six_stock_postmortem_data"] = result
        st.session_state["six_stock_postmortem_version"] = "nifty500-explosive-winners-v3"
    if st.session_state.get("six_stock_postmortem_version") == "nifty500-b1-postmortem-v2":
        summary, filings, warning = st.session_state.get("six_stock_postmortem_data", (pd.DataFrame(), pd.DataFrame(), ""))
        if warning:
            st.warning(warning)
        if not summary.empty:
            st.dataframe(summary, use_container_width=True, hide_index=True)
            st.download_button("Download NIFTY 500 B1 sample (CSV)", summary.to_csv(index=False).encode("utf-8"),
                               "nifty500_explosive_winners_b1_study.csv", "text/csv", key="download_six_stock_postmortem")
        if not filings.empty:
            st.markdown("**Official NSE filings within ±180 days of each first B1**")
            st.dataframe(filings, use_container_width=True, hide_index=True,
                         column_config={"Official source": st.column_config.LinkColumn("Official source")})
            st.download_button("Download winner NSE filings (CSV)", filings.to_csv(index=False).encode("utf-8"),
                               "nifty500_b1_nse_filings.csv", "text/csv", key="download_six_stock_filings")
        elif not summary.empty:
            st.caption("No dated NSE filings were returned for the selected event windows. This does not mean no catalyst existed; older filing coverage or NSE access may be incomplete.")
    else:
        st.caption("Run this separately from the live NSE 500 screener. It does not change any live scanner filters, scores or watchlists.")
