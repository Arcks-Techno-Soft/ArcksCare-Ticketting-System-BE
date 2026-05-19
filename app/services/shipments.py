"""Helpers for ticket-shipment maintenance.

Currently a single startup migration that adds the `delivered_at` column to
existing `ticket_shipments` rows on Postgres / SQLite. `create_all` only
adds NEW tables, never columns to existing ones, so we apply this small
ALTER directly. Idempotent.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger("skposcare.shipments")


def ensure_shipment_delivered_at_column(engine: Engine) -> None:
    insp = inspect(engine)
    if "ticket_shipments" not in insp.get_table_names():
        return  # Table doesn't exist yet — create_all will include the column.
    columns = {c["name"] for c in insp.get_columns("ticket_shipments")}
    if "delivered_at" in columns:
        return
    # SQLite uses dynamic typing — "TIMESTAMP" stores the same value either way;
    # Postgres wants TIMESTAMPTZ for timezone-aware datetimes (matching the
    # SQLAlchemy `DateTime(timezone=True)` mapping used everywhere else).
    column_type = "TIMESTAMPTZ" if engine.dialect.name == "postgresql" else "TIMESTAMP"
    with engine.begin() as conn:
        conn.execute(
            text(f"ALTER TABLE ticket_shipments ADD COLUMN delivered_at {column_type} NULL")
        )
    logger.info("Added ticket_shipments.delivered_at column (%s)", column_type)
