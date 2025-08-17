import os
import io
import uuid
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple

import pytz
import requests
from fastapi import FastAPI, Form, UploadFile, File, HTTPException, Request
from fastapi.responses import PlainTextResponse, HTMLResponse, FileResponse, RedirectResponse
from icalendar import Calendar
from twilio.rest import Client
from urllib.parse import urlencode

# ----------------------------
# Config / Environment
# ----------------------------

TIMEZONE = os.getenv("TIMEZONE", "Europe/London")
TZ = pytz.timezone(TIMEZONE)

DEFAULT_DAYS = int(os.getenv("DEFAULT_DAYS", "14"))

# Colour palette fallback for chips if not supplied per-flat
PALETTE = [
    "#FF9800",  # orange
    "#2196F3",  # blue
    "#4CAF50",  # green
    "#9C27B0",  # purple
    "#E91E63",  # pink
    "#00BCD4",  # cyan
    "#795548",  # brown
]

# Twilio
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
TWILIO_WHATSAPP_TO = os.getenv("TWILIO_WHATSAPP_TO", "").strip()

def _wa_format(num: str) -> str:
    """Ensure numbers are prefixed with whatsapp: for WA API."""
    if not num:
        return ""
    num = num.strip()
    if not num.startswith("whatsapp:"):
        num = f"whatsapp:{num}"
    return num

WA_FROM = _wa_format(TWILIO_WHATSAPP_FROM)
WA_TO = _wa_format(TWILIO_WHATSAPP_TO)

twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception:
        twilio_client = None


# ----------------------------
# Load flats from env
# Supported vars (examples):
#   FLAT7_ICS_URL, FLAT7_NAME, FLAT7_NICK, FLAT7_COLOUR
#   FLAT8_..., FLAT9_...
# ----------------------------

def load_flats_from_env(max_flats: int = 99) -> Dict[str, Dict[str, str]]:
    """
    Returns dict keyed by display name:
      { "Flat 7": {"url":"...", "nick":"Orange", "colour":"#ff9800"}, ... }
    We look for FLAT7_*, FLAT8_*, ... up to FLAT99_* by default.
    """
    flats: Dict[str, Dict[str, str]] = {}
    palette_i = 0
    for n in range(1, max_flats + 1):
        # We only expect 7,8,9 in your setup, but this is flexible.
        url = os.getenv(f"FLAT{n}_ICS_URL", "").strip()
        if not url:
            continue
        name = os.getenv(f"FLAT{n}_NAME", f"Flat {n}").strip()
        nick = os.getenv(f"FLAT{n}_NICK", name).strip()
        colour = os.getenv(f"FLAT{n}_COLOUR", PALETTE[palette_i % len(PALETTE)]).strip()
        palette_i += 1
        flats[name] = {"url": url, "nick": nick, "colour": colour}
    return flats

FLATS = load_flats_from_env()

# ----------------------------
# ICS helpers
# ----------------------------

def fetch_ics(url: str) -> str:
    if not url:
        return ""
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text

def parse_bookings(ics_map: Dict[str, str]) -> Dict[str, List[Tuple[date, str]]]:
    """
    Parse each ICS (key = flat display name).
    Build a mapping: flat -> list of (date, status) where status in {"in","out"}.

    Booking.com feeds normally encode a keyword in SUMMARY:
      - "check-in"  => status "in"
      - "check-out" => status "out"

    We place the event on the event's DTSTART.date() in local timezone.
    """
    out: Dict[str, List[Tuple[date, str]]] = {}
    for flat_name, ics_text in ics_map.items():
        items: List[Tuple[date, str]] = []
        if not ics_text:
            out[flat_name] = items
            continue
        try:
            cal = Calendar.from_ical(ics_text)
        except Exception:
            out[flat_name] = items
            continue

        for comp in cal.walk():
            if comp.name != "VEVENT":
                continue

            summary = str(comp.get("SUMMARY", "")).lower()
            dtstart_prop = comp.get("DTSTART")

            if not dtstart_prop:
                continue

            # Normalize to aware datetime in our timezone, then take .date()
            dtstart = dtstart_prop.dt
            if isinstance(dtstart, datetime):
                if dtstart.tzinfo is None:
                    dtstart = TZ.localize(dtstart)
                else:
                    dtstart = dtstart.astimezone(TZ)
                d = dtstart.date()
            elif isinstance(dtstart, date):
                d = dtstart
            else:
                continue

            status = None
            if "check-in" in summary or "check in" in summary:
                status = "in"
            elif "check-out" in summary or "check out" in summary:
                status = "out"

            if status:
                items.append((d, status))

        # sort by date
        items.sort(key=lambda x: (x[0], x[1]))
        out[flat_name] = items
    return out

def build_schedule_for_days(days: int) -> Dict[date, List[Dict[str, str]]]:
    """
    Build a dict keyed by day -> list of items:
      item: {flat, nick, status, same_day: yes/no}
    """
    if not FLATS:
        return {}

    # Fetch ICS for each flat
    ics_map: Dict[str, str] = {}
    for flat, meta in FLATS.items():
        try:
            ics_map[flat] = fetch_ics(meta["url"])
        except Exception:
            ics_map[flat] = ""

    bookings = parse_bookings(ics_map)

    today = datetime.now(TZ).date()
    end_day = today + timedelta(days=days - 1)

    # Build quick lookup per flat per day
    per_flat_per_day: Dict[str, Dict[date, List[str]]] = {}
    for flat, events in bookings.items():
        per_flat_per_day[flat] = {}
        for d, status in events:
            if today <= d <= end_day:
                per_flat_per_day[flat].setdefault(d, []).append(status)

    schedule: Dict[date, List[Dict[str, str]]] = {}
    d = today
    while d <= end_day:
        day_items: List[Dict[str, str]] = []
        for flat, daily in per_flat_per_day.items():
            statuses = daily.get(d, [])
            has_out = "out" in statuses
            has_in = "in" in statuses
            same_day = "yes" if (has_out and has_in) else "no"

            if has_out:
                day_items.append({
                    "flat": flat,
                    "nick": FLATS[flat]["nick"],
                    "status": "out",
                    "same_day": same_day
                })
            if has_in:
                day_items.append({
                    "flat": flat,
                    "nick": FLATS[flat]["nick"],
                    "status": "in",
                    "same_day": same_day
                })

        if day_items:
            schedule[d] = day_items
        d += timedelta(days=1)

    return schedule


# ----------------------------
# Web App
# ----------------------------

app = FastAPI()

@app.get("/", response_class=PlainTextResponse)
def root():
    return "Hello! The app is running."

def build_today_badge(d: date) -> str:
    if d == datetime.now(TZ).date():
        return '<span class="badge today">TODAY</span>'
    return ""

# -------- Cleaner HTML view (improved) --------

@app.get("/cleaner", response_class=HTMLResponse)
def cleaner_view(days: int = DEFAULT_DAYS):
    sched = build_schedule_for_days(days)
    if not sched:
        longer = f"/cleaner?{urlencode({'days': max(30, days)})}"
        return f"""
        <html><head><title>Cleaner Schedule</title>
        <style>
          body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; padding: 24px; color:#222 }}
          .day {{ background:#fff; border:1px solid #eee; border-radius:12px; padding:16px 18px; margin:18px 0; box-shadow:0 2px 6px rgba(0,0,0,.04)}}
          h1 {{ margin:0 0 10px }}
          h2 {{ margin:0 0 12px; display:flex; align-items:center; gap:10px }}
          .meta {{ color:#666; margin-bottom:16px }}
          .badge.today {{ background:#111; color:#fff; padding:4px 8px; border-radius:999px; font-size:12px }}
        </style>
        </head>
        <body>
          <h1>Cleaner Schedule</h1>
          <div class="meta">Check-out in <span style="color:#c62828">red</span> • Check-in in <span style="color:#2e7d32">green</span> • Same-day turnover highlighted • Clean 10:00–16:00</div>
          <p>No activity found. Try a longer window: <a href="{longer}">{longer}</a> or see <a href="/debug">/debug</a>.</p>
        </body></html>
        """

    html = ["""
    <html><head><title>Cleaner Schedule</title>
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; padding: 24px; color:#222; background:#f7f7f8 }
      .day { background:#fff; border:1px solid #eee; border-radius:12px; padding:16px 18px; margin:18px 0; box-shadow:0 2px 6px rgba(0,0,0,.04)}
      h1 { margin:0 0 10px }
      h2 { margin:0 0 12px; display:flex; align-items:center; gap:10px }
      .meta { color:#666; margin-bottom:16px }
      .badge.today { background:#111; color:#fff; padding:4px 8px; border-radius:999px; font-size:12px }
      .chip { display:inline-block; padding:4px 10px; border-radius:999px; color:#fff; font-weight:600; margin-right:10px; }
      .status-out { color:#c62828; font-weight:700 }
      .status-in { color:#2e7d32; font-weight:700 }
      .note { color:#666; font-size:14px; margin:8px 0 }
      .btn { display:inline-block; background:#1976d2; color:#fff; text-decoration:none; padding:8px 12px; border-radius:8px; font-size:14px; border:0; cursor:pointer }
      .turn { display:inline-block; background:#FFF3CD; border:1px solid #FFE28A; color:#6b5b00; padding:2px 8px; border-radius:6px; font-size:12px; margin-left:8px }
      form.upload { margin-top:8px; display:flex; gap:8px; flex-wrap:wrap; align-items:center }
      input[type=file] { padding:6px }
      textarea { width:320px; height:70px; padding:8px; border-radius:8px; border:1px solid #ddd; font-family:inherit }
      .row { margin:10px 0; }
    </style>
    </head><body>
      <h1>Cleaner Schedule</h1>
      <div class="meta">Check-out in <span style="color:#c62828">red</span> • Check-in in <span style="color:#2e7d32">green</span> • Same-day turnover highlighted • Clean 10:00–16:00</div>
    """]
    for d, items in sched.items():
        today_badge = build_today_badge(d)
        html.append(f'<div class="day">')
        html.append(f'<h2>{d.strftime("%a %d %b")} {today_badge}</h2>')
        for it in items:
            flat = it["flat"]
            nick = it["nick"]
            colour = FLATS[flat]["colour"] or "#555"
            status_text = "Check-out" if it["status"] == "out" else "Check-in"
            status_class = "status-out" if it["status"] == "out" else "status-in"

            # clearer same-day message
            same = ""
            if it["same_day"] == "yes":
                same = '<span class="turn">Check-out → <b>Clean</b> → Check-in (same-day)</span>'

            chip = f'<span class="chip" style="background:{colour}">{nick}</span>'
            html.append(f'<div class="row">{chip} <span class="{status_class}">{status_text}</span>{same}</div>')

            # Show clean window ONLY for check-out items
            if it["status"] == "out":
                html.append('<div class="note">🧹 Clean between <b>10:00–16:00</b></div>')

            # Upload form (photo + optional notes)
            html.append(f"""
            <form class="upload" action="/upload" method="post" enctype="multipart/form-data">
              <input type="hidden" name="flat" value="{flat}">
              <input type="hidden" name="date" value="{d.isoformat()}">
              <input type="file" name="photo" accept="image/*" required>
              <textarea name="notes" placeholder="Any notes for today? (optional)"></textarea>
              <button class="btn" type="submit">📸 Upload Photos</button>
            </form>
            """)

        html.append("</div>")
    html.append("</body></html>")
    return "\n".join(html)


# ----------------------------
# Media hosting for Twilio to fetch
# Files saved into /tmp/uploads; served at /m/{filename}
# ----------------------------

UPLOAD_DIR = "/tmp/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.get("/m/{fname}")
def serve_media(fname: str):
    path = os.path.join(UPLOAD_DIR, fname)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Not found")
    # Best-effort content type guess
    media_type = "image/jpeg"
    if fname.lower().endswith(".png"):
        media_type = "image/png"
    elif fname.lower().endswith(".webp"):
        media_type = "image/webp"
    return FileResponse(path, media_type=media_type)


# ----------------------------
# Upload endpoint
# ----------------------------

@app.post("/upload")
async def upload_photo(
    flat: str = Form(...),
    date_str: str = Form(""),
    notes: str = Form(""),
    photo: UploadFile = File(...)
):
    # Save file
    ext = ".jpg"
    ct = (photo.content_type or "").lower()
    if "png" in ct:
        ext = ".png"
    elif "webp" in ct:
        ext = ".webp"
    fname = f"{uuid.uuid4().hex}{ext}"
    dest = os.path.join(UPLOAD_DIR, fname)
    content = await photo.read()
    with open(dest, "wb") as f:
        f.write(content)

    # Build public media URL
    # Example: https://cleaner-schedule.onrender.com/m/<fname>
    base = os.getenv("PUBLIC_BASE_URL", "").strip()
    if not base:
        # Try to infer from Render env (works if request headers available, but here fallback)
        # You can set PUBLIC_BASE_URL in Render env to be safe.
        raise_to = False
        base = ""  # if empty, we'll try a relative full path below.

    # If PUBLIC_BASE_URL not set, construct relative path (Twilio requires public absolute URL)
    # So strongly recommend setting PUBLIC_BASE_URL to your app origin, e.g. https://cleaner-schedule.onrender.com
    if not base:
        # We cannot know the absolute host here outside a request context for Twilio.
        # Use Render variable if available: RENDER_EXTERNAL_URL
        base = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if not base:
        # Last resort: hope the caller knows; media URL may not be fetchable by Twilio if not absolute.
        base = ""

    media_url = f"{base}/m/{fname}" if base else f"/m/{fname}"

    # Compose message
    pretty_date = date_str or date.today().isoformat()
    nick = FLATS.get(flat, {}).get("nick", flat)
    note_text = notes.strip() or "No notes."

    msg_body = f"Cleaning complete: {nick}\nDate: {pretty_date}\nNotes: {note_text}"

    # Send WhatsApp via Twilio (if configured and numbers present)
    twilio_err = None
    sid = None
    if twilio_client and WA_FROM and WA_TO:
        try:
            # If PUBLIC_BASE_URL missing, Twilio won't fetch relative URLs => skip media in that case
            kwargs = dict(from_=WA_FROM, to=WA_TO, body=msg_body)
            if media_url.startswith("http"):
                kwargs["media_url"] = [media_url]
            message = twilio_client.messages.create(**kwargs)
            sid = message.sid
        except Exception as e:
            twilio_err = str(e)

    # Redirect back to /cleaner highlighting success (keeps UX simple)
    # Optionally you could render a confirmation page.
    q = {"days": DEFAULT_DAYS}
    redirect_url = f"/cleaner?{urlencode(q)}"
    # If Twilio failed, you could append a small flag the UI could read.
    if twilio_err:
        redirect_url += f"&warn=twilio"
    return RedirectResponse(redirect_url, status_code=303)


# ----------------------------
# Debug page
# ----------------------------

@app.get("/debug", response_class=PlainTextResponse)
def debug():
    sched = build_schedule_for_days(14)
    lines = []
    lines.append("Loaded flats:")
    for flat, meta in FLATS.items():
        lines.append(f"  {flat}: url={'SET' if meta['url'] else 'MISSING'} nick={meta['nick']} colour={meta['colour']}")
    total_days = sum(len(v) for v in sched.values())
    lines.append(f"\nDays with activity in next 14 days: {len(sched)}")
    return "\n".join(lines)


# ----------------------------
# Local run
# ----------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
