# app.py
import os
import uuid
import json
import threading
from datetime import date, datetime, timedelta
from typing import Dict, List, Tuple, Optional

import requests
from icalendar import Calendar
from fastapi import FastAPI, Request, UploadFile, File, Form, HTTPException, Cookie
from fastapi.responses import HTMLResponse, PlainTextResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# Optional DB + image libs
try:
    import psycopg2
except Exception:
    psycopg2 = None

# HEIC -> JPG conversion (optional)
try:
    from PIL import Image
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
except Exception:
    Image = None

# ---------------------------
# Config
# ---------------------------
TIMEZONE = os.getenv("TIMEZONE", "Europe/London")
DEFAULT_DAYS = int(os.getenv("DEFAULT_DAYS", "14"))
CLEAN_START = os.getenv("CLEAN_START", "10:00")
CLEAN_END = os.getenv("CLEAN_END", "16:00")

# Login
APP_PASSWORD = (os.getenv("APP_PASSWORD") or "").strip()
SESSION_COOKIE = "cleaner_auth"

# Counter admin PIN
COUNTER_PASSWORD = (os.getenv("COUNTER_PASSWORD") or "").strip()

# Twilio (optional)
TWILIO_ACCOUNT_SID = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
TWILIO_AUTH_TOKEN = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()
TWILIO_WHATSAPP_TO = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()
TWILIO_CONTENT_SID = (os.getenv("TWILIO_CONTENT_SID") or "").strip()  # not used

PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

# Database
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

# Upload dirs
UPLOAD_DIR = "/tmp/uploads"          # actual image files (publicly served)
MARK_DIR = "/tmp/marks"              # file fallback markers
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(MARK_DIR, exist_ok=True)

# ======== File counter fallback (kept, but DB will override) ========
COUNTER_FILE = os.getenv("COUNTER_FILE", "/tmp/clean_counter.json")
COUNTER_LOCK = threading.Lock()

def _ensure_counter_file():
    if not os.path.exists(COUNTER_FILE):
        with open(COUNTER_FILE, "w") as f:
            json.dump({"count": 0}, f)

def _read_counter_value() -> int:
    _ensure_counter_file()
    try:
        with open(COUNTER_FILE, "r") as f:
            return int(json.load(f).get("count", 0))
    except Exception:
        return 0

def _write_counter_value(v: int):
    try:
        with open(COUNTER_FILE, "w") as f:
            json.dump({"count": max(0, int(v))}, f)
    except Exception:
        pass
# =========================================================

# Optional Twilio import
try:
    from twilio.rest import Client as TwilioClient
    twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN) else None
except Exception:
    twilio_client = None

# ---------------------------
# App
# ---------------------------
app = FastAPI(title="Cleaner Schedule")
app.mount("/static", StaticFiles(directory=UPLOAD_DIR), name="static")

# ---------------------------
# Auth helpers
# ---------------------------
def check_auth(session_token: Optional[str]) -> bool:
    return bool(APP_PASSWORD) and (session_token == APP_PASSWORD)

# ---------------------------
# Flats & ICS helpers
# ---------------------------
PALETTE = ["#FF9800", "#2196F3", "#4CAF50", "#9C27B0", "#E91E63", "#00BCD4", "#795548", "#3F51B5"]

def load_flats(max_flats: int = 50) -> Dict[str, Dict[str, str]]:
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
        ci = to_date(ds); co = to_date(de)  # DTEND is checkout day
        if isinstance(ci, date) and isinstance(co, date):
            spans.append((ci, co))
    return spans

def build_schedule(days: int, start: Optional[date] = None) -> Dict[date, List[Dict]]:
    flats = load_flats()
    if start is None:
        start = datetime.utcnow().date()
    end = start + timedelta(days=days - 1)
    schedule: Dict[date, List[Dict]] = {}
    for flat_name, meta in flats.items():
        spans = parse_bookings(fetch_ics(meta["url"]))
        per_day: Dict[date, Dict[str, bool]] = {}
        for (ci, co) in spans:
            if start <= ci <= end:
                per_day.setdefault(ci, {"in": False, "out": False})
                per_day[ci]["in"] = True
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
    schedule = dict(sorted(schedule.items(), key=lambda kv: kv[0]))
    for day in list(schedule.keys()):
        schedule[day].sort(key=lambda it: (not it["out"], it["flat"].lower()))
    return schedule

# ---------------------------
# DB-backed completion markers + counter (with file fallback)
# ---------------------------
def _pg_conn():
    if not psycopg2 or not DATABASE_URL:
        raise RuntimeError("DB not available")
    return psycopg2.connect(DATABASE_URL)

def _db_init() -> bool:
    try:
        conn = _pg_conn()
        conn.autocommit = True
        with conn.cursor() as cur:
            # Completed rows (one per flat/day)
      cur.execute("""
    CREATE TABLE IF NOT EXISTS counter_offset (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        clean_offset INTEGER NOT NULL DEFAULT 0
    );
""")
cur.execute("INSERT INTO counter_offset (id, clean_offset) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;")


            # Manual offset so + / - can adjust total
            cur.execute("""
                CREATE TABLE IF NOT EXISTS counter_offset (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    offset INTEGER NOT NULL DEFAULT 0
                );
            """)
            cur.execute("INSERT INTO counter_offset (id, offset) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;")
        conn.close()
        return True
    except Exception as e:
        print("DB init failed, using file fallback:", repr(e))
        return False

USE_DB = _db_init()

def mark_path(flat: str, day_iso: str) -> str:
    safe_flat = flat.replace("/", "_").replace("\\", "_").replace(" ", "_")
    return os.path.join(MARK_DIR, f"{day_iso}__{safe_flat}.done")

def _db_completed_count() -> int:
    conn = _pg_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM completed_cleans;")
        n = int(cur.fetchone()[0])
    conn.close()
    return n

def _db_get_offset() -> int:
    conn = _pg_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT clean_offset FROM counter_offset WHERE id=1;")
        val = int(cur.fetchone()[0])
    conn.close()
    return val

def _db_set_offset(v: int) -> None:
    conn = _pg_conn()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("UPDATE counter_offset SET clean_offset=%s WHERE id=1;", (int(v),))
    conn.close()


def is_completed(flat: str, day_iso: str) -> bool:
    if USE_DB:
        try:
            conn = _pg_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM completed_cleans WHERE flat=%s AND day=%s", (flat, day_iso))
                found = cur.fetchone() is not None
            conn.close()
            return found
        except Exception as e:
            print("DB is_completed error, fallback:", repr(e))
    return os.path.exists(mark_path(flat, day_iso))

def set_completed(flat: str, day_iso: str) -> None:
    if USE_DB:
        try:
            conn = _pg_conn()
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO completed_cleans(flat, day) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (flat, day_iso),
                )
            conn.close()
            return
        except Exception as e:
            print("DB set_completed error, fallback:", repr(e))
    try:
        with open(mark_path(flat, day_iso), "w") as f:
            f.write("ok")
    except Exception:
        pass

def get_counter() -> int:
    if USE_DB:
        try:
            return _db_completed_count() + _db_get_offset()
        except Exception as e:
            print("DB get_counter error, fallback:", repr(e))
    # file fallback
    with COUNTER_LOCK:
        return _read_counter_value()

def set_counter(v: int) -> int:
    """Set total to v; with DB we adjust offset so: total = completed + offset."""
    if USE_DB:
        try:
            completed = _db_completed_count()
            _db_set_offset(int(v) - completed)
            return get_counter()
        except Exception as e:
            print("DB set_counter error:", repr(e))
            return get_counter()
    with COUNTER_LOCK:
        _write_counter_value(v)
        return v

def bump_counter(delta: int = 1) -> int:
    """Manual adjust using the offset (DB) or file value (fallback)."""
    if USE_DB:
        try:
            _db_set_offset(_db_get_offset() + int(delta))
            return get_counter()
        except Exception as e:
            print("DB bump_counter error:", repr(e))
            return get_counter()
    with COUNTER_LOCK:
        c = _read_counter_value()
        c = max(0, c + int(delta))
        _write_counter_value(c)
        return c

# ---------------------------
# WhatsApp helper
# ---------------------------
def wa_send_text_and_media(caption: str, media_urls: Optional[List[str]] = None) -> None:
    if not twilio_client or not TWILIO_WHATSAPP_FROM or not TWILIO_WHATSAPP_TO:
        print("Twilio not configured")
        return
    try:
        from_num = TWILIO_WHATSAPP_FROM if TWILIO_WHATSAPP_FROM.startswith("whatsapp:") else f"whatsapp:{TWILIO_WHATSAPP_FROM}"
        to_num   = TWILIO_WHATSAPP_TO   if TWILIO_WHATSAPP_TO.startswith("whatsapp:") else f"whatsapp:{TWILIO_WHATSAPP_TO}"
        if media_urls:
            # send one media per message; first carries caption
            for idx, m in enumerate(media_urls):
                body = caption if idx == 0 else ""
                print(f"Sending WA media: {m}")
                twilio_client.messages.create(from_=from_num, to=to_num, body=body, media_url=[m])
        else:
            print("Sending WA text only")
            twilio_client.messages.create(from_=from_num, to=to_num, body=caption)
    except Exception as e:
        print("Twilio error:", repr(e))

# ---------------------------
# HTML
# ---------------------------
BASE_CSS = f"""
<style>
  :root {{
    --red:#d32f2f; --green:#2e7d32; --muted:#6b7280;
    --card:#ffffff; --bg:#f7f7f8; --chip:#eef2ff; --accent:#111827;
  }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Inter,Arial; margin:24px; background:var(--bg); color:#111; }}
  h1 {{ margin:0 0 8px }}
  .legend {{ color:var(--muted); margin-bottom:12px }}
  .counter-badge {{ display:inline-flex; align-items:center; gap:8px; background:#fff; border:1px solid #eee; border-radius:12px; padding:6px 10px; font-weight:700; margin-bottom:16px }}
  .counter-badge a {{ margin-left:8px; font-weight:600; font-size:13px }}
  .day {{ background:var(--card); border:1px solid #eee; border-radius:14px; padding:16px; margin:16px 0; box-shadow:0 2px 6px rgba(0,0,0,.04); }}
  .day h2 {{ margin:0 0 10px; display:flex; align-items:center; gap:10px }}
  .today {{ background:#111; color:#fff; font-size:12px; padding:3px 8px; border-radius:999px }}
  .row {{ display:flex; align-items:center; gap:12px; padding:10px 0; border-top:1px dashed #eee }}
  .row:first-of-type {{ border-top:none }}
  .pill {{ display:inline-flex; align-items:center; gap:8px; padding:4px 10px; border-radius:999px; font-weight:700; background:var(--chip) }}
  .dot {{ width:8px; height:8px; border-radius:999px; display:inline-block }}
  .status-out {{ color:var(--red); font-weight:800 }}
  .status-in {{ color:var(--green); font-weight:800 }}
  .turn {{ background:#ffedd5; color:#7c2d12; border:2px solid #fdba74; padding:3px 10px; border-radius:999px; font-weight:900; text-transform:uppercase; letter-spacing:.3px }}
  .note {{ color:#666; }}
  .btn {{ margin-left:auto; background:#1976d2; color:#fff; text-decoration:none; padding:8px 12px; border-radius:10px; font-weight:700 }}
  .strike {{ text-decoration: line-through; color:#9aa1a9; }}
  .done {{ background:#16a34a; color:#fff; padding:2px 8px; border-radius:999px; font-weight:800; font-size:12px }}
  .tasks {{ display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap:6px 14px; margin:10px 0 6px; }}
  .tasks label {{ display:flex; align-items:center; gap:8px; font-size:14px; }}
  .card{{ background:#fff; border-radius:14px; padding:16px; box-shadow:0 1px 2px rgba(0,0,0,.05); border:1px solid #eee; }}
</style>
"""

TASK_LABELS = [
    "Floors swept / vacuumed",
    "Floors mopped",
    "Beds made with fresh linen",
    "Bathroom cleaned (toilet, sink, shower)",
    "Towels replaced",
    "Bins emptied & bags replaced",
    "Mirrors & glass cleaned",
    "Kitchen wiped (surfaces, hob, sink)",
    "Toiletries & toilet roll restocked",
    "Final check (lights off, windows/doors locked)",
]

def html_page(body: str) -> str:
    counter_html = f'<div class="counter-badge">✅ Cleans completed: <span>{get_counter()}</span> <a href="/counter">Admin</a></div>'
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cleaner Schedule</title>{BASE_CSS}</head>
<body>
  <h1>Cleaner Schedule</h1>
  <div class="legend">Check-out in <span style="color:#d32f2f;font-weight:800">red</span> • Check-in in <span style="color:#2e7d32;font-weight:800">green</span> • <b>SAME-DAY</b> stands out • Clean {CLEAN_START}–{CLEAN_END}</div>
  {counter_html}
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
        day_iso = d.isoformat()
        parts.append(f'<div class="day"><h2>{heading}{today_badge}</h2>')
        for it in items:
            has_out = it["out"]
            has_in = it["in"]
            same_day = has_out and has_in
            completed = is_completed(it["flat"], day_iso)

            chip = f'<span class="pill"><span class="dot" style="background:{it["colour"]}"></span>{it["nick"]}</span>'
            status_bits: List[str] = []
            if has_out:
                status_bits.append('<span class="status-out">Check-out</span>')
            if has_in and not has_out:
                status_bits.append('<span class="status-in">Check-in</span>')

            turn = '<span class="turn">SAME-DAY TURNAROUND</span>' if same_day else ""

            clean_html = ""
            if has_out:
                line = f'🧹 Clean between <b>{CLEAN_START}–{CLEAN_END}</b>'
                cls = "note strike" if completed else "note"
                clean_html = f'<span class="{cls}">{line}</span>'

            btn = ""
            if has_out:
                upload_href = f'/upload?flat={it["flat"].replace(" ", "%20")}&date={day_iso}'
                btn_text = "📷 Upload Photos" if not completed else "📷 Add more photos"
                btn = f'<a class="btn" href="{upload_href}">{btn_text}</a>'

            done_badge = ' <span class="done">✔ Completed</span>' if completed else ""

            row = f'<div class="row">{chip} {" ".join(status_bits)} {turn} {clean_html} {btn}{done_badge}</div>'
            parts.append(row)
        parts.append("</div>")
    return "\n".join(parts)

# ---------------------------
# Routes
# ---------------------------
@app.get("/", response_class=PlainTextResponse)
def root():
    return "OK"

# ----- Login / Logout -----
@app.get("/login", response_class=HTMLResponse)
def login_page():
    return """
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>Soltan Living - Login</title>
      <style>
        body {font-family: Arial, sans-serif; background:#f7f7f8; display:flex; justify-content:center; align-items:center; height:100vh; margin:0;}
        .card {background:#fff; padding:40px 30px; border-radius:14px; box-shadow:0 4px 10px rgba(0,0,0,0.08); width:320px; text-align:center;}
        h1 {margin:0 0 20px; font-size:22px; color:#111827;}
        .brand {font-size:26px; font-weight:bold; color:#1976d2; margin-bottom:20px;}
        input {width:100%; padding:12px; margin:10px 0 20px; border:1px solid #ddd; border-radius:8px; font-size:16px;}
        button {background:#1976d2; color:#fff; border:none; padding:12px 16px; border-radius:8px; font-weight:bold; font-size:16px; cursor:pointer; width:100%;}
        button:hover {background:#145aa0;}
      </style>
    </head>
    <body>
      <div class="card">
        <div class="brand">Soltan Living</div>
        <h1>Cleaner Login</h1>
        <form method="post" action="/login">
          <input type="password" name="password" placeholder="Enter password" required>
          <button type="submit">Login</button>
        </form>
      </div>
    </body>
    </html>
    """

@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    pw = (form.get("password") or "").strip()
    if APP_PASSWORD and pw == APP_PASSWORD:
        resp = RedirectResponse(url="/cleaner", status_code=303)
        resp.set_cookie(SESSION_COOKIE, APP_PASSWORD, httponly=True, max_age=60*60*12, samesite="lax")
        return resp
    return RedirectResponse(url="/login", status_code=303)

@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp

# ----- Protected pages -----
@app.get("/cleaner", response_class=HTMLResponse)
def cleaner(days: int = DEFAULT_DAYS, session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    if not check_auth(session_token):
        return RedirectResponse(url="/login")
    schedule = build_schedule(days)
    return HTMLResponse(html_page(render_schedule(schedule, days)))

@app.get("/debug", response_class=PlainTextResponse)
def debug(session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    if not check_auth(session_token):
        return RedirectResponse(url="/login")
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

# Serve uploaded media (public for Twilio)
@app.get("/m/{fname}")
def serve_media(fname: str):
    path = os.path.join(UPLOAD_DIR, fname)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Not found")
    mt = "image/jpeg"
    lf = fname.lower()
    if lf.endswith(".png"): mt = "image/png"
    if lf.endswith(".webp"): mt = "image/webp"
    if lf.endswith(".heic"): mt = "image/heic"
    return FileResponse(path, media_type=mt)

# Upload flow: GET form + POST handler
def _upload_form(flat: str, the_date: str, msg: str = "") -> str:
    checks: List[str] = []
    for i, label in enumerate(TASK_LABELS, start=1):
        checks.append(f'<label><input type="checkbox" name="tasks" value="{label}"> {label}</label>')
    tasks_html = '<div class="tasks">' + "".join(checks) + "</div>"
    note = f'<p style="color:#2e7d32;font-weight:700">{msg}</p>' if msg else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Upload</title>{BASE_CSS}</head>
<body>
  <h1>Upload Photos</h1>
  {note}
  <div class="card">
    <h2 style="margin-top:0">{flat} — {the_date}</h2>
    <form action="/upload" method="post" enctype="multipart/form-data" style="display:grid;gap:12px">
      <input type="hidden" name="flat" value="{flat}">
      <input type="hidden" name="date" value="{the_date}">
      <div><div style="font-weight:700;margin-bottom:6px">Tasks completed (tick all that apply)</div>{tasks_html}</div>
      <div><label>Photos (you can select multiple)</label><input type="file" name="photos" multiple accept="image/*"></div>
      <div><label>Notes (optional)</label><textarea name="notes" placeholder="anything i should know ?" style="min-height:90px"></textarea></div>
      <div><button type="submit" style="background:#1976d2;color:#fff;border:0;border-radius:10px;padding:10px 14px;font-weight:700">Send</button>
           <a href="/cleaner" style="margin-left:8px">Back</a></div>
    </form>
  </div>
</body></html>"""

@app.get("/upload", response_class=HTMLResponse)
def upload_form(flat: str, date: str, session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    if not check_auth(session_token):
        return RedirectResponse(url="/login")
    return HTMLResponse(_upload_form(flat, date))

@app.post("/upload")
async def upload_submit(
    request: Request,
    flat: str = Form(...),
    date: str = Form(...),
    notes: str = Form(""),
    tasks: List[str] = Form(None),  # multiple checkboxes named "tasks"
    photos: List[UploadFile] = File(default_factory=list),
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
):
    if not check_auth(session_token):
        return RedirectResponse(url="/login")

    tasks = tasks or []
    tasks_line = ", ".join(tasks) if tasks else "None"

    saved_urls: List[str] = []
    for f in photos or []:
        try:
            # Detect extension; convert HEIC -> JPG if possible
            orig_name = (f.filename or "")
            lf = orig_name.lower()
            ext = ".jpg"
            if lf.endswith(".png"):  ext = ".png"
            elif lf.endswith(".webp"): ext = ".webp"
            elif lf.endswith(".heic"): ext = ".heic"

            raw_bytes = await f.read()

            # If HEIC and we have Pillow+pillow-heif, convert to JPG
            if ext == ".heic" and Image is not None:
                try:
                    import io
                    img = Image.open(io.BytesIO(raw_bytes))
                    rgb = img.convert("RGB")
                    fname = f"{uuid.uuid4().hex}.jpg"
                    dest = os.path.join(UPLOAD_DIR, fname)
                    rgb.save(dest, format="JPEG", quality=90)
                    print(f"Converted HEIC -> JPG: {orig_name} -> {fname}")
                except Exception as e:
                    # Fallback: save as given (may not render in WA)
                    fname = f"{uuid.uuid4().hex}{ext}"
                    dest = os.path.join(UPLOAD_DIR, fname)
                    with open(dest, "wb") as w:
                        w.write(raw_bytes)
                    print("HEIC convert failed, saved raw:", repr(e))
            else:
                # Non-HEIC (or no Pillow) -> save as-is
                fname = f"{uuid.uuid4().hex}{ext}"
                dest = os.path.join(UPLOAD_DIR, fname)
                with open(dest, "wb") as w:
                    w.write(raw_bytes)

            base = PUBLIC_BASE_URL or f"{request.url.scheme}://{request.url.netloc}"
            saved_urls.append(f"{base}/m/{fname}")
        except Exception as e:
            print("Save file error:", repr(e))
            continue

    # Mark completion and bump count only once per flat/day
    already_completed = is_completed(flat, date)
    set_completed(flat, date)
    if not already_completed:
        bump_counter(1)

    # Build caption (used for first photo)
    caption_lines = [
        "🧹 Cleaning update",
        f"Flat: {flat}",
        f"Date: {date}",
        f"Tasks: {tasks_line}",
        f"Photos: {len(saved_urls)}",
    ]
    if notes.strip():
        caption_lines.append(f"Notes: {notes.strip()}")
    caption = "\n".join(caption_lines)

    # Send to WhatsApp
    if saved_urls:
        wa_send_text_and_media(caption, media_urls=saved_urls)
    else:
        wa_send_text_and_media(caption)

    return RedirectResponse(url="/cleaner", status_code=303)

# ---------------------------
# Counter: admin page (+ / - / reset) with PIN
# ---------------------------
@app.get("/api/counter")
def api_counter_value(session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    if not check_auth(session_token):
        return {"count": 0}
    return {"count": get_counter()}

@app.get("/counter", response_class=HTMLResponse)
def counter_page(session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    if not check_auth(session_token):
        return RedirectResponse(url="/login")
    pin_field = ""
    if COUNTER_PASSWORD:
        pin_field = (
            '<input type="password" name="pin" placeholder="PIN" '
            'style="padding:8px;border:1px solid #ddd;border-radius:8px;min-width:100px">'
        )
    body = (
        '<div class="card" style="max-width:520px">'
        '<h2 style="margin-top:0">🧹 Cleans Completed Counter</h2>'
        f'<p style="font-weight:700">Current count: {get_counter()}</p>'
        '<form action="/counter/update" method="post" style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">'
        f'{pin_field}'
        '<button type="submit" name="action" value="plus" '
        'style="background:#16a34a;color:#fff;border:0;border-radius:10px;padding:10px 14px;font-weight:700">➕ Add 1</button>'
        '<button type="submit" name="action" value="minus" '
        'style="background:#f59e0b;color:#fff;border:0;border-radius:10px;padding:10px 14px;font-weight:700">➖ Subtract 1</button>'
        '<button type="submit" name="action" value="reset" '
        'style="background:#ef4444;color:#fff;border:0;border-radius:10px;padding:10px 14px;font-weight:700">🔁 Reset</button>'
        '</form>'
        '<div style="margin-top:10px"><a href="/cleaner">⬅ Back to schedule</a></div>'
        '</div>'
    )
    return HTMLResponse(html_page(body))

@app.post("/counter/update")
def counter_update(
    action: str = Form(...),
    pin: str = Form(default=""),
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
):
    if not check_auth(session_token):
        return RedirectResponse(url="/login")

    if COUNTER_PASSWORD and pin != COUNTER_PASSWORD:
        return RedirectResponse(url="/counter", status_code=303)

    if action == "plus":
        bump_counter(1)
    elif action == "minus":
        bump_counter(-1)
    elif action == "reset":
        set_counter(0)

    return RedirectResponse(url="/counter", status_code=303)

# ---------------------------
# Local run
# ---------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
