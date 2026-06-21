"""
Async write-through to Google Sheets.
Events are queued in memory and flushed in batches every N seconds.

Credentials priority (Railway-compatible):
  1. GOOGLE_CREDENTIALS_JSON env var  — raw JSON string, no file needed
  2. GOOGLE_SHEETS_CREDENTIALS_PATH   — path to a service-account JSON file (local dev)

If neither is set, or GOOGLE_SHEET_ID is empty, Sheets sync stays disabled.
"""

import asyncio
import json
import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

_queue: list[list] = []
_sheet = None  # gspread Worksheet


def _connect() -> Optional[object]:
    try:
        import gspread
        from google.oauth2.service_account import Credentials

        if settings.google_credentials_json:
            info = json.loads(settings.google_credentials_json)
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        elif settings.google_sheets_credentials_path:
            creds = Credentials.from_service_account_file(
                settings.google_sheets_credentials_path, scopes=SCOPES
            )
        else:
            logger.warning("No Google credentials configured — Sheets disabled")
            return None

        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(settings.google_sheet_id)
        try:
            ws = spreadsheet.worksheet("events")
        except gspread.WorksheetNotFound:
            ws = spreadsheet.add_worksheet("events", rows=10000, cols=10)
            ws.append_row([
                "token", "recipient_id", "campaign_id", "event_type",
                "timestamp", "ip", "user_agent", "referrer", "is_preview_bot",
            ])
        logger.info("Google Sheets connected: %s", settings.google_sheet_id)
        return ws
    except Exception as exc:
        logger.error("Sheets init failed: %s", exc)
        return None


def enqueue(row: list) -> None:
    """Non-blocking; called from request handlers."""
    _queue.append(row)


async def _flush() -> None:
    if not _queue or _sheet is None:
        return
    batch, _queue[:] = _queue[:], []
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _sheet.append_rows, batch)
        logger.debug("Flushed %d rows to Sheets", len(batch))
    except Exception as exc:
        logger.error("Sheets flush error: %s — re-queuing %d rows", exc, len(batch))
        _queue.extend(batch)


async def start_batch_worker() -> None:
    global _sheet
    if not settings.google_sheet_id:
        logger.info("GOOGLE_SHEET_ID not set — Sheets sync disabled")
        return
    loop = asyncio.get_running_loop()
    _sheet = await loop.run_in_executor(None, _connect)
    if _sheet is None:
        return

    async def _loop():
        while True:
            await asyncio.sleep(settings.sheets_batch_interval)
            await _flush()

    asyncio.create_task(_loop())
