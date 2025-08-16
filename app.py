
import os
import traceback
from datetime import datetime
from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse, FileResponse
from zoneinfo import ZoneInfo

from booking_cleaner import (
    fetch_ics,
    parse_bookings,
    build_schedule_for_days,
    format_schedule_whatsapp,
    schedule_to_rows,
    render_schedule_pdf,
    maybe_send_whatsapp,
    maybe_send_email,
)

from sheets_integration import push_schedule_to_google_sheet

TZ = os.getenv("TIMEZONE", "Europe/London")
LONDON = ZoneInfo(TZ)

FLATS = {
    os.getenv("FLAT7_NAME", "Flat 7"): os.getenv("FLAT7_ICS_URL", ""),
    os.getenv("FLAT8_NAME", "Flat 8"): os.getenv("FLAT8_ICS_URL", ""),
    os.getenv("FLAT9_NAME", "Flat 9"): os.getenv("FLAT9_ICS_URL", ""),
}

DEFAULT_DAYS = int(os.getenv("DEFAULT_DAYS", "14"))
RUN_HOUR = int(os.getenv("RUN_HOUR", "8"))

WHATSAPP_TO = os.getenv("WHATSAPP_TO", "")
WHATSAPP_FROM = os.getenv("WHATSAPP_FROM", "")
TWILIO_SID = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", "")
EMAIL_TO = os.getenv("EMAIL_TO", "")

GOOGLE_ENABLED = bool(os.getenv("GOOGLE_SHEET_ID", ""))

app = FastAPI()

@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"

def build_all(days: int):
    now = datetime.now(LONDON).date()
    ics_map = {name: fetch_ics(url) for name, url in FLATS.items() if url}
    bookings = parse_bookings(ics_map, timezone=LONDON)
    schedule = build_schedule_for_days(bookings, start_date=now, days=days)
    text = format_schedule_whatsapp(schedule)
    rows = schedule_to_rows(schedule)
    return schedule, text, rows

@app.get("/", response_class=PlainTextResponse)
def root(days: int = Query(DEFAULT_DAYS, ge=1, le=60)):
    try:
        _, text, _ = build_all(days)
        return text
    except Exception as e:
        traceback.print_exc()
        return f"Error: {e}"

@app.get("/pdf")
def pdf(days: int = Query(DEFAULT_DAYS, ge=1, le=60)):
    try:
        schedule, _, _ = build_all(days)
        path = f"/tmp/cleaner_schedule_{datetime.now(LONDON).strftime('%Y%m%d')}.pdf"
        render_schedule_pdf(schedule, path, title="Cleaner Schedule")
        return FileResponse(path, media_type="application/pdf", filename=os.path.basename(path))
    except Exception as e:
        traceback.print_exc()
        return PlainTextResponse(f"Error: {e}", status_code=500)

@app.get("/run-now", response_class=PlainTextResponse)
def run_now(days: int = Query(DEFAULT_DAYS, ge=1, le=60)):
    try:
        schedule, text, rows = build_all(days)

        sheets_result = ""
        if GOOGLE_ENABLED:
            sheets_result = push_schedule_to_google_sheet(rows)

        pdf_path = f"/tmp/cleaner_schedule_{datetime.now(LONDON).strftime('%Y%m%d')}.pdf"
        render_schedule_pdf(schedule, pdf_path, title="Cleaner Schedule")

        send_results = []
        if WHATSAPP_TO and WHATSAPP_FROM and TWILIO_SID and TWILIO_TOKEN:
            send_results.append(maybe_send_whatsapp(text, WHATSAPP_TO, WHATSAPP_FROM, TWILIO_SID, TWILIO_TOKEN))
        if SMTP_HOST and SMTP_USER and SMTP_PASS and EMAIL_FROM and EMAIL_TO:
            send_results.append(maybe_send_email(text, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_FROM, EMAIL_TO, attachment_path=pdf_path))

        results_str = "\n".join([sheets_result] + send_results) if (sheets_result or send_results) else "No external outputs configured; returning text + PDF at /pdf."
        return f"{text}\n\n---\n{results_str}"
    except Exception as e:
        traceback.print_exc()
        return f"Error: {e}"

if os.getenv("ENABLE_LOOP", "true").lower() == "true":
    import threading, time
    def loop():
        last_run_date = None
        while True:
            try:
                now_dt = datetime.now(LONDON)
                if now_dt.hour == RUN_HOUR and (last_run_date != now_dt.date()):
                    try:
                        schedule, text, rows = build_all(DEFAULT_DAYS)
                        if GOOGLE_ENABLED:
                            push_schedule_to_google_sheet(rows)
                        pdf_path = f"/tmp/cleaner_schedule_{now_dt.strftime('%Y%m%d')}.pdf"
                        render_schedule_pdf(schedule, pdf_path, title="Cleaner Schedule")
                        if WHATSAPP_TO and WHATSAPP_FROM and TWILIO_SID and TWILIO_TOKEN:
                            maybe_send_whatsapp(text, WHATSAPP_TO, WHATSAPP_FROM, TWILIO_SID, TWILIO_TOKEN)
                        if SMTP_HOST and SMTP_USER and SMTP_PASS and EMAIL_FROM and EMAIL_TO:
                            maybe_send_email(text, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, EMAIL_FROM, EMAIL_TO, attachment_path=pdf_path)
                    finally:
                        last_run_date = now_dt.date()
                time.sleep(60)
            except Exception:
                traceback.print_exc()
                time.sleep(60)
    t = threading.Thread(target=loop, daemon=True)
    t.start()
