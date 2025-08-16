
import os
import json
import gspread
from typing import List

def push_schedule_to_google_sheet(rows: List[List[str]]) -> str:
    svc_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    tab_name = os.getenv("SHEET_TAB_NAME", "Cleaner Schedule")

    if not svc_json or not sheet_id:
        return "Google Sheets not configured (missing GOOGLE_SERVICE_ACCOUNT_JSON/GOOGLE_SHEET_ID)."

    creds_dict = json.loads(svc_json)
    gc = gspread.service_account_from_dict(creds_dict)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows="200", cols="6")
    ws.clear()
    ws.update("A1", rows)
    return f"Google Sheet updated: {sheet_id} / {tab_name}"
