"""Spare-parts catalog seed + ticket charge calculation.

The catalog values here are placeholder market-typical prices in INR — the
ops team will replace them with real SKUs and prices later. Seeding is
idempotent: rows with the same (product_category, name) won't be duplicated.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Tuple

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..database import MIGRATION_SCHEMA, qualify
from ..models.spare import SpareCatalog
from ..models.ticket import ServiceType, Ticket, WarrantyStatus

# Minimum (and auto-filled default) out-of-warranty service charge, by service
# type. A site visit costs more than remote support — someone travels on-site.
# Covered (under-warranty / AMC) and third-party tickets have no minimum (0).
OOW_MIN_FEE_INR = {
    ServiceType.SITE_VISIT.value: 800,
    ServiceType.REMOTE_SUPPORT.value: 600,
}


def oow_min_service_fee_inr(ticket: Ticket) -> int:
    """The minimum service charge for this ticket: the per-service-type
    out-of-warranty floor when the ticket is out of warranty, else 0 (no floor).
    Staff may edit at/above this; only an Admin can set below it."""
    if ticket.warranty_status == WarrantyStatus.OUT_OF_WARRANTY.value:
        return OOW_MIN_FEE_INR.get(ticket.service_type, 0)
    return 0

logger = logging.getLogger("skposcare.spares")


# (product_category, name, default_price_inr)
DEFAULT_CATALOG: List[Tuple[str, str, int]] = [
    # POS Machine
    ("POS Machine", "Touchscreen panel", 4500),
    ("POS Machine", "Receipt printer head", 1800),
    ("POS Machine", "Power adapter (12V 5A)", 750),
    ("POS Machine", "Cooling fan", 350),
    ("POS Machine", "Internal SSD 128GB", 2200),
    # Printer
    ("Printer", "Print head", 1500),
    ("Printer", "Paper roll holder", 200),
    ("Printer", "Cutter blade", 450),
    ("Printer", "Power supply", 600),
    ("Printer", "Roller assembly", 800),
    # Kitchen Display Screen
    ("Kitchen Display Screen", "LCD panel 15\"", 5500),
    ("Kitchen Display Screen", "HDMI cable", 250),
    ("Kitchen Display Screen", "Mounting bracket", 600),
    ("Kitchen Display Screen", "Power adapter", 700),
    ("Kitchen Display Screen", "Cooling fan", 400),
    # UPS
    ("UPS", "12V battery", 1800),
    ("UPS", "Inverter board", 2500),
    ("UPS", "Cooling fan", 350),
    ("UPS", "Power switch", 150),
    ("UPS", "Display LCD", 900),
    # Kiosk
    ("Kiosk", "Touchscreen overlay", 4000),
    ("Kiosk", "NUC mainboard", 6500),
    ("Kiosk", "Bill acceptor", 8500),
    ("Kiosk", "Power supply", 1100),
    ("Kiosk", "Cooling fan", 500),
    # Tablet
    ("Tablet", "Display assembly", 3800),
    ("Tablet", "Battery", 1500),
    ("Tablet", "Charging port", 600),
    ("Tablet", "Power button flex", 250),
    ("Tablet", "Charger", 450),
    # Monitor
    ("Monitor", "Display panel", 3200),
    ("Monitor", "Power board", 1300),
    ("Monitor", "HDMI port", 350),
    ("Monitor", "Stand", 800),
    ("Monitor", "Power cable", 200),
    # CCTV
    ("CCTV", "IR LED board", 450),
    ("CCTV", "Lens module", 750),
    ("CCTV", "Power adapter", 350),
    ("CCTV", "BNC cable (10m)", 250),
    ("CCTV", "Mount bracket", 200),
]


def seed_spare_catalog(db: Session) -> int:
    """Insert any missing (product_category, name) catalog rows. Returns added count."""
    existing = {
        (row.product_category, row.name)
        for row in db.query(SpareCatalog.product_category, SpareCatalog.name).all()
    }
    added = 0
    for product, name, price in DEFAULT_CATALOG:
        if (product, name) in existing:
            continue
        db.add(SpareCatalog(product_category=product, name=name, default_price_inr=price))
        added += 1
    if added:
        db.commit()
        logger.info("Seeded %d spare catalog rows", added)
    return added


def ensure_service_fee_column(engine: Engine) -> None:
    """Add tickets.service_fee_inr if it's missing.

    `Base.metadata.create_all` doesn't add columns to existing tables, so we
    apply this small ALTER directly. Idempotent — only runs when the column
    isn't present. Works on both SQLite (dev) and Postgres (prod).
    """
    insp = inspect(engine)
    if "tickets" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # Fresh DB — create_all will include the column.
    columns = {c["name"] for c in insp.get_columns("tickets", schema=MIGRATION_SCHEMA)}
    if "service_fee_inr" in columns:
        return
    with engine.begin() as conn:
        conn.execute(
            text(f"ALTER TABLE {qualify('tickets')} ADD COLUMN service_fee_inr INTEGER NOT NULL DEFAULT 800")
        )
    logger.info("Added tickets.service_fee_inr column")


def _has_column(engine: Engine, table: str, column: str) -> bool:
    cols = {c["name"] for c in inspect(engine).get_columns(table, schema=MIGRATION_SCHEMA)}
    return column in cols


def ensure_service_type_column(engine: Engine) -> None:
    """Add tickets.service_type if it's missing.

    Same idempotent startup-ALTER pattern as ensure_service_fee_column, but
    hardened for zero-downtime deploys: ADD COLUMN needs an ACCESS EXCLUSIVE
    lock on `tickets`, and the previous instance keeps serving (holding row
    locks) until the new one is healthy. So we bound the lock wait and retry
    into a gap instead of blocking until the server statement_timeout cancels
    us and crashes startup. The DDL itself is metadata-only on PostgreSQL 11+
    (constant default), so it's instant once the lock is acquired.

    Existing rows backfill to SITE_VISIT (the default service mode).
    """
    insp = inspect(engine)
    if "tickets" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # Fresh DB — create_all will include the column.
    if _has_column(engine, "tickets", "service_type"):
        return

    ddl = (
        f"ALTER TABLE {qualify('tickets')} "
        f"ADD COLUMN service_type VARCHAR(20) NOT NULL DEFAULT 'SITE_VISIT'"
    )
    is_pg = engine.dialect.name == "postgresql"
    attempts = 6
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with engine.begin() as conn:
                if is_pg:
                    # Fail fast if the table is busy (so we retry into a gap),
                    # and don't let a short server statement_timeout cancel the
                    # metadata change once we do hold the lock.
                    conn.execute(text("SET LOCAL lock_timeout = '4s'"))
                    conn.execute(text("SET LOCAL statement_timeout = '120s'"))
                conn.execute(text(ddl))
            logger.info("Added tickets.service_type column")
            return
        except OperationalError as exc:
            last_exc = exc
            # A concurrent instance may have added it while we waited.
            if _has_column(engine, "tickets", "service_type"):
                logger.info("tickets.service_type already present (added concurrently)")
                return
            logger.warning(
                "ensure_service_type_column attempt %d/%d failed (table busy?): %s",
                attempt, attempts, exc,
            )
            if attempt < attempts:
                time.sleep(3 * attempt)
    if _has_column(engine, "tickets", "service_type"):
        return
    raise RuntimeError(
        "Could not add tickets.service_type after retries — the table stayed "
        "locked (likely a long-running transaction). Re-deploy or run the "
        "ALTER manually during a quiet window."
    ) from last_exc


def ensure_payment_columns(engine: Engine) -> None:
    """Add the out-of-warranty payment-tracking columns to tickets if missing.

    All four are nullable with no default, so existing rows stay NULL — which
    the workflow treats as 'legacy ticket, never payment-gated'. That's how the
    feature stays invisible to tickets created before it. Idempotent; uses the
    same bounded-lock retry as ensure_service_type_column for safe deploys.
    """
    insp = inspect(engine)
    if "tickets" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # Fresh DB — create_all will include the columns.

    is_pg = engine.dialect.name == "postgresql"
    ts_type = "TIMESTAMP WITH TIME ZONE" if is_pg else "DATETIME"
    wanted = [
        ("payment_status", "VARCHAR(20)"),
        ("payment_amount_inr", "INTEGER"),
        ("payment_collected_at", ts_type),
        ("payment_collected_by_id", "INTEGER"),
    ]
    for name, coltype in wanted:
        if _has_column(engine, "tickets", name):
            continue
        ddl = f"ALTER TABLE {qualify('tickets')} ADD COLUMN {name} {coltype}"
        attempts = 6
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                with engine.begin() as conn:
                    if is_pg:
                        conn.execute(text("SET LOCAL lock_timeout = '4s'"))
                        conn.execute(text("SET LOCAL statement_timeout = '120s'"))
                    conn.execute(text(ddl))
                logger.info("Added tickets.%s column", name)
                break
            except OperationalError as exc:
                last_exc = exc
                if _has_column(engine, "tickets", name):
                    break  # added concurrently by another instance
                logger.warning(
                    "ensure_payment_columns(%s) attempt %d/%d failed (table busy?): %s",
                    name, attempt, attempts, exc,
                )
                if attempt < attempts:
                    time.sleep(3 * attempt)
        else:
            if not _has_column(engine, "tickets", name):
                raise RuntimeError(
                    f"Could not add tickets.{name} after retries — table stayed "
                    "locked. Re-deploy or run the ALTER manually in a quiet window."
                ) from last_exc


def ensure_third_party_columns(engine: Engine) -> None:
    """Add the THIRD_PARTY_SUPPORT detail columns to tickets if missing.

    All three are nullable with no default, so existing rows stay NULL (only
    third-party tickets ever populate them). Idempotent; uses the same
    bounded-lock retry as ensure_payment_columns for safe zero-downtime deploys.
    """
    insp = inspect(engine)
    if "tickets" not in insp.get_table_names(schema=MIGRATION_SCHEMA):
        return  # Fresh DB — create_all will include the columns.

    is_pg = engine.dialect.name == "postgresql"
    wanted = [
        ("third_party_device_name", "VARCHAR(120)"),
        ("third_party_issue_info", "TEXT"),
        ("third_party_ticket_ref", "VARCHAR(120)"),
    ]
    for name, coltype in wanted:
        if _has_column(engine, "tickets", name):
            continue
        ddl = f"ALTER TABLE {qualify('tickets')} ADD COLUMN {name} {coltype}"
        attempts = 6
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                with engine.begin() as conn:
                    if is_pg:
                        conn.execute(text("SET LOCAL lock_timeout = '4s'"))
                        conn.execute(text("SET LOCAL statement_timeout = '120s'"))
                    conn.execute(text(ddl))
                logger.info("Added tickets.%s column", name)
                break
            except OperationalError as exc:
                last_exc = exc
                if _has_column(engine, "tickets", name):
                    break  # added concurrently by another instance
                logger.warning(
                    "ensure_third_party_columns(%s) attempt %d/%d failed (table busy?): %s",
                    name, attempt, attempts, exc,
                )
                if attempt < attempts:
                    time.sleep(3 * attempt)
        else:
            if not _has_column(engine, "tickets", name):
                raise RuntimeError(
                    f"Could not add tickets.{name} after retries — table stayed "
                    "locked. Re-deploy or run the ALTER manually in a quiet window."
                ) from last_exc


# --------------------------- charge calculation -------------------------- #

def compute_charges(ticket: Ticket) -> Dict[str, object]:
    """Return the billing summary for a ticket.

    Warranty rule: when the ticket is covered (under warranty or AMC), the
    SPARE line items bill at zero (parts are covered). The SERVICE FEE, however,
    is always billable — a covered ticket can still carry a chargeable visit/
    service fee (it just defaults to ₹0 and the engineer sets it). So a covered
    ticket with a non-zero service fee genuinely owes that amount.

    Remote-support rule: spare parts don't apply to remote tickets, so they
    never bill — only the service fee carries through. (Guards block adding
    spares to a remote ticket; this also neutralises any spares left over from
    before a switch to remote.)
    """
    is_warranty = ticket.warranty_status in (
        WarrantyStatus.UNDER_WARRANTY.value,
        WarrantyStatus.AMC.value,
    )
    # Spare parts never bill on remote OR third-party tickets — only the
    # service fee carries through.
    no_spares_billing = ticket.service_type in (
        ServiceType.REMOTE_SUPPORT.value,
        ServiceType.THIRD_PARTY_SUPPORT.value,
    )
    spares_billable = not is_warranty and not no_spares_billing
    line_items = []
    spares_list_price_total = 0
    for s in ticket.spares:
        line_total = int(s.unit_price_inr) * int(s.quantity)
        spares_list_price_total += line_total
        line_items.append(
            {
                "id": s.id,
                "catalog_id": s.catalog_id,
                "name": s.name,
                "unit_price_inr": int(s.unit_price_inr),
                "quantity": int(s.quantity),
                "line_total_inr": line_total,
                "billable": spares_billable,
            }
        )
    spares_billable_total = spares_list_price_total if spares_billable else 0
    service_fee = int(ticket.service_fee_inr or 0)
    # Service fee is always billable — even under warranty/AMC a visit fee may
    # be charged. It defaults to ₹0, so covered tickets owe nothing unless set.
    service_fee_billable = service_fee
    return {
        "warranty_status": ticket.warranty_status,
        "is_warranty": is_warranty,
        "service_fee_inr": service_fee,
        "service_fee_billable_inr": service_fee_billable,
        # Floor for the service fee (0 when none applies). The UI pre-fills and
        # caps non-Admin edits to this; an Admin may go below it.
        "service_fee_min_inr": oow_min_service_fee_inr(ticket),
        "spares_list_price_total_inr": spares_list_price_total,
        "spares_billable_total_inr": spares_billable_total,
        "grand_total_inr": spares_billable_total + service_fee_billable,
        "items": line_items,
    }
