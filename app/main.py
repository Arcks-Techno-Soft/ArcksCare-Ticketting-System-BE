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
from .database import Base, SessionLocal, engine, ensure_schema_exists
from .routers import admin as admin_router
from .routers import auth as auth_router
from .routers import installations as installations_router
from .routers import sign as sign_router
from .routers import tickets as tickets_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("skposcare")

settings = get_settings()

app = FastAPI(
    title=f"{settings.app_name} API",
    description=(
        "SK-POS Support - support ticket intake for hardware customers "
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
app.include_router(auth_router.router)
app.include_router(admin_router.router)
app.include_router(installations_router.router)
app.include_router(sign_router.router)

# Serve uploaded files at /uploads/<ticket_ref>/<filename>.
# The directory is created lazily on first upload; ensure it exists for the mount.
_uploads_dir = Path(settings.local_upload_dir)
_uploads_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=str(_uploads_dir)), name="uploads")


@app.on_event("startup")
def _bootstrap_db() -> None:
    """Auto-create tables on first boot + seed the initial owner/manager users.

    In production, use Alembic migrations instead. This block is idempotent —
    no-op once tables and seed users exist.
    """
    # Import models so SQLAlchemy registers them on Base.metadata.
    from .models import ticket as _t  # noqa: F401
    from .models import user as _u  # noqa: F401
    from .models import spare as _s  # noqa: F401
    from .models import installation as _i  # noqa: F401
    from .models import sub_engineer as _se  # noqa: F401
    from .models import ticket_engineer as _te  # noqa: F401
    from .services.auth import ensure_user_profile_columns, seed_initial_users
    from .services.sample_data import (
        seed_demo_tickets,
        seed_district_test_data,
        seed_sample_tickets,
    )
    from .services.shipments import ensure_shipment_delivered_at_column
    from .services.signing import ensure_resolution_field_signing_columns
    from .services.spares import (
        ensure_service_fee_column,
        ensure_service_type_column,
        seed_spare_catalog,
    )
    from .services.sub_engineers import ensure_sub_engineer_fee_column
    from .services.ticket_service import (
        ensure_raised_by_column,
        ensure_ticket_soft_delete_columns,
    )
    from .services.ticket_workflow import ensure_worknote_attempt_column
    from .services.installation_workflow import (
        ensure_installation_address_columns,
        ensure_installation_invoice_document_columns,
        ensure_installation_note_attempt_column,
        ensure_installation_products_column,
        ensure_installation_resolution_photo_columns,
    )

    # On a non-public (e.g. "test") schema, create it before anything tries to
    # write tables into it.
    ensure_schema_exists()
    # `create_all` adds new tables but NOT new columns on existing tables.
    # Run column migrations first so the freshly-mapped ORM matches the DB.
    ensure_service_fee_column(engine)
    ensure_service_type_column(engine)
    ensure_user_profile_columns(engine)
    ensure_shipment_delivered_at_column(engine)
    ensure_resolution_field_signing_columns(engine)
    ensure_sub_engineer_fee_column(engine)
    ensure_raised_by_column(engine)
    ensure_ticket_soft_delete_columns(engine)
    ensure_installation_invoice_document_columns(engine)
    ensure_installation_products_column(engine)
    ensure_installation_address_columns(engine)
    ensure_installation_resolution_photo_columns(engine)
    # Work-attempt FK columns on the existing notes tables (the attempt tables
    # themselves are created by create_all below).
    ensure_worknote_attempt_column(engine)
    ensure_installation_note_attempt_column(engine)
    Base.metadata.create_all(bind=engine)
    logger.info("Database ready: %s", settings.database_url.split("@")[-1])

    with SessionLocal() as db:
        seed_initial_users(db)
        seed_spare_catalog(db)
        # Fake/demo tickets are dev fixtures only. They are additive and
        # re-create themselves even after deletion, so they must never run
        # against a production database.
        if not settings.is_production:
            seed_sample_tickets(db)
            seed_district_test_data(db)
            seed_demo_tickets(db)


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
