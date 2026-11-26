from __future__ import annotations

from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo
from io import BytesIO
import re
import xml.etree.ElementTree as ET

import pandas as pd
import requests
import streamlit as st
import yfinance as yf


NSE_FO_LOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
NSE_EQUITY_MASTER_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_ANNOUNCEMENTS_RSS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NSE_RESULTS_RSS = "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml"
NSE_ANNOUNCEMENTS_API = "https://www.nseindia.com/api/corporate-announcements"
NSE_FO_LANDING_PAGES = (
    "https://www.nseindia.com/",
    "https://www.nseindia.com/products-services/equity-derivatives-contract-information",
)


def _nse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "application/rss+xml,application/xml,text/xml,text/csv,application/octet-stream,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/",
        "Origin": "https://www.nseindia.com",
    })
    return session


def _normalise_symbol(value: object) -> str | None:
    symbol = str(value or "").strip().upper()
    if not symbol or symbol in {"NAN", "NONE"}:
        return None
    if symbol.startswith("NSE:"):
        symbol = symbol[4:]
    symbol = symbol.replace("-EQ", "").replace(".NS", "").strip()
    if not symbol or any(ch.isspace() for ch in symbol):
        return None
    return symbol


def _normalise_company(value: object) -> str:
    text = str(value or "").lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


@st.cache_data(ttl=900, show_spinner=False)
def fetch_nse_fo_universe() -> tuple[pd.DataFrame, str | None]:
    """Load the current NSE permitted-lot file; withhold partial universes."""
    session = _nse_session()
    try:
        for landing in NSE_FO_LANDING_PAGES:
            try:
                session.get(landing, timeout=12)
            except requests.RequestException:
                pass
        response = session.get(NSE_FO_LOTS_URL, timeout=25)
        response.raise_for_status()
        content = response.content
        content_type = response.headers.get("Content-Type", "unknown")
        if len(content) < 500 or b"<html" in content[:500].lower():
            return pd.DataFrame(), f"NSE permitted-lot download did not return CSV ({len(content)} bytes; {content_type})."

        best: set[str] = set()
        selected_column = None
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin1"):
            for skip in (2, 0, 1, 3):
                try:
                    frame = pd.read_csv(BytesIO(content), encoding=encoding, skiprows=skip, dtype=str)
                    frame.columns = [str(c).replace("\ufeff", "").strip().upper() for c in frame.columns]
                    column = "SYMBOL" if "SYMBOL" in frame.columns else next(
                        (c for c in frame.columns if c in {"UNDERLYING_SYMBOL", "UNDERLYING SYMBOL"}), None
                    )
                    if column:
                        symbols = {_normalise_symbol(v) for v in frame[column].dropna()}
                        symbols.discard(None)
                        if len(symbols) > len(best):
                            best, selected_column = symbols, column
                except Exception:
                    continue
        if len(best) < 50:
            return pd.DataFrame(), (
                f"NSE CSV yielded only {len(best)} valid symbols in {selected_column or 'the symbol column'}. "
                "Incomplete F&O universe withheld."
            )
        result = pd.DataFrame({"Symbol": sorted(best)})
        result["Ticker"] = result["Symbol"] + ".NS"
        result["Universe source"] = "NSE permitted-lot CSV"
        return result, None
    except Exception as exc:
        return pd.DataFrame(), f"NSE F&O universe request failed ({type(exc).__name__})."


@st.cache_data(ttl=21600, show_spinner=False)
def fetch_nse_company_master() -> tuple[pd.DataFrame, str | None]:
    """Map NSE trading symbols to registered company names for RSS matching."""
    try:
        response = _nse_session().get(NSE_EQUITY_MASTER_URL, timeout=25)
        response.raise_for_status()
        frame = pd.read_csv(BytesIO(response.content), dtype=str, encoding="utf-8-sig")
        frame.columns = [str(c).strip().upper() for c in frame.columns]
        if not {"SYMBOL", "NAME OF COMPANY"}.issubset(frame.columns):
            return pd.DataFrame(), "NSE equity master did not contain SYMBOL and NAME OF COMPANY columns."
        if "SERIES" in frame.columns:
            frame = frame[frame["SERIES"].fillna("").str.upper().eq("EQ")]
        frame = frame[["SYMBOL", "NAME OF COMPANY"]].dropna().drop_duplicates("SYMBOL")
        frame["Symbol"] = frame["SYMBOL"].map(_normalise_symbol)
        frame["Company"] = frame["NAME OF COMPANY"].astype(str).str.strip()
        frame["Company key"] = frame["Company"].map(_normalise_company)
        return frame[["Symbol", "Company", "Company key"]].dropna(subset=["Symbol"]), None
    except Exception as exc:
        return pd.DataFrame(), f"NSE equity master request failed ({type(exc).__name__})."


def _localise_feed_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
        return parsed.astimezone(ZoneInfo("Asia/Kolkata"))
    except Exception:
        return None


def _xml_text(parent: ET.Element, tag: str) -> str:
    node = parent.find(tag)
    if node is None or node.text is None:
        return ""
    return re.sub(r"\s+", " ", node.text).strip()


@st.cache_data(ttl=300, show_spinner=False)
def fetch_nse_rss_feed(feed_url: str, feed_label: str) -> tuple[pd.DataFrame, str | None]:
    """Read one NSE RSS feed and preserve the original item title, date and link."""
    try:
        response = _nse_session().get(feed_url, timeout=25)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        items = root.findall(".//item")
        rows = []
        for item in items:
            title = _xml_text(item, "title")
            description = _xml_text(item, "description")
            link = _xml_text(item, "link")
            pub_date = _xml_text(item, "pubDate")
            published = _localise_feed_time(pub_date)
            rows.append({
                "Headline": title,
                "Details": description,
                "Published": published,
                "Published (IST)": published.strftime("%d %b %Y, %I:%M %p") if published else "Time unavailable",
                "Source": "NSE RSS · " + feed_label,
                "Source link": link,
            })
        return pd.DataFrame(rows), None
    except Exception as exc:
        return pd.DataFrame(), f"{feed_label} feed unavailable ({type(exc).__name__})."


def match_fno_announcements(feed_frame: pd.DataFrame, universe: pd.DataFrame,
                            company_master: pd.DataFrame) -> pd.DataFrame:
    """Match RSS company-name headlines only when an exact NSE registered-name match exists."""
    if feed_frame.empty or universe.empty or company_master.empty:
        return pd.DataFrame()
    eligible = set(universe["Symbol"].astype(str))
    mapping = company_master[company_master["Symbol"].isin(eligible)].copy()
    name_to_symbol = {}
    for _, row in mapping.iterrows():
        key = _normalise_company(row["Company"])
        if key:
            name_to_symbol[key] = (row["Symbol"], row["Company"])
    rows = []
    for _, item in feed_frame.iterrows():
        headline = str(item.get("Headline", "")).strip()
        key = _normalise_company(headline)
        matched = name_to_symbol.get(key)
        if not matched:
            # Some RSS titles prefix the company name with a category or filing label.
            candidates = [(name, data) for name, data in name_to_symbol.items()
                          if name and (key.startswith(name + " ") or key.endswith(" " + name))]
            if len(candidates) == 1:
                matched = candidates[0][1]
        if not matched:
            continue
        symbol, company = matched
        rows.append({
            "Symbol": symbol,
            "Company": company,
            "Headline": headline,
            "Details": item.get("Details", ""),
            "Published": item.get("Published"),
            "Published (IST)": item.get("Published (IST)", "Time unavailable"),
            "Feed": item.get("Source", ""),
            "Source link": item.get("Source link", ""),
        })
    if not rows:
        return pd.DataFrame()
    result = pd.DataFrame(rows).drop_duplicates(subset=["Symbol", "Headline", "Published"])
    return result.sort_values("Published", ascending=False, na_position="last").reset_index(drop=True)



@st.cache_data(ttl=300, show_spinner=False)
def fetch_nse_json_announcements(days_back: int = 7) -> tuple[pd.DataFrame, str | None]:
    """Fetch NSE's public website JSON filing table; this is not a documented developer API."""
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    start = today - timedelta(days=max(1, int(days_back)))
    params = {
        "index": "equities",
        "from_date": start.strftime("%d-%m-%Y"),
        "to_date": today.strftime("%d-%m-%Y"),
    }
    try:
        session = _nse_session()
        session.headers.update({"Accept": "application/json, text/plain, */*"})
        response = session.get(NSE_ANNOUNCEMENTS_API, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            if payload.get("data") == [] or "no data found" in str(payload.get("msg", "")).lower():
                return pd.DataFrame(), None
            payload = payload.get("data", [])
        if not isinstance(payload, list):
            return pd.DataFrame(), "NSE announcements endpoint returned an unexpected response structure."
        rows = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            symbol = _normalise_symbol(item.get("symbol"))
            if not symbol:
                continue
            raw_time = item.get("sort_date") or item.get("an_dt") or item.get("exchdisstime")
            published = pd.to_datetime(raw_time, errors="coerce")
            if pd.isna(published):
                continue
            if published.tzinfo is None:
                published = published.tz_localize(ZoneInfo("Asia/Kolkata"))
            else:
                published = published.tz_convert(ZoneInfo("Asia/Kolkata"))
            attachment = str(item.get("attchmntFile") or "").strip()
            if attachment and attachment.startswith("/"):
                attachment = "https://nsearchives.nseindia.com" + attachment
            rows.append({
                "Symbol": symbol,
                "Company": str(item.get("sm_name") or item.get("company") or "").strip(),
                "Headline": str(item.get("desc") or item.get("subject") or "Corporate announcement").strip(),
                "Details": str(item.get("attchmntText") or item.get("summary") or "").strip(),
                "Published": published,
                "Published (IST)": published.strftime("%d %b %Y, %I:%M %p"),
                "Category": str(item.get("desc") or item.get("category") or "").strip(),
                "Source": "NSE Corporate Announcements JSON",
                "Source link": attachment,
                "Filing ID": str(item.get("seq_id") or ""),
            })
        frame = pd.DataFrame(rows)
        if not frame.empty:
            frame = frame.drop_duplicates(subset=["Filing ID", "Symbol", "Published"])
            frame = frame.sort_values("Published", ascending=False).reset_index(drop=True)
        return frame, None
    except Exception as exc:
        return pd.DataFrame(), f"NSE JSON announcements request failed ({type(exc).__name__})."


def _filter_fo_announcements(frame: pd.DataFrame, universe: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or universe.empty:
        return pd.DataFrame()
    symbols = set(universe["Symbol"].astype(str).str.upper())
    return frame[frame["Symbol"].astype(str).str.upper().isin(symbols)].copy().reset_index(drop=True)


@st.cache_data(ttl=900, show_spinner=False)
def get_sector_strength(micro_sectors):
    """Calculate parent-group and microsector RS using the shared MICRO_SECTORS baskets."""
    try:
        micro_baskets = {}
        parent_baskets = {}
        for micro_name, definition in (micro_sectors or {}).items():
            if not isinstance(definition, (tuple, list)) or len(definition) < 2:
                continue
            parent_name, members = str(definition[0]), definition[1]
            tickers = sorted({str(x).upper() for x in members if isinstance(x, str) and x.strip()})
            micro_baskets[micro_name] = tickers
            parent_baskets.setdefault(parent_name, set()).update(tickers)

        tickers = sorted(set(t for names in micro_baskets.values() for t in names) | {"^CRSLDX"})
        if not tickers:
            return {}, {}, "Shared microsector mapping is unavailable."
        history = yf.download(tickers, period="6mo", interval="1d", auto_adjust=True,
                              progress=False, threads=True, group_by="ticker")
        if history is None or history.empty:
            return {}, {}, "Price history unavailable. Positive-sector filtering is disabled until data is available."

        def get_close(ticker):
            try:
                if isinstance(history.columns, pd.MultiIndex):
                    if (ticker, "Close") in history.columns:
                        return history[(ticker, "Close")].dropna()
                    if ("Close", ticker) in history.columns:
                        return history[("Close", ticker)].dropna()
                return pd.Series(dtype=float)
            except Exception:
                return pd.Series(dtype=float)

        close_map = {ticker: get_close(ticker) for ticker in tickers}
        benchmark = close_map.get("^CRSLDX", pd.Series(dtype=float))
        if len(benchmark) < 64:
            return {}, {}, "NIFTY 500 benchmark history is insufficient. Positive-sector filtering is withheld."

        def assess(name, members):
            usable = [close_map[t] for t in members if t in close_map and len(close_map[t]) >= 64]
            minimum = max(2, int(len(members) * 0.5))
            if len(usable) < minimum:
                return {"Name": name, "Members with data": len(usable), "1M RS (pp)": None,
                        "3M RS (pp)": None, "Zone": "Insufficient data", "Positive": False}
            prices = pd.concat(usable, axis=1).sort_index().ffill()
            daily_returns = prices.pct_change()
            group_returns = daily_returns.mean(axis=1).dropna()
            group_index = (1 + group_returns).cumprod()
            common = group_index.index.intersection(benchmark.dropna().index)
            if len(common) < 64:
                return {"Name": name, "Members with data": len(usable), "1M RS (pp)": None,
                        "3M RS (pp)": None, "Zone": "Insufficient data", "Positive": False}
            group = group_index.reindex(common)
            bench = benchmark.reindex(common)
            rs1 = ((group.iloc[-1] / group.iloc[-21] - 1) -
                   (bench.iloc[-1] / bench.iloc[-21] - 1)) * 100
            rs3 = ((group.iloc[-1] / group.iloc[-64] - 1) -
                   (bench.iloc[-1] / bench.iloc[-64] - 1)) * 100
            zone = ("Leading" if rs1 >= 0 and rs3 >= 0 else
                    "Weakening" if rs3 >= 0 else
                    "Improving" if rs1 >= 0 else "Lagging")
            return {"Name": name, "Members with data": len(usable),
                    "1M RS (pp)": round(float(rs1), 2), "3M RS (pp)": round(float(rs3), 2),
                    "Zone": zone, "Positive": zone == "Leading"}

        micro = {name: assess(name, members) for name, members in micro_baskets.items()}
        parent = {name: assess(name, sorted(members)) for name, members in parent_baskets.items()}
        return micro, parent, None
    except Exception as exc:
        return {}, {}, "Sector-strength calculation failed (" + type(exc).__name__ + ")."


def filter_positive_sector_stocks(frame, micro_sectors, micro_strength, parent_strength):
    """Keep a stock only when every mapped parent group and microsector is Leading."""
    if frame.empty:
        return frame.copy()
    symbol_map = {}
    for micro_name, definition in (micro_sectors or {}).items():
        parent_name, members = str(definition[0]), definition[1]
        for ticker in members:
            symbol = str(ticker).upper().replace(".NS", "")
            symbol_map.setdefault(symbol, {"micro": set(), "parent": set()})
            symbol_map[symbol]["micro"].add(micro_name)
            symbol_map[symbol]["parent"].add(parent_name)

    kept = []
    for _, item in frame.iterrows():
        symbol = str(item.get("Symbol", "")).upper().replace(".NS", "")
        groups = symbol_map.get(symbol)
        if not groups:
            continue
        micro_names = sorted(groups["micro"])
        parent_names = sorted(groups["parent"])
        if not all(micro_strength.get(name, {}).get("Positive", False) for name in micro_names):
            continue
        if not all(parent_strength.get(name, {}).get("Positive", False) for name in parent_names):
            continue
        row = item.to_dict()
        row["Sector status"] = "🟢 Leading"
        row["Sector group(s)"] = ", ".join(parent_names)
        row["Microsector status"] = "🟢 Leading"
        row["Microsector(s)"] = ", ".join(micro_names)
        row["Sector 1M RS (pp)"] = ", ".join(
            f"{name}: {parent_strength[name]['1M RS (pp)']}" for name in parent_names
        )
        row["Microsector 1M RS (pp)"] = ", ".join(
            f"{name}: {micro_strength[name]['1M RS (pp)']}" for name in micro_names
        )
        kept.append(row)
    return pd.DataFrame(kept)


def _color_status_table(frame):
    def color(value):
        value = str(value)
        if "Leading" in value or "🟢" in value:
            return "background-color: #123d2a; color: #8ff0b2"
        if "Improving" in value or "🟡" in value:
            return "background-color: #493b12; color: #ffe08a"
        if "Weakening" in value or "Lagging" in value or "🔴" in value:
            return "background-color: #4a1e24; color: #ff9ca5"
        return "background-color: #343434; color: #c8c8c8"
    cols = [x for x in ["Zone", "Sector status", "Microsector status"] if x in frame.columns]
    return frame.style.map(color, subset=cols) if cols else frame.style

def _show_news_table(frame: pd.DataFrame, key: str) -> None:
    if frame.empty:
        st.info("No matching F&O announcements were returned for this time window. This may mean no matching filings or a feed/matching limitation; it is not proof that no company news exists.")
        return
    display = frame.drop(columns=["Published"], errors="ignore").copy()
    st.dataframe(
        _color_status_table(display),
        use_container_width=True,
        hide_index=True,
        height=min(620, 130 + 58 * len(display)),
        column_config={
            "Source link": st.column_config.LinkColumn("Original NSE filing", display_text="Open filing"),
        },
        key=key,
    )
    st.download_button(
        "Download announcements CSV",
        frame.drop(columns=["Published"], errors="ignore").to_csv(index=False).encode("utf-8-sig"),
        file_name=f"{key}.csv",
        mime="text/csv",
        key=f"{key}_download",
    )


def render_news_intelligence(micro_sectors=None) -> None:
    st.title("📰 News Intelligence")
    st.caption("F&O-first catalyst discovery · Official company disclosures · Microsector and price confirmation")
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    st.caption(f"Dashboard time: {now_ist:%d %b %Y, %I:%M %p} IST")

    fresh_tab, developing_tab, confirmed_tab, results_tab = st.tabs(
        ["Fresh News · 24h", "Developing · 7d", "Confirmed Opportunities", "Results & Financial Events"]
    )

    with st.expander("Coverage and data source", expanded=False):
        st.write(
            "The F&O list is sourced from NSE's permitted-lot file. The primary announcement source is "
            "NSE's public corporate-filings JSON endpoint, matched directly by NSE trading symbol. "
            "NSE RSS is retained as a fallback if the JSON endpoint is unavailable."
        )
        st.link_button("NSE RSS feed directory", "https://www.nseindia.in/static/rss-feed")
        st.link_button("NSE Equity Derivatives Contract Information",
                       "https://www.nseindia.com/products-services/equity-derivatives-contract-information")
        st.caption("F&O universe cache: 15 minutes · NSE JSON filing cache: 5 minutes · RSS fallback cache: 5 minutes.")
        if st.button("Refresh source data", key="news_refresh_all_sources"):
            fetch_nse_fo_universe.clear()
            fetch_nse_company_master.clear()
            fetch_nse_rss_feed.clear()
            fetch_nse_json_announcements.clear()

    with st.spinner("Loading NSE F&O universe and company-name master…"):
        universe, universe_error = fetch_nse_fo_universe()
        company_master, master_error = fetch_nse_company_master()

    if universe_error:
        st.error(universe_error)
    if master_error:
        st.error(master_error)

    with st.spinner("Fetching NSE corporate filings for the last 7 days…"):
        market_announcements, api_error = fetch_nse_json_announcements(7)

    if api_error:
        st.warning(api_error)
        st.caption("Falling back to the NSE RSS feed. RSS is a supplementary source and may not contain a full 7-day history.")
        announcement_feed, rss_error = fetch_nse_rss_feed(NSE_ANNOUNCEMENTS_RSS, "Corporate Announcements")
        if rss_error:
            st.warning(rss_error)
        market_announcements = match_fno_announcements(announcement_feed, universe, company_master)
    announcements = _filter_fo_announcements(market_announcements, universe)

    if not universe.empty:
        st.metric("Verified NSE F&O underlyings", f"{len(universe):,}")
    st.caption(
        f"F&O corporate filings matched by NSE symbol: {len(announcements)} · "
        f"All-market NSE JSON rows received: {len(market_announcements)}"
    )

    with st.spinner("Checking parent-sector and microsector relative strength…"):
        micro_strength, parent_strength, strength_error = get_sector_strength(micro_sectors or {})
    if strength_error:
        st.error(strength_error)
        st.warning("News candidates are withheld because positive sector and microsector strength could not be verified.")
        announcements = pd.DataFrame()
    else:
        total_before_filter = len(announcements)
        announcements = filter_positive_sector_stocks(
            announcements, micro_sectors or {}, micro_strength, parent_strength
        )
        st.caption(
            f"Positive parent-sector + microsector filter: {len(announcements)} of "
            f"{total_before_filter} F&O announcements retained."
        )
        with st.expander("Sector and microsector strength monitor", expanded=False):
            micro_table = pd.DataFrame(micro_strength.values())
            parent_table = pd.DataFrame(parent_strength.values())
            st.markdown("**Microsector relative strength**")
            st.dataframe(_color_status_table(micro_table), use_container_width=True, hide_index=True)
            st.markdown("**Parent-sector group relative strength**")
            st.dataframe(_color_status_table(parent_table), use_container_width=True, hide_index=True)

    cutoff_24h = pd.Timestamp(now_ist - timedelta(hours=24))
    cutoff_7d = pd.Timestamp(now_ist - timedelta(days=7))
    if not announcements.empty:
        announcements["Published"] = pd.to_datetime(announcements["Published"], errors="coerce")
        fresh = announcements[announcements["Published"] >= cutoff_24h].copy()
        developing = announcements[announcements["Published"] >= cutoff_7d].copy()
        result_pattern = r"financial result|unaudited result|audited result|quarterly result|outcome of board meeting"
        result_mask = (
            announcements["Headline"].fillna("").str.contains(result_pattern, case=False, regex=True)
            | announcements["Details"].fillna("").str.contains(result_pattern, case=False, regex=True)
        )
        financial_results = announcements[result_mask].copy()
    else:
        fresh, developing, financial_results = pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    with fresh_tab:
        st.subheader("Fresh catalysts · Last 24 hours")
        _show_news_table(fresh, "nse_fresh_announcements")
    with developing_tab:
        st.subheader("Developing opportunities · Last 7 days")
        _show_news_table(developing, "nse_developing_announcements")
    with confirmed_tab:
        st.subheader("Confirmed opportunities")
        st.info("This list will contain only F&O stocks with a Leading parent-sector group and Leading microsector. Individual stock price/volume confirmation and positive-catalyst classification are the next filters.")
    with results_tab:
        st.subheader("NSE financial results announcements")
        if not financial_results.empty:
            _show_news_table(financial_results, "nse_financial_results")
        else:
            st.info("No F&O filings matched the current financial-results keyword filter. This is a filing-headline view, not yet a complete structured financial-statement dataset.")
