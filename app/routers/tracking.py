import asyncio
import hashlib
import logging
import re
import struct
import time
import zlib
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response

from app.bot_filter import is_preview_bot
from app.config import settings
from app import sheets

router = APIRouter()
logger = logging.getLogger(__name__)

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
    # Allowlist: only "true" / "1" are active; "false", "0", "unsubscribed", etc. are not
    if str(link.get("is_active", "true")).lower() not in ("true", "1"):
        return False
    exp = link.get("expires_at", "")
    if exp:
        try:
            dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt < datetime.now(timezone.utc):
                return False
        except ValueError:
            pass
    return True


def _fingerprint(token: str, ip: str, ua: str, event_type: str) -> str:
    minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    return hashlib.sha256(f"{token}|{ip}|{ua}|{event_type}|{minute}".encode()).hexdigest()


# ── Per-IP rate limiting on tracking endpoints ────────────────────────────────

_track_hits: dict[str, list[float]] = defaultdict(list)
_TRACK_WINDOW = 60    # seconds
_TRACK_MAX    = 100   # max hits per IP per window


def _check_track_rate(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _track_hits[ip] if now - t < _TRACK_WINDOW]
    _track_hits[ip] = recent
    if len(recent) >= _TRACK_MAX:
        return False
    _track_hits[ip].append(now)
    if len(_track_hits) > 10_000:
        cutoff = now - _TRACK_WINDOW
        stale = [k for k, v in _track_hits.items() if all(t <= cutoff for t in v)]
        for k in stale:
            del _track_hits[k]
    return True


# ── Geo lookup (IP → country/city/region, cached) ────────────────────────────

_GEO_EMPTY   = {"country": "", "city": "", "region": ""}
_geo_cache: dict[str, dict] = {}
_PRIVATE_IP_RE = re.compile(
    r"^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|::1$|localhost$|unknown$)"
)


async def _geo_lookup(ip: str) -> dict:
    if not ip or _PRIVATE_IP_RE.match(ip):
        return _GEO_EMPTY
    if ip in _geo_cache:
        return _geo_cache[ip]
    try:
        async with httpx.AsyncClient(timeout=0.8) as client:
            r = await client.get(
                f"http://ip-api.com/json/{ip}?fields=countryCode,city,regionName"
            )
            if r.status_code == 200:
                d = r.json()
                geo = {
                    "country": d.get("countryCode", ""),
                    "city":    d.get("city", ""),
                    "region":  d.get("regionName", ""),
                }
            else:
                geo = _GEO_EMPTY
    except Exception:
        geo = _GEO_EMPTY
    _geo_cache[ip] = geo
    return geo


# ── User-agent parsing ────────────────────────────────────────────────────────

def _parse_browser(ua: str) -> str:
    if not ua:                               return ""
    if re.search(r"Edg/",         ua):       return "Edge"
    if re.search(r"OPR|Opera",    ua):       return "Opera"
    if re.search(r"YaBrowser",    ua):       return "Yandex"
    if re.search(r"SamsungBrowser", ua):     return "Samsung"
    if re.search(r"Chrome/",      ua):       return "Chrome"
    if re.search(r"Firefox/",     ua):       return "Firefox"
    if re.search(r"Safari/",      ua) and "Chrome" not in ua: return "Safari"
    if re.search(r"MSIE|Trident", ua):       return "IE"
    return "Other"


def _parse_os(ua: str) -> str:
    if not ua:                                    return ""
    if re.search(r"iPhone|iPod",  ua):            return "iOS"
    if re.search(r"iPad",         ua):            return "iPadOS"
    if re.search(r"Android",      ua):            return "Android"
    if re.search(r"Windows NT",   ua):            return "Windows"
    if re.search(r"CrOS",         ua):            return "ChromeOS"
    if re.search(r"Macintosh|Mac OS X", ua):      return "macOS"
    if re.search(r"Linux",        ua):            return "Linux"
    return "Other"


def _parse_device_type(ua: str) -> str:
    if not ua:                                                   return ""
    if re.search(r"Mobi|Android.*Mobile|iPhone|iPod", ua):      return "Mobile"
    if re.search(r"iPad|Android(?!.*Mobile)|Tablet",  ua):      return "Tablet"
    return "Desktop"


# ── UTM passthrough ───────────────────────────────────────────────────────────

def _append_utm(dest_url: str, incoming_params) -> str:
    utm = {k: v for k, v in incoming_params.items() if k.startswith("utm_")}
    if not utm:
        return dest_url
    parsed = urlparse(dest_url)
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    existing.update(utm)
    return urlunparse(parsed._replace(query=urlencode(existing)))


# ── Outbound webhook ──────────────────────────────────────────────────────────

async def _fire_webhook(payload: dict) -> None:
    if not settings.webhook_url:
        return
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(settings.webhook_url, json=payload)
    except Exception as exc:
        logger.warning("Webhook delivery failed: %s", exc)


# ── Event recording ───────────────────────────────────────────────────────────

def _record(token: str, link: dict, event_type: str,
            ip: str, ua: str, referrer: str, bot: bool,
            geo: dict | None = None, language: str = "") -> bool:
    """Log event; returns False if duplicate (fingerprint collision)."""
    fp = _fingerprint(token, ip, ua, event_type)
    if sheets.is_duplicate(fp):
        return False
    g = geo or _GEO_EMPTY
    sheets.enqueue_event({
        "token":          token,
        "recipient_id":   link.get("recipient_id", ""),
        "campaign_id":    link.get("campaign_id", ""),
        "event_type":     event_type,
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "ip":             ip,
        "user_agent":     ua,
        "referrer":       referrer,
        "is_preview_bot": str(bot),
        "fingerprint":    fp,
        "country":        g["country"],
        "city":           g["city"],
        "region":         g["region"],
        "browser":        _parse_browser(ua) if not bot else "",
        "os":             _parse_os(ua)      if not bot else "",
        "device_type":    _parse_device_type(ua) if not bot else "",
        "language":       language,
    })
    return True


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/t/{token}")
async def track_click(token: str, request: Request):
    link = sheets.get_link(token)
    if not link or not _link_is_live(link):
        return Response(status_code=404, content="Link not found")

    ip = _real_ip(request)
    if not _check_track_rate(ip):
        return Response(status_code=429, content="Too many requests")

    ua       = request.headers.get("user-agent", "")
    referrer = request.headers.get("referer", "")
    language = request.headers.get("accept-language", "").split(",")[0].strip()
    bot      = is_preview_bot(ua)
    geo      = await _geo_lookup(ip) if not bot else _GEO_EMPTY

    recorded = _record(token, link, "click", ip, ua, referrer, bot, geo, language)

    if not bot and recorded:
        asyncio.create_task(_fire_webhook({
            "token":        token,
            "recipient_id": link.get("recipient_id", ""),
            "campaign_id":  link.get("campaign_id", ""),
            "event_type":   "click",
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "ip":           ip,
            "country":      geo["country"],
            "city":         geo["city"],
        }))

    dest = _append_utm(link["dest_url"], request.query_params)
    return RedirectResponse(url=dest, status_code=302)


@router.get("/p/{token_file}")
async def track_open(token_file: str, request: Request):
    token = token_file[:-4] if token_file.endswith(".png") else token_file
    link  = sheets.get_link(token)
    # Always return pixel — don't reveal token validity to email clients
    if link and _link_is_live(link):
        ip = _real_ip(request)
        if _check_track_rate(ip):   # silently skip record if rate-limited; still return pixel
            ua       = request.headers.get("user-agent", "")
            referrer = request.headers.get("referer", "")
            language = request.headers.get("accept-language", "").split(",")[0].strip()
            bot      = is_preview_bot(ua)
            geo      = await _geo_lookup(ip) if not bot else _GEO_EMPTY
            _record(token, link, "open", ip, ua, referrer, bot, geo, language)

    return Response(content=_PIXEL, media_type="image/png", headers=_PIXEL_HEADERS)
