
import os
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple
import requests
from zoneinfo import ZoneInfo
from icalendar import Calendar

def fetch_ics(url: str) -> str:
    if not url:
        return ""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text

def parse_bookings(ics_map: Dict[str, str], timezone: ZoneInfo) -> Dict[str, List[Tuple[date, date]]]:
    results: Dict[str, List[Tuple[date, date]]] = {}
    for flat_name, ics_text in ics_map.items():
        c = Calendar.from_ical(ics_text)
        events: List[Tuple[date, date]] = []
        for component in c.walk():
            if component.name != "VEVENT":
                continue
            dtstart = component.get("dtstart").dt
            dtend = component.get("dtend").dt
            if hasattr(dtstart, "astimezone"):
                dtstart = dtstart.astimezone(timezone).date()
            if hasattr(dtend, "astimezone"):
                dtend = dtend.astimezone(timezone).date()
            if dtend <= dtstart:
                continue
            events.append((dtstart, dtend))
        events.sort(key=lambda x: (x[0], x[1]))
        results[flat_name] = events
    return results

def build_schedule_for_days(bookings: Dict[str, List[Tuple[date, date]]], start_date: date, days: int = 14) -> Dict[date, Dict[str, Dict[str, bool]]]:
    schedule: Dict[date, Dict[str, Dict[str, bool]]] = {}
    for i in range(days):
        d = start_date + timedelta(days=i)
        day_map: Dict[str, Dict[str, bool]] = {}
        for flat_name, events in bookings.items():
            ci = any(evt[0] == d for evt in events)
            co = any(evt[1] == d for evt in events)
            if ci or co:
                day_map[flat_name] = {"check_in": ci, "check_out": co}
        if day_map:
            schedule[d] = day_map
    return schedule

def format_schedule_whatsapp(schedule: Dict[date, Dict[str, Dict[str, bool]]]) -> str:
    if not schedule:
        return "No check-ins or check-outs in the selected window."
    lines: List[str] = []
    for d in sorted(schedule.keys()):
        items: List[str] = []
        for flat_name, flags in sorted(schedule[d].items()):
            ci = flags.get("check_in", False)
            co = flags.get("check_out", False)
            if ci and co:
                items.append(f"{flat_name}: out/clean/in")
            elif co:
                items.append(f"{flat_name}: out/clean")
            elif ci:
                items.append(f"{flat_name}: check-in")
        if items:
            lines.append(f"{d.strftime('%a %d %b')} — " + "; ".join(items))
    return "\n".join(lines) if lines else "No check-ins or check-outs in the selected window."

def schedule_to_rows(schedule: Dict[date, Dict[str, Dict[str, bool]]]) -> List[List[str]]:
    rows: List[List[str]] = [["Date", "Flat", "Action"]]
    for d in sorted(schedule.keys()):
        for flat_name, flags in sorted(schedule[d].items()):
            ci = flags.get("check_in", False)
            co = flags.get("check_out", False)
            if ci and co:
                action = "Check-out / Clean / Check-in"
            elif co:
                action = "Check-out / Clean"
            elif ci:
                action = "Check-in"
            else:
                action = ""
            rows.append([d.strftime("%Y-%m-%d (%a)"), flat_name, action])
    return rows

def render_schedule_pdf(schedule: Dict[date, Dict[str, Dict[str, bool]]], output_path: str, title: str = "Cleaner Schedule") -> str:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas
    from reportlab.lib.units import mm

    c = canvas.Canvas(output_path, pagesize=A4)
    width, height = A4
    left = 20 * mm
    top = height - 20 * mm
    line_height = 7 * mm

    c.setFont("Helvetica-Bold", 16)
    c.drawString(left, top, title)
    c.setFont("Helvetica", 11)

    y = top - 1.5 * line_height
    if not schedule:
        c.drawString(left, y, "No check-ins or check-outs in the selected window.")
    else:
        for d in sorted(schedule.keys()):
            date_str = d.strftime("%A %d %B %Y")
            c.setFont("Helvetica-Bold", 12)
            c.drawString(left, y, date_str)
            y -= line_height

            c.setFont("Helvetica", 11)
            for flat_name, flags in sorted(schedule[d].items()):
                ci = flags.get("check_in", False)
                co = flags.get("check_out", False)
                if ci and co:
                    action = "Check-out → Clean → Check-in"
                elif co:
                    action = "Check-out → Clean"
                elif ci:
                    action = "Check-in"
                else:
                    action = ""
                c.drawString(left + 8 * mm, y, f"- {flat_name}: {action}")
                y -= line_height

            y -= 0.5 * line_height
            if y < 20 * mm:
                c.showPage()
                c.setFont("Helvetica-Bold", 16)
                c.drawString(left, top, title)
                c.setFont("Helvetica", 11)
                y = top - 1.5 * line_height

    c.save()
    return output_path

def maybe_send_whatsapp(message: str, to: str, from_: str, sid: str, token: str) -> str:
    try:
        from twilio.rest import Client
        client = Client(sid, token)
        msg = client.messages.create(body=message, from_=from_, to=to)
        return f"WhatsApp sent: {msg.sid}"
    except Exception as e:
        return f"WhatsApp send failed: {e}"

def maybe_send_email(message: str, host: str, port: int, user: str, password: str, email_from: str, email_to: str, attachment_path: str | None = None) -> str:
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.mime.base import MIMEBase
    from email import encoders

    try:
        if attachment_path:
            msg = MIMEMultipart()
            msg.attach(MIMEText(message, "plain"))
            msg["Subject"] = "Cleaner Schedule"
            msg["From"] = email_from
            msg["To"] = email_to

            with open(attachment_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f'attachment; filename="{os.path.basename(attachment_path)}"')
            msg.attach(part)

            raw = msg.as_string()
        else:
            msg = MIMEText(message, "plain")
            msg["Subject"] = "Cleaner Schedule"
            msg["From"] = email_from
            msg["To"] = email_to
            raw = msg.as_string()

        with smtplib.SMTP(host, port) as s:
            s.starttls()
            s.login(user, password)
            s.sendmail(email_from, [email_to], raw)
        return "Email sent"
    except Exception as e:
        return f"Email send failed: {e}"
