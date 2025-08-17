import os
import io
import uuid
import re
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple

import pytz
import requests
from fastapi import FastAPI, Form, UploadFile, File, HTTPException
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

PALETTE = ["#FF9800", "#2196F3", "#4CAF50", "#9C27B0", "#E91E63", "#00BCD4", "#795548"]

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()
TWILIO_WHATSAPP_TO = os.getenv("TWILIO_WHATSAPP_TO", "").strip()

def _wa(num: str) -> str:
    if not num:
        return ""
    return num if num.startswith("whatsapp:") else f"whatsapp:{num}"

WA_FROM = _wa(TWILIO_WHATSAPP_FROM)
WA_TO = _wa(TWILIO_WHATSAPP_TO)

twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    try:
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception:
        twilio_client = None

# ----------------------------
# Flats from env: FLAT7_*, FLAT8_*, FLAT9_* ...
# ----------------------------

def load_flats_from_env(max_flats: int = 99) -> Dict[str, Dict[str, str]]:
    flats: Dict[str, Dict[str, str]] = {}
    i = 0
    for n in range(1, max_flats + 1):
        url = os.getenv(f"FLAT{n}_ICS_URL", "").strip()
        if not url:
            continue
        name = os.getenv(f"FLAT{n}_NAME", f"Flat {n}").strip()
        nick = os.getenv(f"FLAT{n}_NICK", name).strip()
        colour = os.getenv(f"FLAT{n}_COLOUR", PALETTE[i % len(PALETTE)]).strip()
        i += 1
        flats[name] = {"url": url, "nick": nick, "colour": colour}
    return flats

FLATS = load_flats_from_env()

# ----------------------------
# ICS helpers
# ----------------------------

UA_HEADERS = {"User-Agent": "CleanerSchedule/1.0 (+https://example.com)"}

def fetch_ics(url: str) -> str:
    if not url:
        return ""
    r = requests.get(url, timeout=30, headers=UA_HEADERS)
    r.raise_for_status()
    return r.text

# very tolerant matchers: check-in/out with hyphen, en dash, em dash or space, any case
RE_IN = re.compile(r"check[\s\-–—]?in", re.IGNORECASE)
RE_OUT = re.compile(r"check[\s\-–—]?out", re.IGNORECASE)

def _event_status(summary: str, description: str) -> str:
    """Return 'in'/'out'/'' based on summary/description."""
    text = f"{summary}\n{description}".lower()
    if RE_OUT.search(text):
        return "out"
    if RE_IN.search(text):
        return "in"
    return ""

def parse_bookings(ics_map: Dict[str, str]) -> Dict[str, List[Tuple[date, str]]]:
    """
    Returns { flat: [(date, 'in'|'out'), ...] }.
    Accepts matches in SUMMARY or DESCRIPTION; tolerant to punctuation/case.
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
            summary = str(comp.get("SUMMARY", "") or "")
            description = str(comp.get("DESCRIPTION", "") or "")
            dtstart_prop = comp.get("DTSTART")
            if not dtstart_prop:
                continue

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

            status = _event_status(summary, description)
            if status in ("in", "out"):
                items.append((d, status))

        items.sort(key=lambda x: (x[0], x[1]))
        out[flat_name] = items
    return out

def build_schedule_for_days(days: int) -> Dict[date, List[Dict[str, str]]]:
    if not FLATS:
        return {}

    ics_map: Dict[str, str] = {}
    for flat, meta in FLATS.items():
        try:
            ics_map[flat] = fetch_ics(meta["url"])
        except Exception:
            ics_map[flat] = ""

    bookings = parse_bookings(ics_map)

    today = datetime.now(TZ).date()
    end_day = today + timedelta(days=days - 1)

    per_flat_per_day: Dict[str, Dict[date, List[str]]] = {}
    for flat, events in bookings.items():
        dmap: Dict[date, List[str]] = {}
        for d, status in events:
            if today <= d <= end_day:
                dmap.setdefault(d, []).append(status)
        per_flat_per_day[flat] = dmap

    schedule: Dict[date, List[Dict[str, str]]] = {}
    d = today
    while d <= end_day:
        items: List[Dict[str, str]] = []
        for flat, dmap in per_flat_per_day.items():
            statuses = dmap.get(d, [])
            if not statuses:
                continue
            has_out = "out" in statuses
            has_in = "in" in statuses
            same = "yes" if (has_out and has_in) else "no"

            if has_out:
                items.append({"flat": flat, "nick": FLATS[flat]["nick"], "status": "out", "same_day": same})
            if has_in:
                items.append({"flat": flat, "nick": FLATS[flat]["nick"], "status": "in", "same_day": same})

        if items:
            schedule[d] = items
        d += timedelta(days=1)

    return schedule

# ----------------------------
# Web
# ----------------------------

app = FastAPI()

@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"

def today_badge(d: date) -> str:
    return '<span class="badge today">TODAY</span>' if d == datetime.now(TZ).date() else ""

@app.get("/cleaner", response_class=HTMLResponse)
def cleaner_view(days: int = DEFAULT_DAYS):
    sched = build_schedule_for_days(days)
    if not sched:
        longer = f"/cleaner?{urlencode({'days': max(30, days)})}"
        return f"""
        <html><head><title>Cleaner Schedule</title>
        <style>
          body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; padding: 24px; color:#222 }}
          .badge.today {{ background:#111; color:#fff; padding:4px 8px; border-radius:999px; font-size:12px }}
        </style></head><body>
          <h1>Cleaner Schedule</h1>
          <div>Check-out in <span style="color:#c62828">red</span> • Check-in in <span style="color:#2e7d32">green</span> • Same-day turnover highlighted • Clean 10:00–16:00</div>
          <p>No activity found. Try a longer window: <a href="{longer}">{longer}</a> or see <a href="/debug">/debug</a>.</p>
        </body></html>
        """

    html = ["""
    <html><head><title>Cleaner Schedule</title>
    <style>
      body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; padding:24px; color:#222; background:#f7f7f8 }
      .day { background:#fff; border:1px solid #eee; border-radius:12px; padding:16px 18px; margin:18px 0; box-shadow:0 2px 6px rgba(0,0,0,.04)}
      h1 { margin:0 0 10px }
      h2 { margin:0 0 12px; display:flex; align-items:center; gap:10px }
      .badge.today { background:#111; color:#fff; padding:4px 8px; border-radius:999px; font-size:12px }
      .chip { display:inline-block; padding:4px 10px; border-radius:999px; color:#fff; font-weight:600; margin-right:10px }
      .status-out { color:#c62828; font-weight:700 }
      .status-in { color:#2e7d32; font-weight:700 }
      .turn { display:inline-block; background:#FFF3CD; border:1px solid #FFE28A; color:#6b5b00; padding:2px 8px; border-radius:6px; font-size:12px; margin-left:8px }
      .note { color:#666; font-size:14px; margin:8px 0 }
      .btn { display:inline-block; background:#1976d2; color:#fff; text-decoration:none; padding:8px 12px; border-radius:8px; font-size:14px; border:0; cursor:pointer }
      form.upload { margin-top:8px; display:flex; gap:8px; flex-wrap:wrap; align-items:center }
      input[type=file] { padding:6px }
      textarea { width:320px; height:70px; padding:8px; border-radius:8px; border:1px solid #ddd; font-family:inherit }
      .row { margin:10px 0 }
    </style></head><body>
      <h1>Cleaner Schedule</h1>
      <div>Check-out in <span style="color:#c62828">red</span> • Check-in in <span style="color:#2e7d32">green</span> • Same-day turnover highlighted • Clean 10:00–16:00</div>
    """]
    for d, items in sched.items():
        html.append(f'<div class="day"><h2>{d.strftime("%a %d %b")} {today_badge(d)}</h2>')
        for it in items:
            flat = it["flat"]
            nick = it["nick"]
            colour = FLATS[flat]["colour"] or "#555"
            status_text = "Check-out" if it["status"] == "out" else "Check-in"
            status_class = "status-out" if it["status"] == "out" else "status-in"
            same = ''
            if it["same_day"] == "yes":
                same = '<span class="turn">Check-out → <b>Clean</b> → Check-in (same-day)</span>'
            chip = f'<span class="chip" style="background:{colour}">{nick}</span>'
            html.append(f'<div class="row">{chip} <span class="{status_class}">{status_text}</span>{same}</div>')
            if it["status"] == "out":
                html.append('<div class="note">🧹 Clean between <b>10:00–16:00</b></div>')
            html.append(f"""
            <form class="upload" action="/upload" method="post" enctype="multipart/form-data">
              <input type="hidden" name="flat" value="{flat}">
              <input type="hidden" name="date" value="{d.isoformat()}">
              <input type="file" name="photo" accept="image/*" required>
              <textarea name="notes" placeholder="Any notes for today? (optional)"></textarea>
              <button class="btn" type="submit">📸 Upload Photos</button>
            </form>""")
        html.append("</div>")
    html.append("</body></html>")
    return "\n".join(html)

# ----------------------------
# Media for Twilio
# ----------------------------

UPLOAD_DIR = "/tmp/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

@app.get("/m/{fname}")
def serve_media(fname: str):
    path = os.path.join(UPLOAD_DIR, fname)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Not found")
    media_type = "image/jpeg"
    fl = fname.lower()
    if fl.endswith(".png"):
        media_type = "image/png"
    elif fl.endswith(".webp"):
        media_type = "image/webp"
    return FileResponse(path, media_type=media_type)

# ----------------------------
# Upload + WhatsApp
# ----------------------------

@app.post("/upload")
async def upload_photo(
    flat: str = Form(...),
    date: str = Form(""),
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

    base = os.getenv("PUBLIC_BASE_URL", "").strip() or os.getenv("RENDER_EXTERNAL_URL", "").strip()
    media_url = f"{base}/m/{fname}" if base else f"/m/{fname}"

    pretty = date or datetime.now(TZ).date().isoformat()
    nick = FLATS.get(flat, {}).get("nick", flat)
    body = f"Cleaning complete: {nick}\nDate: {pretty}\nNotes: {(notes.strip() or 'No notes.')}"

    if twilio_client and WA_FROM and WA_TO:
        try:
            kwargs = dict(from_=WA_FROM, to=WA_TO, body=body)
            if media_url.startswith("http"):
                kwargs["media_url"] = [media_url]
            twilio_client.messages.create(**kwargs)
        except Exception:
            # still return success to the cleaner
            pass

    return RedirectResponse(f"/cleaner?{urlencode({'days': DEFAULT_DAYS})}", status_code=303)

# ----------------------------
# Debug
# ----------------------------

@app.get("/debug", response_class=PlainTextResponse)
def debug():
    lines = []
    lines.append("Loaded flats:")
    for flat, meta in FLATS.items():
        lines.append(f"  {flat}: url={'SET' if meta['url'] else 'MISSING'} nick={meta['nick']} colour={meta['colour']}")
    # Fetch & parse with counts
    ics_map = {}
    for flat, meta in FLATS.items():
        try:
            ics_map[flat] = fetch_ics(meta["url"])
        except Exception as e:
            ics_map[flat] = ""
    bookings = parse_bookings(ics_map)
    lines.append("")
    for flat, items in bookings.items():
        ins = sum(1 for d,s in items if s == 'in')
        outs = sum(1 for d,s in items if s == 'out')
        lines.append(f"{flat}: total={len(items)} (in={ins}, out={outs})")
    sched = build_schedule_for_days(14)
    lines.append(f"\nDays with activity in next 14 days: {len(sched)}")
    return "\n".join(lines)

# ----------------------------
# Run
# ----------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
