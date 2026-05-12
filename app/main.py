"""FastAPI application entry point.

Run locally with:
    uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .database import Base, engine
from .routers import tickets as tickets_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("arckscare")

settings = get_settings()

app = FastAPI(
    title=f"{settings.app_name} API",
    description=(
        "ArcksCare - support ticket intake for hardware customers "
        "(POS, printers, KDS, UPS, kiosks, CCTV)."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tickets_router.router)

# Serve uploaded files at /uploads/<ticket_ref>/<filename>.
# The directory is created lazily on first upload; ensure it exists for the mount.
_uploads_dir = Path(settings.local_upload_dir)
_uploads_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(_uploads_dir)), name="uploads")


@app.on_event("startup")
def _bootstrap_db() -> None:
    """Auto-create tables for SQLite dev mode.

    In production (Postgres), use Alembic migrations instead. This block is a
    no-op once tables exist.
    """
    # Import models so SQLAlchemy registers them on Base.metadata.
    from .models import ticket as _t  # noqa: F401

    Base.metadata.create_all(bind=engine)
    logger.info("Database ready: %s", settings.database_url.split("@")[-1])


@app.get("/")
def root():
    return {
        "service": settings.app_name,
        "status": "ok",
        "docs": "/docs",
    }


@app.get("/healthz")
def healthcheck():
    return {"status": "ok"}
