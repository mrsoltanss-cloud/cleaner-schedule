import os
from datetime import date
from fastapi import FastAPI, Query
from fastapi.responses import PlainTextResponse
from booking_cleaner import (
    fetch_ics,
    parse_bookings,
    build_schedule_for_days,
    format_schedule,
)

app = FastAPI()

@app.get("/health", response_class=PlainTextResponse)
def health():
    return "ok"

@app.get("/", response_class=PlainTextResponse)
def home(days: int = Query(int(os.getenv("DEFAULT_DAYS", "14")), ge=1, le=60)):
    flats = {
        os.getenv("FLAT7_NAME", "Flat 7"): os.getenv("FLAT7_ICS_URL", ""),
        os.getenv("FLAT8_NAME", "Flat 8"): os.getenv("FLAT8_ICS_URL", ""),
        os.getenv("FLAT9_NAME", "Flat 9"): os.getenv("FLAT9_ICS_URL", ""),
    }

    ics_map = {name: fetch_ics(url) for name, url in flats.items() if url}
    bookings = parse_bookings(ics_map)
    schedule = build_schedule_for_days(bookings, start=date.today(), days=days)
    text = format_schedule(schedule)
    return text or "No check-ins or check-outs in the selected window."
