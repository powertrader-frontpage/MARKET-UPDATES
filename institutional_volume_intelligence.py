"""HVQ scan of underlying stock volume for the NSE NFO-eligible universe."""
from __future__ import annotations
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st
from institutional_volume_data import load_nse_futures_master, fetch_nfo_stock_daily, volume_record_flags


def render_institutional_volume_intelligence(app_id: str | None, access_token: str | None, micro_sectors: dict):
    st.markdown("## Institutional Volume Intelligence")
    st.caption("NFO-eligible stocks · underlying NSE cash-market volume · HVQ only")
    st.info("Both conditions apply to the underlying stock's daily cash volume: Volume > 100,000 AND Volume equals the maximum of the latest 63 completed sessions.")
    if not app_id or not access_token:
        st.warning("Connect FYERS from the Main Dashboard before scanning.")
        return

    scan = st.button("Run / refresh HVQ scan", type="primary", key="run_institutional_volume_scan")
    if scan:
        master, error = load_nse_futures_master()
        if master.empty:
            st.error(error or "NFO stock universe unavailable.")
            return

        memberships = {}
        groups = {}
        for micro_name, (sector_name, symbols) in micro_sectors.items():
            for item in symbols:
                key = str(item).replace(".NS", "").upper()
                memberships.setdefault(key, []).append(str(micro_name))
                groups.setdefault(key, []).append(str(sector_name))

        rows, failures, insufficient = [], [], 0
        progress = st.progress(0.0, text="Checking underlying stock volumes…")
        total = len(master)
        for idx, contract in master.iterrows():
            if idx:
                time.sleep(1.25)
            symbol = str(contract["_underlying"]).replace(".NS", "").upper()
            candles, fetch_error = fetch_nfo_stock_daily(app_id, access_token, symbol)
            if candles.empty:
                failures.append({"Stock": symbol, "Reason": fetch_error or "No daily stock candles"})
                progress.progress((idx + 1) / total, text=f"Checked {idx + 1}/{total} NFO stocks")
                continue
            flags = volume_record_flags(candles)
            if flags.get("HVQ") is None:
                insufficient += 1
            elif flags.get("HVQ"):
                sectors = list(dict.fromkeys(groups.get(symbol, [])))
                micro_names = list(dict.fromkeys(memberships.get(symbol, [])))
                rows.append({
                    "Stock": symbol,
                    "Signal": "HVQ",
                    "Sector": "; ".join(sectors) or "Not mapped",
                    "Microsector": "; ".join(micro_names) or "Not mapped",
                    "Strength": "Awaiting live rotation join",
                    "RS": "—",
                })
            progress.progress((idx + 1) / total, text=f"Checked {idx + 1}/{total} NFO stocks")
        progress.empty()

        result = pd.DataFrame(
            rows,
            columns=["Stock", "Signal", "Sector", "Microsector", "Strength", "RS"],
        )
        st.session_state["ivi_compact_results"] = result
        st.session_state["ivi_compact_failures"] = failures
        st.session_state["ivi_compact_insufficient"] = insufficient
        st.session_state["ivi_compact_universe"] = total
        st.session_state["ivi_compact_time"] = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %H:%M IST")

    if "ivi_compact_results" in st.session_state:
        result = st.session_state["ivi_compact_results"]
        st.caption(
            f"Last scan: {st.session_state['ivi_compact_time']} · "
            f"NFO stocks: {st.session_state['ivi_compact_universe']} · HVQ matches: {len(result)}"
        )
        if result.empty:
            st.info("No stocks met both HVQ conditions in the latest scan.")
        else:
            # Distinct, restrained category colours make the two classification
            # levels easy to scan on mobile without implying rotation strength.
            styled = (
                result.style
                .map(lambda _: "color: #7DD3FC; background-color: rgba(14, 116, 144, 0.16); font-weight: 600;", subset=["Sector"])
                .map(lambda _: "color: #C4B5FD; background-color: rgba(109, 40, 217, 0.16); font-weight: 600;", subset=["Microsector"])
            )
            st.dataframe(styled, use_container_width=True, hide_index=True)
            st.download_button(
                "Download HVQ watchlist",
                result.to_csv(index=False).encode("utf-8"),
                "hvq_watchlist.csv",
                "text/csv",
            )

        fails = st.session_state.get("ivi_compact_failures", [])
        with st.expander(
            f"Scan diagnostics · {len(fails)} data failures · "
            f"{st.session_state.get('ivi_compact_insufficient', 0)} insufficient histories"
        ):
            if fails:
                st.dataframe(pd.DataFrame(fails), use_container_width=True, hide_index=True)
            else:
                st.caption("No data failures.")
"