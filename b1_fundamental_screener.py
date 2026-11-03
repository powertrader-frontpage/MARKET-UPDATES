from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO
import re

import numpy as np
import pandas as pd
import requests
import streamlit as st


SCREENER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MarketIntelligenceTerminal/1.0)",
    "Accept": "text/html,application/json",
}


def _num(value):
    if value is None:
        return np.nan
    text = str(value).replace(",", "").replace("%", "").replace("₹", "").strip()
    text = re.sub(r"[^0-9.\-]", "", text)
    try:
        return float(text)
    except Exception:
        return np.nan


def _company_page(symbol):
    """Resolve an NSE ticker to a Screener.in company page without guessing its slug."""
    try:
        response = requests.get(
            "https://www.screener.in/api/company/search/",
            params={"q": symbol}, headers=SCREENER_HEADERS, timeout=12,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload if isinstance(payload, list) else payload.get("results", []) if isinstance(payload, dict) else []
        # The endpoint returns a ranked JSON list. Its display name often
        # omits the NSE ticker, so requiring the symbol in the title rejects
        # valid company matches (e.g. PPLPHARMA -> Piramal Pharma Ltd.).
        if isinstance(payload, dict):
            rows = payload.get("results") or payload.get("companies") or payload.get("data") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or row.get("href") or row.get("link") or "")
            if url.startswith("/"):
                url = "https://www.screener.in" + url
            if url.startswith("https://www.screener.in/company/"):
                return url
    except Exception:
        return None
    return None


def _extract_company_fundamentals(symbol, company, price):
    url = _company_page(symbol)
    if not url:
        return {"Symbol": symbol, "Company": company, "Fundamental source": "Screener.in · company lookup unavailable"}
    try:
        response = requests.get(url, headers=SCREENER_HEADERS, timeout=15)
        response.raise_for_status()
        tables = pd.read_html(StringIO(response.text))
        metrics = {}
        # Screener.in's headline ratios are list items, not always HTML tables.
        # Extract their visible label/value pairs first, then supplement from tables.
        for match in re.finditer(
            r'<li[^>]*>.*?<span[^>]*class="[^"]*name[^"]*"[^>]*>(.*?)</span>.*?<span[^>]*class="[^"]*number[^"]*"[^>]*>(.*?)</span>',
            response.text, flags=re.IGNORECASE | re.DOTALL
        ):
            label = re.sub(r"<[^>]+>", " ", match.group(1))
            value = re.sub(r"<[^>]+>", " ", match.group(2))
            label = " ".join(label.split()).lower()
            value = " ".join(value.split())
            if label:
                metrics[label] = value
        for table in tables:
            if table.empty:
                continue
            for _, row in table.iterrows():
                vals = [str(v).strip() for v in row.tolist() if pd.notna(v)]
                if len(vals) >= 2:
                    metrics.setdefault(vals[0].strip().lower(), vals[1])

        def metric(*labels):
            for label in labels:
                for key, value in metrics.items():
                    if key == label.lower() or key.startswith(label.lower()):
                        return _num(value)
            return np.nan

        market_cap = metric("Market Cap")
        roe = metric("ROE")
        roce_10y = metric("ROCE 10Years", "ROCE 10 Years", "Average ROCE 10Years")
        sales_10y = metric("Sales growth 10Years", "Sales Growth 10Years", "Sales growth 10 Years")
        peg = metric("PEG Ratio")
        high_raw = next((v for k, v in metrics.items() if k.startswith("high / low")), None)
        high_value = np.nan
        if high_raw is not None:
            parts = re.findall(r"[0-9][0-9,]*\.?[0-9]*", str(high_raw))
            if parts:
                high_value = _num(parts[0])

        # Latest annual sales growth: latest two annual Sales values, not quarterly YoY.
        annual_sales_growth = np.nan
        for table in tables:
            if table.empty or table.shape[1] < 3:
                continue
            sales_row = table[table.iloc[:, 0].astype(str).str.strip().str.lower() == "sales"]
            if sales_row.empty:
                continue
            vals = [_num(v) for v in sales_row.iloc[0, 1:].tolist()]
            vals = [v for v in vals if pd.notna(v)]
            # Screener.in annual columns are displayed newest-first.
            if len(vals) >= 2 and vals[1] != 0:
                annual_sales_growth = (vals[0] / vals[1] - 1) * 100
                break

        distance_below_high = ((high_value - float(price)) / high_value * 100
                               if pd.notna(high_value) and high_value > 0 and pd.notna(price) else np.nan)
        values = {
            "Latest annual sales growth %": annual_sales_growth,
            "10Y sales growth %": sales_10y,
            "ROE %": roe,
            "10Y average ROCE %": roce_10y,
            "Market cap ₹ Cr": market_cap,
            "Distance below 52W high %": distance_below_high,
            "PEG ratio": peg,
        }
        checks = {
            "Annual sales >10%": pd.notna(annual_sales_growth) and annual_sales_growth > 10,
            "10Y sales growth >10%": pd.notna(sales_10y) and sales_10y > 10,
            "ROE >15%": pd.notna(roe) and roe > 15,
            "10Y avg ROCE >15%": pd.notna(roce_10y) and roce_10y > 15,
            "Market cap >₹5,000 Cr": pd.notna(market_cap) and market_cap > 5000,
            "Within 15% of 52W high": pd.notna(distance_below_high) and distance_below_high < 15,
            "PEG ≤2": pd.notna(peg) and peg <= 2,
        }
        passed = [name for name, ok in checks.items() if ok]
        failed = [name for name, ok in checks.items() if not ok]
        missing = [label for label, value in values.items() if pd.isna(value)]
        return {
            "Symbol": symbol, "Company": company, **values,
            "Filters passed": len(passed), "Filters failed": ", ".join(failed) if failed else "—",
            "Missing fields": ", ".join(missing) if missing else "—",
            "Missing field count": len(missing),
            "Qualified": len(passed) == len(checks), "Fundamental source": "Screener.in",
            "Source link": url,
        }
    except Exception as exc:
        return {"Symbol": symbol, "Company": company, "Fundamental source": f"Unavailable ({exc.__class__.__name__})"}


@st.cache_data(ttl=21600, show_spinner=False)
def _load_b1_fundamentals(candidates):
    rows = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(_extract_company_fundamentals, symbol, company, price): symbol
                   for symbol, company, price in candidates}
        for future in as_completed(futures):
            try:
                rows.append(future.result())
            except Exception:
                continue
    return pd.DataFrame(rows)


def render_b1_fundamentals_tab():
    st.markdown("### Smallcap & Midcap · Stage 2 B1 Fundamentals")
    st.caption("Fresh B1 candidates from the completed-week Weinstein scanner · Screener.in fundamentals · 6-hour cache.")
    scan_data = st.session_state.get("explosive_breakout_scan_data")
    if not scan_data or len(scan_data) < 1:
        st.info("Run the Explosive Breakout Scanner first. This tab uses its fresh Stage 2 B1 list; it does not run a second price scan.")
        return
    fresh = scan_data[0].copy()
    if fresh.empty:
        st.info("No fresh Stage 2 B1 stocks are currently available from the scanner.")
        return
    if not {"Symbol", "Company", "Price"}.issubset(fresh.columns):
        st.warning("The current scanner result is missing required symbol, company or price fields.")
        return
    candidates = tuple((str(row["Symbol"]).strip(), str(row["Company"]).strip(), float(row["Price"]))
                       for _, row in fresh.dropna(subset=["Price"]).iterrows())
    if not candidates:
        st.info("No fresh B1 candidates have a valid price for fundamental screening.")
        return
    if st.button("Refresh B1 fundamentals", key="refresh_b1_fundamentals"):
        _load_b1_fundamentals.clear()
    with st.spinner(f"Checking seven fundamental filters for {len(candidates)} fresh B1 stocks…"):
        fundamentals = _load_b1_fundamentals(candidates)
    if fundamentals.empty:
        st.warning("Screener.in did not return usable fundamental records. No stocks have been assumed to qualify.")
        return
    merged = fresh.merge(fundamentals, on=["Symbol", "Company"], how="left")
    qualified = merged[merged.get("Qualified", pd.Series(False, index=merged.index)).fillna(False)].copy()
    q1, q2, q3, q4 = st.columns(4)
    q1.metric("Fresh B1 candidates", len(merged))
    q2.metric("Passed all 7 filters", len(qualified))
    q3.metric("Company lookup unavailable", int(merged.get("Fundamental source", pd.Series("", index=merged.index)).astype(str).str.contains("unavailable|error", case=False).sum()))
    q4.metric("Candidates with missing values", int(merged.get("Missing field count", pd.Series(7, index=merged.index)).fillna(7).gt(0).sum()))
    st.markdown("#### Qualified stocks · all seven conditions met")
    if qualified.empty:
        st.info("No fresh B1 stock currently passes all seven filters with available data.")
    else:
        cols = ["Symbol", "Company", "Segment", "Sector", "Price", "Latest annual sales growth %",
                "10Y sales growth %", "ROE %", "10Y average ROCE %", "Market cap ₹ Cr",
                "Distance below 52W high %", "PEG ratio", "Filters passed", "Source link"]
        st.dataframe(qualified[[c for c in cols if c in qualified.columns]], use_container_width=True,
                     hide_index=True, column_config={"Source link": st.column_config.LinkColumn("Screener.in source")})
        st.download_button("Download qualified B1 stocks (CSV)", qualified.to_csv(index=False).encode("utf-8"),
                           "small_midcap_stage2_b1_fundamentals.csv", "text/csv",
                           key="download_qualified_b1_fundamentals")
    with st.expander("All fresh B1 candidates · filter-by-filter audit", expanded=False):
        audit_cols = ["Symbol", "Company", "Segment", "Price", "Latest annual sales growth %",
                      "10Y sales growth %", "ROE %", "10Y average ROCE %", "Market cap ₹ Cr",
                      "Distance below 52W high %", "PEG ratio", "Filters passed", "Filters failed",
                      "Missing field count", "Missing fields", "Fundamental source", "Source link"]
        st.dataframe(merged[[c for c in audit_cols if c in merged.columns]], use_container_width=True,
                     hide_index=True, column_config={"Source link": st.column_config.LinkColumn("Screener.in source")})
    st.caption("The audit displays all seven extracted values. A missing value is shown as blank and listed separately; it is not treated as a confirmed fundamental failure. Latest annual sales growth is calculated from the latest two annual Sales figures. 10-year sales growth and 10-year average ROCE are read from Screener.in ratios. Market cap is ₹ crore. Distance below the 52-week high uses the scanner price and reported high. Verify source values; this is a research filter, not a buy signal.")
