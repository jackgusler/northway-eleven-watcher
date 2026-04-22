"""
Northway Eleven B4 floor plan availability watcher.

Loads the floorplan-availability page in a headless browser, finds the B4
section, extracts every listed unit, and compares against the last run's state.
If any new unit appears, pushes a notification to ntfy.sh.

Runs in GitHub Actions on a cron. State persists by being committed back to
the repo between runs.
"""

import json
import os
import re
import sys
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

URL = "https://www.eaglerockproperties.com/apartments/ny/ballston-lake/northway-eleven/floorplan-availability"
FLOORPLAN = "B4"
STATE_FILE = Path("state.json")
DEBUG_HTML = Path("last_page.html")  # dumped on error for debugging

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")


def extract_b4_units(page) -> list[dict]:
    """
    Return a list of dicts, one per B4 unit currently listed. Each dict has
    at minimum an 'apt' key (the unique unit identifier) plus whatever other
    fields we can pull (available date, price, beds/baths, sqft).

    The Eagle Rock site is built on a G5/Rent Manager widget that renders a
    card or table per floor plan with expandable unit rows. We locate the B4
    section by its visible label, then scrape everything beneath it.
    """
    # Give the JS widget time to populate. The site lazy-loads units.
    page.wait_for_load_state("networkidle", timeout=30_000)

    # Try a few strategies for locating the B4 block, most specific first.
    # Strategy 1: look for a heading/button containing exactly "B4".
    b4_locator = page.locator(
        "xpath=//*[self::h2 or self::h3 or self::h4 or self::button or "
        "self::div or self::span][normalize-space(text())='B4']"
    ).first

    if b4_locator.count() == 0:
        # Strategy 2: any element whose text starts with "B4 " (e.g. "B4 - 2 Bed")
        b4_locator = page.locator("text=/^B4\\b/").first

    if b4_locator.count() == 0:
        print("Could not find B4 section on page.", file=sys.stderr)
        DEBUG_HTML.write_text(page.content())
        return []

    # Some widgets collapse units until you click the floor plan. Try clicking
    # the B4 header in case units are hidden. Ignore failures (it may not be
    # clickable / may already be expanded).
    try:
        b4_locator.click(timeout=2_000)
        page.wait_for_timeout(1_000)
    except Exception:
        pass

    # Grab the containing section: walk up a few ancestors and take the one
    # that includes unit-looking content (apartment number + date).
    container = b4_locator.locator(
        "xpath=ancestor::*[.//text()[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
        "'abcdefghijklmnopqrstuvwxyz'), 'available')]][1]"
    ).first

    if container.count() == 0:
        # Fall back to the nearest sizable ancestor
        container = b4_locator.locator("xpath=ancestor::*[5]").first

    text = container.inner_text() if container.count() else ""

    units = []
    # Units on Rent Manager widgets typically look like:
    #   "Apt 123-4   Available 07/15/2026   $2,150"
    # or a table row with those fields. Match loosely: an apt-number-ish token
    # followed anywhere by a date.
    pattern = re.compile(
        r"(?P<apt>(?:Apt\.?\s*|Unit\s*|#)?[A-Z0-9][A-Z0-9\-]{1,10})"
        r"[^\n]*?"
        r"(?P<date>\d{1,2}/\d{1,2}/\d{2,4})"
        r"[^\n]*?"
        r"(?P<price>\$[\d,]+)?",
        re.IGNORECASE,
    )

    seen = set()
    for m in pattern.finditer(text):
        apt = m.group("apt").strip()
        # Filter out garbage matches (e.g. the string "B4" itself, or dates-only)
        if apt.upper() in {"B4", "APT", "UNIT"} or len(apt) < 2:
            continue
        if apt in seen:
            continue
        seen.add(apt)
        units.append(
            {
                "apt": apt,
                "available": m.group("date"),
                "price": m.group("price") or "",
            }
        )

    # Always dump the raw text when we find nothing, so we can iterate on
    # selectors without guessing.
    if not units:
        DEBUG_HTML.write_text(page.content())
        print(f"B4 section found but no units parsed. Raw text:\n{text[:2000]}",
              file=sys.stderr)

    return units


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
        print("NTFY_TOPIC not set; skipping notification.", file=sys.stderr)
        return

    lines = [f"{u['apt']} — available {u['available']} {u['price']}".strip()
             for u in new_units]
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
    print(f"Sent ntfy notification to {NTFY_TOPIC}")


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
        page.goto(URL, timeout=45_000)
        units = extract_b4_units(page)
        browser.close()

    print(f"Found {len(units)} B4 unit(s): {units}")

    previous = load_previous()
    previous_apts = {u["apt"] for u in previous}
    new_units = [u for u in units if u["apt"] not in previous_apts]

    if new_units:
        print(f"NEW: {new_units}")
        notify(new_units)
    else:
        print("No new units since last check.")

    save_current(units)
    return 0


if __name__ == "__main__":
    sys.exit(main())