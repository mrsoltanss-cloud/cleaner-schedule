import os
import io
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta, date

import requests
from icalendar import Calendar
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from twilio.rest import Client

# ------------------------------------------------------------------------------
# Config & helpers
# ------------------------------------------------------------------------------

APP_TZ = os.getenv("APP_TZ", "Europe/London")

# Twilio
TW_SID  = os.getenv("TWILIO_ACCOUNT_SID", "")
TW_AUTH = os.getenv("TWILIO_AUTH_TOKEN", "")
TW_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")  # "whatsapp:+447490016919"
TW_TO   = os.getenv("TWILIO_WHATSAPP_TO", "")    # "whatsapp:+447480001112"
twilio_client = Client(TW_SID, TW_AUTH) if TW_SID and TW_AUTH else None

# Where to store uploaded images (Render ephemeral FS is fine for simple use)
MEDIA_DIR = os.getenv("MEDIA_DIR", "uploads")
os.makedirs(MEDIA_DIR, exist_ok=True)

# Serve /media/* publicly so Twilio can fetch the images
app = FastAPI()
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")


# A small colour palette to fall back to (used if FLATx_COLOUR isn’t defined)
PALETTE = [
    "#FF8A00",  # orange
    "#2196F3",  # blue
    "#4CAF50",  # green
    "#9C27B0",  # purple
    "#E91E63",  # pink
    "#795548",  # brown
    "#607D8B",  # blue grey
]


# ------------------------------------------------------------------------------
# Flats configuration
# We support either the legacy style (explicit FLAT7_ICS_URL, FLAT8_ICS_URL…)
# or the numbered style (FLAT1_ICS_URL, FLAT2_ICS_URL … up to FLAT50).
# Each flat can have NAME (display), NICK (badge), and COLOUR (hex).
# ------------------------------------------------------------------------------

def load_flats(max_flats: int = 50) -> Dict[str, Dict[str, str]]:
    """
    Returns dict keyed by display name, e.g.
       {"Flat 7": {"url": "...", "nick": "Orange", "colour": "#ff8a00"}, ...}
    """
    flats: Dict[str, Dict[str, str]] = {}
    palette_i = 0

    # New style (1..50)
    for n in range(1, max_flats + 1):
        url = os.getenv(f"FLAT{n}_ICS_URL", "").strip()
        if not url:
            continue
        name   = os.getenv(f"FLAT{n}_NAME", f"Flat {n}").strip()
        nick   = os.getenv(f"FLAT{n}_NICK", name).strip()
        colour = os.getenv(f"FLAT{n}_COLOUR", PALETTE[palette_i % len(PALETTE)]).strip()
        palette_i += 1
        flats[name] = {"url": url, "nick": nick, "colour": colour}

    # Legacy explicit (e.g., FLAT7_ICS_URL)
    # If they exist and not already included above, add them too.
    for n in [7, 8, 9, 10, 11, 12]:
        url = os.getenv(f"FLAT{n}_ICS_URL", "").strip()
        if not url:
            continue
        name   = os.getenv(f"FLAT{n}_NAME", f"Flat {n}").strip()
        if name in flats:
            continue
        nick   = os.getenv(f"FLAT{n}_NICK", name).strip()
        colour = os.getenv(f"FLAT{n}_COLOUR", PALETTE[palette_i % len(PALETTE)]).strip()
        palette_i += 1
        flats[name] = {"url": url, "nick": nick, "colour": colour}

    return flats


# ------------------------------------------------------------------------------
# ICS parsing
# ------------------------------------------------------------------------------

def fetch_ics(url: str) -> str:
    if not url:
        return ""
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text

def _to_date(v) -> date:
    # ICS DTSTART/DTEND can be date or datetime; normalise to date
    if isinstance(v, datetime):
        return v.date()
    return v

def parse_bookings(ics_map: Dict[str, str]) -> Dict[str, List[Tuple[date, str]]]:
    """
    Build a list of (day, "FlatName: in/out") entries.

    Logic:
      - Check-in on DTSTART => status "in" (guest arrives)
      - Check-out on DTEND => status "out" (guest leaves)
    """
    items: List[Tuple[date, str]] = []

    for flat_name, ics_text in ics_map.items():
        if not ics_text:
            continue
        try:
            cal = Calendar.from_ical(ics_text)
        except Exception:
            continue

        for comp in cal.walk():
            if comp.name != "VEVENT":
                continue
            dtstart = comp.get("DTSTART")
            dtend   = comp.get("DTEND")

            # Extract their values
            dstart = _to_date(dtstart.dt) if dtstart else None
            dend   = _to_date(dtend.dt)   if dtend   else None

            # Check-in on start day
            if dstart:
                items.append((dstart, f"{flat_name}: in"))

            # Check-out on end day
            if dend:
                items.append((dend, f"{flat_name}: out"))

    # Sort and group into dict keyed by day (isoformat)
    by_day: Dict[str, List[Tuple[date, str]]] = {}
    for d, label in sorted(items, key=lambda x: (x[0], x[1])):
        by_day.setdefault(d.isoformat(), []).append((d, label))
    return by_day


def build_schedule(days: int) -> Tuple[Dict[str, List[Tuple[date, str]]], Dict[str, Dict[str, str]]]:
    flats = load_flats()
    ics_map = {name: fetch_ics(info["url"]) for name, info in flats.items()}
    bookings = parse_bookings(ics_map)

    # Only keep next N days
    start = date.today()
    end   = start + timedelta(days=days)
    filtered: Dict[str, List[Tuple[date, str]]] = {}
    for k, vals in bookings.items():
        d = datetime.fromisoformat(k).date()
        if start <= d < end:
            filtered[k] = vals
    return filtered, flats


# ------------------------------------------------------------------------------
# Views (HTML)
# ------------------------------------------------------------------------------

def html_header() -> str:
    return f"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Cleaner Schedule</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {{
      --red: #e53935;
      --green: #2e7d32;
      --muted: #666;
      --card: #fff;
      --bg: #f7f7f8;
      --badge: #111;
    }}
    body {{
      margin: 0; padding: 0; background: var(--bg);
      font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, Arial, sans-serif;
      color: #111;
    }}
    .wrap {{ max-width: 900px; margin: 0 auto; padding: 24px; }}
    h1 {{ margin: 0 0 6px; font-size: 28px; }}
    .sub {{ color: var(--muted); font-size: 14px; margin-bottom: 20px; }}
    .day {{ background: var(--card); border-radius: 14px; box-shadow: 0 1px 2px rgba(0,0,0,.06);
            padding: 18px; margin: 18px 0; }}
    .day h2 {{ font-size: 20px; margin: 0 0 12px; display:flex; align-items:center; gap:10px; }}
    .today {{ background: #111; color:#fff; border-radius: 999px; padding: 2px 10px; font-size: 12px; }}
    .row {{ display:flex; align-items:center; gap:14px; margin: 14px 0; flex-wrap: wrap; }}
    .nick {{ font-weight: 700; color:#fff; padding: 2px 10px; border-radius: 999px; display:inline-block; }}
    .status {{ font-weight: 700; }}
    .out {{ color: var(--red); }}
    .in {{ color: var(--green); }}
    .muted {{ color: var(--muted); font-size: 14px; }}
    .btn {{ appearance:none; border:0; padding:8px 10px; border-radius:10px; cursor:pointer;
            background:#1f6feb; color:#fff; font-weight:600; text-decoration:none; display:inline-flex; gap:8px; align-items:center; }}
    .btn:disabled {{ opacity: .5; cursor: not-allowed; }}
    .note {{ color:#111; font-weight:600; }}
  </style>
</head>
<body>
<div class="wrap">
  <h1>Cleaner Schedule</h1>
  <div class="sub">Check-out in <span style="color:var(--red);font-weight:700;">red</span> •
  Check-in in <span style="color:var(--green);font-weight:700;">green</span> •
  Same-day turnover highlighted • Clean <b>{os.getenv("CLEAN_WINDOW","10:00–16:00")}</b></div>
"""

def html_footer() -> str:
    return "</div></body></html>"

def render_day(d: date, entries: List[str], flats: Dict[str, Dict[str, str]]) -> str:
    # Determine if same-day turnover exists
    has_out = any(e.endswith(": out") for e in entries)
    has_in  = any(e.endswith(": in")  for e in entries)
    turnover = has_out and has_in

    # Build rows
    rows: List[str] = []
    for e in entries:
        # label like "Flat 7: out"
        flat_name, status = e.split(": ")
        info = flats.get(flat_name, {"nick": flat_name, "colour": "#111"})
        nick = info["nick"]
        colour = info["colour"]

        # Upload link for this day/flat
        href = f"/upload?flat={flat_name}&date={d.isoformat()}"

        rows.append(f"""
        <div class="row">
          <span class="nick" style="background:{colour};">{nick}</span>
          <span class="status {'out' if status=='out' else 'in'}">{'Check-out' if status=='out' else 'Check-in'}</span>
          <div class="muted">🧹 Clean between <b>{os.getenv("CLEAN_WINDOW","10:00–16:00")}</b></div>
          <a class="btn" href="{href}">📷 Upload Photos</a>
        </div>
        """)

    today_badge = '<span class="today">TODAY</span>' if d == date.today() else ""
    turnover_badge = ' <span class="note">— Same-day turnover</span>' if turnover else ""
    return f"""
    <section class="day">
      <h2>{d.strftime('%a %d %b')} {today_badge}{turnover_badge}</h2>
      {''.join(rows)}
    </section>
    """


# ------------------------------------------------------------------------------
# Minimal WhatsApp send helper
# ------------------------------------------------------------------------------

def send_whatsapp_message(body: str, media_urls: Optional[List[str]] = None, to: Optional[str] = None):
    if not twilio_client or not TW_FROM:
        print("⚠️ Twilio not configured; skipping send.")
        return
    kwargs = dict(
        from_=TW_FROM,
        to=to or TW_TO or TW_FROM,  # fallback so it never crashes
        body=body
    )
    if media_urls:
        # Twilio accepts up to 10 media URLs
        kwargs["media_url"] = media_urls[:10]
    try:
        msg = twilio_client.messages.create(**kwargs)
        print("📤 WhatsApp sent:", msg.sid)
    except Exception as e:
        print("❗ WhatsApp send failed:", e)


# ------------------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------------------

@app.get("/", response_class=PlainTextResponse)
def root():
    return "Hello! The app is running."

@app.get("/debug", response_class=PlainTextResponse)
def debug():
    sched, flats = build_schedule(days=14)
    lines = ["Loaded flats:"]
    for k, v in flats.items():
        lines.append(f"  {k}: url={'SET' if v['url'] else '—'} nick={v['nick']} colour={v['colour']}")
    lines.append("")
    total_days = len(sched)
    lines.append(f"Days with activity in next 14 days: {total_days}")
    return "\n".join(lines)

@app.get("/cleaner", response_class=HTMLResponse)
def cleaner(days: int = 14):
    sched, flats = build_schedule(days=days)
    html = [html_header()]
    # If nothing found
    if not sched:
        html.append(f'<p>No activity found. Try a longer window: <a href="/cleaner?days=30">/cleaner?days=30</a> or see <a href="/debug">/debug</a>.</p>')
        html.append(html_footer())
        return "".join(html)

    # Render in date order
    for k in sorted(sched.keys()):
        d = datetime.fromisoformat(k).date()
        entries_labels = [lbl for _, lbl in sched[k]]
        html.append(render_day(d, entries_labels, flats))
    html.append(html_footer())
    return "".join(html)


# ----------------------- Upload Photos + Notes -------------------------------

UPLOAD_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Upload Photos</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body{font:16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, Arial, sans-serif;padding:24px;}
    .card{max-width:640px;margin:0 auto;background:#fff;border-radius:14px;padding:18px;
          box-shadow:0 1px 2px rgba(0,0,0,.06);}
    label{font-weight:700;display:block;margin-top:14px;}
    input[type=file], textarea{width:100%;margin-top:6px;}
    textarea{min-height:120px;resize:vertical;}
    .btn{margin-top:16px;appearance:none;border:0;background:#1f6feb;color:#fff;
         padding:10px 12px;border-radius:10px;font-weight:700;cursor:pointer;}
    .muted{color:#666}
  </style>
</head>
<body>
  <div class="card">
    <h2>Upload Photos</h2>
    <p class="muted">Flat: <b>{flat}</b> • Date: <b>{dt}</b></p>

    <form action="/upload" method="post" enctype="multipart/form-data">
      <input type="hidden" name="flat" value="{flat}">
      <input type="hidden" name="dt" value="{dt}">
      <label>Photos (you can select multiple)</label>
      <input type="file" name="photos" multiple accept="image/*" required>

      <label>Notes for host (optional)</label>
      <textarea name="notes" placeholder="Anything we should know (damage, missing items, extra time, etc.)"></textarea>

      <button class="btn" type="submit">Send</button>
    </form>
  </div>
</body>
</html>
"""

@app.get("/upload", response_class=HTMLResponse)
def upload_form(flat: str, date: str):
    # Query param `date` comes as 'YYYY-MM-DD'
    return UPLOAD_HTML.format(flat=flat, dt=date)

@app.post("/upload", response_class=HTMLResponse)
async def upload_submit(
    flat: str = Form(...),
    dt: str = Form(...),
    notes: str = Form(""),
    photos: List[UploadFile] = File(default_factory=list)
):
    # Save files and build public URLs
    urls: List[str] = []
    for f in photos:
        if not f.filename:
            continue
        # Simple unique filename
        safe_name = f"{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{f.filename.replace(' ','_')}"
        path = os.path.join(MEDIA_DIR, safe_name)
        data = await f.read()
        with open(path, "wb") as out:
            out.write(data)
        urls.append(f"{os.getenv('PUBLIC_BASE_URL','https://cleaner-schedule.onrender.com')}/media/{safe_name}")

    # Build WhatsApp message
    note_line = f"\nNotes: {notes.strip()}" if notes.strip() else ""
    body = f"🧽 Cleaning complete\nFlat: {flat}\nDate: {dt}\nPhotos: {len(urls)}{note_line}"

    # Send to host
    send_whatsapp_message(body, media_urls=urls, to=TW_TO)

    # Return a simple confirmation page
    back = f"/cleaner?days=14"
    return f"""
    <html><body style="font-family:Inter, system-ui, -apple-system, Segoe UI, Roboto, Arial;">
      <div style="max-width:620px;margin:40px auto;">
        <h2>Thank you!</h2>
        <p>Your photos and notes were sent to the host via WhatsApp.</p>
        <p><a href="{back}">← Back to schedule</a></p>
      </div>
    </body></html>
    """


# --------------------------- Twilio webhooks ----------------------------------

@app.post("/twilio/inbound")
async def twilio_inbound(
    request: Request,
    From: str = Form(...),
    Body: str = Form(""),
    NumMedia: str = Form("0")
):
    """Receive inbound WhatsApp messages (text or photos)."""
    form = await request.form()
    media_count = int(NumMedia or "0")
    media_urls = [form.get(f"MediaUrl{i}") for i in range(media_count)]

    print("📩 [TWILIO INBOUND]", {"From": From, "Body": Body, "Media": media_urls})

    # (Optional) auto-reply so you see it works
    if twilio_client and TW_FROM:
        try:
            twilio_client.messages.create(
                from_=TW_FROM,
                to=From,
                body=f"✅ Got it! {('(+photo)' if media_count else '')} {Body}".strip()
            )
        except Exception as e:
            print("❗ Auto-reply failed:", e)

    return PlainTextResponse("OK", status_code=200)


@app.post("/twilio/status")
async def twilio_status(request: Request):
    data = await request.form()
    print("📡 [TWILIO STATUS]", dict(data))
    return PlainTextResponse("OK", status_code=200)


# ------------------------------------------------------------------------------
# Local dev runner
# ------------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
