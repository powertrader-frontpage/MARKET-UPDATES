"""HVQ scan of underlying stock volume for the NSE NFO-eligible universe."""
from __future__ import annotations
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st
from institutional_volume_data import load_nse_futures_master, fetch_nfo_stock_daily, volume_record_flags


@st.cache_data(ttl=21600, show_spinner=False)
def _cached_stock_history(app_id: str, access_token: str, symbol: str):
    """Cache successful daily candles for six hours; never cache failed requests."""
    # This delay runs only on cache misses. It keeps requests below FYERS'
    # 50-per-minute Standard-plan ceiling and avoids concurrent request bursts.
    time.sleep(1.3)
    candles, error = fetch_nfo_stock_daily(app_id, access_token, symbol)
    if candles is None or candles.empty:
        raise RuntimeError(error or "No daily stock candles returned")
    return candles


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
        total = len(symbols)

        # Sequential cache misses respect the FYERS rate limit. Previously
        # successful symbols are served from cache and do not incur this delay.
        for idx, symbol in enumerate(symbols):
            try:
                candles = _cached_stock_history(app_id, access_token, symbol)
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
            except Exception as exc:
                failures.append({"Stock": symbol, "Reason": str(exc) or f"Fetch error ({exc.__class__.__name__})"})

            progress.progress((idx + 1) / total, text=f"Checked {idx + 1}/{total} NFO stocks · {len(rows)} HVQ matches")

        progress.empty()
        result = pd.DataFrame(
            sorted(rows, key=lambda item: item["Stock"]),
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
            sector_zones = st.session_state.get("ivi_sector_zone_map", {})
            micro_zones = st.session_state.get("ivi_micro_zone_map", {})
            display_result = result.drop(columns=["Strength"], errors="ignore").copy()
            display_result["Sector Zone"] = display_result["Sector"].map(
                lambda value: "; ".join(dict.fromkeys(
                    sector_zones.get(name.strip(), "Unavailable") for name in str(value).split(";")
                ))
            )
            display_result["Microsector Zone"] = display_result["Microsector"].map(
                lambda value: "; ".join(dict.fromkeys(
                    micro_zones.get(name.strip(), "Unavailable") for name in str(value).split(";")
                ))
            )
            zone_css = {
                "Leading": "color: #22C55E; background-color: rgba(34,197,94,.16); font-weight: 700;",
                "Improving": "color: #60A5FA; background-color: rgba(59,130,246,.16); font-weight: 700;",
                "Weakening": "color: #F59E0B; background-color: rgba(245,158,11,.16); font-weight: 700;",
                "Lagging": "color: #F87171; background-color: rgba(239,68,68,.16); font-weight: 700;",
                "Unavailable": "color: #94A3B8; background-color: rgba(148,163,184,.10);",
            }
            def _zone_style(value):
                return ";".join(zone_css.get(part.strip(), "") for part in str(value).split(";"))
            styled = (
                display_result.style
                .map(lambda _: "color: #7DD3FC; background-color: rgba(14, 116, 144, 0.16); font-weight: 600;", subset=["Sector"])
                .map(lambda _: "color: #C4B5FD; background-color: rgba(109, 40, 217, 0.16); font-weight: 600;", subset=["Microsector"])
                .map(_zone_style, subset=["Sector Zone", "Microsector Zone"])
            )
            if not sector_zones or not micro_zones:
                st.caption("Rotation colours use the Main Dashboard's latest calculated zones. Visit/refresh Main Dashboard once if zones show Unavailable.")
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
