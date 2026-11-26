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


NSE_FO_LOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
NSE_EQUITY_MASTER_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_ANNOUNCEMENTS_RSS = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"
NSE_RESULTS_RSS = "https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml"
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
                    frame.columns = [str(c).replace("\\ufeff", "").strip().upper() for c in frame.columns]
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
    return re.sub(r"\\s+", " ", node.text).strip()


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


def _show_news_table(frame: pd.DataFrame, key: str) -> None:
    if frame.empty:
        st.info("No matching F&O announcements were returned for this time window. This may mean no matching filings or a feed/matching limitation; it is not proof that no company news exists.")
        return
    display = frame.drop(columns=["Published"], errors="ignore").copy()
    st.dataframe(
        display,
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


def render_news_intelligence() -> None:
    st.title("📰 News Intelligence")
    st.caption("F&O-first catalyst discovery · Official company disclosures · Microsector and price confirmation")
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    st.caption(f"Dashboard time: {now_ist:%d %b %Y, %I:%M %p} IST")

    fresh_tab, developing_tab, confirmed_tab, results_tab = st.tabs(
        ["Fresh News · 24h", "Developing · 7d", "Confirmed Opportunities", "Results & Financial Events"]
    )

    with st.expander("Coverage and data source", expanded=False):
        st.write(
            "The F&O list is sourced from NSE's permitted-lot file. Announcements and financial-result headlines "
            "are read from NSE RSS feeds and matched against NSE's registered company-name master. "
            "Only exact company-name matches are included; ambiguous matches are withheld."
        )
        st.link_button("NSE RSS feed directory", "https://www.nseindia.in/static/rss-feed")
        st.link_button("NSE Equity Derivatives Contract Information",
                       "https://www.nseindia.com/products-services/equity-derivatives-contract-information")
        st.caption("F&O universe cache: 15 minutes · Company-name master: 6 hours · RSS cache: 5 minutes.")
        if st.button("Refresh source data", key="news_refresh_all_sources"):
            fetch_nse_fo_universe.clear()
            fetch_nse_company_master.clear()
            fetch_nse_rss_feed.clear()

    with st.spinner("Loading NSE F&O universe and company-name master…"):
        universe, universe_error = fetch_nse_fo_universe()
        company_master, master_error = fetch_nse_company_master()

    if universe_error:
        st.error(universe_error)
    if master_error:
        st.error(master_error)

    announcement_feed, announcement_error = fetch_nse_rss_feed(
        NSE_ANNOUNCEMENTS_RSS, "Corporate Announcements"
    )
    results_feed, results_error = fetch_nse_rss_feed(
        NSE_RESULTS_RSS, "Financial Results"
    )
    if announcement_error:
        st.warning(announcement_error)
    if results_error:
        st.warning(results_error)

    announcements = match_fno_announcements(announcement_feed, universe, company_master)
    financial_results = match_fno_announcements(results_feed, universe, company_master)

    if not universe.empty:
        st.metric("Verified NSE F&O underlyings", f"{len(universe):,}")
    if not announcements.empty:
        st.caption(f"Matched corporate announcements: {len(announcements)} · feed entries: {len(announcement_feed)}")
    else:
        st.caption(f"Corporate RSS entries received: {len(announcement_feed)} · matching F&O companies: 0")

    cutoff_24h = pd.Timestamp(now_ist - timedelta(hours=24))
    cutoff_7d = pd.Timestamp(now_ist - timedelta(days=7))
    if not announcements.empty:
        announcements["Published"] = pd.to_datetime(announcements["Published"], errors="coerce")
        fresh = announcements[announcements["Published"] >= cutoff_24h].copy()
        developing = announcements[announcements["Published"] >= cutoff_7d].copy()
    else:
        fresh, developing = pd.DataFrame(), pd.DataFrame()

    with fresh_tab:
        st.subheader("Fresh catalysts · Last 24 hours")
        _show_news_table(fresh, "nse_fresh_announcements")
    with developing_tab:
        st.subheader("Developing opportunities · Last 7 days")
        _show_news_table(developing, "nse_developing_announcements")
    with confirmed_tab:
        st.subheader("Confirmed opportunities")
        st.info("This section will be activated after catalyst relevance, microsector strength and stock price/volume confirmation are implemented. No candidate is promoted on news alone.")
    with results_tab:
        st.subheader("NSE financial results announcements")
        if not financial_results.empty:
            _show_news_table(financial_results, "nse_financial_results")
        else:
            st.info("No matching F&O financial-result headlines were returned. The RSS feed contains result filing headlines, not a complete structured financial-statement dataset; that extraction is a later step.")
        if results_error:
            st.warning(results_error)
