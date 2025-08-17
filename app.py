# app.py
import os
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple, Optional

import requests
from icalendar import Calendar
from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, PlainTextResponse
from twilio.rest import Client

# ---------------------------------------
# Settings
# ---------------------------------------
TIMEZONE = os.getenv("TIMEZONE", "Europe/London")
DEFAULT_DAYS = int(os.getenv("DEFAULT_DAYS", "14"))
CLEAN_START = "10:00"
CLEAN_END = "16:00"

# Twilio (optional – text only unless you host photos publicly)
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "").strip()  # e.g. whatsapp:+447490016919
TWILIO_WHATSAPP_TO = os.getenv("TWILIO_WHATSAPP_TO", "").strip()      # e.g. whatsapp:+447480001112

twilio_client: Optional[Client] = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and TWILIO_WHATSAPP_TO:
    try:
        twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    except Exception:
        twilio_client = None  # keep app running even if Twilio creds are wrong

app = FastAPI(title="Cleaner Schedule")


# ---------------------------------------
# Utilities: flats, ICS fetch/parse, schedule build
# ---------------------------------------
PALETTE = [
    "#FF9800",  # Orange
    "#2196F3",  # Blue
    "#4CAF50",  # Green
    "#9C27B0",  # Purple
    "#795548",  # Brown
    "#009688",  # Teal
]

def load_flats(max_flats: int = 50) -> Dict[str, Dict[str, str]]:
    """
    Read flat definitions from env.
    Supports FLAT7_*, FLAT8_*, FLAT9_* ... up to FLAT50_* if provided.

    Returns dict keyed by display name, e.g.
    { "Flat 7": {"url": "...", "nick": "Orange", "colour": "#FF9800"}, ... }
    """
    flats: Dict[str, Dict[str, str]] = {}
    palette_i = 0

    for n in range(1, max_flats + 1):
        url = os.getenv(f"FLAT{n}_ICS_URL", "").strip()
        if not url:
            continue
        name = os.getenv(f"FLAT{n}_NAME", f"Flat {n}").strip()
        nick = os.getenv(f"FLAT{n}_NICK", name).strip()
        colour = os.getenv(f"FLAT{n}_COLOUR", PALETTE[palette_i % len(PALETTE)]).strip()
        palette_i += 1
        flats[name] = {"url": url, "nick": nick, "colour": colour}

    # Legacy exact names (7/8/9) still work because the loop covers them.
    return flats


def fetch_ics(url: str) -> str:
    """Fetch ICS safely. Never raise—return '' on any problem."""
    if not url:
        return ""
    try:
        r = requests.get(url, timeout=8)
        if r.status_code >= 400 or not r.text:
            return ""
        return r.text
    except Exception:
        return ""


def parse_bookings(ics_text: str) -> List[Tuple[date, date]]:
    """
    Return list of (checkin_date, checkout_date).
    We treat DTEND.date() as the checkout day so SAME-DAY turnover is detected cleanly.
    """
    if not ics_text.strip():
        return []
    try:
        g = Calendar.from_ical(ics_text)
    except Exception:
        return []

    def to_date(v) -> Optional[date]:
        try:
            if hasattr(v, "dt"):
                if isinstance(v.dt, datetime):
                    return v.dt.date()
                return v.dt
            if isinstance(v, datetime):
                return v.date()
            return v
        except Exception:
            return None

    spans: List[Tuple[date, date]] = []
    for comp in g.walk():
        if getattr(comp, "name", None) != "VEVENT":
            continue
        ds = comp.get("DTSTART")
        de = comp.get("DTEND")
        if not ds or not de:
            continue
        ci = to_date(ds)
        co = to_date(de)
        if isinstance(ci, date) and isinstance(co, date):
            spans.append((ci, co))
    return spans


def daterange(start: date, days: int) -> List[date]:
    return [start + timedelta(days=i) for i in range(days)]


def build_day_map(flats: Dict[str, Dict[str, str]], days: int) -> Dict[date, Dict[str, Dict[str, bool]]]:
    """
    Returns:
    {
      day: {
        "Flat 7": {"in": True/False, "out": True/False},
        ...
      },
      ...
    }
    """
    today = date.today()
    day_map: Dict[date, Dict[str, Dict[str, bool]]] = {d: {} for d in daterange(today, days)}

    for flat_name, meta in flats.items():
        spans = parse_bookings(fetch_ics(meta["url"]))
        # Build per-day flags for this flat
        flags_per_day: Dict[date, Dict[str, bool]] = {}
        for ci, co in spans:
            # Checkin on ci
            flags_per_day.setdefault(ci, {"in": False, "out": False})
            flags_per_day[ci]["in"] = True
            # Checkout on co
            flags_per_day.setdefault(co, {"in": False, "out": False})
            flags_per_day[co]["out"] = True

        # Merge into global day_map (limited to requested window)
        for d in day_map.keys():
            if d in flags_per_day:
                day_map[d].setdefault(flat_name, {"in": False, "out": False})
                day_map[d][flat_name]["in"] = flags_per_day[d]["in"]
                day_map[d][flat_name]["out"] = flags_per_day[d]["out"]

    return day_map


# ---------------------------------------
# WhatsApp helper
# ---------------------------------------
def send_whatsapp_message(text: str) -> None:
    """
    Sends a WhatsApp text message via Twilio. Silently ignores on failure or if not configured.
    NOTE: To send photos, you must provide publicly accessible media URLs.
    """
    if not twilio_client:
        return
    try:
        twilio_client.messages.create(
            from_=f"whatsapp:{TWILIO_WHATSAPP_FROM.replace('whatsapp:', '')}",
            to=f"whatsapp:{TWILIO_WHATSAPP_TO.replace('whatsapp:', '')}",
            body=text,
        )
    except Exception:
        # Do not crash the web app because of Twilio
        pass


# ---------------------------------------
# HTML bits
# ---------------------------------------
BASE_CSS = """
<style>
  :root { --bg:#fafbfc; --card:#ffffff; --text:#1f2937; --muted:#6b7280; }
  *{ box-sizing:border-box; }
  body{ margin:0; font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; background:var(--bg); color:var(--text); }
  .wrap{ max-width:1000px; margin:24px auto; padding:0 16px; }
  h1{ font-size:32px; margin:0 0 8px; }
  .sub{ color:var(--muted); margin-bottom:16px; }
  .legend-dot{ display:inline-block; width:8px; height:8px; border-radius:999px; vertical-align:middle; margin:0 6px 0 12px; }
  .today-badge{ background:#111827; color:#fff; font-size:12px; padding:2px 8px; border-radius:999px; margin-left:8px; }
  .day{ background:var(--card); border-radius:14px; padding:12px 16px; margin:16px 0; box-shadow: 0 1px 2px rgba(0,0,0,0.04);}
  .day h2{ font-size:18px; margin:0 0 12px; display:flex; align-items:center; gap:8px; }
  .row{ display:flex; align-items:center; gap:12px; padding:10px 0; border-top:1px solid #f0f2f5; }
  .row:first-child{ border-top:none; }
  .pill{ display:inline-flex; align-items:center; gap:8px; padding:4px 10px; border-radius:999px; font-weight:600; font-size:14px; background:#f3f4f6; color:#111827; }
  .flat-pill{ border:1px solid rgba(0,0,0,0.08); }
  .status-out{ color:#ef4444; }
  .status-in{ color:#10b981; }
  .same{ background:#fff7ed; color:#c2410c; border:1px solid #fed7aa; }
  .muted{ color:var(--muted); font-size:14px; }
  .muted b{ color:#111827; }
  .btn{ margin-left:auto; padding:8px 12px; background:#2563eb; color:#fff; text-decoration:none; border-radius:10px; font-weight:600; display:inline-flex; align-items:center; gap:8px; }
  .btn:hover{ background:#1d4ed8; }
  .cam{ width:16px; height:16px; display:inline-block; background:#fff; border-radius:3px; position:relative; }
  .cam:before{ content:""; position:absolute; width:4px; height:4px; background:#1f2937; border-radius:50%; top:3px; right:3px; }
</style>
"""

def html_page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
{BASE_CSS}
<body>
  <div class="wrap">
    <h1>{title}</h1>
    {body}
  </div>
</body>
</html>
"""


def render_cleaner(flats: Dict[str, Dict[str, str]], day_map: Dict[date, Dict[str, Dict[str, bool]]], days: int) -> str:
    lines: List[str] = []
    lines.append('<div class="sub">Check-out in <span style="color:#ef4444">red</span> • Check-in in <span style="color:#10b981">green</span> • Same-day turnover highlighted • Clean '
                 f'{CLEAN_START}–{CLEAN_END}</div>')

    any_rows = False
    today = date.today()

    for d in daterange(today, days):
        entries = day_map.get(d, {})
        if not entries:
            continue
        # Order by flat display name for consistency
        ordered = sorted(entries.items(), key=lambda kv: kv[0])

        # Only show the day block if at least one flat has activity (in or out)
        to_show = [(fname, flags) for fname, flags in ordered if flags.get("in") or flags.get("out")]
        if not to_show:
            continue
        any_rows = True

        is_today = (d == today)
        heading = d.strftime("%a %d %b")
        lines.append('<div class="day">')
        lines.append(f'<h2>{heading}{" <span class=\\"today-badge\\">TODAY</span>" if is_today else ""}</h2>')

        for flat_name, flags in to_show:
            meta = flats.get(flat_name, {"nick": flat_name, "colour": "#f3f4f6"})
            nick = meta["nick"]
            colour = meta["colour"]
            has_out = bool(flags.get("out"))
            has_in = bool(flags.get("in"))
            same_day = has_out and has_in

            # Pill for flat (colour-coded)
            flat_pill = f'<span class="pill flat-pill" style="background:{colour}22;border-color:{colour}55;"><span class="legend-dot" style="background:{colour}"></span>{nick}</span>'

            # Status label(s)
            status_bits: List[str] = []
            if has_out:
                status_bits.append('<span class="pill status-out">Check-out</span>')
            if has_in and not has_out:
                status_bits.append('<span class="pill status-in">Check-in</span>')

            # Same-day turnover badge
            same_badge = ''
            if same_day:
                same_badge = '<span class="pill same">Check-out → Clean → Check-in (same-day)</span>'

            # Clean window (only on checkout or same-day)
            clean_line = ''
            if has_out:
                clean_line = f'<span class="muted">🧹 Clean between <b>{CLEAN_START}–{CLEAN_END}</b></span>'

            # Upload button (always show so cleaners can attach proof or notes)
            upload_href = f'/upload?flat={flat_name.replace(" ", "%20")}&date={d.isoformat()}'
            btn = f'<a class="btn" href="{upload_href}"><span class="cam"></span> Upload Photos</a>'

            row_html = f'''
              <div class="row">
                {flat_pill}
                {' '.join(status_bits)}
                {same_badge}
                {clean_line}
                {btn}
              </div>
            '''
            lines.append(row_html)

        lines.append('</div>')  # /day

    if not any_rows:
        lines.append(f'<p>No activity found. Try a longer window: <a href="/cleaner?days={max(days, 30)}">/cleaner?days={max(days, 30)}</a> or see <a href="/debug">/debug</a>.</p>')

    return "".join(lines)


# ---------------------------------------
# Routes
# ---------------------------------------
@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"


@app.get("/cleaner", response_class=HTMLResponse)
def cleaner(request: Request, days: int = DEFAULT_DAYS):
    flats = load_flats()
    day_map = build_day_map(flats, days)
    body = render_cleaner(flats, day_map, days)
    return HTMLResponse(html_page("Cleaner Schedule", body))


@app.get("/debug", response_class=PlainTextResponse)
def debug(days: int = DEFAULT_DAYS):
    flats = load_flats()
    out_lines = []
    out_lines.append("Loaded flats:")
    for name, meta in flats.items():
        out_lines.append(f"  {name}: url={'SET' if bool(meta['url']) else 'MISSING'} nick={meta['nick']} colour={meta['colour']}")
    out_lines.append("")
    day_map = build_day_map(flats, days)
    today = date.today()
    for fname in sorted(flats.keys()):
        total = in_c = out_c = 0
        for d in daterange(today, days):
            flags = day_map.get(d, {}).get(fname, {})
            if flags.get("in") or flags.get("out"):
                total += 1
            if flags.get("in"):
                in_c += 1
            if flags.get("out"):
                out_c += 1
        out_lines.append(f"{fname}: total={total} (in={in_c}, out={out_c})")
    active_days = sum(1 for d in daterange(today, days) if day_map.get(d))
    out_lines.append(f"\nDays with activity in next {days} days: {active_days}")
    return "\n".join(out_lines)


# -------- Upload form + handler --------
UPLOAD_CSS = """
<style>
  .card{ background:#fff; border-radius:14px; padding:16px; box-shadow:0 1px 2px rgba(0,0,0,.05); }
  label{ display:block; margin:10px 0 4px; font-weight:600; }
  input[type=file], textarea{ width:100%; }
  textarea{ min-height:120px; padding:8px; }
  .actions{ margin-top:12px; display:flex; gap:8px; }
  .btnp{ background:#2563eb; color:#fff; border:none; padding:10px 14px; border-radius:10px; font-weight:600; cursor:pointer; }
  .link{ color:#2563eb; text-decoration:none; }
</style>
"""

@app.get("/upload", response_class=HTMLResponse)
def upload_form(flat: str, date: str):
    body = f"""
      {UPLOAD_CSS}
      <div class="card">
        <p><b>Flat:</b> {flat} &nbsp; <b>Date:</b> {date}</p>
        <form action="/upload" method="post" enctype="multipart/form-data">
          <input type="hidden" name="flat" value="{flat}">
          <input type="hidden" name="day" value="{date}">
          <label>Photos (you can select multiple)</label>
          <input type="file" name="photos" multiple accept="image/*">
          <label>Notes for the host</label>
          <textarea name="notes" placeholder="Anything I should know?"></textarea>
          <div class="actions">
            <button class="btnp" type="submit">Send</button>
            <a class="link" href="/cleaner">Back to schedule</a>
          </div>
        </form>
      </div>
    """
    return HTMLResponse(html_page("Upload Photos", body))


@app.post("/upload", response_class=HTMLResponse)
async def handle_upload(
    flat: str = Form(...),
    day: str = Form(...),
    notes: str = Form(""),
    photos: List[UploadFile] = File(default_factory=list),
):
    """
    Saves photos locally (Render ephemeral disk) and sends a WhatsApp text with summary.
    NOTE: Photos are NOT sent to WhatsApp because Twilio requires public URLs.
    To enable media on WhatsApp, upload the images to public storage (e.g. S3) and
    include those URLs in the Twilio message's media_url list.
    """
    # Save files locally (if provided)
    saved_files: List[str] = []
    try:
        folder = f"/mnt/data/uploads/{day}_{flat.replace(' ', '_')}"
        os.makedirs(folder, exist_ok=True)
        for f in photos:
            # basic safe filename
            fname = f.filename or "photo.jpg"
            safe = fname.replace("/", "_").replace("\\", "_")
            path = os.path.join(folder, safe)
            with open(path, "wb") as out:
                out.write(await f.read())
            saved_files.append(path)
    except Exception:
        # ignore file errors – still send the WhatsApp note
        pass

    # WhatsApp text summary
    msg = f"Cleaning complete.\nFlat: {flat}\nDate: {day}\nPhotos: {len(saved_files)}"
    if notes.strip():
        msg += f"\nNotes: {notes.strip()}"

    send_whatsapp_message(msg)

    body = f"""
      {UPLOAD_CSS}
      <div class="card">
        <p>Thanks! Your update has been sent.</p>
        <ul>
          <li><b>Flat:</b> {flat}</li>
          <li><b>Date:</b> {day}</li>
          <li><b>Photos saved:</b> {len(saved_files)}</li>
        </ul>
        <p><a class="link" href="/cleaner">Back to schedule</a></p>
      </div>
    """
    return HTMLResponse(html_page("Upload Complete", body))


# ---------------------------------------
# Local dev runner
# ---------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
