-- Reference schema (the app auto-creates these via SQLAlchemy on startup).
-- Run manually only if you need to pre-create or inspect the schema.

CREATE TABLE IF NOT EXISTS links (
    id           SERIAL PRIMARY KEY,
    token        VARCHAR(20)  NOT NULL UNIQUE,
    dest_url     TEXT         NOT NULL,
    campaign_id  VARCHAR(255),
    recipient_id VARCHAR(255),
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_links_token       ON links (token);
CREATE INDEX IF NOT EXISTS idx_links_campaign_id ON links (campaign_id);
CREATE INDEX IF NOT EXISTS idx_links_recipient_id ON links (recipient_id);

CREATE TABLE IF NOT EXISTS events (
    id             SERIAL PRIMARY KEY,
    token          VARCHAR(20)  NOT NULL,
    recipient_id   VARCHAR(255),
    campaign_id    VARCHAR(255),
    event_type     VARCHAR(10)  NOT NULL CHECK (event_type IN ('open','click')),
    timestamp      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    ip             VARCHAR(45),
    user_agent     TEXT,
    referrer       TEXT,
    is_preview_bot BOOLEAN      NOT NULL DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_events_token       ON events (token);
CREATE INDEX IF NOT EXISTS idx_events_campaign_id ON events (campaign_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp   ON events (timestamp);
