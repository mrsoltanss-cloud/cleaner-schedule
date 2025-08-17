from fastapi import FastAPI, Query, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, JSONResponse
import os, json, requests, uuid
from icalendar import Calendar
from datetime import datetime, date, timedelta
from typing import Dict, List
from twilio.rest import Client

app = FastAPI()

# ========= CONFIG =========
CLEAN_WINDOW = os.getenv("CLEAN_WINDOW", "10:00–16:00")

FLATS: Dict[str, Dict[str, str]] = {
    "Flat7": {"url": os.getenv("FLAT7_ICS_URL", ""), "nick": os.getenv("FLAT7_NICK", "Orange"), "colour": os.getenv("FLAT7_COLOUR", "#FF9800")},
    "Flat8": {"url": os.getenv("FLAT8_ICS_URL", ""), "nick": os.getenv("FLAT8_NICK", "Blue"),   "colour": os.getenv("FLAT8_COLOUR", "#2196F3")},
    "Flat9": {"url": os.getenv("FLAT9_ICS_URL", ""), "nick": os.getenv("FLAT9_NICK", "Green"),  "colour": os.getenv("FLAT9_COLOUR", "#4CAF50")},
}

# WhatsApp (Twilio Sandbox or real WhatsApp-enabled number)
TWILIO_ACCOUNT_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN    = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")   # e.g. whatsapp:+14155238886
TWILIO_WHATSAPP_TO   = os.getenv("TWILIO_WHATSAPP_TO", "")     # e.g. whatsapp:+44XXXXXXXXXX

# Completion ticks are stored here (ephemeral on free plans)
COMPLETIONS_FILE = "/tmp/completions.json"

# ========= HELPERS =========
def load_completions() -> Dict[str, Dict[str, bool]]:
    if not os.path.exists(COMPLETIONS_FILE):
        return {}
    try:
        with open(COMPLETIONS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_completions(data: Dict[str, Dict[str, bool]]):
    with open(COMPLETIONS_FILE, "w") as f:
        json.dump(data, f)

def send_whatsapp(text: str) -> bool:
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and TWILIO_WHATSAPP_TO):
        return False
    try:
        Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN).messages.create(
            from_=TWILIO_WHATSAPP_FROM, to=TWILIO_WHATSAPP_TO, body=text
        )
        return True
    except Exception as e:
        print("WhatsApp send failed:", e)
        return False

def fetch_calendar(ics_url: str) -> Calendar:
    if not ics_url:
        return Calendar()
    r = requests.get(ics_url, timeout=30)
    r.raise_for_status()
    return Calendar.from_ical(r.text)

def parse_bookings(flats: Dict[str, Dict[str, str]], days: int) -> Dict[str, List[Dict]]:
    today = date.today()
    end = today + timedelta(days=days)
    schedule: Dict[str, List[Dict]] = {}

    for flat, meta in flats.items():
        url = meta.get("url", "")
        if not url:
            continue
        try:
            cal = fetch_calendar(url)
        except Exception:
            continue

        for comp in cal.walk("vevent"):
            try:
                s = comp.decoded("dtstart").date()
                e = comp.decoded("dtend").date()
            except Exception:
                continue
            if today <= s <= end:
                schedule.setdefault(s.strftime("%a %d %b"), []).append({"flat": flat, "status": "in"})
            if today <= e <= end:
                schedule.setdefault(e.strftime("%a %d %b"), []).append({"flat": flat, "status": "out"})

    # same-day turnaround
    for day, events in list(schedule.items()):
        ins  = [x for x in events if x["status"] == "in"]
        outs = [x for x in events if x["status"] == "out"]
        for o in outs:
            for i in ins:
                if o["flat"] == i["flat"]:
                    try:
                        events.remove(o); events.remove(i)
                    except ValueError:
                        pass
                    events.append({"flat": o["flat"], "status": "turnaround"})
                    break
    return schedule

# ========= HTML (inline upload) =========
def html_cleaner_view(schedule: Dict[str, List[Dict]], flats: Dict[str, Dict[str, str]]) -> str:
    today_str = date.today().strftime("%a %d %b")
    completions = load_completions()

    html = f"""<!doctype html>
<meta charset="utf-8">
<meta http-equiv="refresh" content="300">
<title>Cleaner Schedule</title>
<style>
  :root {{ --red:#d93025; --green:#1f9d55; --amber:#b76e00; --muted:#666; }}
  body {{ font-family: system-ui, Arial, sans-serif; padding: 22px; background:#fafafa; }}
  h1 {{ margin:0 0 6px }}
  .sub {{ color: var(--muted); margin-bottom: 18px }}
  .day {{ margin: 22px 0; padding: 12px; background:#fff; border-radius:12px; box-shadow:0 2px 5px rgba(0,0,0,.06) }}
  .today {{ background:#111; color:#fff; padding:3px 8px; border-radius:999px; font-size:12px; margin-left:8px }}
  .pill {{ display:inline-block; margin:4px 6px 4px 0; padding:4px 8px; border-radius:999px; font-weight:700; color:#fff }}
  .in {{ color: var(--green); font-weight:700 }}
  .out {{ color: var(--red); font-weight:700 }}
  .turn {{ color: var(--amber); font-weight:800 }}
  .win {{ font-style:italic; color: var(--muted); margin:4px 0 8px }}
  .btn {{ padding:6px 12px; border:none; border-radius:6px; background:#2196F3; color:#fff; cursor:pointer }}
  .btn[disabled]{{ opacity:.6; cursor:default }}
  .tick {{ color: var(--green); font-weight:700 }}
  .tiny {{ font-size:12px; color: var(--muted) }}
</style>
<body>
<h1>Cleaner Schedule</h1>
<div class="sub">Check-out in red • Check-in in green • Same-day turnover highlighted • Clean {CLEAN_WINDOW}</div>

<script>
async function uploadFor(flat, day, inputEl, btnEl, tickEl) {{
  if (!inputEl.files || inputEl.files.length === 0) return;
  btnEl.disabled = true; btnEl.textContent = 'Uploading…';
  const fd = new FormData();
  for (const f of inputEl.files) fd.append('files', f);
  fd.append('flat', flat);
  fd.append('date', day);
  try {{
    const res = await fetch('/upload', {{ method: 'POST', body: fd }});
    if (!res.ok) throw new Error('Upload failed');
    const js = await res.json();
    tickEl.textContent = '✅ Cleaning completed';
    btnEl.remove(); inputEl.remove();
  }} catch (e) {{
    alert('Upload failed. Please try again.'); 
    btnEl.disabled = false; btnEl.textContent = '📸 Upload Photos';
  }}
}}
</script>
"""

    # sort days chronologically
    def dkey(d: str) -> datetime:
        return datetime.strptime(d, "%a %d %b")

    if not schedule:
        html += f"<p><b>No activity found.</b> Try a longer window: <a href='/cleaner?days=30'>/cleaner?days=30</a> or see <a href='/debug'>/debug</a>.</p>"

    for day, events in sorted(schedule.items(), key=lambda kv: dkey(kv[0])):
        badge = "<span class='today'>TODAY</span>" if day == today_str else ""
        html += f"<div class='day'><h2>{day} {badge}</h2>"

        for ev in events:
            flat = ev["flat"]
            meta = flats.get(flat, {})
            nick = meta.get("nick", flat)
            colour = meta.get("colour", "#444")
            pill = f"<span class='pill' style='background:{colour}'>{nick}</span>"

            done = completions.get(day, {}).get(flat, False)

            if ev["status"] == "in":
                html += f"<p>{pill} <span class='in'>Check-in</span></p>"
            elif ev["status"] == "out":
                html += f"<p>{pill} <span class='out'>Check-out</span></p>"
                html += f"<div class='win'>🧹 Clean between <b>{CLEAN_WINDOW}</b></div>"
            else:  # turnaround
                html += f"<p>{pill} <span class='turn'>Check-out → Clean → Check-in (same day)</span></p>"
                html += f"<div class='win'>🧹 Clean between <b>{CLEAN_WINDOW}</b></div>"

            # show upload button / tick only when cleaning is needed (out/turnaround)
            if ev["status"] in ("out", "turnaround"):
                if done:
                    html += "<p class='tick'>✅ Cleaning completed</p>"
                else:
                    # unique ids per card
                    fid = f"file_{uuid.uuid4().hex[:8]}"
                    bid = f"btn_{uuid.uuid4().hex[:8]}"
                    tid = f"tick_{uuid.uuid4().hex[:8]}"
                    html += f"""
                    <p>
                      <input id="{fid}" type="file" accept="image/*" multiple style="display:none" />
                      <button id="{bid}" class="btn" onclick="document.getElementById('{fid}').click()">📸 Upload Photos</button>
                      <span id="{tid}" class="tiny"></span>
                    </p>
                    <script>
                      (function() {{
                        const f = document.getElementById('{fid}');
                        const b = document.getElementById('{bid}');
                        const t = document.getElementById('{tid}');
                        f.addEventListener('change', () => uploadFor('{flat}', '{day}', f, b, t));
                      }})();
                    </script>
                    """

        html += "</div>"

    html += "</body>"
    return html

# ========= ROUTES =========
@app.get("/health", response_class=PlainTextResponse)
def health(): return "ok"

@app.get("/", response_class=HTMLResponse)
def root(): return "<h1>Cleaner Schedule</h1><p>Open <a href='/cleaner?days=14'>Cleaner View</a></p>"

@app.get("/cleaner", response_class=HTMLResponse)
def cleaner(days: int = Query(14, ge=1, le=90)):
    schedule = parse_bookings(FLATS, days)
    return html_cleaner_view(schedule, FLATS)

# POST-only upload API (used by the inline button)
@app.post("/upload", response_class=JSONResponse)
async def upload_submit(flat: str = Form(...), date: str = Form(...), files: List[UploadFile] = File(...)):
    # save to /tmp (ephemeral on free plan)
    saved = []
    for f in files:
        data = await f.read()
        safe = f.filename.replace(" ", "_")
        path = f"/tmp/{flat}_{date}_{safe}"
        with open(path, "wb") as out:
            out.write(data)
        saved.append(safe)

    # mark complete
    completions = load_completions()
    completions.setdefault(date, {})
    completions[date][flat] = True
    save_completions(completions)

    # notify via WhatsApp (best effort)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    file_list = ", ".join(saved) if saved else "no files"
    send_whatsapp(f"✅ Cleaning complete\nFlat: {flat}\nDate: {date}\nPhotos: {file_list}\nTime: {ts}")

    return {"ok": True, "saved": saved}

# simple diagnostics
@app.get("/debug", response_class=PlainTextResponse)
def debug(days: int = Query(14, ge=1, le=120)):
    lines = ["Loaded flats:"]
    for k, v in FLATS.items():
        url = (v.get("url") or "").strip()
        lines.append(f"  {k}: url={'SET' if url else 'MISSING'} nick={v.get('nick')} colour={v.get('colour')}")
    try:
        sched = parse_bookings(FLATS, days)
        lines.append(f"\nDays with activity in next {days} days: {len(sched)}")
        for d in sorted(sched.keys(), key=lambda s: datetime.strptime(s, '%a %d %b')):
            lines.append(f"  {d}: " + ", ".join([f"{e['flat']}:{e['status']}" for e in sched[d]]))
    except Exception as e:
        lines.append(f"\nERROR building schedule: {e!r}")
    return "\n".join(lines)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
