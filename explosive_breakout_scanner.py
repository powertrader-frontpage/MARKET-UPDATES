from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from io import StringIO
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET
from urllib.parse import quote_plus

import numpy as np
import pandas as pd
import requests
import streamlit as st
import yfinance as yf


@st.cache_data(ttl=21600, show_spinner=False)
def _breakout_universe():
    sources = {
        "Mid-cap": "https://www.niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv",
        "Small-cap": "https://www.niftyindices.com/IndexConstituent/ind_niftysmallcap250list.csv",
    }
    rows, errors = [], []
    for segment, url in sources.items():
        try:
            response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            response.raise_for_status()
            frame = pd.read_csv(StringIO(response.text))
            symbol_col = next((c for c in frame.columns if str(c).strip().lower() == "symbol"), None)
            name_col = next((c for c in frame.columns if str(c).strip().lower() in ("company name", "companyname")), None)
            sector_col = next((c for c in frame.columns if str(c).strip().lower() in ("industry", "sector")), None)
            if not symbol_col:
                errors.append(f"{segment}: Symbol column missing")
                continue
            for _, row in frame.iterrows():
                symbol = str(row.get(symbol_col, "")).strip()
                if symbol and symbol.lower() != "nan":
                    rows.append({
                        "Symbol": symbol, "Company": str(row.get(name_col, symbol)).strip() if name_col else symbol,
                        "Segment": segment, "Sector": str(row.get(sector_col, "—")).strip() if sector_col else "—",
                        "Ticker": symbol + ".NS",
                    })
        except Exception as exc:
            errors.append(f"{segment} constituent list unavailable ({exc.__class__.__name__})")
    if not rows:
        return pd.DataFrame(), "; ".join(errors) or "No constituents returned"
    return pd.DataFrame(rows).drop_duplicates("Ticker").reset_index(drop=True), "; ".join(errors) if errors else None


@st.cache_data(ttl=21600, show_spinner=False)
def _breakout_prices(tickers: tuple[str, ...]):
    frames = []
    for start in range(0, len(tickers), 80):
        batch = list(tickers[start:start + 80])
        try:
            raw = yf.download(batch, period="2y", interval="1d", auto_adjust=True,
                              group_by="ticker", threads=False, progress=False, actions=False)
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
                    for col in ["High", "Low", "Close", "Volume"]:
                        if col in part:
                            part[col] = pd.to_numeric(part[col], errors="coerce")
                    if "High" not in part or "Low" not in part or "Volume" not in part:
                        continue
                    part = part.dropna(subset=["Date", "High", "Low", "Close"])
                    if not part.empty:
                        part["Ticker"] = ticker
                        frames.append(part[["Date", "Ticker", "High", "Low", "Close", "Volume"]])
                except Exception:
                    continue
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _news_for_symbol(symbol: str, company: str):
    query = quote_plus(f'"{company}" OR {symbol} stock India')
    url = f"https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"
    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0 MarketIntelligenceTerminal/1.0"}, timeout=10)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        rows = []
        for item in root.findall(".//item")[:8]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            source = item.find("source")
            publisher = (source.text or "").strip() if source is not None else "Google News"
            if title and link:
                rows.append({"Headline": title, "Published": pub, "Publisher": publisher, "Source link": link})
        return rows
    except Exception:
        return []


def _nse_announcements_for_symbol(symbol: str, company: str):
    """Fetch recent exchange-filed announcements; official NSE source only."""
    try:
        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
        }
        session.get("https://www.nseindia.com", headers=headers, timeout=8)
        end = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        start = end - timedelta(days=180)
        url = "https://www.nseindia.com/api/corporate-announcements"
        response = session.get(url, params={
            "index": "equities", "symbol": symbol,
            "from": start.strftime("%d-%m-%Y"), "to": end.strftime("%d-%m-%Y"),
        }, headers=headers, timeout=12)
        response.raise_for_status()
        payload = response.json()
        rows = []
        if isinstance(payload, dict):
            payload = payload.get("data", [])
        for item in payload if isinstance(payload, list) else []:
            headline = str(item.get("desc") or item.get("subject") or item.get("attchmntText") or "").strip()
            published = str(item.get("an_dt") or item.get("broadcastDateTime") or item.get("dt") or "").strip()
            link = str(item.get("attchmntFile") or item.get("fileName") or "").strip()
            if link and link.startswith("/"):
                link = "https://www.nseindia.com" + link
            if headline:
                rows.append({
                    "Symbol": symbol, "Company": company, "Headline": headline,
                    "Published": published, "Publisher": "NSE India · Corporate announcement",
                    "Source link": link or "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
                    "Evidence status": "Official exchange filing",
                })
        return rows
    except Exception:
        return []


@st.cache_data(ttl=21600, show_spinner=False)
def _breakout_official_announcements(shortlist: tuple[tuple[str, str], ...]):
    rows = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(_nse_announcements_for_symbol, symbol, company): symbol
                   for symbol, company in shortlist}
        for future in as_completed(futures):
            try:
                rows.extend(future.result() or [])
            except Exception:
                continue
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["_date"] = pd.to_datetime(frame["Published"], errors="coerce", dayfirst=True, utc=True)
    frame = frame.sort_values("_date", ascending=False, na_position="last")
    return frame.drop(columns="_date").drop_duplicates(["Symbol", "Headline"]).reset_index(drop=True)


@st.cache_data(ttl=21600, show_spinner=False)
def _breakout_news(shortlist: tuple[tuple[str, str], ...]):
    rows = []
    # News enrichment is deliberately limited to the highest-ranked technical candidates.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(_news_for_symbol, symbol, company): (symbol, company) for symbol, company in shortlist}
        for future in as_completed(futures):
            symbol, company = futures[future]
            try:
                for item in future.result():
                    rows.append({"Symbol": symbol, "Company": company, **item})
            except Exception:
                continue
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame["_date"] = pd.to_datetime(frame["Published"], errors="coerce", utc=True)
    return frame.sort_values("_date", ascending=False, na_position="last").drop(columns="_date").reset_index(drop=True)


def _completed_weekly(group: pd.DataFrame):
    data = group.sort_values("Date").set_index("Date")
    weekly = pd.DataFrame({
        "Close": data["Close"].resample("W-FRI").last(),
        "High": data["High"].resample("W-FRI").max(),
        "Low": data["Low"].resample("W-FRI").min(),
        "Volume": data["Volume"].resample("W-FRI").sum(min_count=1),
    }).dropna(subset=["Close", "High", "Low"])
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    current_friday = now.date() + timedelta(days=(4 - now.weekday()) % 7)
    if len(weekly) and weekly.index[-1].date() == current_friday and (
        now.date() < current_friday or (now.date() == current_friday and now.time() < time(15, 45))
    ):
        weekly = weekly.iloc[:-1]
    if len(weekly) < 34:
        return None
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
    volume_avg = weekly["Volume"].shift(1).rolling(10, min_periods=8).mean()
    volume_ratio = weekly["Volume"] / volume_avg.replace(0, np.nan)
    prior_base_high = high.shift(1).rolling(4, min_periods=4).max()
    return weekly, stage, sma, slope, volume_ratio, prior_base_high



def _pine_base_progress(close, high, low, stage_series, min_advance_pct=10.0, min_base_weeks=4):
    """Port of the user's Pine Weinstein base-count rules; first valid breakout is count 2."""
    count = 0
    stage2_low = base_low = base_high = last_breakout = np.nan
    base_start = None
    counts, fresh_first_breakout, breakout_levels = [], [], []
    for i in range(len(close)):
        c, h, l = close.iloc[i], high.iloc[i], low.iloc[i]
        in_stage2 = stage_series.iloc[i] == "Stage 2"
        if not in_stage2 or any(pd.isna(v) for v in (c, h, l)):
            count = 0
            stage2_low = base_low = base_high = last_breakout = np.nan
            base_start = None
            counts.append(0)
            fresh_first_breakout.append(False)
            breakout_levels.append(np.nan)
            continue
        prior_count = count
        if count == 0:
            count = 1
            stage2_low = l
            base_low, base_high = l, h
            last_breakout = h
            base_start = i
            counts.append(count)
            fresh_first_breakout.append(False)
            breakout_levels.append(np.nan)
            continue
        if l < stage2_low:
            count = 1
            stage2_low = l
            base_low, base_high = l, h
            last_breakout = h
            base_start = i
            counts.append(count)
            fresh_first_breakout.append(False)
            breakout_levels.append(h)
            continue
        prior_base_high = base_high
        base_low = min(base_low, l)
        base_high = max(base_high, h)
        duration = i - base_start
        range_pct = ((base_high - base_low) / base_low * 100) if base_low > 0 else 0.0
        duration_ok = duration >= min_base_weeks or (duration >= 2 and range_pct >= 90.0)
        advance_ok = pd.notna(last_breakout) and last_breakout > 0 and ((c / last_breakout - 1) * 100) >= min_advance_pct
        breakout_level = prior_base_high
        breakout_event = c > prior_base_high and duration_ok and advance_ok
        if breakout_event:
            count += 1
            base_low, base_high = l, h
            last_breakout = c
            base_start = i
        counts.append(count)
        fresh_first_breakout.append(prior_count == 1 and count == 2)
        breakout_levels.append(breakout_level)
    return (pd.Series(counts, index=close.index), pd.Series(fresh_first_breakout, index=close.index),
            pd.Series(breakout_levels, index=close.index))


@st.cache_data(ttl=21600, show_spinner=False)
def _quarterly_growth(ticker: str):
    """Yahoo quarterly fundamentals; blank metrics are retained when source coverage is inadequate."""
    try:
        q = yf.Ticker(ticker).quarterly_income_stmt
        if q is None or q.empty:
            return {}
        q = q.copy()
        q.columns = pd.to_datetime(q.columns, errors="coerce")
        q = q.loc[:, ~q.columns.isna()].sort_index(axis=1, ascending=False)
        if q.shape[1] < 5:
            return {}
        def metric(names, col):
            for name in names:
                if name in q.index:
                    value = pd.to_numeric(q.loc[name, col], errors="coerce")
                    if pd.notna(value):
                        return float(value)
            return np.nan
        rev_names = ["Total Revenue", "Operating Revenue", "Revenue"]
        profit_names = ["Net Income", "Net Income Common Stockholders", "Net Income From Continuing Operation Net Minority Interest"]
        op_names = ["Operating Income", "EBIT"]
        rev_now, rev_yoy_base = metric(rev_names, q.columns[0]), metric(rev_names, q.columns[4])
        profit_now, profit_yoy_base = metric(profit_names, q.columns[0]), metric(profit_names, q.columns[4])
        op_now, op_yoy = metric(op_names, q.columns[0]), metric(op_names, q.columns[4])
        sales_growth = (rev_now / rev_yoy_base - 1) * 100 if np.isfinite(rev_now) and np.isfinite(rev_yoy_base) and rev_yoy_base > 0 else np.nan
        profit_growth = (profit_now / profit_yoy_base - 1) * 100 if np.isfinite(profit_now) and np.isfinite(profit_yoy_base) and profit_yoy_base > 0 else np.nan
        margin_now = op_now / rev_now * 100 if np.isfinite(op_now) and np.isfinite(rev_now) and rev_now > 0 else np.nan
        margin_yoy = op_yoy / rev_yoy_base * 100 if np.isfinite(op_yoy) and np.isfinite(rev_yoy_base) and rev_yoy_base > 0 else np.nan
        loss_recovery = bool(np.isfinite(profit_now) and np.isfinite(profit_yoy_base) and profit_now > 0 and profit_yoy_base < 0)
        return {
            "Sales growth YoY %": sales_growth, "Profit growth YoY %": profit_growth,
            "Operating margin %": margin_now,
            "Margin expansion YoY (pp)": margin_now - margin_yoy if np.isfinite(margin_now) and np.isfinite(margin_yoy) else np.nan,
            "Loss to profit": loss_recovery,
        }
    except Exception:
        return {}


def _catalyst_label(headline: str):
    text = str(headline).lower()
    negative = ("fraud", "default", "insolvency", "sebi probe", "search operation", "resignation",
                "pledge invoked", "downgrade", "plant shutdown", "ban", "regulatory action")
    if any(word in text for word in negative):
        return "Negative / risk", -3
    categories = [
        (("order win", "orders worth", "contract worth", "purchase order", "work order",
          "letter of award", "loa", "supply agreement", "customer contract"), "Order / contract", 3),
        (("guidance raised", "raises guidance", "revised guidance", "outlook raised"), "Guidance upgrade", 3),
        (("capacity expansion", "new plant", "new facility", "commissioned", "inaugurated",
          "capacity addition", "commercial production"), "Capacity / commissioning", 2),
        (("profit rises", "profit jumps", "pat rises", "revenue rises", "earnings growth",
          "financial results", "audited financial results", "quarterly results"), "Results / earnings", 2),
        (("approval", "regulatory clearance", "product launch", "new customer", "acquisition",
          "strategic partnership", "joint venture"), "Business development", 1),
    ]
    for words, label, score in categories:
        if any(word in text for word in words):
            return label, score
    return "Routine / unclassified", 0


@st.cache_data(ttl=21600, show_spinner=False)
def _breakout_enrichment(shortlist: tuple[tuple[str, str], ...]):
    rows = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(_quarterly_growth, symbol + ".NS"): (symbol, company)
                   for symbol, company in shortlist}
        for future in as_completed(futures):
            symbol, company = futures[future]
            try:
                metrics = future.result() or {}
                rows.append({"Symbol": symbol, **metrics})
            except Exception:
                rows.append({"Symbol": symbol})
    fundamentals = pd.DataFrame(rows)
    official = _breakout_official_announcements(shortlist)
    news = _breakout_news(shortlist)
    # Official NSE filings are the only source eligible for "confirmed" catalyst status.
    if not official.empty:
        labels = official["Headline"].map(_catalyst_label)
        official["Catalyst type"] = [x[0] for x in labels]
        official["Catalyst points"] = [x[1] for x in labels]
        official["Evidence status"] = np.where(official["Catalyst points"] > 0,
                                                "Official filing · catalyst match",
                                                "Official filing · review")
        official["_published"] = pd.to_datetime(official["Published"], errors="coerce", dayfirst=True, utc=True)
        recent = official[official["_published"] >= pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=180)]
        if not recent.empty:
            ranked = recent.sort_values(["Catalyst points", "_published"], ascending=[False, False])
            best = ranked.drop_duplicates("Symbol")
            keep = best[["Symbol", "Headline", "Published", "Catalyst type", "Catalyst points",
                         "Source link", "Evidence status"]].rename(columns={
                "Headline": "Official catalyst", "Published": "Official catalyst date",
                "Source link": "Official source"
            })
            fundamentals = fundamentals.merge(keep, on="Symbol", how="left")
            fundamentals["Official catalyst score"] = pd.to_numeric(
                fundamentals["Catalyst points"], errors="coerce").fillna(0)
    if not news.empty:
        labels = news["Headline"].map(_catalyst_label)
        news["Catalyst type"] = [x[0] for x in labels]
        news["Catalyst points"] = [x[1] for x in labels]
        news["Evidence status"] = "Media coverage · not exchange-verified"
    if not official.empty:
        combined_news = pd.concat([official, news], ignore_index=True, sort=False) if not news.empty else official
    else:
        combined_news = news
    if "Official catalyst score" not in fundamentals:
        fundamentals["Official catalyst score"] = 0
    return fundamentals, combined_news


def run_explosive_breakout_scan():
    universe, warning = _breakout_universe()
    if universe.empty:
        return pd.DataFrame(), pd.DataFrame(), warning
    prices = _breakout_prices(tuple(universe["Ticker"].tolist()))
    if prices.empty:
        return pd.DataFrame(), pd.DataFrame(), warning or "Price history unavailable from Yahoo Finance."
    records = []
    by_ticker = {row["Ticker"]: row for _, row in universe.iterrows()}
    for ticker, group in prices.groupby("Ticker"):
        info = by_ticker.get(ticker)
        if info is None:
            continue
        calc = _completed_weekly(group)
        if calc is None:
            continue
        weekly, stage, sma, slope, volume_ratio, resistance = calc
        if len(weekly) < 2:
            continue
        i = -1
        close = float(weekly["Close"].iloc[i])
        prior_stage = stage.iloc[-2]
        current_stage = stage.iloc[-1]
        # First week of the current uninterrupted Weinstein stage.
        stage_change_positions = np.flatnonzero(stage.to_numpy() != current_stage)
        stage_start_pos = int(stage_change_positions[-1] + 1) if len(stage_change_positions) else 0
        stage_start_date = weekly.index[stage_start_pos]
        base_counts, fresh_b1_event, pine_breakout_levels = _pine_base_progress(weekly["Close"], weekly["High"], weekly["Low"], stage)
        base_count = int(base_counts.iloc[-1])
        fresh_b1 = bool(fresh_b1_event.iloc[-1])
        # For Stage 1 candidates, Pine's Stage-2 base counter is intentionally inactive.
        # Use the prior completed 4-week high as a watchlist resistance reference instead.
        breakout_level = (float(pine_breakout_levels.iloc[-1]) if pd.notna(pine_breakout_levels.iloc[-1])
                          else (float(resistance.iloc[-1]) if pd.notna(resistance.iloc[-1]) else np.nan))
        breakout = bool(base_counts.iloc[-1] > base_counts.iloc[-2])
        vol = float(volume_ratio.iloc[-1]) if pd.notna(volume_ratio.iloc[-1]) else np.nan
        ma30 = float(sma.iloc[-1]) if pd.notna(sma.iloc[-1]) else np.nan
        slope_pct = float((sma.iloc[-1] / sma.iloc[-3] - 1) * 100) if len(sma) >= 3 and pd.notna(sma.iloc[-3]) and sma.iloc[-3] else np.nan
        gap30 = (close / ma30 - 1) * 100 if pd.notna(ma30) and ma30 else np.nan
        daily = group.sort_values("Date")
        dclose = daily["Close"].dropna()
        daily_change = (float(dclose.iloc[-1]) / float(dclose.iloc[-2]) - 1) * 100 if len(dclose) >= 2 else np.nan
        ema10 = dclose.ewm(span=10, adjust=False).mean().iloc[-1]
        ema21 = dclose.ewm(span=21, adjust=False).mean().iloc[-1]
        ema55 = dclose.ewm(span=55, adjust=False).mean().iloc[-1]
        ema_aligned = close > ema10 > ema21 > ema55
        # Relative strength is ranked against the scanned mid/small-cap universe.
        ret13 = float(weekly["Close"].iloc[-1] / weekly["Close"].iloc[-14] - 1) * 100 if len(weekly) >= 14 else np.nan
        records.append({
            "Symbol": info["Symbol"], "Company": info["Company"], "Segment": info["Segment"], "Sector": info["Sector"],
            "Price": close, "Stage": current_stage, "Previous Stage": prior_stage,
            "Fresh B1": bool(fresh_b1), "Base Count": base_count, "4W breakout": bool(breakout), "Breakout level": breakout_level,
            "Volume vs 10W Avg": vol, "13W return %": ret13, "30W slope %": slope_pct,
            "Gap vs 30W %": gap30, "EMA 10>21>55": bool(ema_aligned), "Daily change %": daily_change,
            "Weekly date": weekly.index[-1].strftime("%Y-%m-%d"),
            "Stage start date": stage_start_date.strftime("%Y-%m-%d"),
        })
    result = pd.DataFrame(records)
    if result.empty:
        return result, pd.DataFrame(), warning
    rank = result["13W return %"].rank(pct=True) * 100
    breakout_pts = result["4W breakout"].astype(int) * 15
    stage_pts = result["Stage"].eq("Stage 2").astype(int) * 10
    vol_pts = pd.to_numeric(result["Volume vs 10W Avg"], errors="coerce").clip(0, 2).fillna(0) / 2 * 15
    slope_pts = pd.to_numeric(result["30W slope %"], errors="coerce").clip(0, 8).fillna(0) / 8 * 10
    ema_pts = result["EMA 10>21>55"].astype(int) * 10
    rs_pts = rank.fillna(0) * 0.20
    proximity = pd.to_numeric(result["Gap vs 30W %"], errors="coerce")
    extension_pts = np.where((proximity >= 0) & (proximity <= 15), 10, 0)
    result["Technical Score"] = (breakout_pts + stage_pts + vol_pts + slope_pts + ema_pts + rs_pts + extension_pts).round(1)
    result["Setup"] = np.select(
        [result["Fresh B1"] & result["4W breakout"], result["Fresh B1"], result["Stage"].eq("Stage 2") & result["4W breakout"]],
        ["Fresh Stage 2 + breakout", "Fresh Stage 2 transition", "Stage 2 breakout follow-through"],
        default="Stage 2 watch"
    )
    fresh = result[result["Fresh B1"] & result["4W breakout"]].copy()
    follow = result[result["Stage"].eq("Stage 2") & result["Base Count"].ge(3) & result["4W breakout"] & ~result["Fresh B1"]].copy()
    # Upcoming list is for Stage 2 stocks that have not yet cleared the recent 4-week resistance.
    # Keep candidates close to resistance with positive relative momentum; exclude completed breakouts.
    resistance_gap = (pd.to_numeric(result["Breakout level"], errors="coerce") / pd.to_numeric(result["Price"], errors="coerce") - 1) * 100
    result["Gap to breakout %"] = resistance_gap
    upcoming = result[
        result["Stage"].isin(["Stage 1", "Stage 2"])
        & ~result["4W breakout"]
        & resistance_gap.between(0, 5)
        & (pd.to_numeric(result["13W return %"], errors="coerce") > 0)
    ].copy()
    # Enrich only the top 30 technical names to avoid hundreds of fundamentals/news requests.
    shortlist_df = result.sort_values("Technical Score", ascending=False).head(30)
    pairs = tuple((str(row["Symbol"]), str(row["Company"])) for _, row in shortlist_df.iterrows())
    fundamentals, news = _breakout_enrichment(pairs) if pairs else (pd.DataFrame(), pd.DataFrame())
    # Attach the most recent positive catalyst headline to the technical stage timeline.
    # This is a timing comparison, not proof that the headline caused the price move.
    if not news.empty and {"Symbol", "Published", "Headline", "Catalyst type", "Catalyst points"}.issubset(news.columns):
        news_timeline = news.copy()
        news_timeline["_catalyst_date"] = pd.to_datetime(news_timeline["Published"], errors="coerce", utc=True)
        news_timeline = news_timeline[
            news_timeline["_catalyst_date"].notna()
            & (pd.to_numeric(news_timeline["Catalyst points"], errors="coerce") > 0)
        ].sort_values("_catalyst_date", ascending=False)
        news_timeline = news_timeline.drop_duplicates("Symbol", keep="first")
        news_timeline = news_timeline[["Symbol", "_catalyst_date", "Catalyst type", "Headline"]].rename(
            columns={"_catalyst_date": "Latest catalyst date", "Headline": "Latest catalyst"}
        )
        result = result.merge(news_timeline, on="Symbol", how="left")
        result["Latest catalyst date"] = pd.to_datetime(result["Latest catalyst date"], errors="coerce", utc=True)
        stage_dates = pd.to_datetime(result["Stage start date"], errors="coerce", utc=True)
        result["Catalyst-to-stage days"] = (stage_dates - result["Latest catalyst date"]).dt.days
        result["Catalyst timing"] = np.select(
            [result["Catalyst-to-stage days"].notna() & (result["Catalyst-to-stage days"] > 0),
             result["Catalyst-to-stage days"].notna() & (result["Catalyst-to-stage days"] <= 0)],
            ["Catalyst before stage", "Catalyst on/after stage start"],
            default="No dated positive catalyst found"
        )
        result["Latest catalyst date"] = result["Latest catalyst date"].dt.strftime("%Y-%m-%d")
    else:
        result["Latest catalyst date"] = np.nan
        result["Latest catalyst"] = np.nan
        result["Catalyst type"] = np.nan
        result["Catalyst-to-stage days"] = np.nan
        result["Catalyst timing"] = "No dated positive catalyst found"
    if not fundamentals.empty:
        result = result.merge(fundamentals, on="Symbol", how="left")
        sales = pd.to_numeric(result["Sales growth YoY %"], errors="coerce") if "Sales growth YoY %" in result else pd.Series(np.nan, index=result.index)
        profit = pd.to_numeric(result["Profit growth YoY %"], errors="coerce") if "Profit growth YoY %" in result else pd.Series(np.nan, index=result.index)
        margin = pd.to_numeric(result["Margin expansion YoY (pp)"], errors="coerce") if "Margin expansion YoY (pp)" in result else pd.Series(np.nan, index=result.index)
        fundamental_score = (sales.clip(0, 50).fillna(0) / 50 * 7
                             + profit.clip(0, 75).fillna(0) / 75 * 6
                             + margin.clip(0, 10).fillna(0) / 10 * 2)
        fundamental_score += result.get("Loss to profit", pd.Series(False, index=result.index)).fillna(False).astype(bool) * 2
        result["Fundamental Score"] = fundamental_score.clip(0, 15).round(1)
    else:
        result["Fundamental Score"] = 0.0
    catalyst = pd.to_numeric(result.get("News catalyst score", pd.Series(0, index=result.index)), errors="coerce").fillna(0)
    result["Catalyst Score"] = catalyst.clip(-3, 3).map(lambda x: round(x / 3 * 10, 1))
    # Keep technical evidence dominant; fundamentals and news add confirmation rather than vetoing early breakouts.
    result["Opportunity Score"] = (result["Technical Score"] * 0.75
                                   + result["Fundamental Score"]
                                   + result["Catalyst Score"]).clip(0, 100).round(1)
    fresh = result[result["Fresh B1"] & result["4W breakout"]].copy()
    follow = result[result["Stage"].eq("Stage 2") & result["Base Count"].ge(3) & result["4W breakout"] & ~result["Fresh B1"]].copy()
    resistance_gap = (pd.to_numeric(result["Breakout level"], errors="coerce") / pd.to_numeric(result["Price"], errors="coerce") - 1) * 100
    result["Gap to breakout %"] = resistance_gap
    upcoming = result[result["Stage"].isin(["Stage 1", "Stage 2"]) & ~result["4W breakout"] & resistance_gap.between(0, 5)
                      & (pd.to_numeric(result["13W return %"], errors="coerce") > 0)].copy()
    for frame in (fresh, follow, upcoming):
        frame.sort_values(["Opportunity Score", "Technical Score", "Volume vs 10W Avg"], ascending=False, inplace=True, na_position="last")
        frame.reset_index(drop=True, inplace=True)
    return fresh, follow, warning, upcoming, result, news


def render_explosive_breakout_scanner():
    st.markdown("""<div style="font-size:clamp(26px,3.5vw,38px);font-weight:850;letter-spacing:-1px;margin:10px 0 8px;background:linear-gradient(90deg,#19e6a0,#29c7d6,#4aa8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">🚀 Explosive Breakout Scanner</div>""", unsafe_allow_html=True)
    st.caption("Nifty Midcap 150 + Nifty Smallcap 250 · Weekly Weinstein stages · Fresh B1 breakouts · Volume and relative-strength ranking · Recent news enrichment")
    st.info("Uses the weekly Weinstein stage and base-count rules: the first valid breakout after a B1 base is flagged as Fresh B1. Financial results and recent headlines add confirmation; verify material announcements on NSE/BSE.")
    c1, c2 = st.columns([1, 3])
    with c1:
        run = st.button("Scan / refresh breakouts", type="primary", key="run_explosive_breakout_scan")
    with c2:
        st.caption("Price history and constituents are cached for 6 hours. News is fetched only for the top technical candidates to limit load.")
    # Bump this whenever scanner filters or classification rules change. This
    # invalidates old session_state tables that otherwise survive code deployments.
    scan_version = "catalyst-stage-benchmark-replay-v1"
    needs_refresh = st.session_state.get("explosive_breakout_scan_version") != scan_version
    if run or needs_refresh or st.session_state.get("explosive_breakout_scan_loaded", False):
        if run or needs_refresh or "explosive_breakout_scan_data" not in st.session_state:
            with st.spinner("Scanning mid-cap and small-cap weekly price history…"):
                output = run_explosive_breakout_scan()
            st.session_state["explosive_breakout_scan_version"] = scan_version
            # Keep the scan result and the latest-run time in this browser session.
            if len(output) == 6:
                fresh, follow, warning, upcoming, all_rows, news = output
                st.session_state["explosive_breakout_scan_data"] = (fresh, follow, upcoming, all_rows, news)
                st.session_state["explosive_breakout_scan_warning"] = warning
                st.session_state["explosive_breakout_scan_time"] = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y %H:%M IST")
                previous = st.session_state.get("explosive_breakout_previous_symbols", set())
                current = set(all_rows["Symbol"].astype(str)) if not all_rows.empty else set()
                fresh["Status"] = fresh["Symbol"].astype(str).map(lambda x: "🆕 NEW" if x not in previous and previous else ("Baseline" if not previous else "Existing"))
                st.session_state["explosive_breakout_previous_symbols"] = current
            else:
                st.session_state["explosive_breakout_scan_data"] = (pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
                st.session_state["explosive_breakout_scan_warning"] = output[2] if len(output) > 2 else "Scan unavailable"
        st.session_state["explosive_breakout_scan_loaded"] = True
        if st.session_state.get("explosive_breakout_scan_warning"):
            st.warning(st.session_state["explosive_breakout_scan_warning"])
        fresh, follow, upcoming, all_rows, news = st.session_state.get("explosive_breakout_scan_data", (pd.DataFrame(),)*5)
        # Final display guard: early-watch candidates may be Stage 1 or Stage 2,
        # but never Stage 3/4 or unclassified.
        if not upcoming.empty and "Stage" in upcoming.columns:
            upcoming = upcoming.loc[
                upcoming["Stage"].astype(str).str.strip().str.casefold().isin(["stage 1", "stage 2"])
            ].copy()
            upcoming.reset_index(drop=True, inplace=True)
        elif not upcoming.empty:
            upcoming = pd.DataFrame()
        if st.session_state.get("explosive_breakout_scan_time"):
            st.caption("Last scan: " + st.session_state["explosive_breakout_scan_time"])
        if not fresh.empty:
            st.subheader("Fresh B1 · Stage 2 breakouts")
            display = ["Status", "Opportunity Score", "Technical Score", "Fundamental Score", "Catalyst Score", "Symbol", "Company", "Segment", "Sector", "Price", "Breakout level", "Base Count", "Volume vs 10W Avg", "Sales growth YoY %", "Profit growth YoY %", "13W return %", "30W slope %", "EMA 10>21>55", "Daily change %"]
            st.dataframe(fresh[[c for c in display if c in fresh.columns]], use_container_width=True, hide_index=True)
        else:
            st.info("No fresh B1 breakout met all current conditions. Check Upcoming Breakouts and Stage 2 follow-through below.")
        st.subheader("Upcoming Breakouts · Early watch")
        if upcoming.empty:
            st.caption("No Stage 1/2 candidates are currently within 5% of their recent breakout resistance with positive 13-week momentum.")
        else:
            st.dataframe(upcoming[[c for c in ["Opportunity Score", "Technical Score", "Symbol", "Company", "Segment", "Sector", "Price", "Stage", "Gap to breakout %", "Volume vs 10W Avg", "Sales growth YoY %", "Profit growth YoY %", "Catalyst Score"] if c in upcoming.columns]], use_container_width=True, hide_index=True)
        st.subheader("Strong B2+ · Breakout follow-through")
        if follow.empty:
            st.caption("No additional Stage 2 stocks with later base breakouts currently qualify.")
        else:
            st.dataframe(follow[[c for c in ["Opportunity Score", "Technical Score", "Symbol", "Company", "Segment", "Sector", "Price", "Breakout level", "Base Count", "Volume vs 10W Avg", "Sales growth YoY %", "Profit growth YoY %"] if c in follow.columns]], use_container_width=True, hide_index=True)
        with st.expander("Catalyst → Weinstein Stage timeline", expanded=False):
            timeline_cols = ["Symbol", "Company", "Stage", "Stage start date", "Latest catalyst date",
                             "Catalyst type", "Latest catalyst", "Catalyst-to-stage days", "Catalyst timing"]
            timeline = all_rows[[c for c in timeline_cols if c in all_rows.columns]].copy() if not all_rows.empty else pd.DataFrame()
            if not timeline.empty:
                timeline = timeline[timeline.get("Latest catalyst", pd.Series(index=timeline.index, dtype="object")).notna()]
                st.dataframe(timeline, use_container_width=True, hide_index=True)
                st.caption("Catalyst timing compares the latest positive headline found in the technical shortlist with the start of the current uninterrupted Weinstein stage. It is an investigative clue, not proof of causation. News coverage is limited to recent Google News results; verify dates and events against official NSE/BSE filings.")
            else:
                st.caption("No dated positive catalyst was found for the current technical shortlist.")
        with st.expander("Recent news & catalysts · technical shortlist", expanded=False):
            if not news.empty:
                st.dataframe(news, use_container_width=True, hide_index=True, column_config={"Source link": st.column_config.LinkColumn("Source link")})
                st.caption("Headline classification is automated and can be imperfect. Verify every material event using the linked source and NSE/BSE filings.")
            else:
                st.caption("No recent headlines were returned for the top technical shortlist. No catalyst is assumed.")
        st.download_button("Download full mid/small-cap scan (CSV)", all_rows.to_csv(index=False).encode("utf-8"), "explosive_breakout_mid_smallcap_scan.csv", "text/csv", key="download_explosive_breakout_scan")
        st.caption("Opportunity Score combines technical ranking, available quarterly fundamentals and recent news catalysts. Fundamentals/news are enriched for the top 30 technical candidates only; missing data is not treated as positive. Scores are research heuristics, not probabilities or buy signals. Weekly logic uses completed candles and your Pine base-count rules.")
    else:
        st.info("Press Scan / refresh to evaluate the Midcap 150 and Smallcap 250 universe. The scan is separate from the NSE 500 Weinstein screener and Turnaround Board.")
