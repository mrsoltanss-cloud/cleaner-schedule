# app.py
import os
import uuid
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple, Optional

import requests
from icalendar import Calendar
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------
# Config
# ---------------------------
TIMEZONE = os.getenv("TIMEZONE", "Europe/London")
DEFAULT_DAYS = int(os.getenv("DEFAULT_DAYS", "14"))
CLEAN_START = os.getenv("CLEAN_START", "10:00")
CLEAN_END = os.getenv("CLEAN_END", "16:00")

# Twilio (optional but recommended)
TWILIO_ACCOUNT_SID = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()  # e.g. +447490016919 or whatsapp:+447490016919
TWILIO_WHATSAPP_TO = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()      # e.g. +447480001112 or whatsapp:+447480001112

# Public base URL for media (set this in Render: PUBLIC_BASE_URL=https://your-app.onrender.com)
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

# Upload directory (served at /m/<filename>)
UPLOAD_DIR = "/tmp/uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Optional Twilio import (don’t crash app if not installed/misconfigured)
try:
    from twilio.rest import Client as TwilioClient
    _twilio_ok = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and TWILIO_WHATSAPP_TO)
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if _twilio_ok else None
except Exception:
    twilio_client = None
    _twilio_ok = False

# ---------------------------
# App
# ---------------------------
app = FastAPI(title="Cleaner Schedule")

# Serve raw uploaded files publicly so Twilio can fetch them as media
app.mount("/static", StaticFiles(directory=UPLOAD_DIR), name="static")

# ---------------------------
# Flats & ICS helpers
# ---------------------------
PALETTE = ["#FF9800", "#2196F3", "#4CAF50", "#9C27B0", "#E91E63", "#00BCD4", "#795548", "#3F51B5"]

def load_flats(max_flats: int = 50) -> Dict[str, Dict[str, str]]:
    """
    Read FLAT{n}_ICS_URL (+ optional FLAT{n}_NAME/NICK/COLOUR) from env.
    Skips placeholders like 'SET'/'TODO'.
    Returns { "Flat 7": {"url": "...", "nick": "Orange", "colour": "#FF9800"}, ... }
    """
    flats: Dict[str, Dict[str, str]] = {}
    i = 0
    for n in range(1, max_flats + 1):
        url = (os.getenv(f"FLAT{n}_ICS_URL") or "").strip()
        if not url or url.upper() in {"SET", "TODO"}:
            continue
        name = (os.getenv(f"FLAT{n}_NAME") or f"Flat {n}").strip()
        nick = (os.getenv(f"FLAT{n}_NICK") or name).strip()
        colour = (os.getenv(f"FLAT{n}_COLOUR") or PALETTE[i % len(PALETTE)]).strip()
        flats[name] = {"url": url, "nick": nick, "colour": colour}
        i += 1
    return flats

UA_HEADERS = {"User-Agent": "CleanerSchedule/1.0 (+https://example.com)"}

def fetch_ics(url: str) -> str:
    """Fetch ICS safely. Never raise—return '' on any problem."""
    if not url:
        return ""
    try:
        r = requests.get(url, headers=UA_HEADERS, timeout=10)
        if r.status_code >= 400 or not r.text:
            return ""
        return r.text
    except Exception:
        return ""

def parse_bookings(ics_text: str) -> List[Tuple[date, date]]:
    """
    Returns list of (checkin_date, checkout_date).
    Booking.com style: DTSTART = check-in (all-day), DTEND = check-out (all-day, exclusive).
    We use DTEND.date() as the checkout day so same-day turnover is detected.
    """
    if not ics_text.strip():
        return []
    try:
        cal = Calendar.from_ical(ics_text)
    except Exception:
        return []

    spans: List[Tuple[date, date]] = []

    def to_date(v) -> Optional[date]:
        try:
            if hasattr(v, "dt"):
                # could be datetime or date
                return v.dt.date() if isinstance(v.dt, datetime) else v.dt
            return v.date() if isinstance(v, datetime) else v
        except Exception:
            return None

    for comp in cal.walk():
        if getattr(comp, "name", None) != "VEVENT":
            continue
        ds = comp.get("DTSTART"); de = comp.get("DTEND")
        if not ds or not de:
            continue
        ci = to_date(ds); co = to_date(de)
        if isinstance(ci, date) and isinstance(co, date):
            spans.append((ci, co))
    return spans

def build_schedule(days: int, start: Optional[date] = None) -> Dict[date, List[Dict]]:
    """
    Returns { day_date: [ {flat, nick, colour, in:bool, out:bool}, ... ] }
    """
    flats = load_flats()
    if start is None:
        start = datetime.utcnow().date()
    end = start + timedelta(days=days - 1)

    schedule: Dict[date, List[Dict]] = {}
    for flat_name, meta in flats.items():
        spans = parse_bookings(fetch_ics(meta["url"]))
        per_day: Dict[date, Dict[str, bool]] = {}
        for (ci, co) in spans:
            # check-in day
            if start <= ci <= end:
                per_day.setdefault(ci, {"in": False, "out": False})
                per_day[ci]["in"] = True
            # check-out day (DTEND)
            if start <= co <= end:
                per_day.setdefault(co, {"in": False, "out": False})
                per_day[co]["out"] = True

        for d, flags in per_day.items():
            schedule.setdefault(d, [])
            schedule[d].append({
                "flat": flat_name,
                "nick": meta["nick"],
                "colour": meta["colour"],
                "in": bool(flags.get("in")),
                "out": bool(flags.get("out")),
            })

    # sort days + items
    schedule = dict(sorted(schedule.items(), key=lambda kv: kv[0]))
    for day in list(schedule.keys()):
        # show check-out rows first, then by flat name
        schedule[day].sort(key=lambda it: (not it["out"], it["flat"].lower()))
    return schedule

# ---------------------------
# HTML
# ---------------------------
BASE_CSS = f"""
<style>
  :root {{
    --red:#d32f2f; --green:#2e7d32; --muted:#6b7280;
    --card:#ffffff; --bg:#f7f7f8; --chip:#eef2ff;
  }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Arial; margin:24px; background:var(--bg); color:#111; }}
  h1 {{ margin:0 0 8px }}
  .legend {{ color:var(--muted); margin-bottom:16px }}
  .day {{ background:var(--card); border:1px solid #eee; border-radius:14px; padding:16px; margin:16px 0; box-shadow:0 2px 6px rgba(0,0,0,.04); }}
  .day h2 {{ margin:0 0 10px; display:flex; align-items:center; gap:10px }}
  .today {{ background:#111; color:#fff; font-size:12px; padding:3px 8px; border-radius:999px }}
  .row {{ display:flex; align-items:center; gap:12px; padding:10px 0; border-top:1px dashed #eee }}
  .row:first-of-type {{ border-top:none }}
  .pill {{ display:inline-flex; align-items:center; gap:8px; padding:4px 10px; border-radius:999px; font-weight:700; background:var(--chip) }}
  .dot {{ width:8px; height:8px; border-radius:999px; display:inline-block }}
  .status-out {{ color:var(--red); font-weight:800 }}
  .status-in {{ color:var(--green); font-weight:800 }}
  .turn {{ background:#fff3cd; border:1px solid #ffe08a; color:#6b5b00; padding:2px 8px; border-radius:999px; font-weight:800; font-size:12px }}
  .note {{ color:#666; }}
  .btn {{ margin-left:auto; background:#1976d2; color:#fff; text-decoration:none; padding:8px 12px; border-radius:10px; font-weight:700 }}
</style>
"""

def html_page(body: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cleaner Schedule</title>{BASE_CSS}</head>
<body>
  <h1>Cleaner Schedule</h1>
  <div class="legend">Check-out in <span style="color:#d32f2f;font-weight:800">red</span> • Check-in in <span style="color:#2e7d32;font-weight:800">green</span> • Same-day turnover highlighted • Clean {CLEAN_START}–{CLEAN_END}</div>
  {body}
</body></html>"""

def render_schedule(sched: Dict[date, List[Dict]], days: int) -> str:
    if not sched:
        longer = max(days, 30)
        return f'<p>No activity found. Try a longer window: <a href="/cleaner?days={longer}">/cleaner?days={longer}</a> or see <a href="/debug">/debug</a>.</p>'
    today = datetime.utcnow().date()
    parts: List[str] = []
    for d, items in sched.items():
        heading = d.strftime("%a %d %b")
        today_badge = ' <span class="today">TODAY</span>' if d == today else ""
        parts.append(f'<div class="day"><h2>{heading}{today_badge}</h2>')
        # show rows
        any_checkout = any(it["out"] for it in items)
        for it in items:
            chip = f'<span class="pill"><span class="dot" style="background:{it["colour"]}"></span>{it["nick"]}</span>'
            same_day = it["out"] and it["in"]
            status = []
            if it["out"]:
                status.append('<span class="status-out">Check-out</span>')
            if it["in"] and not it["out"]:
                status.append('<span class="status-in">Check-in</span>')
            turn = ' <span class="turn">Check-out → Clean → Check-in (same-day)</span>' if same_day else ""
            clean = f'<span class="note">🧹 Clean between <b>{CLEAN_START}–{CLEAN_END}</b></span>' if it["out"] else ""
            upload_href = f'/upload?flat={it["flat"].replace(" ", "%20")}&date={d.isoformat()}'
            btn = f'<a class="btn" href="{upload_href}">📷 Upload Photos</a>'
            row = f'<div class="row">{chip} {" ".join(status)}{turn} {clean} {btn}</div>'
            parts.append(row)
        parts.append("</div>")
    return "\n".join(parts)

# ---------------------------
# WhatsApp helper
# ---------------------------
def _wa_fmt(num: str) -> str:
    if not num:
        return ""
    return num if num.startswith("whatsapp:") else f"whatsapp:{num}"

def send_whatsapp(body: str, media_urls: Optional[List[str]] = None) -> None:
    """
    Sends a WhatsApp message via Twilio. Uses media_urls if absolute (http/https).
    Silently skips if Twilio not configured or send fails.
    """
    if not (_twilio_ok and twilio_client):
        return
    from_num = _wa_fmt(TWILIO_WHATSAPP_FROM)
    to_num = _wa_fmt(TWILIO_WHATSAPP_TO)
    try:
        kwargs = {"from_": from_num, "to": to_num, "body": body}
        if media_urls:
            # Only include media if they’re absolute URLs (Twilio requires public URLs)
            abs_urls = [u for u in media_urls if u.startswith("http://") or u.startswith("https://")]
            if abs_urls:
                kwargs["media_url"] = abs_urls
        twilio_client.messages.create(**kwargs)
    except Exception:
        pass  # don't crash the app on Twilio errors

# ---------------------------
# Routes
# ---------------------------
@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"

@app.get("/cleaner", response_class=HTMLResponse)
def cleaner(days: int = DEFAULT_DAYS):
    schedule = build_schedule(days)
    return HTMLResponse(html_page(render_schedule(schedule, days)))

@app.get("/debug", response_class=PlainTextResponse)
def debug():
    flats = load_flats()
    lines = ["Loaded flats:"]
    for name, meta in flats.items():
        lines.append(f"  {name}: url={'SET' if meta['url'] else 'MISSING'} nick={meta['nick']} colour={meta['colour']}")
    schedule = build_schedule(14)
    lines.append("")
    for flat in flats.keys():
        tot = inn = outn = 0
        for d, items in schedule.items():
            for it in items:
                if it["flat"] == flat:
                    tot += 1
                    if it["in"]: inn += 1
                    if it["out"]: outn += 1
        lines.append(f"{flat}: total={tot} (in={inn}, out={outn})")
    lines.append(f"\nDays with activity in next 14 days: {len(schedule)}")
    return "\n".join(lines)

# Serve uploaded media so Twilio can fetch
@app.get("/m/{fname}")
def serve_media(fname: str):
    path = os.path.join(UPLOAD_DIR, fname)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Not found")
    # simple type sniff
    mt = "image/jpeg"
    lf = fname.lower()
    if lf.endswith(".png"): mt = "image/png"
    if lf.endswith(".webp"): mt = "image/webp"
    return FileResponse(path, media_type=mt)

# Upload flow: GET form + POST handler
def _upload_form(flat: str, the_date: str, msg: str = "") -> str:
    note = f'<p style="color:#2e7d32;font-weight:700">{msg}</p>' if msg else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Upload</title>{BASE_CSS}</head>
<body>
  <h1>Upload Photos</h1>
  {note}
  <div class="day">
    <h2>{flat} — {the_date}</h2>
    <form action="/upload" method="post" enctype="multipart/form-data" style="display:grid;gap:10px">
      <input type="hidden" name="flat" value="{flat}">
      <input type="hidden" name="date" value="{the_date}">
      <label>Photos (you can select multiple)</label>
      <input type="file" name="photos" multiple accept="image/*">
      <label>Notes (optional)</label>
      <textarea name="notes" style="min-height:90px"></textarea>
      <div>
        <button type="submit" style="background:#1976d2;color:#fff;border:0;border-radius:10px;padding:10px 14px;font-weight:700">Send</button>
        <a href="/cleaner" style="margin-left:8px">Back</a>
      </div>
    </form>
  </div>
</body></html>"""

@app.get("/upload", response_class=HTMLResponse)
def upload_form(flat: str, date: str):
    return HTMLResponse(_upload_form(flat, date))

@app.post("/upload")
async def upload_submit(
    request: Request,
    flat: str = Form(...),
    date: str = Form(...),
    notes: str = Form(""),
    photos: List[UploadFile] = File(default_factory=list),
):
    # Save files with UUID filenames
    saved_urls: List[str] = []
    for f in photos or []:
        try:
            ext = ".jpg"
            lf = (f.filename or "").lower()
            if lf.endswith(".png"): ext = ".png"
            elif lf.endswith(".webp"): ext = ".webp"
            fname = f"{uuid.uuid4().hex}{ext}"
            dest = os.path.join(UPLOAD_DIR, fname)
            with open(dest, "wb") as w:
                w.write(await f.read())
            # build public URL for Twilio media
            base = PUBLIC_BASE_URL
            if not base:
                # derive from request if env not set
                base = f"{request.url.scheme}://{request.url.netloc}"
            saved_urls.append(f"{base}/m/{fname}")
        except Exception:
            continue  # ignore file errors, still send text

    # Build WhatsApp body
    body_lines = [
        "🧹 Cleaning update",
        f"Flat: {flat}",
        f"Date: {date}",
        f"Photos: {len(saved_urls)}",
    ]
    if notes.strip():
        body_lines.append(f"Notes: {notes.strip()}")
    send_whatsapp("\n".join(body_lines), media_urls=saved_urls)

    # Redirect back to cleaner view with a simple success
    return RedirectResponse(url="/cleaner", status_code=303)

# ---------------------------
# Local run
# ---------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
