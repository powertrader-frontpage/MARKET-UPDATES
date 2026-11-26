"""HVQ-only NSE single-stock futures volume scanner."""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from institutional_volume_data import load_nse_futures_master, fetch_continuous_futures_daily, volume_record_flags


def render_institutional_volume_intelligence(app_id: str | None, access_token: str | None,
                                              micro_sectors: dict):
    st.markdown(
        """<div style="font-size:clamp(27px,3.7vw,40px);font-weight:850;letter-spacing:-1px;line-height:1.15;margin:8px 0 6px;background:linear-gradient(90deg,#19e6a0,#29c7d6,#4aa8ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;">Institutional Volume Intelligence</div>""",
        unsafe_allow_html=True,
    )
    st.caption("NSE single-stock futures · HVQ only · 63 sessions · Research watchlist, not a buy/sell signal.")
    st.info("The scanner uses only futures-segment daily volume. Both conditions must pass: Daily Volume > 100,000 AND Daily Volume equals the 63-session Daily Max. No cash-market, market-cap, price, turnover, RSI or other filters are applied.")
    if not app_id or not access_token:
        st.warning("Connect FYERS from the Main Dashboard before running the futures-volume scan.")
        return

    with st.expander("Exact scanner rules", expanded=True):
        st.markdown(
            "- Universe: NSE single-stock futures segment (index futures excluded).\n"
            "- Condition 1: Daily futures Volume > 100,000.\n"
            "- Condition 2: Daily futures Volume = Daily Max(63, Daily Volume).\n"
            "- Both conditions are AND conditions.\n"
            "- Only completed daily candles are evaluated. No HVY or HVE calculations."
        )

    st.markdown("### HVQ validation — reference stocks")
    st.caption("This test checks Britannia, KFin Technologies and Torrent directly, without applying any additional filters.")
    validate = st.button("Validate Britannia, KFin and Torrent", key="validate_hvq_reference")
    scan = st.button("Run / refresh HVQ futures scan", type="primary", key="run_institutional_volume_scan")

    if validate or scan:
        with st.spinner("Loading the current NSE futures universe…"):
            master, master_error = load_nse_futures_master()
        if master.empty:
            st.error(master_error or "No NSE futures contracts were returned.")
            return

        if validate:
            names = {"BRITANNIA": "Britannia", "KFINTECH": "KFin Technologies", "TORNTPHARM": "Torrent Pharmaceuticals"}
            reference_rows = []
            for symbol, name in names.items():
                matches = master[master["_underlying"].astype(str).str.upper() == symbol]
                if matches.empty:
                    reference_rows.append({"Stock": name, "Status": "Not found in current futures master"})
                    continue
                contract = matches.iloc[0]
                time.sleep(1.5)
                candles, error = fetch_continuous_futures_daily(app_id, access_token, str(contract["_ticker"]))
                if candles.empty:
                    reference_rows.append({"Stock": name, "FYERS symbol": str(contract["_ticker"]), "Status": "History request failed", "Details": error or ""})
                    continue
                flags = volume_record_flags(candles)
                reference_rows.append({
                    "Stock": name,
                    "FYERS symbol": str(contract["_ticker"]),
                    "Status": flags.get("HVQ_status"),
                    "Completed sessions": flags.get("history_sessions"),
                    "Latest candle date": pd.to_datetime(candles["Date"].iloc[-1]).strftime("%Y-%m-%d"),
                    "Latest futures volume": int(flags.get("latest_volume", 0)),
                    "63-session maximum": int(flags["HVQ_63D_max"]) if flags.get("HVQ_63D_max") is not None else None,
                    "Above 100,000": flags.get("above_100k"),
                    "63-session high": flags.get("is_63_session_high"),
                    "HVQ qualified": flags.get("HVQ"),
                    "Details": error or "",
                })
            st.session_state["ivi_reference_validation"] = reference_rows
            st.session_state["ivi_reference_validation_time"] = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %H:%M:%S IST")

        if scan:
            rows = []
            failures = []
            insufficient = 0
            progress = st.progress(0.0, text="Scanning NSE single-stock futures…")
            total = len(master)
            for idx, contract in master.iterrows():
                # Pace requests; the data helper retries HTTP 429 responses with backoff.
                if idx:
                    time.sleep(1.2)
                symbol = str(contract["_underlying"]).strip().upper()
                candles, error = fetch_continuous_futures_daily(app_id, access_token, str(contract["_ticker"]))
                if candles.empty:
                    failures.append({"Symbol": symbol, "FYERS symbol": str(contract["_ticker"]), "Reason": error or "No candles returned"})
                    progress.progress((idx + 1) / total, text=f"History failures {idx + 1}/{total}")
                    continue
                flags = volume_record_flags(candles)
                if flags.get("HVQ") is None:
                    insufficient += 1
                elif flags.get("HVQ"):
                    rows.append({
                        "Symbol": symbol,
                        "Signal": "HVQ",
                        "Latest futures volume": int(flags["latest_volume"]),
                        "63-session maximum": int(flags["HVQ_63D_max"]),
                        "Signal date": pd.to_datetime(candles["Date"].iloc[-1]).strftime("%Y-%m-%d"),
                        "History sessions": flags["history_sessions"],
                        "FYERS symbol": str(contract["_ticker"]),
                    })
                progress.progress((idx + 1) / total, text=f"Scanned {idx + 1}/{total} futures underlyings")
            progress.empty()
            result = pd.DataFrame(rows)
            if not result.empty:
                result = result.sort_values("Latest futures volume", ascending=False).reset_index(drop=True)
            st.session_state["ivi_results"] = result
            st.session_state["ivi_failures"] = failures
            st.session_state["ivi_insufficient"] = insufficient
            st.session_state["ivi_master_count"] = total
            st.session_state["ivi_scan_time"] = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %H:%M:%S IST")

    if st.session_state.get("ivi_reference_validation") is not None:
        st.caption("Last reference validation: " + st.session_state.get("ivi_reference_validation_time", ""))
        st.dataframe(pd.DataFrame(st.session_state["ivi_reference_validation"]), use_container_width=True, hide_index=True)

    if "ivi_scan_time" in st.session_state:
        result = st.session_state.get("ivi_results", pd.DataFrame())
        failures = st.session_state.get("ivi_failures", [])
        st.caption(f"Last scan: {st.session_state['ivi_scan_time']} · Futures underlyings: {st.session_state.get('ivi_master_count', 0)}")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Futures universe", st.session_state.get("ivi_master_count", 0))
        m2.metric("HVQ matches", len(result))
        m3.metric("History failures", len(failures))
        m4.metric("Insufficient history", st.session_state.get("ivi_insufficient", 0))
        if not result.empty:
            st.dataframe(result, use_container_width=True, hide_index=True, height=560)
            st.download_button("Download HVQ watchlist CSV", result.to_csv(index=False).encode("utf-8"),
                               file_name="institutional_hvq_watchlist.csv", mime="text/csv",
                               key="download_institutional_hvq")
        else:
            st.warning("No futures contracts met both HVQ conditions in this scan.")
        if failures:
            with st.expander(f"FYERS history failures ({len(failures)})"):
                st.dataframe(pd.DataFrame(failures), use_container_width=True, hide_index=True)
