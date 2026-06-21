"""
Dashboard router:
  GET  /dashboard          → serves the SPA HTML
  POST /dashboard/login    → validates password, sets session cookie
  GET  /dashboard/logout   → clears cookie

  All /api/* routes require the session cookie.
  GET  /api/me             → auth probe (200 ok / 401)
  GET  /api/events         → filtered event list
  GET  /api/summary        → per-link aggregates
  GET  /api/timeseries     → hourly opens/clicks
  GET  /api/campaigns      → top campaigns
  GET  /api/overview       → stats cards
  POST /api/links          → create a tracking link
"""

import hashlib
import hmac
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.crud import (
    create_link,
    get_events,
    get_link_summary,
    get_stats_overview,
    get_timeseries,
    get_top_campaigns,
)
from app.database import get_db
from app.token_gen import generate_token

router = APIRouter()

_STATIC = Path(__file__).parent.parent.parent / "static"
_SESSION_COOKIE = "tracker_session"


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _session_value() -> str:
    """Deterministic HMAC token — changes if password or secret_key changes."""
    return hmac.new(
        settings.secret_key.encode(),
        settings.dashboard_password.encode(),
        hashlib.sha256,
    ).hexdigest()


def _is_authed(request: Request) -> bool:
    token = request.cookies.get(_SESSION_COOKIE, "")
    return hmac.compare_digest(token, _session_value())


def require_auth(request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Dashboard HTML ────────────────────────────────────────────────────────────

@router.get("/dashboard", include_in_schema=False)
async def dashboard_page():
    return FileResponse(_STATIC / "dashboard.html")


# ── Auth endpoints ────────────────────────────────────────────────────────────

@router.post("/dashboard/login")
async def dashboard_login(request: Request):
    form = await request.form()
    password = form.get("password", "")
    if not hmac.compare_digest(str(password), settings.dashboard_password):
        raise HTTPException(status_code=401, detail="Invalid password")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        _SESSION_COOKIE,
        _session_value(),
        httponly=True,
        samesite="strict",
        max_age=86400 * 7,
    )
    return resp


@router.get("/dashboard/logout")
async def dashboard_logout():
    resp = RedirectResponse(url="/dashboard", status_code=302)
    resp.delete_cookie(_SESSION_COOKIE)
    return resp


# ── API ───────────────────────────────────────────────────────────────────────

@router.get("/api/me")
async def api_me(request: Request):
    require_auth(request)
    return {"authenticated": True}


@router.get("/api/overview")
async def api_overview(request: Request, db: AsyncSession = Depends(get_db)):
    require_auth(request)
    return await get_stats_overview(db)


@router.get("/api/events")
async def api_events(
    request: Request,
    campaign_id: Optional[str] = None,
    recipient_id: Optional[str] = None,
    event_type: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    include_bots: bool = False,
    limit: int = 500,
    db: AsyncSession = Depends(get_db),
):
    require_auth(request)
    df = datetime.fromisoformat(date_from) if date_from else None
    dt = datetime.fromisoformat(date_to) if date_to else None
    events = await get_events(
        db,
        campaign_id=campaign_id,
        recipient_id=recipient_id,
        event_type=event_type,
        date_from=df,
        date_to=dt,
        include_bots=include_bots,
        limit=min(limit, 2000),
    )
    return [
        {
            "id": e.id,
            "token": e.token,
            "recipient_id": e.recipient_id,
            "campaign_id": e.campaign_id,
            "event_type": e.event_type,
            "timestamp": e.timestamp.isoformat() if e.timestamp else None,
            "ip": e.ip,
            "user_agent": e.user_agent,
            "referrer": e.referrer,
            "is_preview_bot": e.is_preview_bot,
        }
        for e in events
    ]


@router.get("/api/summary")
async def api_summary(request: Request, db: AsyncSession = Depends(get_db)):
    require_auth(request)
    rows = await get_link_summary(db)
    for r in rows:
        if r.get("created_at"):
            r["created_at"] = r["created_at"].isoformat()
        if r.get("expires_at"):
            r["expires_at"] = r["expires_at"].isoformat()
    return rows


@router.get("/api/timeseries")
async def api_timeseries(
    request: Request,
    hours: int = 48,
    db: AsyncSession = Depends(get_db),
):
    require_auth(request)
    rows = await get_timeseries(db, hours=min(hours, 720))
    return [
        {
            "hour": r["hour"].isoformat() if r["hour"] else None,
            "event_type": r["event_type"],
            "count": r["count"],
        }
        for r in rows
    ]


@router.get("/api/campaigns")
async def api_campaigns(request: Request, db: AsyncSession = Depends(get_db)):
    require_auth(request)
    return await get_top_campaigns(db)


# ── Link creation ─────────────────────────────────────────────────────────────

class CreateLinkRequest(BaseModel):
    dest_url: str
    campaign_id: Optional[str] = None
    recipient_id: Optional[str] = None
    token: Optional[str] = None        # custom token override
    is_active: bool = True
    expires_at: Optional[datetime] = None


@router.post("/api/links", status_code=201)
async def api_create_link(
    request: Request,
    body: CreateLinkRequest,
    db: AsyncSession = Depends(get_db),
):
    require_auth(request)
    token = body.token or generate_token()
    link = await create_link(
        db,
        token=token,
        dest_url=body.dest_url,
        campaign_id=body.campaign_id,
        recipient_id=body.recipient_id,
        is_active=body.is_active,
        expires_at=body.expires_at,
    )
    base = str(request.base_url).rstrip("/")
    return {
        "token": link.token,
        "dest_url": link.dest_url,
        "campaign_id": link.campaign_id,
        "recipient_id": link.recipient_id,
        "is_active": link.is_active,
        "expires_at": link.expires_at.isoformat() if link.expires_at else None,
        "click_url": f"{base}/t/{link.token}",
        "pixel_url": f"{base}/p/{link.token}.png",
        "created_at": link.created_at.isoformat() if link.created_at else None,
    }
