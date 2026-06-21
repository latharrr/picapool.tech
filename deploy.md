# Deploying picapool.tech tracker on Railway

## 1 — Create the Railway project

```bash
# Install Railway CLI
npm i -g @railway/cli
railway login

# From the repo root
railway init          # creates a new project
railway up            # deploys using Dockerfile
```

Or go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo** → select this repo.

---

## 2 — Add the Postgres plugin

In the Railway dashboard for your project:

1. Click **+ New** → **Database** → **PostgreSQL**
2. Railway provisions the DB and injects `DATABASE_URL` into your service automatically.
   The URL will be `postgres://...` — the app normalises it to `postgresql+asyncpg://` at startup.
3. Tables are created automatically on first boot (SQLAlchemy `create_all`).

---

## 3 — Set environment variables

In the Railway dashboard → your service → **Variables**, add:

| Variable | Value | Required |
|---|---|---|
| `DASHBOARD_PASSWORD` | Strong password for `/dashboard` | **Yes** |
| `SECRET_KEY` | `openssl rand -hex 32` output | **Yes** |
| `GOOGLE_CREDENTIALS_JSON` | Full contents of service_account.json (single line) | No |
| `GOOGLE_SHEET_ID` | Sheet ID from the Google Sheets URL | No |
| `SHEETS_BATCH_INTERVAL` | Seconds between Sheets flushes (default `10`) | No |
| `IGNORED_THRESHOLD_HOURS` | Hours before no-event link = ignored (default `48`) | No |

> `DATABASE_URL` is injected automatically by the Postgres plugin — do **not** set it manually.

Generate `SECRET_KEY`:
```bash
openssl rand -hex 32
```

### Google Sheets credential (no file upload needed)

Railway doesn't support mounted files. Instead, paste the service-account JSON as a single-line string:

```bash
# One-liner to collapse service_account.json to a single line
cat service_account.json | jq -c . | pbcopy   # macOS
cat service_account.json | jq -c .            # Linux — copy output manually
```

Paste the result as the value of `GOOGLE_CREDENTIALS_JSON`.

---

## 4 — Custom domain (picapool.tech)

In Railway dashboard → your service → **Settings** → **Networking** → **Custom Domain**:

1. Add `picapool.tech`
2. Railway shows you a `CNAME` target (e.g. `xxx.up.railway.app`)

In your DNS provider (Cloudflare recommended):

| Type | Name | Value | Proxy |
|---|---|---|---|
| CNAME | `@` | `xxx.up.railway.app` | Yes (orange cloud) |
| CNAME | `www` | `xxx.up.railway.app` | Yes |

Railway issues a TLS certificate automatically once DNS propagates (~1–5 min with Cloudflare).

> If your DNS provider doesn't support CNAME on the apex (`@`), use an **ALIAS** or **ANAME** record, or use Cloudflare which flattens CNAME at the apex.

---

## 5 — Verify the deployment

```bash
# Health check
curl https://picapool.tech/dashboard         # → 200 HTML

# Create a test link
curl -X POST https://picapool.tech/api/links \
  -H "Content-Type: application/json" \
  -b "tracker_session=<cookie-from-browser>" \
  -d '{"dest_url":"https://example.com","campaign_id":"test","recipient_id":"alice"}'

# Test click tracking
curl -I https://picapool.tech/t/<token>      # → 302 redirect

# Test pixel tracking
curl -I https://picapool.tech/p/<token>.png  # → 200 image/png
```

---

## 6 — Using tracking links in emails

**Click tracking** — wrap every link:
```
https://picapool.tech/t/<token>
```

**Open tracking** — add at the bottom of your email HTML:
```html
<img src="https://picapool.tech/p/<token>.png" width="1" height="1"
     alt="" style="display:none;border:0;" />
```

Use **Dashboard → Create Link** (single) or **Bulk Generator** (CSV output) to generate tokens.

---

## Bot & dedup behaviour

| Scenario | Result |
|---|---|
| WhatsApp / Slack / iMessage preview fetches link | Logged with `is_preview_bot=true`, excluded from click counts |
| Real user clicks the link | Logged with `is_preview_bot=false`, counted immediately as genuine click |
| Same IP + UA hits same token twice within one minute | Second hit silently dropped (fingerprint dedup) |
| Link with `is_active=false` or past `expires_at` | Returns 404 for click; returns pixel silently for open |

Genuine clicks = `event_type='click' AND is_preview_bot=false`. No dependency on a prior bot hit.

---

## "Ignored" logic

Computed at query time, no background job:

> A link is **ignored** when `created_at < NOW() - IGNORED_THRESHOLD_HOURS` and it has zero associated events.

---

## Environment variables reference

| Variable | Default | Description |
|---|---|---|
| `DASHBOARD_PASSWORD` | *(required)* | `/dashboard` login password |
| `SECRET_KEY` | *(required)* | Signs session cookies |
| `DATABASE_URL` | *(injected by Railway)* | Postgres connection string |
| `GOOGLE_CREDENTIALS_JSON` | `""` | Raw service-account JSON (Railway) |
| `GOOGLE_SHEETS_CREDENTIALS_PATH` | `service_account.json` | Key file path (local dev) |
| `GOOGLE_SHEET_ID` | `""` | Target spreadsheet ID |
| `SHEETS_BATCH_INTERVAL` | `10` | Seconds between Sheets flushes |
| `IGNORED_THRESHOLD_HOURS` | `48` | Hours until no-event link = ignored |
