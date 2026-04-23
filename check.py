"""
Northway Eleven B4 floor plan availability watcher.

Hits the Spherexx API directly (no headless browser). The widget on
eaglerockproperties.com authenticates with hardcoded public credentials
and pulls unit data from /api/unit. We replicate that:

  1. POST /api/authenticate (Basic auth: fpaw:ndoklnes) -> JWT
  2. GET  /api/unit (Bearer JWT) -> list of all units for property 7086
  3. Filter for FloorplanName == "B4"
  4. Diff against last run:
       - new units  -> high-priority ntfy ("grab it")
       - gone units -> default-priority ntfy ("someone else got one")
"""

import base64
import json
import os
import sys
from pathlib import Path

import requests

API_BASE = "https://presentation.spherexx.app/api"
# Public credentials baked into the widget's JS. Same value the website uses.
BASIC_AUTH = base64.b64encode(b"fpaw:ndoklnes").decode("ascii")
FLOORPLAN = "B4"
PROPERTY_URL = (
    "https://www.eaglerockproperties.com/apartments/ny/ballston-lake/"
    "northway-eleven/floorplan-availability"
)
STATE_FILE = Path("state.json")

NTFY_TOPIC = os.environ.get("NTFY_TOPIC")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")


def log(msg: str) -> None:
    print(f"[watcher] {msg}", flush=True)


def get_token() -> str:
    r = requests.post(
        f"{API_BASE}/authenticate",
        headers={
            "Authorization": f"Basic {BASIC_AUTH}",
            "Origin": "https://presentation.spherexx.app",
            "Referer": "https://presentation.spherexx.app/",
            "Accept": "*/*",
        },
        timeout=15,
    )
    r.raise_for_status()
    try:
        data = r.json()
        if isinstance(data, dict):
            for key in ("token", "Token", "jwt", "access_token"):
                if key in data:
                    return data[key]
            raise RuntimeError(f"Unexpected auth response shape: {list(data)}")
        if isinstance(data, list) and data and isinstance(data[0], str):
            return data[0]
        if isinstance(data, str):
            return data
    except ValueError:
        return r.text.strip().strip('"')
    raise RuntimeError(f"Could not parse auth response: {r.text[:200]}")


def fetch_units(token: str) -> list[dict]:
    r = requests.get(
        f"{API_BASE}/unit",
        headers={
            "Authorization": f"Bearer {token}",
            "Origin": "https://presentation.spherexx.app",
            "Referer": "https://presentation.spherexx.app/",
            "Accept": "*/*",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def normalize(api_unit: dict) -> dict:
    avail = api_unit.get("AvailableDate", "")
    if "T" in avail:
        avail = avail.split("T", 1)[0]
    return {
        "apt": api_unit["Name"],
        "available": avail,
        "price": float(api_unit.get("Price", 0)),
        "floor": str(api_unit.get("Floor", "")),
        "sqft": api_unit.get("Sqft"),
        "bed": api_unit.get("Bed"),
        "bath": api_unit.get("Bath"),
        "floorplan": api_unit.get("FloorplanName", ""),
    }


def load_previous() -> list[dict]:
    if not STATE_FILE.exists():
        return []
    try:
        return json.loads(STATE_FILE.read_text()).get("units", [])
    except Exception:
        return []


def save_current(units: list[dict]) -> None:
    STATE_FILE.write_text(json.dumps({"units": units}, indent=2) + "\n")


def _post_ntfy(title: str, body: str, priority: str, tags: str) -> None:
    if not NTFY_TOPIC:
        log(f"NTFY_TOPIC not set; skipping notification ({title!r}).")
        return
    resp = requests.post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": tags,
            "Click": PROPERTY_URL,
        },
        timeout=15,
    )
    resp.raise_for_status()
    log(f"Sent ntfy notification: {title}")


def notify_new(new_units: list[dict]) -> None:
    lines = [
        f"{u['apt']} — Floor {u['floor']} — avail {u['available']} — "
        f"${u['price']:.0f}"
        for u in new_units
    ]
    _post_ntfy(
        title=f"Northway Eleven B4: {len(new_units)} new unit(s)",
        body="\n".join(lines),
        priority="high",
        tags="house,bell",
    )


def notify_gone(gone_units: list[dict]) -> None:
    """Someone probably signed a lease — a B4 we were tracking disappeared
    from the API response."""
    lines = [
        f"{u['apt']} — was Floor {u['floor']} — "
        f"was avail {u['available']} — was ${u['price']:.0f}"
        for u in gone_units
    ]
    _post_ntfy(
        title=f"Northway Eleven B4: {len(gone_units)} unit(s) no longer listed",
        body="\n".join(lines),
        priority="default",
        tags="warning",
    )


def main() -> int:
    log("Fetching auth token...")
    token = get_token()
    log(f"Got token ({len(token)} chars).")

    log("Fetching units...")
    raw = fetch_units(token)
    log(f"API returned {len(raw)} total unit(s).")

    b4_units = [normalize(u) for u in raw if u.get("FloorplanName") == FLOORPLAN]
    log(f"{len(b4_units)} are {FLOORPLAN}: {[u['apt'] for u in b4_units]}")

    previous = load_previous()
    previous_by_apt = {u["apt"]: u for u in previous}
    current_by_apt = {u["apt"]: u for u in b4_units}

    new_apts = set(current_by_apt) - set(previous_by_apt)
    gone_apts = set(previous_by_apt) - set(current_by_apt)

    new_units = [current_by_apt[a] for a in sorted(new_apts)]
    gone_units = [previous_by_apt[a] for a in sorted(gone_apts)]

    if new_units:
        log(f"NEW units detected: {[u['apt'] for u in new_units]}")
        notify_new(new_units)
    if gone_units:
        log(f"GONE units detected: {[u['apt'] for u in gone_units]}")
        notify_gone(gone_units)
    if not new_units and not gone_units:
        log("No changes since last check.")

    save_current(b4_units)
    return 0


if __name__ == "__main__":
    sys.exit(main())