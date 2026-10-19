"""Browser smoke test: click through every view and fail on any JS error.

Usage:
    python scripts/ui_smoke.py http://127.0.0.1:8000/
Requires: pip install playwright && playwright install chromium
(or an installed Chrome: set UI_SMOKE_CHANNEL=chrome).
"""
from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/"
DECORAH = {"latitude": 43.30, "longitude": -91.79}


def main() -> int:
    errors: list[str] = []
    with sync_playwright() as p:
        channel = os.getenv("UI_SMOKE_CHANNEL") or None
        browser = p.chromium.launch(channel=channel, headless=True)
        for scheme in ("light", "dark"):
            ctx = browser.new_context(viewport={"width": 1280, "height": 860}, color_scheme=scheme,
                                      geolocation=DECORAH, permissions=["geolocation"])
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(f"[{scheme}] pageerror: {e}"))
            page.on("console", lambda m: errors.append(f"[{scheme}] console.{m.type}: {m.text}")
                    if m.type == "error" else None)

            page.goto(BASE, wait_until="networkidle")
            page.wait_for_selector("#windows-list li")
            # Near me → a ranked list appears.
            page.click("#near-me-btn")
            page.wait_for_selector("#near-list li", timeout=20000)
            # Open the first nearby stream.
            page.click("#near-list li button")
            page.wait_for_selector(".hero", timeout=20000)
            page.click("details.why > summary")
            # Journal: log a trip from the stream page, then see it in the journal.
            page.click("#log-card .log-btn >> nth=2")
            page.fill("#log-fly", "Pink Squirrel #14")
            page.click("#log-save")
            page.wait_for_selector("#open-journal", timeout=10000)
            page.click("#open-journal")
            page.wait_for_selector("#reports-body .jentry", timeout=10000)
            # Calendar + species popup.
            page.click('.tab[data-view="calendar"]')
            page.wait_for_selector(".cal-row")
            page.click(".cal-row >> nth=0")
            page.wait_for_timeout(400)
            page.keyboard.press("Escape")
            # Reports, Learn topic, info pages.
            page.click('.tab[data-view="reports"]')
            page.wait_for_timeout(600)
            page.click('.tab[data-view="learn"]')
            page.wait_for_selector(".learn-card")
            page.click(".learn-card >> nth=0")
            page.wait_for_timeout(600)
            for pid in ("about", "data", "privacy"):
                page.goto(f"{BASE}#/page/{pid}")
                page.wait_for_selector("#page-body .page")
            ctx.close()
        browser.close()
    if errors:
        print("\n".join(errors))
        return 1
    print("ui smoke: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
