import os
from datetime import date, timedelta
from typing import Dict, List, Tuple

import requests
from icalendar import Calendar
from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse, HTMLResponse


# ==============================
#  Config (customisable)
# ==============================

# Flat display names (from your env, with safe defaults)
FLAT7_NAME = os.getenv("FLAT7_NAME", "Flat 7")
FLAT8_NAME = os.getenv("FLAT8_NAME", "Flat 8")
FLAT9_NAME = os.getenv("FLAT9_NAME", "Flat 9")

# Booking.com ICS URLs
FLAT7_ICS = os.getenv("FLAT7_ICS_URL", "")
FLAT8_ICS = os.getenv("FLAT8_ICS_URL", "")
FLAT9_ICS = os.getenv("FLAT9_ICS_URL", "")

# Nicknames + per-flat colours (change here or via env if you like)
NICKNAMES = {
    FLAT7_NAME: os.getenv("FLAT7_NICK", "Orange"),
    FLAT8_NAME: os.getenv("FLAT8_NICK", "Blue"),
    FLAT9_NAME: os.getenv("FLAT9_NICK", "Green"),
}
FLAT_COLOURS = {
    FLAT7_NAME: os.getenv("FLAT7_COLOUR", "#ff8a00"),  # orange
    FLAT8_NAME: os.getenv("FLAT8_COLOUR", "#3a7afe"),  # blue
    FLAT9_NAME: os.getenv("FLAT9_COLOUR", "#31c36b"),  # green
}

# Cleaning window (shows on every relevant row)
CLEAN_WINDOW = os.getenv("CLEAN_WINDOW", "10:00–16:00")

DEFAULT_DAYS = int(os.getenv("DEFAULT_DAYS", "14"))


# ==============================
#  ICS helpers
# ==============================

def fetch_ics(url: str) -> str:
    """Download raw ICS text."""
    if not url:
        return ""
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def parse_bookings(ics_map: Dict[str, str]) -> Dict[str, List[Tuple[date, date]]]:
    """
    Parse bookings per flat -> list of (start_date, end_date).
    We treat VEVENT dtstart (arrival) == check-in day,
    and dtend (departure) == check-out day.
    """
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

            # Normalise to dates
            if hasattr(dtstart, "date"):
                dtstart = dtstart.date()
            if hasattr(dtend, "date"):
                dtend = dtend.date()

            # Sanity checks
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
    days: int = DEFAULT_DAYS,
) -> Dict[date, Dict[str, Dict[str, bool]]]:
    """What happens on each day in the window."""
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


def format_schedule_text(schedule: Dict[date, Dict[str, Dict[str, bool]]]) -> str:
    """Plain text schedule (kept for quick checks at '/')."""
    if not schedule:
        return "No check-ins or check-outs in the selected window."

    lines: List[str] = []
    for d in sorted(schedule.keys()):
        parts: List[str] = []
        for flat_name, flags in sorted(schedule[d].items()):
            ci = flags.get("check_in", False)
            co = flags.get("check_out", False)
            if ci and co:
                parts.append(f"{flat_name}: out/clean/in")
            elif co:
                parts.append(f"{flat_name}: out/clean")
            elif ci:
                parts.append(f"{flat_name}: check-in")
        if parts:
            lines.append(f"{d.strftime('%a %d %b')} — " + "; ".join(parts))
    return "\n".join(lines)


# ==============================
#  HTML rendering
# ==============================

def html_cleaner_view(schedule: Dict[date, Dict[str, Dict[str, bool]]]) -> str:
    """Pretty, colour-coded HTML for cleaners with TODAY on top."""
    today = date.today()

    if not schedule:
        return """<!doctype html><meta charset="utf-8">
        <style>body{font-family:system-ui,Arial,sans-serif;padding:24px}</style>
        <h1>Cleaner Schedule</h1><p>No check-ins or check-outs.</p>"""

    # Split today vs future for grouping
    ordered_days = [today] + [d for d in sorted(schedule.keys()) if d != today and d in schedule]  # today first
    for d in sorted(schedule.keys()):
        if d not in ordered_days:
            ordered_days.append(d)

    def flat_badge(flat: str) -> str:
        nick = NICKNAMES.get(flat, flat)
        colour = FLAT_COLOURS.get(flat, "#999")
        return f"<span class='flat-badge' style='background:{colour}22;border-color:{colour};color:{colour}'>{nick}</span>"

    # Build rows for a day
    def day_rows(d: date) -> str:
        rows: List[str] = []
        for flat, flags in sorted(schedule.get(d, {}).items()):
            ci = flags.get("check_in", False)
            co = flags.get("check_out", False)

            # Decide status text + colours
            if ci and co:
                status = "Check-out → Clean → Check-in"
                cls = "turnover"
                icon = "🔁"
            elif co:
                status = "Check-out → Clean"
                cls = "checkout"
                icon = "🔚"
            elif ci:
                status = "Check-in"
                cls = "checkin"
                icon = "🔑"
            else:
                status = "—"
                cls = ""
                icon = ""

            # Cleaning window where relevant
            window = f"<div class='window'>🧹 Clean between <b>{CLEAN_WINDOW}</b></div>" if (co or (ci and co)) else ""

            rows.append(
                f"""
                <tr class="{cls}">
                  <td class="flat">{flat_badge(flat)}<div class="name">{flat}</div></td>
                  <td class="status"><span class="icon">{icon}</span> {status} {window}</td>
                </tr>
                """
            )
        return "\n".join(rows)

    # HTML skeleton
    parts: List[str] = [
        """<!doctype html><meta charset="utf-8">
        <style>
          :root {
            --fg:#111; --muted:#666; --line:#e7e7e7; --bg:#fff;
            --green:#1f9d55; --red:#d93025; --amber:#b76e00;
          }
          *{box-sizing:border-box}
          body{font-family:system-ui,Arial,sans-serif;background:var(--bg);color:var(--fg);margin:0;padding:24px;line-height:1.55}
          h1{margin:0 0 8px;font-size:24px}
          .sub{color:var(--muted);margin-bottom:18px}
          .day{margin:22px 0 10px;font-weight:800;font-size:18px;display:flex;align-items:center;gap:10px}
          .today-badge{display:inline-block;background:#111;color:#fff;padding:2px 10px;border-radius:999px;font-size:12px;letter-spacing:.4px}
          table{width:100%;border-collapse:collapse;margin-bottom:6px}
          td{border-top:1px solid var(--line);padding:12px 10px;vertical-align:top}
          td.flat{width:230px}
          .name{font-size:14px;color:var(--muted);margin-top:2px}
          .flat-badge{display:inline-block;padding:2px 8px;border-radius:999px;border:1px solid #ddd;font-size:12px;margin-right:6px}
          .status .icon{margin-right:6px}
          .window{margin-top:4px;color:var(--muted)}
          tr.checkin td.status{color:var(--green);font-weight:600}
          tr.checkout td.status{color:var(--red);font-weight:600}
          tr.turnover td.status{color:var(--amber);font-weight:800}
          .legend{margin-top:10px;color:var(--muted);font-size:14px}
          .badge{display:inline-block;padding:2px 8px;border-radius:999px;border:1px solid var(--line);margin-right:8px}
        </style>
        <h1>Cleaner Schedule</h1>
        <div class="sub">Colour-coded by flat • Check-out in red • Check-in in green • Same-day turnover highlighted</div>
        """
    ]

    # TODAY section
    if today in schedule:
        parts.append(f"<div class='day'>📅 {today.strftime('%A %d %B')} <span class='today-badge'>TODAY</span></div>")
        parts.append("<table>")
        parts.append(day_rows(today))
        parts.append("</table>")

    # Future days
    for d in [d for d in sorted(schedule.keys()) if d != today]:
        parts.append(f"<div class='day'>📅 {d.strftime('%A %d %B')}</div>")
        parts.append("<table>")
        parts.append(day_rows(d))
        parts.append("</table>")

    parts.append("""
      <div class="legend">
        <span class="badge">🔚 Check-out</span>
        <span class="badge">🧹 Clean</span>
        <span class="badge">🔑 Check-in</span>
        <span class="badge">🔁 Same-day Turnover</span>
      </div>
    """)

    return "".join(parts)


# ==============================
#  FastAPI app & routes
# ==============================

app = FastAPI()


@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"


@app.get("/", response_class=PlainTextResponse)
def home(days: int = Query(DEFAULT_DAYS, ge=1, le=60)):
    flats = {FLAT7_NAME: FLAT7_ICS, FLAT8_NAME: FLAT8_ICS, FLAT9_NAME: FLAT9_ICS}
    ics_map = {name: fetch_ics(url) for name, url in flats.items() if url}
    bookings = parse_bookings(ics_map)
    schedule = build_schedule_for_days(bookings, start=date.today(), days=days)
    return format_schedule_text(schedule)


@app.get("/cleaner", response_class=HTMLResponse)
def cleaner(days: int = Query(DEFAULT_DAYS, ge=1, le=60)):
    flats = {FLAT7_NAME: FLAT7_ICS, FLAT8_NAME: FLAT8_ICS, FLAT9_NAME: FLAT9_ICS}
    ics_map = {name: fetch_ics(url) for name, url in flats.items() if url}
    bookings = parse_bookings(ics_map)
    schedule = build_schedule_for_days(bookings, start=date.today(), days=days)
    return html_cleaner_view(schedule)
