"""HVQ scan of underlying stock volume for the NSE NFO-eligible universe."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st
from institutional_volume_data import load_nse_futures_master, fetch_nfo_stock_daily, volume_record_flags


@st.cache_data(ttl=21600, show_spinner=False)
def _cached_stock_history(app_id: str, access_token: str, symbol: str):
    """Reuse daily candles for six hours; refreshes automatically after expiry."""
    return fetch_nfo_stock_daily(app_id, access_token, symbol)


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

        memberships, groups = {}, {}
        for micro_name, (sector_name, symbols) in micro_sectors.items():
            for item in symbols:
                key = str(item).replace(".NS", "").upper()
                memberships.setdefault(key, []).append(str(micro_name))
                groups.setdefault(key, []).append(str(sector_name))

        symbols = list(dict.fromkeys(
            str(value).replace(".NS", "").upper()
            for value in master["_underlying"].tolist()
        ))
        rows, failures, insufficient = [], [], 0
        progress = st.progress(0.0, text="Checking underlying stock volumes…")
        completed = 0

        # A small worker pool speeds up the first scan without sending a large
        # burst of requests to FYERS. Cached symbols make repeat scans much faster.
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(_cached_stock_history, app_id, access_token, symbol): symbol
                for symbol in symbols
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    candles, fetch_error = future.result()
                except Exception as exc:
                    candles, fetch_error = pd.DataFrame(), f"Fetch error ({exc.__class__.__name__})"

                if candles.empty:
                    failures.append({"Stock": symbol, "Reason": fetch_error or "No daily stock candles"})
                else:
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

                completed += 1
                progress.progress(completed / len(symbols), text=f"Checked {completed}/{len(symbols)} NFO stocks")

        progress.empty()
        result = pd.DataFrame(
            sorted(rows, key=lambda item: item["Stock"]),
            columns=["Stock", "Signal", "Sector", "Microsector", "Strength", "RS"],
        )
        st.session_state["ivi_compact_results"] = result
        st.session_state["ivi_compact_failures"] = failures
        st.session_state["ivi_compact_insufficient"] = insufficient
        st.session_state["ivi_compact_universe"] = len(symbols)
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
