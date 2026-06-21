import hashlib
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Event, Link


# ── Fingerprint (dedup) ───────────────────────────────────────────────────────

def _make_fingerprint(token: str, ip: str, ua: str, event_type: str) -> str:
    """SHA-256 of token|ip|ua|event_type|UTC-minute. Identical hits within the
    same minute from the same client collapse to one event."""
    minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    raw = f"{token}|{ip or ''}|{ua or ''}|{event_type}|{minute}"
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Links ─────────────────────────────────────────────────────────────────────

async def get_link(db: AsyncSession, token: str) -> Optional[Link]:
    from sqlalchemy import select
    result = await db.execute(select(Link).where(Link.token == token))
    return result.scalar_one_or_none()


async def create_link(
    db: AsyncSession,
    token: str,
    dest_url: str,
    campaign_id: Optional[str] = None,
    recipient_id: Optional[str] = None,
    is_active: bool = True,
    expires_at: Optional[datetime] = None,
) -> Link:
    link = Link(
        token=token,
        dest_url=dest_url,
        campaign_id=campaign_id,
        recipient_id=recipient_id,
        is_active=is_active,
        expires_at=expires_at,
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)
    return link


# ── Events ────────────────────────────────────────────────────────────────────

async def log_event(
    db: AsyncSession,
    *,
    token: str,
    event_type: str,
    ip: str,
    user_agent: str,
    referrer: str,
    recipient_id: Optional[str],
    campaign_id: Optional[str],
    is_preview_bot: bool,
) -> Optional[datetime]:
    """Insert an event and return its DB timestamp.

    Returns None if the fingerprint already exists (duplicate within the same
    minute from the same client) — callers should skip Sheets logging in that case.

    Genuine clicks are counted immediately on any non-bot hit. There is no
    dependency on a prior bot hit; bot detection is purely UA-based.
    """
    fp = _make_fingerprint(token, ip, user_agent, event_type)

    stmt = (
        pg_insert(Event)
        .values(
            token=token,
            recipient_id=recipient_id,
            campaign_id=campaign_id,
            event_type=event_type,
            ip=ip,
            user_agent=user_agent,
            referrer=referrer,
            is_preview_bot=is_preview_bot,
            fingerprint=fp,
        )
        .on_conflict_do_nothing(index_elements=["fingerprint"])
        .returning(Event.timestamp)
    )
    result = await db.execute(stmt)
    await db.commit()
    row = result.fetchone()
    return row[0] if row else None  # None → duplicate, silently skipped


async def get_events(
    db: AsyncSession,
    campaign_id: Optional[str] = None,
    recipient_id: Optional[str] = None,
    event_type: Optional[str] = None,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
    include_bots: bool = False,
    limit: int = 500,
) -> list[Event]:
    from sqlalchemy import select
    q = select(Event).order_by(Event.timestamp.desc())
    if campaign_id:
        q = q.where(Event.campaign_id == campaign_id)
    if recipient_id:
        q = q.where(Event.recipient_id == recipient_id)
    if event_type:
        q = q.where(Event.event_type == event_type)
    if date_from:
        q = q.where(Event.timestamp >= date_from)
    if date_to:
        q = q.where(Event.timestamp <= date_to)
    if not include_bots:
        q = q.where(Event.is_preview_bot.is_(False))
    q = q.limit(limit)
    result = await db.execute(q)
    return result.scalars().all()


async def get_link_summary(db: AsyncSession, limit: int = 1000) -> list:
    sql = text("""
        SELECT
            l.token,
            l.dest_url,
            l.campaign_id,
            l.recipient_id,
            l.created_at,
            l.is_active,
            l.expires_at,
            COUNT(CASE WHEN e.event_type = 'open'  AND NOT e.is_preview_bot THEN 1 END) AS opens,
            COUNT(CASE WHEN e.event_type = 'click' AND NOT e.is_preview_bot THEN 1 END) AS clicks,
            CASE
                WHEN COUNT(e.id) = 0
                 AND l.created_at < NOW() - (INTERVAL '1 hour' * :threshold_h)
                THEN true ELSE false
            END AS ignored
        FROM links l
        LEFT JOIN events e ON l.token = e.token
        GROUP BY l.id
        ORDER BY l.created_at DESC
        LIMIT :limit
    """)
    result = await db.execute(
        sql,
        {"threshold_h": settings.ignored_threshold_hours, "limit": limit},
    )
    return [dict(r) for r in result.mappings()]


async def get_timeseries(db: AsyncSession, hours: int = 48) -> list:
    sql = text("""
        SELECT
            date_trunc('hour', timestamp) AS hour,
            event_type,
            COUNT(*) AS count
        FROM events
        WHERE timestamp > NOW() - (INTERVAL '1 hour' * :hours)
          AND NOT is_preview_bot
        GROUP BY 1, 2
        ORDER BY 1
    """)
    result = await db.execute(sql, {"hours": hours})
    return [dict(r) for r in result.mappings()]


async def get_top_campaigns(db: AsyncSession, limit: int = 10) -> list:
    sql = text("""
        SELECT
            COALESCE(campaign_id, '(none)') AS campaign_id,
            COUNT(CASE WHEN event_type = 'click' AND NOT is_preview_bot THEN 1 END) AS clicks,
            COUNT(CASE WHEN event_type = 'open'  AND NOT is_preview_bot THEN 1 END) AS opens,
            COUNT(DISTINCT token) AS total_links
        FROM events
        GROUP BY 1
        ORDER BY clicks DESC
        LIMIT :limit
    """)
    result = await db.execute(sql, {"limit": limit})
    return [dict(r) for r in result.mappings()]


async def get_stats_overview(db: AsyncSession) -> dict:
    sql = text("""
        SELECT
            (SELECT COUNT(*) FROM links) AS total_sent,
            COUNT(CASE WHEN event_type = 'open'  AND NOT is_preview_bot THEN 1 END) AS total_opens,
            COUNT(CASE WHEN event_type = 'click' AND NOT is_preview_bot THEN 1 END) AS total_clicks,
            COUNT(CASE WHEN is_preview_bot THEN 1 END) AS total_bot_hits
        FROM events
    """)
    result = await db.execute(sql)
    row = result.mappings().one()

    ignored_sql = text("""
        SELECT COUNT(*) AS ignored
        FROM links l
        WHERE l.created_at < NOW() - (INTERVAL '1 hour' * :threshold_h)
          AND NOT EXISTS (SELECT 1 FROM events e WHERE e.token = l.token)
    """)
    ignored_result = await db.execute(
        ignored_sql, {"threshold_h": settings.ignored_threshold_hours}
    )
    ignored = ignored_result.scalar_one()

    return {
        "total_sent": row["total_sent"],
        "total_opens": row["total_opens"],
        "total_clicks": row["total_clicks"],
        "total_bot_hits": row["total_bot_hits"],
        "total_ignored": ignored,
    }
