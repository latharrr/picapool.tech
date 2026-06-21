"""
Google Sheets as the sole data store.

Two worksheets:
  "links"  – one row per tracking link
  "events" – one row per tracking event (written in async batches)

In-memory cache is refreshed in the background every CACHE_TTL seconds so
that request handlers never block on a Sheets API call.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

LINKS_HDR  = ["token", "dest_url", "campaign_id", "recipient_id",
               "created_at", "is_active", "expires_at"]
EVENTS_HDR = ["token", "recipient_id", "campaign_id", "event_type",
               "timestamp", "ip", "user_agent", "referrer",
               "is_preview_bot", "fingerprint"]

# ── Module-level state ────────────────────────────────────────────────────────
_links_ws  = None
_events_ws = None

_cache: dict = {"links": [], "events": []}
_write_queue: list[list] = []

# In-process dedup: fingerprint → timestamp seen
_dedup: dict[str, float] = {}
_DEDUP_TTL = 120  # seconds


# ── Connection ────────────────────────────────────────────────────────────────

def _make_creds():
    from google.oauth2.service_account import Credentials
    if settings.google_credentials_json:
        return Credentials.from_service_account_info(
            json.loads(settings.google_credentials_json), scopes=SCOPES
        )
    return Credentials.from_service_account_file(
        settings.google_sheets_credentials_path, scopes=SCOPES
    )


def _connect():
    global _links_ws, _events_ws
    try:
        import gspread
        client = gspread.authorize(_make_creds())
        ss = client.open_by_key(settings.google_sheet_id)

        def _ws(name, headers):
            try:
                return ss.worksheet(name)
            except gspread.WorksheetNotFound:
                ws = ss.add_worksheet(name, rows=50000, cols=len(headers))
                ws.append_row(headers)
                return ws

        _links_ws  = _ws("links",  LINKS_HDR)
        _events_ws = _ws("events", EVENTS_HDR)
        logger.info("Sheets connected: %s", settings.google_sheet_id)
    except Exception as exc:
        logger.error("Sheets connect failed: %s", exc)


# ── Dedup (in-memory, per-process) ───────────────────────────────────────────

def is_duplicate(fp: str) -> bool:
    now = time.time()
    # Prune old entries
    for k in [k for k, v in _dedup.items() if now - v > _DEDUP_TTL]:
        del _dedup[k]
    if fp in _dedup:
        return True
    _dedup[fp] = now
    return False


# ── Cache helpers ─────────────────────────────────────────────────────────────

def _ws_to_dicts(ws, headers: list[str]) -> list[dict]:
    rows = ws.get_all_values()
    if not rows:
        return []
    # Tolerate sheet header row
    start = 1 if rows[0] == headers else 0
    return [dict(zip(headers, r)) for r in rows[start:] if any(r)]


# ── Sync reads (called from executor) ─────────────────────────────────────────

def _reload_links():
    if _links_ws:
        _cache["links"] = _ws_to_dicts(_links_ws, LINKS_HDR)


def _reload_events():
    if _events_ws:
        _cache["events"] = _ws_to_dicts(_events_ws, EVENTS_HDR)


# ── Public sync accessors (use cached data — always fast) ─────────────────────

def get_link(token: str) -> Optional[dict]:
    for link in _cache["links"]:
        if link.get("token") == token:
            return link
    return None


def all_links() -> list[dict]:
    return _cache["links"]


def all_events() -> list[dict]:
    return _cache["events"]


# ── Writes ─────────────────────────────────────────────────────────────────────

async def append_link(link: dict) -> None:
    row = [str(link.get(h, "")) for h in LINKS_HDR]
    loop = asyncio.get_running_loop()
    if _links_ws:
        await loop.run_in_executor(None, _links_ws.append_row, row)
    _cache["links"].append(link)  # optimistic local update


def enqueue_event(event: dict) -> None:
    """Non-blocking — flushed in the background."""
    _write_queue.append([str(event.get(h, "")) for h in EVENTS_HDR])
    # Optimistic local update so dashboard reflects new events quickly
    _cache["events"].append(event)


# ── Background flush ──────────────────────────────────────────────────────────

async def _flush():
    if not _write_queue or _events_ws is None:
        return
    batch, _write_queue[:] = _write_queue[:], []
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, _events_ws.append_rows, batch)
        logger.debug("Flushed %d events to Sheets", len(batch))
    except Exception as exc:
        logger.error("Sheets flush failed: %s — re-queuing %d rows", exc, len(batch))
        _write_queue.extend(batch)


# ── Aggregations (pure Python over cached data) ───────────────────────────────

def _is_bot(e: dict) -> bool:
    return str(e.get("is_preview_bot", "")).lower() in ("true", "1", "yes")


def get_overview() -> dict:
    links  = _cache["links"]
    events = _cache["events"]
    threshold = time.time() - settings.ignored_threshold_hours * 3600

    opens = clicks = bots = 0
    tokens_with_events: set[str] = set()

    for e in events:
        tokens_with_events.add(e.get("token", ""))
        if _is_bot(e):
            bots += 1
        elif e.get("event_type") == "open":
            opens += 1
        elif e.get("event_type") == "click":
            clicks += 1

    ignored = 0
    for lk in links:
        try:
            ts = datetime.fromisoformat(
                lk["created_at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            continue
        if ts < threshold and lk.get("token") not in tokens_with_events:
            ignored += 1

    return {
        "total_sent": len(links),
        "total_opens": opens,
        "total_clicks": clicks,
        "total_bot_hits": bots,
        "total_ignored": ignored,
        "ignored_threshold_hours": settings.ignored_threshold_hours,
    }


def get_events_filtered(
    campaign_id: Optional[str] = None,
    recipient_id: Optional[str] = None,
    event_type: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    include_bots: bool = False,
    limit: int = 500,
) -> list[dict]:
    result = []
    for e in reversed(_cache["events"]):
        if campaign_id  and e.get("campaign_id")  != campaign_id:  continue
        if recipient_id and e.get("recipient_id") != recipient_id: continue
        if event_type   and e.get("event_type")   != event_type:   continue
        if not include_bots and _is_bot(e): continue
        if date_from or date_to:
            try:
                ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
                if date_from and ts < date_from: continue
                if date_to   and ts > date_to:   continue
            except (KeyError, ValueError):
                pass
        result.append(e)
        if len(result) >= limit:
            break
    return result


def get_link_summary() -> list[dict]:
    threshold = time.time() - settings.ignored_threshold_hours * 3600

    by_token: dict[str, dict] = {}
    for e in _cache["events"]:
        t = e.get("token", "")
        if t not in by_token:
            by_token[t] = {"opens": 0, "clicks": 0}
        if not _is_bot(e):
            if e.get("event_type") == "open":  by_token[t]["opens"]  += 1
            if e.get("event_type") == "click": by_token[t]["clicks"] += 1

    rows = []
    for lk in reversed(_cache["links"]):
        token = lk.get("token", "")
        stats = by_token.get(token, {"opens": 0, "clicks": 0})
        try:
            ts = datetime.fromisoformat(lk["created_at"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            ts = 0
        rows.append({
            **lk,
            "opens":   stats["opens"],
            "clicks":  stats["clicks"],
            "ignored": ts < threshold and token not in by_token,
        })
    return rows


def get_timeseries(hours: int = 48) -> list[dict]:
    cutoff = time.time() - hours * 3600
    buckets: dict[tuple, int] = {}
    for e in _cache["events"]:
        if _is_bot(e): continue
        try:
            ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
            if ts.timestamp() < cutoff: continue
            key = (ts.strftime("%Y-%m-%dT%H:00:00"), e.get("event_type", ""))
            buckets[key] = buckets.get(key, 0) + 1
        except (KeyError, ValueError):
            pass
    return [{"hour": k[0], "event_type": k[1], "count": v}
            for k, v in sorted(buckets.items())]


def get_top_campaigns(limit: int = 10) -> list[dict]:
    camps: dict[str, dict] = {}
    for e in _cache["events"]:
        cid = e.get("campaign_id") or "(none)"
        if cid not in camps:
            camps[cid] = {"clicks": 0, "opens": 0, "_tokens": set()}
        if not _is_bot(e):
            if e.get("event_type") == "click": camps[cid]["clicks"] += 1
            if e.get("event_type") == "open":  camps[cid]["opens"]  += 1
        camps[cid]["_tokens"].add(e.get("token", ""))
    result = [
        {"campaign_id": cid,
         "clicks": v["clicks"], "opens": v["opens"],
         "total_links": len(v["_tokens"])}
        for cid, v in camps.items()
    ]
    result.sort(key=lambda x: x["clicks"], reverse=True)
    return result[:limit]


def build_analysis_context() -> str:
    ov = get_overview()
    sent, opens, clicks = ov["total_sent"], ov["total_opens"], ov["total_clicks"]
    open_rate  = f"{opens/sent*100:.1f}%"  if sent else "N/A"
    click_rate = f"{clicks/sent*100:.1f}%" if sent else "N/A"

    lines = [
        f"Total links sent: {sent}",
        f"Opens: {opens} ({open_rate})",
        f"Clicks: {clicks} ({click_rate})",
        f"Bot hits filtered out: {ov['total_bot_hits']}",
        f"Ignored (no activity >{settings.ignored_threshold_hours}h): {ov['total_ignored']}",
        "", "Top campaigns:",
    ]
    for c in get_top_campaigns(10):
        rate = (f"{c['clicks']/c['total_links']*100:.1f}%"
                if c["total_links"] else "N/A")
        lines.append(
            f"  {c['campaign_id']}: {c['clicks']} clicks, "
            f"{c['opens']} opens, {c['total_links']} links, rate {rate}"
        )
    return "\n".join(lines)


# ── Startup ───────────────────────────────────────────────────────────────────

async def startup():
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _connect)
    # Initial cache load
    await loop.run_in_executor(None, _reload_links)
    await loop.run_in_executor(None, _reload_events)
    logger.info("Cache loaded: %d links, %d events",
                len(_cache["links"]), len(_cache["events"]))

    async def _refresh_loop():
        while True:
            await asyncio.sleep(settings.cache_ttl_seconds)
            # Flush first so reload sees the latest events in Sheets
            await _flush()
            loop2 = asyncio.get_running_loop()
            await loop2.run_in_executor(None, _reload_links)
            await loop2.run_in_executor(None, _reload_events)

    async def _flush_loop():
        while True:
            await asyncio.sleep(settings.sheets_batch_interval)
            await _flush()

    asyncio.create_task(_refresh_loop())
    asyncio.create_task(_flush_loop())
