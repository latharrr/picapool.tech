import struct
import zlib
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot_filter import is_preview_bot
from app.crud import get_link, log_event
from app.database import get_db
from app import sheets

router = APIRouter()

# ── 1×1 transparent PNG, built once at startup ────────────────────────────────

def _png_chunk(tag: bytes, data: bytes) -> bytes:
    raw = tag + data
    return struct.pack(">I", len(data)) + raw + struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF)


def _make_pixel() -> bytes:
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    idat = _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00", level=9))
    iend = _png_chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


_PIXEL = _make_pixel()
_PIXEL_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}


def _real_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _link_is_live(link) -> bool:
    """Return False when the link is disabled or past its expiry."""
    if not link.is_active:
        return False
    if link.expires_at and link.expires_at < datetime.now(timezone.utc):
        return False
    return True


# ── Click tracking ────────────────────────────────────────────────────────────

@router.get("/t/{token}", summary="Click tracker — logs event and redirects")
async def track_click(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    link = await get_link(db, token)
    if not link or not _link_is_live(link):
        return Response(status_code=404, content="Link not found")

    ua = request.headers.get("user-agent", "")
    referrer = request.headers.get("referer", "")
    ip = _real_ip(request)
    bot = is_preview_bot(ua)

    ts = await log_event(
        db,
        token=token,
        event_type="click",
        ip=ip,
        user_agent=ua,
        referrer=referrer,
        recipient_id=link.recipient_id,
        campaign_id=link.campaign_id,
        is_preview_bot=bot,
    )
    if ts is not None:  # None means fingerprint collision → duplicate, skip Sheets
        sheets.enqueue([
            token, link.recipient_id, link.campaign_id, "click",
            str(ts), ip, ua, referrer, str(bot),
        ])

    return RedirectResponse(url=link.dest_url, status_code=302)


# ── Open tracking (pixel) ─────────────────────────────────────────────────────

@router.get("/p/{token_file}", summary="Open tracker — logs event and returns 1×1 pixel")
async def track_open(token_file: str, request: Request, db: AsyncSession = Depends(get_db)):
    token = token_file[:-4] if token_file.endswith(".png") else token_file

    link = await get_link(db, token)
    # Always return the pixel — don't reveal to email clients whether the token exists
    if not link or not _link_is_live(link):
        return Response(content=_PIXEL, media_type="image/png", headers=_PIXEL_HEADERS)

    ua = request.headers.get("user-agent", "")
    referrer = request.headers.get("referer", "")
    ip = _real_ip(request)
    bot = is_preview_bot(ua)

    ts = await log_event(
        db,
        token=token,
        event_type="open",
        ip=ip,
        user_agent=ua,
        referrer=referrer,
        recipient_id=link.recipient_id,
        campaign_id=link.campaign_id,
        is_preview_bot=bot,
    )
    if ts is not None:
        sheets.enqueue([
            token, link.recipient_id, link.campaign_id, "open",
            str(ts), ip, ua, referrer, str(bot),
        ])

    return Response(content=_PIXEL, media_type="image/png", headers=_PIXEL_HEADERS)
