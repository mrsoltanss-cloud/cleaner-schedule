from fastapi import FastAPI, UploadFile, File, Form, Query, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
import os, json, requests
from icalendar import Calendar
from datetime import datetime, date, timedelta
from typing import Dict, List, Tuple

# --- Twilio (WhatsApp) ---
from twilio.rest import Client

app = FastAPI()

# =========================
# Configuration
# =========================

# Cleaning window shown on the page
CLEAN_WINDOW = os.getenv("CLEAN_WINDOW", "10:00–16:00")

# Flats (keep simple for now; you already use these env vars)
FLATS: Dict[str, Dict[str, str]] = {
    "Flat7": {
        "url": os.getenv("FLAT7_ICS_URL", ""),
        "nick": os.getenv("FLAT7_NICK", "Orange"),
        "colour": os.getenv("FLAT7_COLOUR", "#FF9800"),
    },
    "Flat8": {
        "url": os.getenv("FLAT8_ICS_URL", ""),
        "nick": os.getenv("FLAT8_NICK", "Blue"),
        "colour": os.getenv("FLAT8_COLOUR", "#2196F3"),
    },
    "Flat9": {
        "url": os.getenv("FLAT9_ICS_URL", ""),
        "nick": os.getenv("FLAT9_NICK", "Green"),
        "colour": os.getenv("FLAT9_COLOUR", "#4CAF50"),
    },
}

# WhatsApp / Twilio env vars
TWILIO_ACCOUNT_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN    = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")   # e.g. 'whatsapp:+14155238886' (sandbox)
TWILIO_WHATSAPP_TO   = os.getenv("TWILIO_WHATSAPP_TO", "")     # e.g. 'whatsapp:+44XXXXXXXXXX' (your phone)

# Where we mark completed cleans (ephemeral on free plan)
COMPLETIONS_FILE = "/tmp/completions.json"


# =========================
# Helpers
# =========================

def load_completions() -> Dict[str, Dict[str, bool]]:
    if not os.path.exists(COMPLETIONS_FILE):
        return {}
    try:
        with open(COMPLETIONS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_completions(data: Dict[str, Dict[str, bool]]) -> None:
    with open(COMPLETIONS_FILE, "w") as f:
        json.dump(data, f)

def send_whatsapp(text: str) -> bool:
    """Send a WhatsApp message to you. Returns True/False; never crashes the request."""
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and TWILIO_WHATSAPP_TO):
        # Not configured -> skip silently
        return False
    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=TWILIO_WHATSAPP_TO,
            body=text
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
    """
    Returns: { 'Sat 16 Aug': [ {'flat':'Flat7','status':'out'|'in'|'turnaround'}, ... ], ... }
    """
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
            # Safe-fail if ICS fetch/parse fails
            continue

        for comp in cal.walk("vevent"):
            try:
                dtstart = comp.decoded("dtstart").date()
                dtend   = comp.decoded("dtend").date()
            except Exception:
                continue

            if today <= dtstart <= end:
                key = dtstart.strftime("%a %d %b")
                schedule.setdefault(key, []).append({"flat": flat, "status": "in"})
            if today <= dtend <= end:
                key = dtend.strftime("%a %d %b")
                schedule.setdefault(key, []).append({"flat": flat, "status": "out"})

    # Merge same-day in+out into a single 'turnaround'
    for day, events in list(schedule.items()):
        ins  = [e for e in events if e["status"] == "in"]
        outs = [e for e in events if e["status"] == "out"]
        for o in outs:
            for i in ins:
                if o["flat"] == i["flat"]:
                    try:
                        events.remove(o)
                        events.remove(i)
                    except ValueError:
                        pass
                    events.append({"flat": o["flat"], "status": "turnaround"})
                    break
    return schedule


# =========================
# UI (HTML)
# =========================

def html_cleaner_view(schedule: Dict[str, List[Dict]], flats: Dict[str, Dict[str, str]]) -> str:
    """Render the coloured cleaner page with TODAY badge, buttons, and auto-refresh."""
    today_str = date.today().strftime("%a %d %b")
    completions = load_completions()

    html = f"""<!doctype html>
<meta charset="utf-8">
<meta http-equiv="refresh" content="300"> <!-- auto-refresh every 5 minutes -->
<title>Cleaner Schedule</title>
<style>
  :root {{
    --red:#d93025; --green:#1f9d55; --amber:#b76e00; --muted:#666; --line:#e7e7e7;
  }}
  *{{box-sizing:border-box}}
  body {{ font-family: system-ui, Arial, sans-serif; padding: 22px; background: #fafafa; color: #111; }}
  h1 {{ margin: 0 0 6px }}
  .sub {{ color: var(--muted); margin-bottom: 18px }}
  .day {{ margin: 22px 0; padding: 12px 12px 8px; background: #fff; border-radius: 12px; box-shadow: 0 2px 5px rgba(0,0,0,.06); }}
  .today-badge {{ background:#111;color:#fff;padding:4px 8px;border-radius:999px;margin-left:10px;font-size:12px }}
  .flatpill {{ display:inline-block; margin:4px 6px 4px 0; padding:4px 8px; border-radius:999px; font-weight:700; color:#fff; }}
  .checkin {{ color: var(--green); font-weight:700 }}
  .checkout {{ color: var(--red); font-weight:700 }}
  .turnaround {{ color: var(--amber); font-weight:800 }}
  .clean-window {{ font-style: italic; color: var(--muted); margin: 4px 0 8px }}
  .btn {{ padding: 6px 12px; border: none; border-radius: 6px; background: #2196F3; color: white; cursor: pointer; }}
  .btn:hover {{ background: #1976D2; }}
  .tick {{ color: var(--green); font-weight:700; }}
</style>
<body>
<h1>Cleaner Schedule</h1>
<div class="sub">Check-out in red • Check-in in green • Same-day turnover highlighted • Clean {CLEAN_WINDOW}</div>
"""

    # Sort days chronologically
    def keyer(k: str) -> datetime:
        return datetime.strptime(k, "%a %d %b")

    for day, events in sorted(schedule.items(), key=lambda kv: keyer(kv[0])):
        badge = " <span class='today-badge'>TODAY</span>" if day == today_str else ""
        html += f"<div class='day'><h2>{day}{badge}</h2>"

        if not events:
            html += "<p>No activity.</p>"
        else:
            for ev in events:
                flat = ev["flat"]
                meta = flats.get(flat, {})
                nick = meta.get("nick", flat)
                colour = meta.get("colour", "#444")
                pill = f"<span class='flatpill' style='background:{colour}'>{nick}</span>"

                is_done = completions.get(day, {}).get(flat, False)

                if ev["status"] == "out":
                    html += f"<p>{pill} <span class='checkout'>Check-out</span></p>"
                    html += f"<div class='clean-window'>🧹 Clean between <b>{CLEAN_WINDOW}</b></div>"
                    if is_done:
                        html += "<p class='tick'>✅ Cleaning completed</p>"
                    else:
                        html += f"""
                        <form action="/upload" method="get" style="display:inline;">
                          <input type="hidden" name="flat" value="{flat}">
                          <input type="hidden" name="date" value="{day}">
                          <button type="submit" class="btn">📸 Upload Photos</button>
                        </form>
                        """
                elif ev["status"] == "in":
                    html += f"<p>{pill} <span class='checkin'>Check-in</span></p>"
                elif ev["status"] == "turnaround":
                    html += f"<p>{pill} <span class='turnaround'>Check-out → Clean → Check-in (same day)</span></p>"
                    html += f"<div class='clean-window'>🧹 Clean between <b>{CLEAN_WINDOW}</b></div>"
                    if is_done:
                        html += "<p class='tick'>✅ Cleaning completed</p>"
                    else:
                        html += f"""
                        <form action="/upload" method="get" style="display:inline;">
                          <input type="hidden" name="flat" value="{flat}">
                          <input type="hidden" name="date" value="{day}">
                          <button type="submit" class="btn">📸 Upload Photos</button>
                        </form>
                        """
        html += "</div>"

    html += "</body>"
    return html


# =========================
# Routes
# =========================

@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"

@app.get("/", response_class=HTMLResponse)
def root():
    # Simple landing — you can redirect to /cleaner if you prefer
    return """<h1>Cleaner Schedule</h1><p>Open <a href='/cleaner?days=14'>Cleaner View</a></p>"""

@app.get("/cleaner", response_class=HTMLResponse)
def cleaner(days: int = Query(14, ge=1, le=90)):
    schedule = parse_bookings(FLATS, days)
    return html_cleaner_view(schedule, FLATS)

@app.get("/upload", response_class=HTMLResponse)
def upload_form(flat: str = "", date: str = ""):
    return f"""
    <h2>Upload Photos for {flat} on {date}</h2>
    <form action="/upload" method="post" enctype="multipart/form-data">
      <input type="hidden" name="flat" value="{flat}">
      <input type="hidden" name="date" value="{date}">
      <input type="file" name="files" accept="image/*" multiple required>
      <button type="submit">Upload</button>
    </form>
    """

@app.post("/upload", response_class=HTMLResponse)
async def upload_submit(flat: str = Form(...), date: str = Form(...), files: List[UploadFile] = File(...)):
    # Save to /tmp (ephemeral on Render free)
    saved = []
    for f in files:
        data = await f.read()
        safe_name = f.filename.replace(" ", "_")
        path = f"/tmp/{flat}_{date}_{safe_name}"
        with open(path, "wb") as out:
            out.write(data)
        saved.append(safe_name)

    # Mark complete
    completions = load_completions()
    completions.setdefault(date, {})
    completions[date][flat] = True
    save_completions(completions)

    # WhatsApp notify (best-effort)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    file_list = ", ".join(saved) if saved else "no files"
    send_whatsapp(f"✅ Cleaning complete\nFlat: {flat}\nDate: {date}\nPhotos: {file_list}\nTime: {ts}")

    return f"<p>✅ Uploaded {len(saved)} file(s) for {flat} on {date}. Cleaning marked complete!</p><p><a href='/cleaner?days=14'>Back to Schedule</a></p>"

@app.get("/test-whatsapp", response_class=PlainTextResponse)
def test_whatsapp():
    ok = send_whatsapp("🔔 Test from Cleaner Schedule — if you see this, WhatsApp is configured.")
    if ok:
        return "Sent test WhatsApp ✔️"
    raise HTTPException(status_code=400, detail="WhatsApp not configured — check your TWILIO_* env vars.")


# =========================
# Local run
# =========================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))

