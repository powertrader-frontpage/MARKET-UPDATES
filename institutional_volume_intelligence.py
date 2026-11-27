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
    # Compare each of the previous 10 sessions with its immediately prior close.
    prior_10_indices = data.index[-11:-1]
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
                    # Calculate daily change from the latest two completed cash-equity sessions.
                    completed = _latest_completed_session(candles)
                    daily_change = None
                    if len(completed) >= 2 and float(completed["Close"].iloc[-2]) != 0:
                        daily_change = (
                            float(completed["Close"].iloc[-1]) / float(completed["Close"].iloc[-2]) - 1
                        ) * 100
                    rows.append({
                        "Stock": symbol,
                        "Signal": signals,
                        "Daily Change %": daily_change,
                        "Sector": "; ".join(sectors) or "Not mapped",
                        "Microsector": "; ".join(micro_names) or "Not mapped",
                    })
            except Exception as exc:
                failures.append({"Stock": symbol, "Reason": str(exc) or f"Fetch error ({exc.__class__.__name__})"})

            progress.progress((idx + 1) / total, text=f"Checked {idx + 1}/{total} NFO stocks · {len(rows)} matches")

        progress.empty()
        result = pd.DataFrame(
            sorted(rows, key=lambda item: item["Stock"]),
            columns=["Stock", "Signal", "Daily Change %", "Sector", "Microsector"],
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
            sector_zones = st.session_state.get("master_sector_rotation_zones", {})
            sector_weekly_strength = st.session_state.get("master_sector_rotation_weekly_strength", {})
            micro_zones = st.session_state.get("master_microsector_rotation_zones", {})
            # Reuse the exact broad-group mapping already used by the Main Dashboard.
            # Leave groups without a direct sector-index equivalent explicitly unmapped.
            group_to_main_sector = {
                "Pharma": "Pharma", "Technology": "IT", "Industrials": "Infrastructure",
                "Media & Services": "Media", "Financials": "Financial Services",
                "Consumer": "Consumption", "Consumer Durables": "Consumption",
                "Power": "Energy", "Auto": "Auto", "Construction": "Infrastructure",
                "Real Estate": "Realty",
            }
            display_result = result.copy()
            # Stack multiple memberships on separate lines to keep the mobile table readable.
            display_result["Sector"] = display_result["Sector"].map(
                lambda value: "\\n".join(dict.fromkeys(x.strip() for x in str(value).split(";") if x.strip()))
            )
            display_result["Microsector"] = display_result["Microsector"].map(
                lambda value: "\\n".join(dict.fromkeys(x.strip() for x in str(value).split(";") if x.strip()))
            )
            display_result["Sector Zone"] = result["Sector"].map(
                lambda value: "\\n".join(
                    (
                        f"{name.strip()} → {group_to_main_sector[name.strip()]}: "
                        f"{sector_zones.get(group_to_main_sector[name.strip()], 'Unavailable')}"
                        + (
                            f" ({sector_weekly_strength[group_to_main_sector[name.strip()]]:+.2f} pp · 1W vs NIFTY)"
                            if group_to_main_sector.get(name.strip()) in sector_weekly_strength
                            and pd.notna(sector_weekly_strength[group_to_main_sector[name.strip()]])
                            else ""
                        )
                    ) if name.strip() in group_to_main_sector else
                    f"{name.strip()}: No direct sector-index mapping"
                    for name in dict.fromkeys(str(value).split(";")) if name.strip()
                )
            )
            display_result["Microsector Zone"] = result["Microsector"].map(
                lambda value: "\\n".join(
                    f"{name.strip()}: {micro_zones.get(name.strip(), 'Unavailable')}"
                    for name in dict.fromkeys(str(value).split(";")) if name.strip()
                )
            )
            zone_css = {
                "Leading": "color: #22C55E; background-color: rgba(34,197,94,.16); font-weight: 700;",
                "Improving": "color: #60A5FA; background-color: rgba(59,130,246,.16); font-weight: 700;",
                "Weakening": "color: #F59E0B; background-color: rgba(245,158,11,.16); font-weight: 700;",
                "Lagging": "color: #F87171; background-color: rgba(239,68,68,.16); font-weight: 700;",
                "Unavailable": "color: #94A3B8; background-color: rgba(148,163,184,.10);",
            }

            def _zone_style(value):
                zones = [line.rsplit(": ", 1)[-1].strip() for line in str(value).split("\\n")]
                # One cell can contain multiple memberships; use the strongest
                # available zone for the cell tint while preserving every label.
                priority = {"Leading": 4, "Improving": 3, "Weakening": 2, "Lagging": 1, "Unavailable": 0}
                # Zone labels now include weekly strength text; detect the zone token itself.
                selected = max(
                    (zone for zone in priority if zone in str(value)),
                    key=lambda zone: priority.get(zone, 0),
                    default="Unavailable",
                )
                return zone_css.get(selected, zone_css["Unavailable"])

            styled = (
                display_result.style
                .set_properties(**{
                    "text-align": "left",
                    "vertical-align": "middle",
                    "white-space": "pre-line",
                    "padding": "7px 9px",
                })
                .map(
                    lambda value: "text-align: center; font-weight: 700; color: #22C55E;" if "HVQ" in str(value)
                    else "text-align: center; font-weight: 700; color: #60A5FA;",
                    subset=["Signal"],
                )
                .map(
                    lambda value: (
                        "color: #F87171; font-weight: 800;" if pd.notna(value) and float(value) < 0
                        else "color: #22C55E; font-weight: 800;" if pd.notna(value) and float(value) > 0
                        else "color: #94A3B8; font-weight: 700;"
                    ),
                    subset=["Daily Change %"],
                )
                .format({"Daily Change %": lambda value: "—" if pd.isna(value) else f"{float(value):+.2f}%"})
                .map(lambda _: "color: #7DD3FC; font-weight: 600;", subset=["Sector"])
                .map(lambda _: "color: #C4B5FD; font-weight: 600;", subset=["Microsector"])
                .map(_zone_style, subset=["Sector Zone", "Microsector Zone"])
            )
            if not sector_zones or not micro_zones:
                st.caption("Rotation zones are unavailable until the Main Dashboard has completed its latest rotation calculations.")
            st.dataframe(
                styled,
                column_config={
                    "Stock": st.column_config.TextColumn("Stock", width="small"),
                    "Signal": st.column_config.TextColumn("Signal", width="small"),
                    "Daily Change %": st.column_config.TextColumn("Daily %", width="small"),
                    "Sector": st.column_config.TextColumn("Sector", width="medium"),
                    "Microsector": st.column_config.TextColumn("Microsector", width="large"),
                    "Sector Zone": st.column_config.TextColumn("Sector Zone", width="medium"),
                    "Microsector Zone": st.column_config.TextColumn("Microsector Zone", width="large"),
                },
                use_container_width=True,
                hide_index=True,
                height=min(620, 42 + 42 * len(display_result)),
            )
            st.download_button(
                "Download HVQ + PPV watchlist",
                display_result.to_csv(index=False).encode("utf-8"),
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
