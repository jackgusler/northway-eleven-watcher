"""
Northway Eleven B4 floor plan availability watcher.

Loads the floorplan-availability page in a headless browser, finds the B4
section, extracts every listed unit, and compares against the last run's state.
If any new unit appears, pushes a notification to ntfy.sh.
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
DEBUG_HTML = Path("last_page.html")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")


def log(msg: str) -> None:
    """Print to stdout with a marker so logs are easy to scan in CI."""
    print(f"[watcher] {msg}", flush=True)


def extract_b4_units(page) -> list[dict]:
    page.wait_for_load_state("networkidle", timeout=30_000)
    log(f"Page loaded. Title: {page.title()!r}")

    # Always dump the rendered page so we can iterate on selectors regardless
    # of whether this run finds anything.
    DEBUG_HTML.write_text(page.content())
    log(f"Wrote rendered HTML ({DEBUG_HTML.stat().st_size} bytes) to {DEBUG_HTML}")

    # Quick sanity check: does "B4" appear anywhere in the rendered DOM?
    b4_text_count = page.locator("text=B4").count()
    log(f"Elements containing the text 'B4': {b4_text_count}")

    # Show the first few matches so we can see what surrounds them.
    for i in range(min(b4_text_count, 5)):
        try:
            el = page.locator("text=B4").nth(i)
            tag = el.evaluate("e => e.tagName")
            txt = (el.inner_text() or "").strip().replace("\n", " | ")[:200]
            log(f"  match #{i}: <{tag}> {txt!r}")
        except Exception as e:
            log(f"  match #{i}: error inspecting -> {e}")

    # Strategy 1: exact "B4" label
    b4_locator = page.locator(
        "xpath=//*[self::h2 or self::h3 or self::h4 or self::button or "
        "self::div or self::span or self::a][normalize-space(text())='B4']"
    ).first

    if b4_locator.count() == 0:
        log("Strategy 1 (exact 'B4' text) found nothing. Trying strategy 2.")
        b4_locator = page.locator("text=/^B4\\b/").first

    if b4_locator.count() == 0:
        log("Strategy 2 (regex starts-with 'B4') found nothing either.")
        log("Could not find B4 section on page. Bailing out.")
        return []

    log(f"Found B4 anchor element. Tag={b4_locator.evaluate('e => e.tagName')}")

    # Try clicking in case units are hidden behind a collapse/accordion.
    try:
        b4_locator.click(timeout=2_000)
        page.wait_for_timeout(1_000)
        log("Clicked B4 anchor (in case it was a collapsed accordion).")
    except Exception as e:
        log(f"B4 anchor not clickable (probably already expanded): {e}")

    # Re-dump after clicking, since the DOM may have expanded.
    DEBUG_HTML.write_text(page.content())

    # Walk up to a container that mentions "available" so we capture the unit list.
    container = b4_locator.locator(
        "xpath=ancestor::*[.//text()[contains(translate(., "
        "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
        "'available')]][1]"
    ).first

    if container.count() == 0:
        log("No ancestor of B4 contained 'available'. Falling back to 5-up ancestor.")
        container = b4_locator.locator("xpath=ancestor::*[5]").first

    text = container.inner_text() if container.count() else ""
    log(f"Container text length: {len(text)}")
    log(f"Container text preview:\n---\n{text[:1500]}\n---")

    # Match unit rows. Look for an apt-id-ish token on the same line as a date.
    pattern = re.compile(
        r"(?P<apt>(?:Apt\.?\s*|Unit\s*|#)?[A-Z0-9][A-Z0-9\-]{1,10})"
        r"[^\n]*?"
        r"(?P<date>\d{1,2}/\d{1,2}/\d{2,4})"
        r"[^\n]*?"
        r"(?P<price>\$[\d,]+)?",
        re.IGNORECASE,
    )

    units = []
    seen = set()
    for m in pattern.finditer(text):
        apt = m.group("apt").strip()
        if apt.upper() in {"B4", "APT", "UNIT"} or len(apt) < 2:
            continue
        if apt in seen:
            continue
        seen.add(apt)
        units.append({
            "apt": apt,
            "available": m.group("date"),
            "price": m.group("price") or "",
        })

    log(f"Parsed {len(units)} unit(s) from container text.")
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
        log("NTFY_TOPIC not set; skipping notification.")
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
    log(f"Sent ntfy notification (topic redacted)")


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
        page.goto(URL, timeout=45_000)
        units = extract_b4_units(page)
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