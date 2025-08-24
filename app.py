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
TWILIO_WHATSAPP_FROM = (os.getenv("TWILIO_WHATSAPP_FROM") or "").strip()     # e.g. whatsapp:+1415xxxxxxx or +1415...
TWILIO_WHATSAPP_TO = (os.getenv("TWILIO_WHATSAPP_TO") or "").strip()         # your personal number to receive updates
TWILIO_CONTENT_SID = (os.getenv("TWILIO_CONTENT_SID") or "").strip()         # approved template SID

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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS completed_cleans (
                    flat TEXT NOT NULL,
                    day  DATE NOT NULL,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    PRIMARY KEY (flat, day)
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS counter_offset (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    clean_offset INTEGER NOT NULL DEFAULT 0
                );
            """)
            cur.execute("INSERT INTO counter_offset (id, clean_offset) VALUES (1, 0) ON CONFLICT (id) DO NOTHING;")
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
        row = cur.fetchone()
        val = int(row[0]) if row else 0
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
    with COUNTER_LOCK:
        return _read_counter_value()

def set_counter(v: int) -> int:
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

# ----- helpers to delete completed marks -----
def clear_completed(day_iso: Optional[str] = None, flat: Optional[str] = None):
    """
    Delete completion markers from DB or files.
      - If day_iso only -> clears all flats on that day.
      - If day_iso + flat -> clears one flat on that day.
      - If neither -> clears everything (danger!).
    """
    if USE_DB:
        try:
            conn = _pg_conn()
            conn.autocommit = True
            with conn.cursor() as cur:
                if day_iso and flat:
                    cur.execute("DELETE FROM completed_cleans WHERE day=%s AND flat=%s", (day_iso, flat))
                elif day_iso:
                    cur.execute("DELETE FROM completed_cleans WHERE day=%s", (day_iso,))
                else:
                    cur.execute("DELETE FROM completed_cleans")
            conn.close()
            return
        except Exception as e:
            print("DB clear_completed error, fallback:", repr(e))

    try:
        if day_iso and flat:
            safe_flat = flat.replace("/", "_").replace("\\", "_").replace(" ", "_")
            p = os.path.join(MARK_DIR, f"{day_iso}__{safe_flat}.done")
            if os.path.exists(p):
                os.remove(p)
        elif day_iso:
            for fname in os.listdir(MARK_DIR):
                if fname.startswith(f"{day_iso}__") and fname.endswith(".done"):
                    os.remove(os.path.join(MARK_DIR, fname))
        else:
            for fname in os.listdir(MARK_DIR):
                if fname.endswith(".done"):
                    os.remove(os.path.join(MARK_DIR, fname))
    except Exception as e:
        print("File clear_completed error:", repr(e))

# ---------------------------
# WhatsApp helpers (freeform + template + queue)
# ---------------------------
PHOTO_QUEUE_FILE = "/tmp/photo_queue.json"
QUEUE_LOCK = threading.Lock()

def _wa_numbers():
    from_num = TWILIO_WHATSAPP_FROM if TWILIO_WHATSAPP_FROM.startswith("whatsapp:") else f"whatsapp:{TWILIO_WHATSAPP_FROM}"
    to_num   = TWILIO_WHATSAPP_TO   if TWILIO_WHATSAPP_TO.startswith("whatsapp:")   else f"whatsapp:{TWILIO_WHATSAPP_TO}"
    return from_num, to_num

def _load_queue() -> List[dict]:
    with QUEUE_LOCK:
        if not os.path.exists(PHOTO_QUEUE_FILE):
            return []
        try:
            with open(PHOTO_QUEUE_FILE, "r") as f:
                return json.load(f) or []
        except Exception:
            return []

def _save_queue(queue: List[dict]) -> None:
    with QUEUE_LOCK:
        with open(PHOTO_QUEUE_FILE, "w") as f:
            json.dump(queue, f)

def _queue_item(caption: str, media_urls: List[str]) -> None:
    item = {
        "caption": caption,
        "media_urls": media_urls or [],
        "ts": datetime.utcnow().isoformat()
    }
    q = _load_queue()
    q.append(item)
    _save_queue(q)
    print(f"Queued {len(media_urls)} photos for later send.")

def _release_queue_and_send():
    """Send all queued items now (called when inbound WA message arrives or from /queue Release)."""
    if not twilio_client:
        print("Twilio not configured; cannot release queue.")
        return
    from_num, to_num = _wa_numbers()
    q = _load_queue()
    if not q:
        print("Queue empty; nothing to send.")
        return
    # clear first to avoid loops if send fails halfway
    _save_queue([])
    for item in q:
        caption = item.get("caption", "")
        media_urls = item.get("media_urls", [])
        try:
            if media_urls:
                for idx, m in enumerate(media_urls):
                    body = caption if idx == 0 else ""
                    print(f"[Queue release] Sending media: {m}")
                    twilio_client.messages.create(from_=from_num, to=to_num, body=body, media_url=[m])
            else:
                print("[Queue release] Sending text only.")
                twilio_client.messages.create(from_=from_num, to=to_num, body=caption)
        except Exception as e:
            print("Queue release send error:", repr(e))

def wa_send_with_template(details_text: str) -> None:
    """Send using approved WhatsApp template (fills {{1}} with details_text)."""
    if not twilio_client or not TWILIO_CONTENT_SID:
        print("Template send skipped: missing Twilio client or TWILIO_CONTENT_SID")
        return
    try:
        from_num, to_num = _wa_numbers()
        vars_json = json.dumps({"1": details_text})
        msg = twilio_client.messages.create(
            from_=from_num,
            to=to_num,
            content_sid=TWILIO_CONTENT_SID,
            content_variables=vars_json,
        )
        print(f"✅ Template sent (sid={msg.sid})")
    except Exception as e:
        print("Template send error:", repr(e))

def wa_send_text_and_media_or_queue(caption: str, media_urls: Optional[List[str]], details_text_for_template: str):
    """
    Try freeform with media first. If blocked (63016), queue photos and
    send a template asking the user to reply to open the 24h window.
    """
    if not twilio_client or not TWILIO_WHATSAPP_FROM or not TWILIO_WHATSAPP_TO:
        print("Twilio not configured; skipping WA send.")
        return

    from_num, to_num = _wa_numbers()

    try:
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
        err = repr(e)
        print("Twilio freeform error:", err)
        if "63016" in err or "outside the allowed window" in err.lower():
            # Queue photos for later delivery
            _queue_item(caption=caption, media_urls=media_urls or [])
            # Include a link to the first photo (if any) in the template text for convenience
            first_link = (media_urls[0] if media_urls else "")
            appended = f" — View: {first_link}" if first_link else ""
            wa_send_with_template(details_text_for_template + appended)
        else:
            # Unexpected error; just log it
            print("Unexpected Twilio error (not 63016):", err)

def get_queue_count() -> int:
    try:
        return len(_load_queue())
    except Exception:
        return 0

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
  .badges {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px }}
  .counter-badge, .queue-badge {{ display:inline-flex; align-items:center; gap:8px; background:#fff; border:1px solid #eee; border-radius:12px; padding:6px 10px; font-weight:700; }}
  .counter-badge a, .queue-badge a {{ margin-left:8px; font-weight:600; font-size:13px }}
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
  .mono {{ font-family: ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace; }}
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
    queue_ct = get_queue_count()
    counter_html = f'<div class="counter-badge">✅ Cleans completed: <span>{get_counter()}</span> <a href="/counter">Admin</a></div>'
    queue_html = f'<div class="queue-badge">📦 Queued WA: <span>{queue_ct}</span> <a href="/queue">Manage</a></div>' if queue_ct > 0 else ''
    badges = f'<div class="badges">{counter_html}{queue_html}</div>'
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cleaner Schedule</title>{BASE_CSS}</head>
<body>
  <h1>Cleaner Schedule</h1>
  <div class="legend">Check-out in <span style="color:#d32f2f;font-weight:800">red</span> • Check-in in <span style="color:#2e7d32;font-weight:800">green</span> • <b>SAME-DAY</b> stands out • Clean {CLEAN_START}–{CLEAN_END}</div>
  {badges}
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

    # Mark completion (counter persists via DB offset; no bump here)
    set_completed(flat, date)

    # Build caption for freeform
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

    # Build details for template {{1}} (we add first photo link on fallback)
    details_text = f"{flat} — {date} — {len(saved_urls)} photos — Tasks: {tasks_line}"
    if notes.strip():
        details_text += f" — Notes: {notes.strip()}"

    # Try freeform media; if outside 24h, queue & send template asking to reply
    wa_send_text_and_media_or_queue(caption, saved_urls if saved_urls else None, details_text)

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

    queue_ct = get_queue_count()
    queue_note = f'<p class="mono">📦 Queued WhatsApp sends: <b>{queue_ct}</b> — <a href="/queue">Manage queue</a></p>' if queue_ct > 0 else ""

    # one reusable PIN input
    pin_field = ""
    if COUNTER_PASSWORD:
        pin_field = (
            '<input type="password" name="pin" placeholder="PIN" '
            'style="padding:8px;border:1px solid #ddd;border-radius:8px;min-width:120px" required>'
        )

    body = (
        '<div class="card" style="max-width:640px">'
        '<h2 style="margin-top:0">🧹 Cleans Completed Counter</h2>'
        f'<p style="font-weight:700">Current count: {get_counter()}</p>'
        f'{queue_note}'

        # counter controls
        '<form action="/counter/update" method="post" '
        'style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px">'
        f'{pin_field}'
        '<button type="submit" name="action" value="plus" '
        'style="background:#16a34a;color:#fff;border:0;border-radius:10px;padding:10px 14px;font-weight:700">➕ Add 1</button>'
        '<button type="submit" name="action" value="minus" '
        'style="background:#f59e0b;color:#fff;border:0;border-radius:10px;padding:10px 14px;font-weight:700">➖ Subtract 1</button>'
        '<button type="submit" name="action" value="reset" '
        'style="background:#ef4444;color:#fff;border:0;border-radius:10px;padding:10px 14px;font-weight:700">🔁 Reset total</button>'
        '</form>'

        '<hr style="margin:14px 0;border:0;border-top:1px solid #eee">'

        # reset completed marks
        '<h3 style="margin:0 0 8px">Reset completed marks</h3>'
        '<p class="legend" style="margin:6px 0 10px">'
        'Choose a scope. For <b>Clear ALL</b>, you must type <code>CONFIRM</code>.'
        '</p>'

        '<form action="/completed/reset" method="post" '
        'style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
        f'{pin_field}'
        '<input type="date" name="day" placeholder="YYYY-MM-DD" '
        'style="padding:8px;border:1px solid #ddd;border-radius:8px">'
        '<input type="text" name="flat" placeholder="Optional: Flat name (exact)" '
        'style="padding:8px;border:1px solid #ddd;border-radius:8px">'
        '<input type="text" name="confirm" placeholder="Type CONFIRM for Clear ALL" '
        'style="padding:8px;border:1px solid #ddd;border-radius:8px;flex:1;min-width:220px">'
        '<button type="submit" name="scope" value="day" '
        'style="background:#0ea5e9;color:#fff;border:0;border-radius:10px;padding:10px 14px;font-weight:700">Clear day</button>'
        '<button type="submit" name="scope" value="flat_day" '
        'style="background:#6366f1;color:#fff;border:0;border-radius:10px;padding:10px 14px;font-weight:700">Clear flat+day</button>'
        '<button type="submit" name="scope" value="all" '
        'style="background:#ef4444;color:#fff;border:0;border-radius:10px;padding:10px 14px;font-weight:700">Clear ALL</button>'
        '</form>'

        '<div style="margin-top:14px"><a href="/cleaner">⬅ Back to schedule</a></div>'
        '</div>'
    )
    return HTMLResponse(html_page(body))

@app.post("/completed/reset")
def completed_reset(
    scope: str = Form(...),                 # "day" | "flat_day" | "all"
    day: str = Form(default=""),            # YYYY-MM-DD
    flat: str = Form(default=""),
    confirm: str = Form(default=""),        # must equal "CONFIRM" for scope=all
    pin: str = Form(default=""),
    session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE),
):
    if not check_auth(session_token):
        return RedirectResponse(url="/login")

    if COUNTER_PASSWORD and pin != COUNTER_PASSWORD:
        return RedirectResponse(url="/counter", status_code=303)

    if scope == "all":
        if confirm.strip().upper() != "CONFIRM":
            return RedirectResponse(url="/counter", status_code=303)

    if scope == "flat_day" and day and flat:
        clear_completed(day_iso=day, flat=flat)
    elif scope == "day" and day:
        clear_completed(day_iso=day)
    elif scope == "all":
        clear_completed()

    return RedirectResponse(url="/counter", status_code=303)

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
# Queue management UI (PIN-gated)
# ---------------------------
@app.get("/queue", response_class=HTMLResponse)
def queue_page(session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    if not check_auth(session_token):
        return RedirectResponse(url="/login")

    q = _load_queue()
    items_html = []
    if q:
        for i, item in enumerate(q, start=1):
            ts = item.get("ts", "")
            cap = item.get("caption", "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            media = item.get("media_urls", [])
            first = media[0] if media else ""
            items_html.append(
                f'<div class="card"><div style="font-weight:700">#{i} — {ts}</div>'
                f'<div class="mono" style="white-space:pre-wrap;margin:6px 0">{cap}</div>'
                f'<div>Photos: <b>{len(media)}</b> {"• <a href=\'"+first+"\' target=\'_blank\'>first link</a>" if first else ""}</div></div>'
            )
    else:
        items_html.append('<p>No queued items.</p>')

    pin_field = ""
    if COUNTER_PASSWORD:
        pin_field = (
            '<input type="password" name="pin" placeholder="PIN" '
            'style="padding:8px;border:1px solid #ddd;border-radius:8px;min-width:120px" required>'
        )

    body = (
        '<div class="card" style="max-width:760px">'
        f'<h2 style="margin-top:0">📦 WhatsApp Queue ({len(q)})</h2>'
        + "".join(items_html) +
        '<form action="/queue/release" method="post" style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
        f'{pin_field}'
        '<button type="submit" style="background:#16a34a;color:#fff;border:0;border-radius:10px;padding:10px 14px;font-weight:700">▶️ Release now</button>'
        '</form>'
        '<form action="/queue/clear" method="post" style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
        f'{pin_field}'
        '<button type="submit" style="background:#ef4444;color:#fff;border:0;border-radius:10px;padding:10px 14px;font-weight:700">🗑 Clear queue</button>'
        '</form>'
        '<div style="margin-top:14px"><a href="/counter">⬅ Back to counter</a></div>'
        '</div>'
    )
    return HTMLResponse(html_page(body))

@app.post("/queue/release")
def queue_release(pin: str = Form(default=""), session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    if not check_auth(session_token):
        return RedirectResponse(url="/login")
    if COUNTER_PASSWORD and pin != COUNTER_PASSWORD:
        return RedirectResponse(url="/queue", status_code=303)
    _release_queue_and_send()
    return RedirectResponse(url="/queue", status_code=303)

@app.post("/queue/clear")
def queue_clear(pin: str = Form(default=""), session_token: Optional[str] = Cookie(default=None, alias=SESSION_COOKIE)):
    if not check_auth(session_token):
        return RedirectResponse(url="/login")
    if COUNTER_PASSWORD and pin != COUNTER_PASSWORD:
        return RedirectResponse(url="/queue", status_code=303)
    _save_queue([])
    return RedirectResponse(url="/queue", status_code=303)

# ---------------------------
# Twilio WhatsApp inbound webhook (auto-release queue)
# ---------------------------
@app.post("/wa/incoming")
async def wa_incoming(request: Request):
    """
    Twilio will POST here on inbound WhatsApp messages.
    We treat ANY inbound message as consent to open the 24h window,
    then immediately release queued media.
    """
    try:
        form = await request.form()
        from_num = (form.get("From") or "")
        body = (form.get("Body") or "").strip()
        print(f"Incoming WA from {from_num}: {body!r}")
    except Exception as e:
        print("Inbound parse error:", repr(e))

    _release_queue_and_send()
    return PlainTextResponse("OK")

# ---------------------------
# Local run
# ---------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
