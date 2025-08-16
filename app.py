from fastapi import FastAPI
from booking_cleaner import generate_schedule

app = FastAPI()

@app.get("/")
def home(days: int = 14):
    """Show the cleaner schedule for the next N days (default 14)."""
    return generate_schedule(days)

@app.get("/health")
def health():
    """Health check endpoint."""
    return {"status": "ok"}
