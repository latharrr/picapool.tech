import hashlib
import hmac
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.config import settings
from app import sheets, groq_ai
from app.token_gen import generate_token

router = APIRouter()

_STATIC = Path(__file__).parent.parent.parent / "static"
_SESSION_COOKIE = "tracker_session"

_login_attempts: dict[str, list[float]] = defaultdict(list)
_LOGIN_WINDOW = 300   # 5-minute window
_LOGIN_MAX    = 10    # attempts before lockout


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For")
    return fwd.split(",")[0].strip() if fwd else (
        request.client.host if request.client else "unknown"
    )


# ── Auth ──────────────────────────────────────────────────────────────────────

def _session_value() -> str:
    # Include ISO week so stolen cookies expire within the same window as max_age
    week = datetime.now(timezone.utc).strftime("%G%V")
    return hmac.new(
        settings.secret_key.encode(),
        f"{settings.dashboard_password}:{week}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _is_authed(request: Request) -> bool:
    return hmac.compare_digest(
        request.cookies.get(_SESSION_COOKIE, ""), _session_value()
    )


def require_auth(request: Request):
    if not _is_authed(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Dashboard HTML ────────────────────────────────────────────────────────────

@router.get("/dashboard", include_in_schema=False)
async def dashboard_page():
    return FileResponse(_STATIC / "dashboard.html")


@router.post("/dashboard/login")
async def dashboard_login(request: Request):
    ip = _client_ip(request)
    now = time.time()
    recent = [t for t in _login_attempts[ip] if now - t < _LOGIN_WINDOW]
    _login_attempts[ip] = recent
    if len(recent) >= _LOGIN_MAX:
        raise HTTPException(status_code=429, detail="Too many login attempts")
    _login_attempts[ip].append(now)

    form = await request.form()
    if not hmac.compare_digest(str(form.get("password", "")), settings.dashboard_password):
        raise HTTPException(status_code=401, detail="Invalid password")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(_SESSION_COOKIE, _session_value(),
                    httponly=True, samesite="strict", max_age=86400 * 7)
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
async def api_overview(request: Request):
    require_auth(request)
    return sheets.get_overview()


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
):
    require_auth(request)
    df = datetime.fromisoformat(date_from) if date_from else None
    dt = datetime.fromisoformat(date_to)   if date_to   else None
    return sheets.get_events_filtered(
        campaign_id=campaign_id, recipient_id=recipient_id,
        event_type=event_type, date_from=df, date_to=dt,
        include_bots=include_bots, limit=min(limit, 2000),
    )


@router.get("/api/summary")
async def api_summary(request: Request):
    require_auth(request)
    return sheets.get_link_summary()


@router.get("/api/timeseries")
async def api_timeseries(request: Request, hours: int = 48):
    require_auth(request)
    return sheets.get_timeseries(hours=min(hours, 720))


@router.get("/api/campaigns")
async def api_campaigns(request: Request):
    require_auth(request)
    return sheets.get_top_campaigns()


# ── Groq AI analysis ──────────────────────────────────────────────────────────

class AnalyzeRequest(BaseModel):
    question: str


@router.post("/api/analyze")
async def api_analyze(request: Request, body: AnalyzeRequest):
    require_auth(request)
    if not body.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    context = sheets.build_analysis_context()
    answer  = await groq_ai.ask(body.question.strip(), context)
    return {"answer": answer}


# ── Link creation ─────────────────────────────────────────────────────────────

class CreateLinkRequest(BaseModel):
    dest_url: str
    campaign_id: Optional[str] = None
    recipient_id: Optional[str] = None
    token: Optional[str] = None
    is_active: bool = True
    expires_at: Optional[datetime] = None


@router.post("/api/links", status_code=201)
async def api_create_link(request: Request, body: CreateLinkRequest):
    require_auth(request)
    token = body.token or generate_token()
    now   = datetime.now(timezone.utc).isoformat()
    exp   = body.expires_at
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    link  = {
        "token":        token,
        "dest_url":     body.dest_url,
        "campaign_id":  body.campaign_id or "",
        "recipient_id": body.recipient_id or "",
        "created_at":   now,
        "is_active":    str(body.is_active),
        "expires_at":   exp.isoformat() if exp else "",
    }
    await sheets.append_link(link)
    base = settings.base_url.rstrip("/") if settings.base_url else str(request.base_url).rstrip("/")
    return {
        **link,
        "click_url": f"{base}/t/{token}",
        "pixel_url": f"{base}/p/{token}.png",
    }
