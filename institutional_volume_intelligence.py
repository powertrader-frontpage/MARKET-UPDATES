"""HVQ and Pocket Pivot Volume scan for the NSE NFO-eligible universe."""
from __future__ import annotations
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import streamlit as st
from institutional_volume_data import load_nse_futures_master, fetch_nfo_stock_daily


@st.cache_data(ttl=21600, show_spinner=False)
def _cached_stock_history(app_id: str, access_token: str, symbol: str):
    """Cache successful daily candles for six hours; never cache failed requests."""
    time.sleep(1.3)  # Stay within FYERS Standard-plan request limits on cache misses.
    candles, error = fetch_nfo_stock_daily(app_id, access_token, symbol)
    if candles is None or candles.empty:
        raise RuntimeError(error or "No daily stock candles returned")
    return candles


def _latest_completed_session(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    if "Date" in data:
        data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
        data = data.dropna(subset=["Date"]).sort_values("Date")
        now_ist = pd.Timestamp.now(tz="Asia/Kolkata")
        if not data.empty and data["Date"].iloc[-1].date() == now_ist.date() and now_ist.hour < 16:
            data = data.iloc[:-1]
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in data:
            data[col] = pd.to_numeric(data[col], errors="coerce")
    return data.dropna(subset=["Open", "Close", "Volume"]).reset_index(drop=True)


def _volume_signals(frame: pd.DataFrame) -> tuple[bool, bool, str | None]:
    """Return (HVQ, PPV, diagnostic); both signals use completed cash-equity bars."""
    data = _latest_completed_session(frame)
    if len(data) < 64:
        return False, False, f"Insufficient history ({len(data)}/64 sessions)"

    volume = data["Volume"]
    latest_volume = float(volume.iloc[-1])

    # HVQ: strict new record versus the preceding 63 completed sessions.
    prior_63_max = float(volume.iloc[-64:-1].max())
    hvq = latest_volume > 100000 and latest_volume > prior_63_max

    # PPV: today's up-close volume exceeds every down-day volume in the
    # preceding 10 sessions, and the close is above its 10-session SMA.
    close = data["Close"]
    prior_close = close.shift(1)
    prior_10 = data.iloc[-11:-1].copy()
    prior_10_down = prior_10.loc[prior_10["Close"] < prior_10["Close"].shift(1), "Volume"]
    # Include the first day in the 10-session window by comparing it with
    # the immediately preceding close from the full history.
    prior_10_indices = data.index[-10:-1]
    down_volumes = []
    for pos in prior_10_indices:
        if pos > 0 and float(data.at[pos, "Close"]) < float(data.at[pos - 1, "Close"]):
            down_volumes.append(float(data.at[pos, "Volume"]))
    ppv_reference = max(down_volumes) if down_volumes else None
    sma10 = float(close.iloc[-10:].mean())
    ppv = (
        ppv_reference is not None
        and float(close.iloc[-1]) > float(prior_close.iloc[-1])
        and float(close.iloc[-1]) > sma10
        and latest_volume > ppv_reference
    )
    return bool(hvq), bool(ppv), None


def render_institutional_volume_intelligence(app_id: str | None, access_token: str | None, micro_sectors: dict):
    st.markdown("## Institutional Volume Intelligence")
    st.caption("NFO-eligible stocks · underlying NSE cash-market volume · HVQ + Pocket Pivot Volume")
    st.info(
        "HVQ: cash volume > 100,000 and a strict new 63-session volume record. "
        "PPV: up-close volume exceeds the highest down-day volume in the prior 10 sessions, "
        "with close above the 10-day moving average. Stocks qualifying under either signal are shown."
    )
    if not app_id or not access_token:
        st.warning("Connect FYERS from the Main Dashboard before scanning.")
        return

    scan = st.button("Run / refresh HVQ + PPV scan", type="primary", key="run_institutional_volume_scan")
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

        for idx, symbol in enumerate(symbols):
            try:
                candles = _cached_stock_history(app_id, access_token, symbol)
                hvq, ppv, diagnostic = _volume_signals(candles)
                if diagnostic:
                    insufficient += 1
                elif hvq or ppv:
                    signals = "HVQ + PPV" if hvq and ppv else "HVQ" if hvq else "PPV"
                    sectors = list(dict.fromkeys(groups.get(symbol, [])))
                    micro_names = list(dict.fromkeys(memberships.get(symbol, [])))
                    rows.append({
                        "Stock": symbol,
                        "Signal": signals,
                        "Sector": "; ".join(sectors) or "Not mapped",
                        "Microsector": "; ".join(micro_names) or "Not mapped",
                    })
            except Exception as exc:
                failures.append({"Stock": symbol, "Reason": str(exc) or f"Fetch error ({exc.__class__.__name__})"})

            progress.progress((idx + 1) / total, text=f"Checked {idx + 1}/{total} NFO stocks · {len(rows)} matches")

        progress.empty()
        result = pd.DataFrame(
            sorted(rows, key=lambda item: item["Stock"]),
            columns=["Stock", "Signal", "Sector", "Microsector"],
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
            f"NFO stocks: {st.session_state['ivi_compact_universe']} · Qualifying stocks: {len(result)}"
        )
        if result.empty:
            st.info("No stocks met either HVQ or PPV condition in the latest scan.")
        else:
            sector_zones = st.session_state.get("ivi_sector_zone_map", {})
            micro_zones = st.session_state.get("ivi_micro_zone_map", {})
            display_result = result.copy()
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
                .map(lambda value: "color: #22C55E; background-color: rgba(34,197,94,.14); font-weight: 700;" if "HVQ" in str(value) else "color: #60A5FA; background-color: rgba(59,130,246,.14); font-weight: 700;", subset=["Signal"])
                .map(lambda _: "color: #7DD3FC; background-color: rgba(14, 116, 144, 0.16); font-weight: 600;", subset=["Sector"])
                .map(lambda _: "color: #C4B5FD; background-color: rgba(109, 40, 217, 0.16); font-weight: 600;", subset=["Microsector"])
                .map(_zone_style, subset=["Sector Zone", "Microsector Zone"])
            )
            if not sector_zones or not micro_zones:
                st.caption("Rotation colours use the Main Dashboard's latest calculated zones. Visit/refresh Main Dashboard once if zones show Unavailable.")
            st.dataframe(styled, use_container_width=True, hide_index=True)
            st.download_button(
                "Download HVQ + PPV watchlist",
                result.to_csv(index=False).encode("utf-8"),
                "hvq_ppv_watchlist.csv",
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
