import os
import io
import uuid
import shutil
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple, Optional

import pytz
import requests
from fastapi import FastAPI, Form, File, UploadFile, Request, Query
from fastapi.responses import PlainTextResponse, HTMLResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from icalendar import Calendar, Event

# Optional: Twilio (we'll import guarded so app still runs if not installed)
try:
    from twilio.rest import Client as TwilioClient
except Exception:  # pragma: no cover
    TwilioClient = None  # type: ignore

# -----------------------------
# Config & Utilities
# -----------------------------

TZ_NAME = os.getenv("TIMEZONE", "Europe/London")
TZ = pytz.timezone(TZ_NAME)

DEFAULT_DAYS = int(os.getenv("DEFAULT_DAYS", "14"))

# Where to save uploads (and serve back so Twilio can pull media)
UPLOAD_DIR = os.path.join(os.getcwd(), "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(title="Cleaner Schedule")

# Mount a static route for uploaded files
# (Render allows reading files written during runtime for the life of the instance)
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")


def load_flats(max_flats: int = 50) -> Dict[str, Dict[str, str]]:
    """
    Read FLAT{n}_ENV variables and return:
    {
      "Flat 7": {"url": "...", "nick": "Orange", "colour": "#FF9800"},
      ...
    }
    Falls back to the explicit "Flat {n}" if FLAT{n}_NAME missing.
    """
    flats: Dict[str, Dict[str, str]] = {}
    # A simple palette fallback if colour not provided
    palette = [
        "#FF9800", "#2196F3", "#4CAF50", "#E91E63", "#9C27B0",
        "#00BCD4", "#795548", "#607D8B", "#FF5722", "#8BC34A",
    ]
    pi = 0
    for n in range(1, max_flats + 1):
        url = (os.getenv(f"FLAT{n}_ICS_URL") or "").strip()
        if not url:
            continue
        name = (os.getenv(f"FLAT{n}_NAME") or f"Flat {n}").strip()
        nick = (os.getenv(f"FLAT{n}_NICK") or name).strip()
        colour = (os.getenv(f"FLAT{n}_COLOUR") or palette[pi % len(palette)]).strip()
        pi += 1
        flats[name] = {"url": url, "nick": nick, "colour": colour}
    return flats


def fetch_ics(url: str) -> str:
    if not url:
        return ""
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def _to_local(dt) -> datetime:
    """Coerce ical dt/datetime/Date into timezone-aware local datetime."""
    if isinstance(dt, date) and not isinstance(dt, datetime):
        # All-day date -> treat as midnight local
        return TZ.localize(datetime(dt.year, dt.month, dt.day, 0, 0, 0))
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            # Assume UTC if naive in ICS
            dt = pytz.utc.localize(dt)
        return dt.astimezone(TZ)
    # Fallback
    return TZ.localize(datetime.utcnow()).astimezone(TZ)


def parse_bookings(ics_text: str) -> List[Tuple[datetime, datetime, str]]:
    """
    Parse ICS and return list of (local_dtstart, local_dtend, summary)
    We treat DTEND as exclusive (typical iCal). So checkout day is (end - 1 day).
    """
    out: List[Tuple[datetime, datetime, str]] = []
    if not ics_text.strip():
        return out
    cal = Calendar.from_ical(ics_text)
    for comp in cal.walk():
        if comp.name != "VEVENT":
            continue
        try:
            dtstart = _to_local(comp.get("dtstart").dt)
            dtend = _to_local(comp.get("dtend").dt)
            summary = str(comp.get("summary") or "").strip()
            out.append((dtstart, dtend, summary))
        except Exception:
            continue
    return out


def build_schedule_for_days(
    flats: Dict[str, Dict[str, str]],
    days: int,
    start_day: Optional[date] = None,
) -> Dict[date, List[Dict]]:
    """
    Build day -> list of items {flat, nick, colour, in, out}
    'in' and 'out' are booleans for check-in / check-out on that day.
    Cleaning is implied on 'out' days. Same-day turnover means item has both in & out True.
    """
    if start_day is None:
        start_day = datetime.now(TZ).date()

    schedule: Dict[date, List[Dict]] = {}

    for flat_name, meta in flats.items():
        url = meta["url"]
        events = parse_bookings(fetch_ics(url))
        for dtstart, dtend, _summary in events:
            # Check-in day = dtstart.date()
            in_day = dtstart.date()
            # Checkout day = (dtend - 1 day).date() (DTEND exclusive)
            co_day = (dtend - timedelta(days=1)).date()

            # Only consider days in the requested window
            for d, flag in [(in_day, "in"), (co_day, "out")]:
                if start_day <= d <= (start_day + timedelta(days=days - 1)):
                    schedule.setdefault(d, [])
                    # Merge with existing same flat entry for same day if exists
                    existing = next((it for it in schedule[d] if it["flat"] == flat_name), None)
                    if existing:
                        existing[flag] = True
                    else:
                        schedule[d].append(
                            {
                                "flat": flat_name,
                                "nick": meta["nick"],
                                "colour": meta["colour"],
                                "in": flag == "in",
                                "out": flag == "out",
                            }
                        )

    # Sort each day by (check-out first), then name
    for d in schedule:
        schedule[d].sort(key=lambda it: (not it.get("out", False), it["flat"]))
    return dict(sorted(schedule.items(), key=lambda kv: kv[0]))


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def render_cleaner(schedule: Dict[date, List[Dict]], days: int, request: Request) -> str:
    """Pretty HTML view for cleaners."""
    today = datetime.now(TZ).date()
    base = f"{request.url.scheme}://{request.url.netloc}"

    css = """
    <style>
      :root {
        --red: #e53935;
        --green: #2e7d32;
        --muted: #6b7280;
        --badge-bg: #eef2ff;
      }
      body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, Arial, sans-serif; margin: 24px; color: #111827; }
      .header { font-size: 28px; font-weight: 800; margin-bottom: 8px; }
      .sub { color: #6b7280; margin-bottom: 18px; }
      .legend b { color: #374151; }
      .legend .red { color: var(--red); font-weight:600; }
      .legend .green { color: var(--green); font-weight:600; }

      .day { background: #fff; border: 1px solid #e5e7eb; border-radius: 16px; padding: 18px; margin: 18px 0; box-shadow: 0 1px 2px rgba(0,0,0,0.03);}
      .day h2 { font-size: 20px; margin: 0 0 12px 0; display:flex; align-items:center; gap:10px;}
      .today { background: #10b981; color: white; border-radius: 999px; padding: 3px 10px; font-size: 12px; letter-spacing: .3px; }

      .row { display: flex; align-items: center; gap: 12px; padding: 10px 0; border-top: 1px dashed #e5e7eb; }
      .row:first-of-type { border-top: none; }
      .pill { display:inline-flex; align-items:center; gap:6px; line-height:1; padding: 6px 10px; border-radius: 999px; font-weight:700; color:#1f2937; background: var(--badge-bg); border:1px solid #e5e7eb;}
      .nick-dot { width:8px; height:8px; border-radius:999px; display:inline-block; }
      .status { font-weight: 700; }
      .status.red { color: var(--red); }
      .status.green { color: var(--green); }
      .muted { color: var(--muted); }
      .brush { opacity: .8 }

      .btn { background:#2563eb; color:white; border:none; padding:8px 12px; border-radius:10px; font-weight:600; cursor:pointer; text-decoration:none; display:inline-flex; align-items:center; gap:8px;}
      .btn:hover { background:#1d4ed8; }
      .cam { font-size: 16px; }

      .turn { display:inline-flex; align-items:center; gap:6px; font-weight:700; }
      .arrow { color:#9ca3af; }
      .turn-flag { background:#fef3c7; color:#92400e; padding:2px 8px; border-radius:999px; font-size:12px; font-weight:800; }
      .cleanwin { color:#6b7280; }
    </style>
    """

    header = f"""
    <div class="header">Cleaner Schedule</div>
    <div class="sub legend">
      Check-out in <span class="red">red</span> • Check-in in <span class="green">green</span> • Same-day turnover highlighted • Clean 10:00–16:00
    </div>
    """

    out: List[str] = [css, header]

    if not schedule:
        out.append(
            f'<div class="muted">No activity found. Try a longer window: '
            f'<a href="/cleaner?days={max(days, 30)}">/cleaner?days={max(days, 30)}</a> or see <a href="/debug">/debug</a>.</div>'
        )
        return "\n".join(out)

    for d, items in schedule.items():
        day_title = d.strftime("%a %d %b")
        is_today = (d == today)

        # --- TODAY badge without inline-escape mess
        today_badge = ' <span class="today">TODAY</span>' if is_today else ''
        out.append(f'<div class="day"><h2>{day_title}{today_badge}</h2>')

        # build rows for each flat
        for item in items:
            nick_html = (
                f'<span class="pill"><span class="nick-dot" style="background:{_html_escape(item["colour"])}"></span>'
                f'{_html_escape(item["nick"])}</span>'
            )

            status_bits: List[str] = []
            # Show order OUT first, then IN
            if item.get("out"):
                status_bits.append('<span class="status red">Check-out</span>')
            if item.get("in"):
                status_bits.append('<span class="status green">Check-in</span>')

            status_html = " ".join(status_bits)

            # Determine “same day turnover”
            same_day = item.get("in") and item.get("out")
            turn_html = ""
            if same_day:
                turn_html = (
                    ' <span class="turn">'
                    '<span class="arrow">→</span> Clean <span class="arrow">→</span> Check-in'
                    ' <span class="turn-flag">SAME-DAY</span>'
                    '</span>'
                )

            # Show cleaning window only when there is an OUT (cleaning needed)
            clean_win_html = ""
            if item.get("out"):
                clean_win_html = '<span class="cleanwin"><span class="brush">🧹</span> Clean between <b>10:00–16:00</b></span>'

            # Upload link
            up_href = (
                f'/upload?flat={_html_escape(item["flat"])}'
                f'&date={_html_escape(d.isoformat())}'
            )
            upload_btn = f'<a class="btn" href="{up_href}"><span class="cam">📷</span> Upload Photos</a>'

            # Compose row
            row_bits = [nick_html, status_html]
            if clean_win_html:
                row_bits.append(f" {clean_win_html}")
            if turn_html:
                row_bits.append(f" {turn_html}")
            row_bits.append(f" {upload_btn}")

            out.append(f'<div class="row">{" ".join(row_bits)}</div>')

        out.append("</div>")  # end .day

    return "\n".join(out)


# -----------------------------
# Routes
# -----------------------------

@app.get("/", response_class=PlainTextResponse)
def root():
    return "Hello! The app is running."


@app.get("/debug", response_class=PlainTextResponse)
def debug():
    flats = load_flats()
    lines = ["Loaded flats:"]
    for name, meta in flats.items():
        lines.append(f"  {name}: url={'SET' if meta['url'] else 'MISSING'} nick={meta['nick']} colour={meta['colour']}")
    sched = build_schedule_for_days(flats, days=14)
    lines.append("")
    for fname in flats.keys():
        # quick counts
        in_c = out_c = tot = 0
        for d, items in sched.items():
            for it in items:
                if it["flat"] == fname:
                    tot += 1
                    if it.get("in"): in_c += 1
                    if it.get("out"): out_c += 1
        lines.append(f"{fname}: total={tot} (in={in_c}, out={out_c})")
    lines.append("")
    lines.append(f"Days with activity in next 14 days: {len(sched)}")
    return "\n".join(lines)


@app.get("/cleaner", response_class=HTMLResponse)
def cleaner_view(request: Request, days: int = Query(DEFAULT_DAYS, ge=1, le=90)):
    flats = load_flats()
    schedule = build_schedule_for_days(flats, days=days)
    return HTMLResponse(render_cleaner(schedule, days, request))


# -----------------------------
# Upload + Notes + WhatsApp
# -----------------------------

def _twilio_ready() -> bool:
    return all(
        os.getenv(k)
        for k in ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "TWILIO_WHATSAPP_TO"]
    ) and TwilioClient is not None


def _send_whatsapp(message: str, media_urls: List[str]) -> Optional[str]:
    """Send WhatsApp message with optional media. Returns SID or None."""
    if not _twilio_ready():
        return None
    client = TwilioClient(os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN"))
    from_num = os.getenv("TWILIO_WHATSAPP_FROM")
    to_num = os.getenv("TWILIO_WHATSAPP_TO")
    # Twilio expects 'whatsapp:+...' format; ensure prefix present
    if from_num and not from_num.startswith("whatsapp:"):
        from_num = f"whatsapp:{from_num}"
    if to_num and not to_num.startswith("whatsapp:"):
        to_num = f"whatsapp:{to_num}"
    msg = client.messages.create(
        from_=from_num,
        to=to_num,
        body=message,
        media_url=media_urls or None,
    )
    return getattr(msg, "sid", None)


def _html_page(title: str, body_html: str) -> HTMLResponse:
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{_html_escape(title)}</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, Arial, sans-serif; margin: 24px; color:#111827; }}
    .card {{ background:#fff; border:1px solid #e5e7eb; border-radius:16px; padding:20px; max-width:720px; }}
    .title {{ font-weight:800; font-size:22px; margin:0 0 14px 0; }}
    .muted {{ color:#6b7280; }}
    .ok {{ color:#065f46; background:#d1fae5; padding:8px 12px; border-radius:10px; display:inline-block; }}
    .err {{ color:#7f1d1d; background:#fee2e2; padding:8px 12px; border-radius:10px; display:inline-block; }}
    input[type="file"] {{ padding: 10px; border:1px dashed #d1d5db; border-radius:10px; width:100%; }}
    textarea {{ width:100%; min-height:80px; padding:10px; border:1px solid #d1d5db; border-radius:10px; }}
    .row {{ margin:12px 0; }}
    .btn {{ background:#2563eb; color:white; border:none; padding:10px 14px; border-radius:10px; font-weight:700; cursor:pointer; }}
    a.btn {{ text-decoration:none; display:inline-block; }}
  </style>
</head>
<body>
  <div class="card">
    {body_html}
  </div>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/upload", response_class=HTMLResponse)
async def upload_form(flat: str, date: str):
    title = "Upload Photos"
    body = f"""
      <div class="title">Upload Photos — {_html_escape(flat)} — {_html_escape(date)}</div>
      <form action="/upload" method="post" enctype="multipart/form-data">
        <input type="hidden" name="flat" value="{_html_escape(flat)}">
        <input type="hidden" name="date" value="{_html_escape(date)}">

        <div class="row">
          <label>Photos (you can select multiple):</label><br>
          <input type="file" name="photos" accept="image/*" multiple>
        </div>

        <div class="row">
          <label>Notes for host (optional):</label><br>
          <textarea name="notes" placeholder="Anything I should know?"></textarea>
        </div>

        <div class="row"><button class="btn" type="submit">Send</button>
          &nbsp; <a class="btn" style="background:#6b7280" href="/cleaner">Back</a>
        </div>
      </form>
      <div class="muted">We’ll notify via WhatsApp when available.</div>
    """
    return _html_page(title, body)


@app.post("/upload", response_class=HTMLResponse)
async def upload_submit(
    request: Request,
    flat: str = Form(...),
    date: str = Form(...),
    photos: List[UploadFile] = File(default=[]),
    notes: str = Form(default=""),
):
    saved_urls: List[str] = []

    # Save files and expose via /uploads/...
    base_url = f"{request.url.scheme}://{request.url.netloc}"
    for file in photos or []:
        if not file.filename:
            continue
        ext = os.path.splitext(file.filename)[1].lower()
        safe_name = f"{uuid.uuid4().hex}{ext}"
        dest = os.path.join(UPLOAD_DIR, safe_name)
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        saved_urls.append(f"{base_url}/uploads/{safe_name}")

    # Build WhatsApp message
    msg_lines = [
        f"Cleaning upload received ✅",
        f"Flat: {flat}",
        f"Date: {date}",
    ]
    if notes.strip():
        msg_lines.append("")
        msg_lines.append(f"Notes: {notes.strip()}")

    sid = None
    err = None
    if _twilio_ready():
        try:
            sid = _send_whatsapp("\n".join(msg_lines), media_urls=saved_urls)
        except Exception as e:
            err = str(e)

    # Response
    detail = '<span class="ok">Uploaded.</span>'
    if _twilio_ready():
        if sid:
            detail += f" WhatsApp sent (SID: {_html_escape(sid)})."
        elif err:
            detail += f' <span class="err">WhatsApp error: {_html_escape(err)}</span>'
        else:
            detail += " WhatsApp not sent."
    else:
        detail += " (WhatsApp not configured.)"

    body = f"""
      <div class="title">Upload complete</div>
      <div class="row">{detail}</div>
      <div class="row"><a class="btn" href="/cleaner">Back to schedule</a></div>
      <div class="muted">Saved {len(saved_urls)} photo(s).</div>
    """
    return _html_page("Uploaded", body)


# -----------------------------
# Uvicorn entrypoint (Render)
# -----------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
