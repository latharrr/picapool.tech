from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.database import init_db
from app.routers import dashboard, tracking
from app import sheets


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await sheets.start_batch_worker()
    yield


app = FastAPI(
    title="Picapool Tracker",
    description="Click / open tracking service for picapool.tech",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,   # disable public Swagger
    redoc_url=None,
)

app.include_router(tracking.router)
app.include_router(dashboard.router)
