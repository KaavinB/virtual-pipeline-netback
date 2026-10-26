"""Capture README screenshots of the public dashboard, one per tab (make screenshots).

    python -m dashboards.metabase.screenshots <public-dashboard-url>

Drives the locally installed Chrome through Playwright, clicks each tab like a user would and
waits for every query to finish before capturing the tab.
"""

import sys

from playwright.sync_api import sync_playwright

from ingestion.config import ROOT

OUT = ROOT / "docs" / "img"
# Metabase scrolls inside its own container, so each shot gets a viewport tall enough for its tab.
SHOTS = [  # (file, tab, query string, viewport height)
    ("dashboard_start", "Start here", "site=EF-02", 1220),
    ("dashboard_overview", "Overview", "site=EF-02", 1780),
    ("dashboard_whatif", "Overview", "site=EF-02&henry_hub=6&discount_pct=70", 1040),
    ("dashboard_economics", "Economics", "site=EF-02", 1640),
    ("dashboard_sensitivity", "Sensitivity", "site=PB-07", 1420),
    ("dashboard_data", "Data & assumptions", "site=EF-02", 1060),
]


def main(url: str) -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        page = browser.new_page(device_scale_factor=1)
        for name, tab, query, height in SHOTS:
            page.set_viewport_size({"width": 1440, "height": height})
            page.goto(f"{url}?{query}", wait_until="networkidle")
            page.get_by_role("tab", name=tab).click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(2500)  # let charts finish animating
            page.screenshot(path=OUT / f"{name}.png")
            print(f"saved docs/img/{name}.png")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
