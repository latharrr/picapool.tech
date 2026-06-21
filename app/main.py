from contextlib import asynccontextmanager
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
