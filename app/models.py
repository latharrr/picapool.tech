from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func
from app.database import Base


class Link(Base):
    __tablename__ = "links"

    id = Column(Integer, primary_key=True)
    token = Column(String(20), unique=True, nullable=False, index=True)
    dest_url = Column(Text, nullable=False)
    campaign_id = Column(String(255), nullable=True, index=True)
    recipient_id = Column(String(255), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True)
    token = Column(String(20), nullable=False, index=True)
    recipient_id = Column(String(255), nullable=True)
    campaign_id = Column(String(255), nullable=True, index=True)
    event_type = Column(String(10), nullable=False)  # 'open' | 'click'
    timestamp = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    ip = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    referrer = Column(Text, nullable=True)
    is_preview_bot = Column(Boolean, default=False, nullable=False)
    # Dedup: hash(token|ip|ua|event_type|minute). PostgreSQL unique index ignores NULLs,
    # but every app-created event will have a value.
    fingerprint = Column(String(64), nullable=True, unique=True)
