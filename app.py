import os
from typing import Dict, List, Tuple, Optional
from datetime import datetime, timedelta, date
import requests
from icalendar import Calendar
from fastapi import FastAPI, Form, UploadFile, File, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
import uvicorn

# -----------------------------
# Helpers: config & time
# -----------------------------

def get_tzname() -> str:
    return os.getenv("TIMEZONE", "Europe/London")

def today_local() -> date:
    # Keep it simple; Render boxes are UTC. For UK usage this is fine for daily granularity.
    return datetime.utcnow().date()

# -----------------------------
# Flats config (env-driven)
# -----------------------------

PALETTE = [
    "#FF9800", "#2196F3", "#4CAF50", "#9C27B0", "#00BCD4",
    "#E91E63", "#795548", "#3F51B5", "#FF5722", "#607D8B",
]

def load_flats(max_flats: int = 50) -> Dict[str, Dict[str, str]]:
    """
    Returns dict keyed by display name, e.g.:
      {
        "Flat 7": {"url": "...", "nick": "Orange", "colour": "#FF9800"},
        ...
      }
    Supports new style FLAT1_ICS_URL.. or legacy FLAT7_ICS_URL, FLAT8_ICS_URL, FLAT9_ICS_URL.
    """
    flats: Dict[str, Dict[str, str]] = {}
    palette_i = 0

    # New style: FLAT1..FLAT50
    for n in range(1, max_flats + 1):
        url = (os.getenv(f"FLAT{n}_ICS_URL") or "").strip()
        if not url:
            continue
        name = (os.getenv(f"FLAT{n}_NAME") or f"Flat {n}").strip()
        nick = (os.getenv(f"FLAT{n}_NICK") or name).strip()
        colour = (os.getenv(f"FLAT{n}_COLOUR") or PALETTE[palette_i % len(PALETTE)]).strip()
        palette_i += 1
        flats[name] = {"url": url, "nick": nick, "colour": colour}

    # Legacy quick-fill (only if new style produced nothing)
    if not flats:
        legacy = [7, 8, 9]
        for n in legacy:
            url = (os.getenv(f"FLAT{n}_ICS_URL") or "").strip()
            if not url:
                continue
            name = (os.getenv(f"FLAT{n}_NAME") or f"Flat {n}").strip()
            nick = (os.getenv(f"FLAT{n}_NICK") or name).strip()
            colour = (os.getenv(f"FLAT{n}_COLOUR") or PALETTE[palette_i % len(PALETTE)]).strip()
            palette_i += 1
            flats[name] = {"url": url, "nick": nick, "colour": colour}

    return flats

# -----------------------------
# ICS parsing
# -----------------------------

def fetch_ics(url: str) -> str:
    if not url:
        return ""
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text

def parse_bookings(ics_text: str) -> List[Tuple[date, date]]:
    """
    Returns list of (checkin_date, checkout_date) tuples.
    Booking.com style:
      - DTSTART = check-in day (all-day)
      - DTEND   = check-out day (all-day, exclusive)
    We treat checkout day == DTEND.date() (not DTEND-1), so SAME-DAY turnover works naturally.
    """
    if not ics_text.strip():
        return []
    g = Calendar.from_ical(ics_text)
    spans: List[Tuple[date, date]] = []
    for comp in g.walk():
        if comp.name != "VEVENT":
            continue
        dtstart_raw = comp.get("DTSTART")
        dtend_raw = comp.get("DTEND")
        if not dtstart_raw or not dtend_raw:
            continue

        def to_date(v) -> date:
            if hasattr(v, "dt"):
                if isinstance(v.dt, datetime):
                    return v.dt.date()
                return v.dt
            if isinstance(v, datetime):
                return v.date()
            return v

        ci = to_date(dtstart_raw)
        co = to_date(dtend_raw)
        if isinstance(ci, date) and isinstance(co, date):
            spans.append((ci, co))

    return spans

# -----------------------------
# Build schedule structure
# -----------------------------

class DayItem:
    def __init__(self):
        self.out = False
        self.inn = False  # 'in' is reserved keyword in Python
        self.same_day = False

def build_schedule_for_days(
    flats: Dict[str, Dict[str, str]],
    days: int
) -> Dict[date, Dict[str, DayItem]]:
    """
    schedule[day][flat_name] = DayItem()
    """
    start = today_local()
    end = start + timedelta(days=days)
    schedule: Dict[date, Dict[str, DayItem]] = {}

    for flat_name, info in flats.items():
        ics_text = fetch_ics(info["url"])
        spans = parse_bookings(ics_text)
        for (ci, co) in spans:
            # Check-in day:
            if start <= ci < end:
                schedule.setdefault(ci, {})
                item = schedule[ci].setdefault(flat_name, DayItem())
                item.inn = True

            # Checkout day = DTEND.date() (Booking.com all-day)
            co_day = co
            if start <= co_day < end:
                schedule.setdefault(co_day, {})
                item = schedule[co_day].setdefault(flat_name, DayItem())
                item.out = True

    # Mark same-day turnovers
    for d, flats_map in schedule.items():
        for fname, item in flats_map.items():
            if item.out and item.inn:
                item.same_day = True

    return schedule

# -----------------------------
# HTML rendering
# -----------------------------

def badge(text: str, bg: str, color: str = "#fff") -> str:
    return (
        '<span style="display:inline-block;padding:4px 10px;border-radius:9999px;'
        f'font-weight:600;background:{bg};color:{color};margin-right:8px;">{t}</span>'
    ).format(t=text)

def button(text: str, href: str) -> str:
    return (
        f'<a href="{href}" '
        'style="display:inline-flex;align-items:center;gap:8px;padding:8px 12px;'
        'border-radius:10px;background:#1d4ed8;color:#fff;text-decoration:none;'
        'box-shadow:0 2px 6px rgba(0,0,0,.1);font-weight:600;">'
        '<span>📷</span><span>{txt}</span></a>'
    ).format(txt=text)

def cleaner_page_html(schedule: Dict[date, Dict[str, DayItem]],
                      flats: Dict[str, Dict[str, str]],
                      days: int) -> str:
    today = today_local()

    head = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Cleaner Schedule</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body { font-family: -apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Inter,system-ui,Arial,sans-serif;
           background:#f7fafc; color:#111827; margin:0; }
    .container { max-width: 980px; margin: 0 auto; padding: 24px; }
    h1 { font-size: 28px; margin: 0 0 8px 0; }
    .sub { color:#4b5563; margin-bottom: 18px; }
    .card { background:#fff; border-radius:16px; box-shadow: 0 4px 16px rgba(0,0,0,.06);
            padding:18px; margin:16px 0; }
    .row { display:flex; align-items:center; gap:12px; flex-wrap:wrap; padding:10px 0; }
    .flatchip { display:inline-block; padding:6px 10px; border-radius:9999px; color:#fff; font-weight:700; }
    .label { font-weight:700; }
    .note { color:#6b7280; font-size:14px; }
    .meta { font-size:14px; color:#6b7280; margin-top:4px; }
    .daytitle { font-size:18px; font-weight:800; margin:0 0 8px 0; display:flex; align-items:center; gap:10px; }
    .today { background:#111827; color:#fff; padding:3px 8px; border-radius:9999px; font-size:12px; font-weight:800; }
    .muted { color:#6b7280; }
    .sep { height:1px; background:#f1f5f9; margin:8px 0; }
  </style>
</head>
<body>
<div class="container">
  <h1>Cleaner Schedule</h1>
  <div class="sub">Check-out in <span style="color:#dc2626;font-weight:700">red</span> •
    Check-in in <span style="color:#16a34a;font-weight:700">green</span> •
    Same-day turnover highlighted • Clean 10:00–16:00</div>
"""

    # days sorted
    day_keys = sorted(schedule.keys())
    parts: List[str] = [head]

    if not day_keys:
        parts.append('<p>No activity found. Try a longer window: '
                     f'<a href="/cleaner?days={max(days, 30)}">/cleaner?days={max(days, 30)}</a> '
                     'or see <a href="/debug">/debug</a>.</p>')
    else:
        for d in day_keys:
            title = d.strftime("%a %d %b")
            title_html = f'<div class="daytitle">{title}'
            if d == today:
                title_html += ' <span class="today">TODAY</span>'
            title_html += '</div>'

            parts.append(f'<div class="card">{title_html}')

            # flats per day in consistent order: by flat name
            for fname in sorted(schedule[d].keys(), key=lambda n: n.lower()):
                item = schedule[d][fname]
                info = flats.get(fname, {})
                chip = f'<span class="flatchip" style="background:{info.get("colour","#374151")}">{info.get("nick",fname)}</span>'

                # label(s)
                labels: List[str] = []
                if item.out:
                    labels.append('<span class="label" style="color:#dc2626">Check-out</span>')
                if item.inn:
                    labels.append('<span class="label" style="color:#16a34a">Check-in</span>')

                same_day_html = ""
                if item.same_day:
                    same_day_html = badge("SAME-DAY", "#111827")

                # Clean window line: only show if checkout occurs (out) — skip for pure check-in
                clean_line = ""
                if item.out:
                    clean_line = '<div class="meta">🧹 Clean between <strong>10:00–16:00</strong></div>'

                # Upload link
                up_href = f'/upload?flat={fname.replace(" ", "%20")}&date={d.isoformat()}'
                up_btn = button("Upload Photos", up_href)

                row = (
                    '<div class="row">'
                    f'{chip} {same_day_html} {" ".join(labels)}'
                    f'{clean_line}'
                    f'<div style="flex:1"></div>{up_btn}'
                    '</div>'
                )
                parts.append(row)

            parts.append('</div>')  # card

    parts.append('</div></body></html>')
    return "".join(parts)

# -----------------------------
# Upload handling + WhatsApp
# -----------------------------

def send_whatsapp_message(body: str, media_urls: Optional[List[str]] = None) -> None:
    sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    token = os.getenv("TWILIO_AUTH_TOKEN", "")
    from_ = os.getenv("TWILIO_WHATSAPP_FROM", "")
    to_ = os.getenv("TWILIO_WHATSAPP_TO", "")

    if not (sid and token and from_ and to_):
        return  # Silently skip if Twilio isn’t configured

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = {
        "From": f"whatsapp:{from_}",
        "To": f"whatsapp:{to_}",
        "Body": body,
    }

    files = None
    # Twilio WhatsApp requires publicly reachable media URLs.
    # If you have MEDIA_BASE_URL, you could pass URLs instead of raw uploads.
    if media_urls:
        # Twilio supports MediaUrl1, MediaUrl2, ...
        for i, m in enumerate(media_urls, start=1):
            data[f"MediaUrl{i}"] = m

    requests.post(url, data=data, auth=(sid, token), timeout=30)

def upload_form_html(flat: str, d: str, message: str = "") -> str:
    return f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Upload – {flat} – {d}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body {{ font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,Inter,system-ui,Arial,sans-serif;
           background:#f7fafc; color:#111827; margin:0; }}
    .wrap {{ max-width:680px; margin:0 auto; padding:24px; }}
    .card {{ background:#fff;border-radius:16px;box-shadow:0 4px 16px rgba(0,0,0,.06);padding:18px; }}
    h1 {{ margin:0 0 8px 0; font-size:22px; }}
    .msg {{ margin:12px 0; color:#059669; }}
    label {{ display:block; font-weight:700; margin:10px 0 6px 0; }}
    textarea {{ width:100%; min-height:80px; border:1px solid #e5e7eb; border-radius:8px; padding:10px; }}
    input[type="file"] {{ width:100%; padding:6px; border:1px dashed #cbd5e1; border-radius:8px; background:#f8fafc; }}
    button {{ background:#1d4ed8;color:#fff;border:none;border-radius:10px;padding:10px 14px;font-weight:700; }}
    a {{ color:#1d4ed8; text-decoration:none; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Upload photos – {flat} – {d}</h1>
      <p>Add any notes for this clean (these notes will be included in the WhatsApp message to you).</p>
      {"<div class='msg'>"+message+"</div>" if message else ""}
      <form action="/upload" method="post" enctype="multipart/form-data">
        <input type="hidden" name="flat" value="{flat}">
        <input type="hidden" name="date" value="{d}">
        <label>Photos</label>
        <input type="file" name="photos" accept="image/*" multiple />
        <label>Notes (optional)</label>
        <textarea name="notes" placeholder="Anything I should know?"></textarea>
        <div style="height:12px"></div>
        <button type="submit">Submit</button>
        <span style="margin-left:10px"><a href="/cleaner?days=30">Back to schedule</a></span>
      </form>
    </div>
  </div>
</body>
</html>
"""

# -----------------------------
# FastAPI app & routes
# -----------------------------

app = FastAPI()

@app.get("/", response_class=PlainTextResponse)
def root():
    return "Hello! The app is running :)"

@app.get("/cleaner", response_class=HTMLResponse)
def cleaner(days: Optional[int] = None):
    flats = load_flats()
    d = days if (days is not None and days > 0) else int(os.getenv("DEFAULT_DAYS", "14"))
    schedule = build_schedule_for_days(flats, d)
    return HTMLResponse(content=cleaner_page_html(schedule, flats, d))

@app.get("/debug", response_class=PlainTextResponse)
def debug():
    flats = load_flats()
    lines: List[str] = []
    lines.append("Loaded flats:")
    for fname, info in flats.items():
        lines.append(f"  {fname}: url={'SET' if info.get('url') else 'MISSING'} nick={info.get('nick')} colour={info.get('colour')}")
    # Build a short schedule for visibility
    schedule = build_schedule_for_days(flats, int(os.getenv("DEFAULT_DAYS", "14")))
    for fname in flats.keys():
        total = inn = out = 0
        for _, daymap in schedule.items():
            if fname in daymap:
                total += 1
                if daymap[fname].inn:
                    inn += 1
                if daymap[fname].out:
                    out += 1
        lines.append(f"\n{fname}: total={total} (in={inn}, out={out})")
    lines.append(f"\nDays with activity in next {os.getenv('DEFAULT_DAYS','14')} days: {len(schedule)}")
    return "\n".join(lines)

@app.get("/upload", response_class=HTMLResponse)
def upload_get(flat: str, date: str):
    return HTMLResponse(upload_form_html(flat, date))

@app.post("/upload", response_class=HTMLResponse)
async def upload_post(
    flat: str = Form(...),
    date: str = Form(...),
    notes: str = Form(""),
    photos: List[UploadFile] = File(default_factory=list),
    request: Request = None,
):
    # Save uploads (local only; not public)
    saved = 0
    save_dir = "/mnt/data/uploads"
    os.makedirs(save_dir, exist_ok=True)
    file_urls: List[str] = []

    base_url = os.getenv("MEDIA_BASE_URL", "").rstrip("/")
    for f in photos or []:
        try:
            fn = f.filename or f"photo_{saved+1}.jpg"
            safe_name = fn.replace("/", "_").replace("\\", "_")
            path = os.path.join(save_dir, f"{date}_{flat.replace(' ','_')}_{safe_name}")
            with open(path, "wb") as w:
                w.write(await f.read())
            saved += 1
            if base_url:
                # If you host uploaded files and expose them, include the public URL so Twilio can fetch it
                file_urls.append(f"{base_url}/{os.path.basename(path)}")
        except Exception:
            pass

    # Compose WhatsApp message
    msg_lines = [
        f"Cleaning complete ✅",
        f"Flat: {flat}",
        f"Date: {date}",
        f"Photos: {saved}",
    ]
    if (notes or "").strip():
        msg_lines.append("")
        msg_lines.append("Notes:")
        msg_lines.append(notes.strip())

    body = "\n".join(msg_lines)
    # Send WhatsApp (text; media if MEDIA_BASE_URL provided)
    try:
        send_whatsapp_message(body, media_urls=file_urls if file_urls else None)
    except Exception:
        # Don't break UX if Twilio fails
        pass

    # Show form again with success message
    return HTMLResponse(upload_form_html(flat, date, "Thanks! Your photos/notes have been recorded and sent."))

# -----------------------------
# Local run (Render uses Procfile)
# -----------------------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
