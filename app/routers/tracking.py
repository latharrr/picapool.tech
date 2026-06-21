import hashlib
import struct
import zlib
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from app.bot_filter import is_preview_bot
from app import sheets

router = APIRouter()

# ── 1×1 transparent PNG ───────────────────────────────────────────────────────

def _png_chunk(tag: bytes, data: bytes) -> bytes:
    raw = tag + data
    return struct.pack(">I", len(data)) + raw + struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF)


_PIXEL = (
    b"\x89PNG\r\n\x1a\n"
    + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
    + _png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00\x00", level=9))
    + _png_chunk(b"IEND", b"")
)
_PIXEL_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"}


def _real_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For")
    return fwd.split(",")[0].strip() if fwd else (
        request.client.host if request.client else "unknown"
    )


def _link_is_live(link: dict) -> bool:
    if str(link.get("is_active", "true")).lower() in ("false", "0"):
        return False
    exp = link.get("expires_at", "")
    if exp:
        try:
            if datetime.fromisoformat(exp.replace("Z", "+00:00")) < datetime.now(timezone.utc):
                return False
        except ValueError:
            pass
    return True


def _fingerprint(token: str, ip: str, ua: str, event_type: str) -> str:
    minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    return hashlib.sha256(f"{token}|{ip}|{ua}|{event_type}|{minute}".encode()).hexdigest()


def _record(token: str, link: dict, event_type: str,
            ip: str, ua: str, referrer: str, bot: bool) -> bool:
    """Log event; returns False if duplicate (fingerprint collision)."""
    fp = _fingerprint(token, ip, ua, event_type)
    if sheets.is_duplicate(fp):
        return False
    sheets.enqueue_event({
        "token": token,
        "recipient_id": link.get("recipient_id", ""),
        "campaign_id": link.get("campaign_id", ""),
        "event_type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ip": ip,
        "user_agent": ua,
        "referrer": referrer,
        "is_preview_bot": str(bot),
        "fingerprint": fp,
    })
    return True


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/t/{token}")
async def track_click(token: str, request: Request):
    link = sheets.get_link(token)
    if not link or not _link_is_live(link):
        return Response(status_code=404, content="Link not found")

    ua       = request.headers.get("user-agent", "")
    referrer = request.headers.get("referer", "")
    ip       = _real_ip(request)
    bot      = is_preview_bot(ua)
    _record(token, link, "click", ip, ua, referrer, bot)

    return RedirectResponse(url=link["dest_url"], status_code=302)


@router.get("/p/{token_file}")
async def track_open(token_file: str, request: Request):
    token = token_file[:-4] if token_file.endswith(".png") else token_file
    link  = sheets.get_link(token)
    # Always return pixel regardless — don't reveal token validity to email clients
    if link and _link_is_live(link):
        ua       = request.headers.get("user-agent", "")
        referrer = request.headers.get("referer", "")
        ip       = _real_ip(request)
        bot      = is_preview_bot(ua)
        _record(token, link, "open", ip, ua, referrer, bot)

    return Response(content=_PIXEL, media_type="image/png", headers=_PIXEL_HEADERS)
