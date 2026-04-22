"""
Northway Eleven B4 floor plan availability watcher.

Flow:
  1. Open the floorplan-availability page.
  2. Dismiss the appointment-scheduling modal that blocks initial render.
  3. Click the "B4" floor plan card to apply the B4 filter.
  4. Read the right-hand "Matching Homes" panel, which lists each available
     unit with its name (e.g. "Nway-3BSCOTT"), avail date, price, and floor.
  5. Diff against last run, ntfy on new units.
"""

import json
import os
import re
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

URL = "https://www.eaglerockproperties.com/apartments/ny/ballston-lake/northway-eleven/floorplan-availability"
FLOORPLAN = "B4"
STATE_FILE = Path("state.json")
DEBUG_HTML = Path("last_page.html")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")


def log(msg: str) -> None:
    print(f"[watcher] {msg}", flush=True)


def dismiss_modal(page) -> None:
    """The site shows a 'Schedule an Appointment' modal that blocks the rest
    of the page from finishing render. Try a few likely close-button selectors
    and ignore failures (the modal may not appear on every load)."""
    candidates = [
        "button:has-text('Close')",
        "button[aria-label='Close']",
        "[aria-label='Close' i]",
        "button.close",
        ".modal button:has-text('×')",
        "button:has-text('No thanks')",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=2_000)
                log(f"Dismissed modal via selector: {sel}")
                page.wait_for_timeout(500)
                return
        except Exception:
            continue
    log("No modal close button matched — proceeding (modal may not be present).")


def click_b4_card(page) -> bool:
    """Click the B4 floor plan card (the one with the dark header reading
    'B4'). Returns True on success."""
    # The floorplan cards have a dark banner with the plan name as text.
    # Try a couple of click targets in order of specificity.
    selectors = [
        # A dark header div whose text is exactly "B4 Available - <date>"
        "xpath=//*[starts-with(normalize-space(.), 'B4') and "
        "(contains(., 'Available') or contains(., 'No Availability'))]",
        # Generic: any clickable element containing exactly "B4"
        "text=/^\\s*B4\\s*$/",
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count():
                loc.scroll_into_view_if_needed(timeout=3_000)
                loc.click(timeout=3_000)
                log(f"Clicked B4 card via selector: {sel}")
                page.wait_for_timeout(1_500)
                return True
        except Exception as e:
            log(f"  selector {sel!r} failed: {e}")
            continue
    log("Could not click B4 card.")
    return False


def extract_units_from_panel(page) -> list[dict]:
    """Scrape the 'Matching Homes' panel on the right side of the page.

    Each home appears as a card with text like:
        Nway-3BSCOTT
        2 Bed / 1.5 Bath / 1389 SqFt
        Avail. 7/07/26
        *Total Monthly Leasing Price
        $1,975* | 12-Mo.
        Floor 1
        B4

    We anchor on the unit name (the 'Nway-' prefix is stable across the
    Eagle Rock platform)."""

    # Pull the full visible text of the page after filtering, then regex out
    # each card. This is more robust than trying to identify the panel
    # container precisely.
    body_text = page.locator("body").inner_text()

    # Each card: name on first line, details over next ~5 lines, ending in B4.
    # We look for "Nway-..." followed within 300 chars by "B4".
    pattern = re.compile(
        r"(?P<name>Nway-[A-Z0-9]+)"
        r"[\s\S]{0,400}?"
        r"Avail\.?\s*(?P<date>\d{1,2}/\d{1,2}/\d{2,4})"
        r"[\s\S]{0,200}?"
        r"\$(?P<price>[\d,]+)"
        r"[\s\S]{0,100}?"
        r"(?P<term>\d+-Mo\.?)"
        r"[\s\S]{0,100}?"
        r"Floor\s*(?P<floor>\d+)"
        r"\s*B4\b",
        re.IGNORECASE,
    )

    units = []
    seen = set()
    for m in pattern.finditer(body_text):
        name = m.group("n")
        if name in seen:
            continue
        seen.add(name)
        units.append({
            "apt": name,
            "available": m.group("date"),
            "price": f"${m.group('price')}",
            "term": m.group("term"),
            "floor": m.group("floor"),
        })

    log(f"Parsed {len(units)} B4 unit(s) from matching homes panel.")
    return units


def scrape(page) -> list[dict]:
    page.goto(URL, timeout=45_000)
    page.wait_for_load_state("domcontentloaded", timeout=30_000)
    log(f"Page loaded. Title: {page.title()!r}")

    # Give the popup a moment to appear, then dismiss.
    page.wait_for_timeout(2_000)
    dismiss_modal(page)

    # Wait for the floorplan widget to render at least one card.
    try:
        page.wait_for_selector("text=Available", timeout=15_000)
    except PWTimeout:
        log("Timed out waiting for any 'Available' text on the page.")

    # Always dump the post-modal HTML for debugging.
    DEBUG_HTML.write_text(page.content())
    log(f"Wrote rendered HTML ({DEBUG_HTML.stat().st_size} bytes) to {DEBUG_HTML}")

    b4_count = page.locator("text=B4").count()
    log(f"Elements containing 'B4' on page: {b4_count}")

    if b4_count == 0:
        log("No B4 anywhere — either no B4 listed or page didn't load. Bailing.")
        return []

    if not click_b4_card(page):
        return []

    # After click, the right panel updates. Give it a moment to populate.
    page.wait_for_timeout(2_000)
    DEBUG_HTML.write_text(page.content())  # overwrite with post-filter HTML

    return extract_units_from_panel(page)


def load_previous() -> list[dict]:
    if not STATE_FILE.exists():
        return []
    try:
        return json.loads(STATE_FILE.read_text()).get("units", [])
    except Exception:
        return []


def save_current(units: list[dict]) -> None:
    STATE_FILE.write_text(json.dumps({"units": units}, indent=2) + "\n")


def notify(new_units: list[dict]) -> None:
    if not NTFY_TOPIC:
        log("NTFY_TOPIC not set; skipping notification.")
        return
    lines = [
        f"{u['apt']} — Floor {u.get('floor', '?')} — "
        f"avail {u['available']} — {u['price']} ({u.get('term', '')})".strip()
        for u in new_units
    ]
    body = "\n".join(lines) if lines else "New B4 availability"
    resp = requests.post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Title": f"Northway Eleven B4: {len(new_units)} new unit(s)",
            "Priority": "high",
            "Tags": "house,bell",
            "Click": URL,
        },
        timeout=15,
    )
    resp.raise_for_status()
    log("Sent ntfy notification (topic redacted)")


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 1000},
        )
        page = context.new_page()
        log(f"Navigating to {URL}")
        units = scrape(page)
        browser.close()

    log(f"Final unit list: {units}")

    previous = load_previous()
    previous_apts = {u["apt"] for u in previous}
    new_units = [u for u in units if u["apt"] not in previous_apts]

    if new_units:
        log(f"NEW units detected: {new_units}")
        notify(new_units)
    else:
        log("No new units since last check.")

    save_current(units)
    return 0


if __name__ == "__main__":
    sys.exit(main())