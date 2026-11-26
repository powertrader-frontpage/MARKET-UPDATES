from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from io import BytesIO

import pandas as pd
import requests
import streamlit as st

NSE_FO_LOTS_URL = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
NSE_FO_LANDING_PAGES = (
    "https://www.nseindia.com/",
    "https://www.nseindia.com/products-services/equity-derivatives-contract-information",
)


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


@st.cache_data(ttl=900, show_spinner=False)
def fetch_nse_fo_universe() -> tuple[pd.DataFrame, str | None]:
    """Load the current NSE permitted-lot file; withhold partial universes."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/csv,application/octet-stream,text/plain,*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.nseindia.com/products-services/equity-derivatives-contract-information",
        "Origin": "https://www.nseindia.com",
    }
    session = requests.Session()
    session.headers.update(headers)
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


def render_news_intelligence() -> None:
    st.title("📰 News Intelligence")
    st.caption("F&O-first catalyst discovery · Official company disclosures · Microsector and price confirmation")
    now_ist = datetime.now(ZoneInfo("Asia/Kolkata"))
    st.caption(f"Dashboard time: {now_ist:%d %b %Y, %I:%M %p} IST")

    fresh_tab, developing_tab, confirmed_tab, results_tab = st.tabs(
        ["Fresh News · 24h", "Developing · 7d", "Confirmed Opportunities", "Results & Financial Events"]
    )

    with st.expander("Coverage and data source", expanded=True):
        st.write(
            "The universe is sourced from NSE's current permitted-lot file. "
            "News collection will use exchange disclosures first, with reputable financial news as supplementary context."
        )
        st.link_button(
            "Open NSE Equity Derivatives Contract Information",
            "https://www.nseindia.com/products-services/equity-derivatives-contract-information",
        )
        st.caption("F&O universe cache: 15 minutes. Failed or incomplete downloads are shown as unavailable, not guessed.")
        if st.button("Refresh NSE F&O universe", key="news_refresh_fo_universe"):
            fetch_nse_fo_universe.clear()
        with st.spinner("Checking the current NSE F&O universe…"):
            universe, error = fetch_nse_fo_universe()
        if error:
            st.warning(error)
        elif not universe.empty:
            st.metric("Verified NSE F&O underlyings", f"{len(universe):,}")
            st.dataframe(universe, use_container_width=True, hide_index=True, height=260)
            st.download_button(
                "Download verified F&O universe",
                universe.to_csv(index=False).encode("utf-8-sig"),
                "nse_fo_universe.csv",
                "text/csv",
                key="news_download_fo_universe",
            )

    with fresh_tab:
        st.subheader("Fresh catalysts · Last 24 hours")
        st.info("Next stage: collect new NSE/BSE corporate announcements, assess catalyst quality and retain source links and timestamps.")
    with developing_tab:
        st.subheader("Developing opportunities · Last 7 days")
        st.info("Next stage: track recent catalysts and identify stocks awaiting microsector and price confirmation.")
    with confirmed_tab:
        st.subheader("Confirmed opportunities")
        st.info("Only stocks passing positive catalyst, positive microsector and individual price/volume confirmation will appear here.")
    with results_tab:
        st.subheader("Results & Financial Events")
        st.info("Next stage: collect exchange-filed quarterly results and compare revenue, EBITDA, profit and margins. Missing values remain blank.")
