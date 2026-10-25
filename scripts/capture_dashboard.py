#!/usr/bin/env python3
"""Capture the Market Intelligence Terminal on NSE trading days.

This job only captures and stores an image artifact. It does not publish to X.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

IST = ZoneInfo("Asia/Kolkata")
DASHBOARD_URL = os.environ.get(
    "DASHBOARD_URL",
    "https://market-updates-5tqz2bgy4eyofrpfj8t6rt.streamlit.app/",
)
OUTPUT = Path("output/market-dashboard.png")


def is_nse_session(day) -> bool:
    """Use the XNSE trading calendar to skip weekends and exchange holidays."""
    calendar = xcals.get_calendar("XNSE")
    session = day.strftime("%Y-%m-%d")
    try:
        return bool(calendar.is_session(session))
    except Exception as exc:
        print(f"Unable to verify NSE calendar for {session}: {exc}", file=sys.stderr)
        raise


def main() -> int:
    now = datetime.now(IST)
    print(f"Local capture time: {now.isoformat()}")

    try:
        open_today = is_nse_session(now.date())
    except Exception:
        print("Skipping capture because the exchange calendar could not be verified.")
        return 1

    if not open_today:
        print("NSE is closed today; no screenshot will be captured.")
        return 0

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1440, "height": 1100},
            device_scale_factor=1,
            color_scheme="dark",
        )
        try:
            response = page.goto(
                DASHBOARD_URL,
                wait_until="domcontentloaded",
                timeout=120_000,
            )
            if response and response.status >= 400:
                raise RuntimeError(f"Dashboard returned HTTP {response.status}")
            # Streamlit may rerun while data sources initialize; wait for a real app surface.
            page.locator('[data-testid="stAppViewContainer"]').wait_for(timeout=90_000)
            page.wait_for_timeout(25_000)
            body_text = page.locator("body").inner_text(timeout=15_000)
            if "Script execution error" in body_text or "Traceback" in body_text:
                raise RuntimeError("Dashboard displays an execution error; refusing to save a broken screenshot.")
            if len(body_text.strip()) < 200:
                raise RuntimeError("Dashboard content appears incomplete; refusing to save a partial screenshot.")
            page.screenshot(path=str(OUTPUT), full_page=True, animations="disabled")
            print(f"Screenshot saved: {OUTPUT}")
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("Timed out waiting for the dashboard to load; no screenshot saved.") from exc
        finally:
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
