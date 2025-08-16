
# Auto Cleaner Schedule (Booking.com iCal → WhatsApp/Email/Google Sheets/PDF)

Endpoints:
- `/` — Next N days of check-ins/outs (default 14)
- `/pdf` — Printable PDF of schedule
- `/run-now` — Build schedule, push to Google Sheet, create PDF, send WhatsApp/Email if configured
- `/health` — Status check

**Deploy on Railway (quick):**
1) Create a new Railway project → "Deploy from Repo" or "Deploy from Template". If uploading directly, drag these files in.  
2) Add Environment Variables from `.env.sample` (paste your iCal URLs, etc.).  
3) Set a service with `requirements.txt` and `Procfile`.  
4) Deploy → Open the service URL:
   - `/health` → should return `ok`
   - `/` → shows schedule
   - `/run-now` → sends (if configured) and updates Google Sheet/PDF

**Google Sheets config:**
- Create a Google service account; share your Sheet with that service account email (Editor).  
- Set `GOOGLE_SERVICE_ACCOUNT_JSON` (paste JSON), `GOOGLE_SHEET_ID`, and `SHEET_TAB_NAME`.  

**WhatsApp (Twilio) optional:**
- Set `TWILIO_SID`, `TWILIO_TOKEN`, `WHATSAPP_FROM`, `WHATSAPP_TO`.  
- Use WhatsApp sandbox or a WhatsApp-enabled number.

**Email optional:**
- Use SMTP creds; for Gmail, use an App Password.

**Local run:**
```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export $(cat .env.sample | xargs)  # then override secrets properly
uvicorn app:app --reload
```
