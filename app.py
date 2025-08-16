import os
from datetime import date, timedelta
from typing import Dict, List, Tuple

import requests
from icalendar import Calendar
from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse


# ---------- ICS helpers (moved here to avoid imports/indent issues) ----------

def fetch_ics(url: str) -> str:
    """Download raw ICS text."""
    if not url:
        return ""
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def parse_bookings(ics_map: Dict[str, str]) -> Dict[str, List[Tuple[date, date]]]:
    """Parse bookings per flat -> list of (start_date, end_date)."""
    results: Dict[str, List[Tuple[date, date]]] = {}

    for flat_name, ics_text in ics_map.items():
        events: List[Tuple[date, date]] = []

        if not ics_text:
            results[flat_name] = events
            continue

        cal = Calendar.from_ical(ics_text)

        for comp in cal.walk():
            if comp.name != "VEVENT":
                continue

            dtstart_prop = comp.get("dtstart")
            dtend_prop = comp.get("dtend")
            if not dtstart_prop or not dtend_prop:
                continue

            dtstart = dtstart_prop.dt
            dtend = dtend_prop.dt

            # Normalize to date objects
            if hasattr(dtstart, "date"):
                dtstart = dtstart.date()
            if hasattr(dtend, "date"):
                dtend = dtend.date()

            # Skip bad ranges
            if not isinstance(dtstart, date) or not isinstance(dtend, date):
                continue
            if dtend <= dtstart:
                continue

            events.append((dtstart, dtend))

        events.sort(key=lambda x: (x[0], x[1]))
        results[flat_name] = events

    return results


def build_schedule_for_days(
    bookings: Dict[str, List[Tuple[date, date]]],
    start: date,
    days: int = 14,
) -> Dict[date, Dict[str, Dict[str, bool]]]:
    """Build 'what happens each day' for the given window."""
    schedule: Dict[date, Dict[str, Dict[str, bool]]] = {}

    for i in range(days):
        d = start + timedelta(days=i)
        day_map: Dict[str, Dict[str, bool]] = {}

        for flat, events in bookings.items():
            check_in = any(s == d for s, e in events)
            check_out = any(e == d for s, e in events)
            if check_in or check_out:
                day_map[flat] = {"check_in": check_in, "check_out": check_out}

        if day_map:
            schedule[d] = day_map

    return schedule


def format_schedule(schedule: Dict[date, Dict[str, Dict[str, bool]]]) -> str:
    """Turn schedule into simple cleaner-friendly text."""
    if not schedule:
        return "No check-ins or check-outs in the selected window."

    lines: List[str] = []

    for d in sorted(schedule.keys()):
        items: List[str] = []

        for flat_name, flags in sorted(schedule[d].items()):
            ci = flags.get("check_in", False)
            co = flags.get("check_out", False)

            if ci and co:
                items.append(f"{flat_name}: out/clean/in")
            elif co:
                items.append(f"{flat_name}: out/clean")
            elif ci:
                items.append(f"{flat_name}: check-in")

        if items:
            lines.append(f"{d.strftime('%a %d %b')} — " + "; ".join(items))

    return "\n".join(lines)


# ------------------------------- FastAPI app ---------------------------------

app = FastAPI()


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


@app.get("/", response_class=PlainTextResponse)
def home(days: int = Query(int(os.getenv("DEFAULT_DAYS", "14")), ge=1, le=60)):
    # Read calendar URLs from environment variables
    flats = {
        os.getenv("FLAT7_NAME", "Flat 7"): os.getenv("FLAT7_ICS_URL", ""),
        os.getenv("FLAT8_NAME", "Flat 8"): os.getenv("FLAT8_ICS_URL", ""),
        os.getenv("FLAT9_NAME", "Flat 9"): os.getenv("FLAT9_ICS_URL", ""),
    }

    # Fetch and parse calendars
    ics_map = {name: fetch_ics(url) for name, url in flats.items() if url}
    bookings = parse_bookings(ics_map)

    # Build next N days and format for cleaners
    schedule = build_schedule_for_days(bookings, start=date.today(), days=days)
    text = format_schedule(schedule)

    return text or "No check-ins or check-outs in the selected window."
