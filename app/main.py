from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI
from app.routers import dashboard, tracking
from app import sheets


@asynccontextmanager
async def lifespan(app: FastAPI):
    await sheets.startup()
    yield


app = FastAPI(
    title="Picapool Tracker",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

app.include_router(tracking.router)
app.include_router(dashboard.router)


@app.get("/healthz", include_in_schema=False)
async def healthz():
    sheets_ok = sheets._links_ws is not None and sheets._events_ws is not None
    return {
        "status":        "ok" if sheets_ok else "degraded",
        "sheets":        "connected" if sheets_ok else "disconnected",
        "cached_links":  len(sheets._cache["links"]),
        "cached_events": len(sheets._cache["events"]),
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    }
