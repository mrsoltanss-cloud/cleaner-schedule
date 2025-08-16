from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, PlainTextResponse
import requests
import json, os
from icalendar import Calendar
from datetime import datetime, date, timedelta
from typing import Dict, List
# --- WhatsApp via Twilio ---
from twilio.rest import Client

app = FastAPI()

# ------------ CONFIG: Flats -------------
FLATS = {
    "Flat7": {
        "url": os.getenv("FLAT7_ICS_URL", ""),
        "nick": "Orange",
        "colour": "#FF9800"
    },
    "Flat8": {
        "url": os.getenv("FLAT8_ICS_URL", ""),
        "nick": "Blue",
        "colour": "#2196F3"
    },
    "Flat9": {
        "url": os.getenv("FLAT9_ICS_URL", ""),
        "nick": "Green",
        "colour": "#4CAF50"
    },
}

CLEAN_WINDOW = os.getenv("CLEAN_WINDOW", "10:00–16:00")

# ------------ Completion Store ----------
COMPLETIONS_FILE = "/tmp/completions.json"

def load_completions():
    if not os.path.exists(COMPLETIONS_FILE):
        return {}
    try:
        with open(COMPLETIONS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_completions(data):
    with open(COMPLETIONS_FILE, "w") as f:
        json.dump(data, f)

# ------------ WhatsApp Notify -----------
TWILIO_SID   = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
WA_FROM       = os.getenv("TWILIO_WHATSAPP_FROM", "")  # e.g. 'whatsapp:+14155238886' (Twilio sandbox)
WA_TO         = os.getenv("TWILIO_WHATSAPP_TO", "")    # e.g. 'whatsapp:+4477xxxxxxx' (your phone)

def _norm_wa(s: str) -> str:
    if not s:
        return s
    return s if s.startswith("whatsapp:") else f"whatsapp:{s}"

def send_whatsapp(text: str) -> bool:
    if not (TWILIO_SID and TWILIO_TOKEN and WA_FROM and WA_TO):
        # Missing config → silently skip
        return False
    try:
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        client.messages.create(
            from_=_norm_wa(WA_FROM),
            to=_norm_wa(WA_TO),
            body=text,
        )
        return True
    except Exception as e:
        # Don’t crash the request if WhatsApp fails
        print("WhatsApp send failed:", e)
        return False

# ------------ Calendar Helpers ----------
def fetch_calendar(ics_url: str) -> Calendar:
    if not ics_url:
        return Calendar()
    r = requests.get(ics_url, timeout=30)
    r.raise_for_status()
    return Calendar.from_ical(r.text)

def parse_bookings(flats: Dict[str, Dict[str, str]], days: int = 14) -> Dict[str, List[Dict]]:
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
                start = comp.decoded("dtstart").date()
                endd = comp.decoded("dtend").date()
            except Exception:
                continue

            if today <= start <= end:
                schedule.setdefault(start.strftime("%a %d %b"), []).append({"flat": flat, "status": "in"})
            if today <= endd <= end:
                schedule.setdefault(endd.strftime("%a %d %b"), []).append({"flat": flat, "status": "out"})

    # Merge same-day in+out into "turnaround"
    for day, events in list(schedule.items()):
        ins  = [e for e in events if e["status"] == "in"]
        outs = [e for e in events if e["status"] == "out"]
        for o in outs:
            for i in ins:
                if o["flat"] == i["flat"]:
                    events.remove(o)
                    events.remove(i)
                    events.append({"flat": o["flat"], "status": "turnaround"})
                    break
    return schedule

# ------------ HTML View -----------------
def html_cleaner_view(schedule: Dict[str, List[Dict]], flats: Dict[str, Dict[str, str]]) -> str:
    today_str = date.today().strftime("%a %d %b")
    completions = load_completions()

    html = f"""<!doctype html>
<meta charset="utf-8">
<meta http-equiv="refresh" content="300"> <!-- auto refresh every 5 min -->
<title>Cleaner Schedule</title>
<style>
  :root {{
    --red:#d93025; --green:#1f9d55; --amber:#b76e00; --muted:#666; --line:#e7e7e7;
  }}
  body {{ font-family: Arial, sans-serif; padding: 20px; background: #fafafa; }}
  h1 {{ text-align: left; margin: 0 0 6px }}
  .sub {{ color: var(--muted); margin-bottom: 16px }}
  .day {{ margin: 20px 0; padding: 10px; background: #fff; border-radius: 12px; box-shadow: 0 2px 5px rgba(0,0,0,0.06); }}
  .today-badge {{ background: #111; color: #fff; padding: 4px 8px; border-radius: 999px; margin-left: 10px; font-size: 12px; }}
  .flatpill {{ display: inline-block; margin: 4px 6px 4px 0; padding: 4px 8px; border-radius: 999px; font-weight: 700; color: #fff; }}
  .checkin {{ color: var(--green); font-weight: 700; }}
  .checkout {{ color: var(--red); font-weight: 700; }}
  .turnaround {{ color: var(--amber); font-weight: 800; }}
  .clean-window {{ font-style: italic; color: var(--muted); margin-top: 4px; }}
  .btn {{ padding: 6px 12px; border: none; border-radius: 6px; background: #2196F3; color: white; cursor: pointer; }}
  .btn:hover {{ background: #1976D2; }}
  .tick {{ color: var(--green); font-weight: 700; }}
</style>
<body>
<h1>Cleaner Schedule</h1>
<div class="sub">Check-out in red • Check-in in green • Same-day turnover highlighted • Clean {CLEAN_WINDOW}</div>
"""

    for day, events in sorted(schedule.items(), key=lambda kv: datetime.strptime(kv[0], "%a %d %b")):
        badge = " <span class='today-badge'>TODAY</span>" if day == today_str else ""
        html += f"<div class='day'><h2>{day}{badge}</h2>"
        if not events:
            html += "<p>No activity.</p>"
        else:
            # group by flat to avoid duplicates if any
            for ev in events:
                flat = ev["flat"]
                meta = flats.get(flat, {})
                nick = meta.get("nick", flat)
                colour = meta.get("colour", "#444")
                pill = f"<span class='flatpill' style='background:{colour}'>{nick}</span>"

                # has this flat/day been marked complete?
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

# ------------ Routes --------------------
@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"

@app.get("/", response_class=HTMLResponse)
def root():
    return """<h1>Cleaner Schedule</h1><p>Open <a href='/cleaner?days=14'>Cleaner View</a></p>"""

@app.get("/cleaner", response_class=HTMLResponse)
def cleaner(days: int = 14):
    schedule = parse_bookings(FLATS, days)
    return html_cleaner_view(schedule, FLATS)

@app.get("/upload", response_class=HTMLResponse)
def upload_form(flat: str = "", date: str = ""):
    return f"""
    <h2>Upload Photos for {flat} on {date}</h2>
    <form action="/upload" method="post" enctype="multipart/form-data">
      <input type="hidden" name="flat" value="{flat}">
      <input type="hidden" name="date" value="{date}">
      <input type="file" name="files" accept="image/*" multiple>
      <button type="submit">Upload</button>
    </form>
    """

@app.post("/upload", response_class=HTMLResponse)
async def upload_submit(flat: str = Form(...), date: str = Form(...), files: List[UploadFile] = File(...)):
    # Save photos to /tmp (ephemeral on free plan)
    saved_names = []
    for f in files:
        content = await f.read()
        save_path = f"/tmp/{flat}_{date}_{f.filename}".replace(" ", "_")
        with open(save_path, "wb") as out:
            out.write(content)
        saved_names.append(f.filename)

    # Mark completion
    completions = load_completions()
    completions.setdefault(date, {})
    completions[date][flat] = True
    save_completions(completions)

    # Send WhatsApp notification
    uploaded_list = ", ".join(saved_names) if saved_names else "no file names"
    msg = f"✅ Cleaning complete\nFlat: {flat}\nDate: {date}\nPhotos: {uploaded_list}\nTime: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    send_whatsapp(msg)

    return f"<p>✅ Uploaded {len(saved_names)} file(s) for {flat} on {date}. Cleaning marked complete!</p><p><a href='/cleaner?days=14'>Back to Schedule</a></p>"

# ------------ Local run (ignored on Render) -----------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
