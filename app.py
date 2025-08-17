import os
import io
import uuid
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple

import requests
from fastapi import FastAPI, Request, UploadFile, Form
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from icalendar import Calendar
from pytz import timezone, UTC

# ====== App & static uploads ======
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = os.path.join(os.getcwd(), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
# Serve uploaded files at /uploads/<filename>
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# ====== Config / helpers ======
PALETTE = ["#FF9800", "#2196F3", "#4CAF50", "#9C27B0", "#F44336", "#009688"]
CLEAN_START = os.getenv("CLEAN_START", "10:00")
CLEAN_END = os.getenv("CLEAN_END", "16:00")
DEFAULT_DAYS = int(os.getenv("DEFAULT_DAYS", "14"))
TZ_NAME = os.getenv("TIMEZONE", "Europe/London")
TZ = timezone(TZ_NAME)

def load_flats() -> Dict[str, Dict[str, str]]:
    """
    Returns dict keyed by display name, e.g.
      {
        "Flat 7": {"url": "https://...ics", "nick": "Orange", "colour": "#FF9800"},
        ...
      }
    Supports:
      - Your current envs: FLAT7_ICS_URL / FLAT8_ICS_URL / FLAT9_ICS_URL
      - Generic pattern:   FLAT{n}_ICS_URL for n in 1..50
      - Optional branding: FLAT{n}_NAME, FLAT{n}_NICK, FLAT{n}_COLOUR
    Skips placeholder values like "SET"/"TODO".
    """
    flats: Dict[str, Dict[str, str]] = {}
    palette_i = 0

    def add_flat(n: int, url_key: str):
        nonlocal palette_i
        url = os.getenv(url_key, "")
        if not url or url.strip().upper() in {"SET", "TODO"}:
            return

        name = os.getenv(f"FLAT{n}_NAME", f"Flat {n}").strip()
        nick = os.getenv(f"FLAT{n}_NICK", name).strip()
        colour = os.getenv(
            f"FLAT{n}_COLOUR",
            PALETTE[palette_i % len(PALETTE)],
        ).strip()

        flats[name] = {"url": url.strip(), "nick": nick, "colour": colour}
        palette_i += 1

    # Read explicit legacy 7/8/9 first
    for n in (7, 8, 9):
        add_flat(n, f"FLAT{n}_ICS_URL")

    # Also support generic FLAT1..FLAT50
    for n in range(1, 51):
        url = os.getenv(f"FLAT{n}_ICS_URL", "")
        if not url or url.strip().upper() in {"SET", "TODO"}:
            continue
        # Avoid duplicate key if already added via legacy
        name_probe = os.getenv(f"FLAT{n}_NAME", f"Flat {n}").strip()
        if name_probe in flats:
            continue
        add_flat(n, f"FLAT{n}_ICS_URL")

    return flats

def fetch_ics(url: str) -> str:
    if not url:
        return ""
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text

def parse_ics_to_ranges(ics_str: str) -> List[Tuple[date, date]]:
    """
    Parse ICS into a list of (checkin_date, checkout_date) in local TZ (date only).
    Assumes VEVENT DTSTART is check-in and DTEND is check-out (Booking.com style).
    """
    if not ics_str:
        return []

    cal = Calendar.from_ical(ics_str)
    ranges: List[Tuple[date, date]] = []

    for component in cal.walk():
        if component.name != "VEVENT":
            continue

        dtstart = component.get("DTSTART")
        dtend = component.get("DTEND")

        if not dtstart or not dtend:
            continue

        # Normalize to timezone-aware datetime
        def to_dt(x):
            val = x.dt
            if isinstance(val, datetime):
                return val if val.tzinfo else TZ.localize(val)
            # If it's a date, treat as midnight local time
            return TZ.localize(datetime.combine(val, datetime.min.time()))

        start_dt = to_dt(dtstart)
        end_dt = to_dt(dtend)

        # Convert to our local timezone
        start_local = start_dt.astimezone(TZ)
        end_local = end_dt.astimezone(TZ)

        # Represent as dates (checkin date, checkout date)
        ranges.append((start_local.date(), end_local.date()))

    return ranges

def build_schedule(flats: Dict[str, Dict[str, str]], days: int) -> Dict[date, List[Dict]]:
    """
    Returns: { day_date: [ {flat, nick, colour, in?:bool, out?:bool}, ... ] }
    We derive per-day check-in/out from each reservation range (start,end).
    """
    today = datetime.now(TZ).date()
    end_day = today + timedelta(days=days)

    schedule: Dict[date, List[Dict]] = {}

    # Prepare ICS for each flat
    for flat_name, meta in flats.items():
        try:
            ics = fetch_ics(meta["url"])
            ranges = parse_ics_to_ranges(ics)
        except Exception:
            ranges = []

        # For each reservation, mark check-in and check-out days
        for (dstart, dend) in ranges:
            # Check-in
            if today <= dstart < end_day:
                schedule.setdefault(dstart, [])
                schedule[dstart].append(
                    {"flat": flat_name, "nick": meta["nick"], "colour": meta["colour"], "in": True, "out": False}
                )
            # Check-out
            if today <= dend < end_day:
                schedule.setdefault(dend, [])
                schedule[dend].append(
                    {"flat": flat_name, "nick": meta["nick"], "colour": meta["colour"], "in": False, "out": True}
                )

    # Merge flat entries per day: if a flat has both in & out on same day -> same-day turnaround
    for d, items in list(schedule.items()):
        merged: Dict[str, Dict] = {}
        for it in items:
            key = it["flat"]
            if key not in merged:
                merged[key] = {**it}
            else:
                merged[key]["in"] = merged[key].get("in", False) or it.get("in", False)
                merged[key]["out"] = merged[key].get("out", False) or it.get("out", False)
        schedule[d] = list(merged.values())

    # Sort days & within day by flat name
    schedule = dict(sorted(schedule.items(), key=lambda kv: kv[0]))
    for d in schedule:
        schedule[d].sort(key=lambda x: x["flat"])

    return schedule

# ====== HTML rendering ======
def html_header() -> str:
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Cleaner Schedule</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 24px; color:#111; }}
    .wrap {{ max-width: 980px; margin: 0 auto; }}
    .subtle {{ color:#666; }}
    .legend span {{ margin-right: 12px; }}
    .pill {{ display:inline-block; padding:4px 10px; border-radius:999px; font-weight:600; color:#fff; }}
    .today {{ background:#111; color:#fff; font-size:12px; padding:3px 8px; border-radius:999px; margin-left:8px; vertical-align:middle; }}
    .day {{ background:#fafafa; border:1px solid #eee; border-radius:14px; padding:18px 20px; margin:16px 0; }}
    .row {{ display:flex; align-items:center; gap:12px; padding:8px 0; }}
    .nick {{ font-weight:700; padding:3px 10px; border-radius:999px; color:#fff; }}
    .status-out {{ color:#d32f2f; font-weight:700; }}
    .status-in {{ color:#2e7d32; font-weight:700; }}
    .note {{ font-size:13px; color:#444; }}
    .btn {{ background:#1976d2; color:#fff; padding:8px 12px; border-radius:8px; text-decoration:none; display:inline-flex; align-items:center; gap:8px; font-weight:600; }}
    .btn:hover {{ background:#125ea8; }}
    .muted {{ color:#888; }}
    .same {{ background:#fff3cd; border:1px solid #ffe08a; color:#5c3c00; padding:2px 8px; border-radius:6px; font-weight:600; }}
  </style>
</head>
<body>
<div class="wrap">
  <h1>Cleaner Schedule</h1>
  <div class="subtle legend">
    <span>Check-out in <span style="color:#d32f2f;font-weight:700;">red</span></span> •
    <span>Check-in in <span style="color:#2e7d32;font-weight:700;">green</span></span> •
    <span>Same-day turnover highlighted</span> •
    <span>Clean {CLEAN_START}–{CLEAN_END}</span>
  </div>
"""
def html_footer() -> str:
    return "</div></body></html>"

def render_cleaner(schedule: Dict[date, List[Dict]], days: int) -> str:
    today = datetime.now(TZ).date()
    out = [html_header()]

    if not schedule:
        out.append(f"""
        <p class="muted">No activity found. Try a longer window: <a href="/cleaner?days=30">/cleaner?days=30</a> or see <a href="/debug">/debug</a>.</p>
        """)
        out.append(html_footer())
        return "\n".join(out)

    for d, items in schedule.items():
        day_title = d.strftime("%a %d %b")
        is_today = (d == today)
        out.append(f'<div class="day"><h2 style="margin:0 0 8px 0;">{day_title}{" <span class=\\"today\\">TODAY</span>" if is_today else ""}</h2>')

        # detect if any check-out exists for this day (to show clean window)
        any_checkout = any(it.get("out") for it in items)

        # rows
        for it in items:
            colour = it["colour"]
            nick = it["nick"]
            flat = it["flat"]
            is_out = it.get("out", False)
            is_in = it.get("in", False)
            same_day = is_out and is_in

            status_parts = []
            if same_day:
                status_html = '<span class="same">Check-out → Clean → Check-in (same day)</span>'
            else:
                if is_out:
                    status_parts.append('<span class="status-out">Check-out</span>')
                if is_in:
                    status_parts.append('<span class="status-in">Check-in</span>')
                status_html = " • ".join(status_parts) if status_parts else ""

            # Clean window shown if there is a checkout that day, or same-day
            show_clean_band = same_day or (is_out and not is_in) or (any_checkout and not is_in)

            # Upload link
            up_href = f'/upload?flat={flat.replace(" ", "%20")}&date={d.isoformat()}'

            out.append('<div class="row">')
            out.append(f'<span class="nick" style="background:{colour}">{nick}</span>')
            out.append(status_html)
            if show_clean_band:
                out.append(f'<span class="note">🧹 Clean between <strong>{CLEAN_START}–{CLEAN_END}</strong></span>')
            out.append(f'<a class="btn" href="{up_href}">📷 Upload Photos</a>')
            out.append('</div>')

        out.append('</div>')

    out.append(html_footer())
    return "\n".join(out)

# ====== Routes ======
@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"

@app.get("/cleaner", response_class=HTMLResponse)
def cleaner(days: int = DEFAULT_DAYS):
    flats = load_flats()
    schedule = build_schedule(flats, days)
    return HTMLResponse(render_cleaner(schedule, days))

@app.get("/debug", response_class=PlainTextResponse)
def debug():
    flats = load_flats()
    lines = ["Loaded flats:"]
    for name, meta in flats.items():
        lines.append(f"  {name}: url={meta['url']} nick={meta['nick']} colour={meta['colour']}")
    # quick probe counts
    try_days = 14
    sched = build_schedule(flats, try_days)
    counts = {k: len(v) for k, v in sched.items()}
    lines.append("\nCounts per day (next 14 days):")
    for d in sorted(counts.keys()):
        lines.append(f"  {d.isoformat()}: {counts[d]}")
    lines.append(f"\nDays with activity in next {try_days} days: {len(sched)}")
    return "\n".join(lines) + "\n"

# ====== Upload + WhatsApp ======
TW_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TW_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TW_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
TW_TO = os.getenv("TWILIO_WHATSAPP_TO", "").strip()

def send_whatsapp(body: str, media_urls: List[str]):
    """
    Send WhatsApp message (text + media) using Twilio.
    Requires approved WA sender (Business) and proper FROM/TO in E.164 without 'whatsapp:' prefix here;
    we add the prefix below.
    """
    if not (TW_SID and TW_TOKEN and TW_FROM and TW_TO):
        # Silently skip if not configured (avoids 500s while setting up)
        return

    try:
        from twilio.rest import Client
        client = Client(TW_SID, TW_TOKEN)
        client.messages.create(
            from_=f"whatsapp:{TW_FROM}",
            to=f"whatsapp:{TW_TO}",
            body=body,
            media_url=media_urls if media_urls else None,
        )
    except Exception as e:
        # Avoid crashing the request: log to console
        print("Twilio send error:", repr(e))

def html_upload_form(flat: str, the_date: str, error: str = "") -> str:
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Upload Photos</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 24px; color:#111; }}
    .wrap {{ max-width: 720px; margin: 0 auto; }}
    .box {{ background:#fafafa; border:1px solid #eee; border-radius:12px; padding:18px; }}
    .row {{ margin:12px 0; }}
    .btn {{ background:#1976d2; color:#fff; padding:10px 14px; border-radius:8px; text-decoration:none; font-weight:600; display:inline-block; border:0; }}
    .btn:hover {{ background:#125ea8; }}
    .err {{ color:#b00020; font-weight:700; }}
  </style>
</head>
<body>
<div class="wrap">
  <h2>Upload Photos — {flat} — {the_date}</h2>
  <p>Attach one or more photos and (optional) leave a note.</p>
  {"<p class='err'>" + error + "</p>" if error else ""}
  <div class="box">
    <form method="post" enctype="multipart/form-data" action="/upload">
      <input type="hidden" name="flat" value="{flat}">
      <input type="hidden" name="date" value="{the_date}">
      <div class="row">
        <label>Photos (you can select multiple):</label><br/>
        <input name="photos" type="file" accept="image/*" multiple required />
      </div>
      <div class="row">
        <label>Optional note for host:</label><br/>
        <textarea name="note" rows="4" style="width:100%;" placeholder="Any issues, damages, extra time needed, missing items, etc."></textarea>
      </div>
      <div class="row">
        <button class="btn" type="submit">Send</button>
        <a class="btn" style="background:#555;margin-left:8px;" href="/cleaner">Back</a>
      </div>
    </form>
  </div>
</div>
</body>
</html>
"""

@app.get("/upload", response_class=HTMLResponse)
def get_upload(flat: str, date: str):
    # simple guard: require known flat
    flats = load_flats()
    if flat not in flats:
        return HTMLResponse(html_upload_form(flat, date, error="Unknown flat (check the link)."))
    return HTMLResponse(html_upload_form(flat, date))

@app.post("/upload", response_class=HTMLResponse)
async def post_upload(request: Request, flat: str = Form(...), date: str = Form(...), photos: List[UploadFile] = Form(...), note: str = Form("")):
    # Save files and build public URLs
    saved_urls: List[str] = []
    for f in photos:
        if not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        safe_name = f"{uuid.uuid4().hex}{ext}"
        dst_path = os.path.join(UPLOAD_DIR, safe_name)
        data = await f.read()
        with open(dst_path, "wb") as out:
            out.write(data)
        # Public URL that Twilio can fetch
        base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        if not base:
            # Infer from request (works on Render)
            base = str(request.url).split("/upload")[0]
        saved_urls.append(f"{base}/uploads/{safe_name}")

    # Compose WA body
    body_lines = [
        f"Cleaning complete: {flat}",
        f"Date: {date}",
    ]
    if note and note.strip():
        body_lines.append(f"Note: {note.strip()}")
    body = "\n".join(body_lines)

    # Send WhatsApp (if configured)
    send_whatsapp(body, saved_urls)

    # Simple thank you + back link
    files_list = "".join([f'<li><a href="{u}" target="_blank">{u}</a></li>' for u in saved_urls]) or "<li>(no files?)</li>"
    return HTMLResponse(f"""
<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Uploaded</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;margin:24px;color:#111;}}
.wrap{{max-width:720px;margin:0 auto;}}
.box{{background:#fafafa;border:1px solid #eee;border-radius:12px;padding:18px;}}
.btn{{background:#1976d2;color:#fff;padding:10px 14px;border-radius:8px;text-decoration:none;font-weight:600;display:inline-block;}}
.btn:hover{{background:#125ea8;}}
</style></head>
<body>
<div class="wrap">
  <h2>Thanks! 📸</h2>
  <div class="box">
    <p>We received your photos for <strong>{flat}</strong> — <strong>{date}</strong>.</p>
    {"<p>Links we saved:</p><ul>" + files_list + "</ul>"}
    <p><a class="btn" href="/cleaner">Back to schedule</a></p>
  </div>
</div>
</body></html>
""")

# ====== Run locally ======
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
