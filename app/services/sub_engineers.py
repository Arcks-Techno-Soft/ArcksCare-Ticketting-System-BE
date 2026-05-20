"""Sub-engineer helpers — schema migration for the per-ticket contractor fee."""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger("skposcare.sub_engineers")


def ensure_sub_engineer_fee_column(engine: Engine) -> None:
    """Add sub_engineers.fee_inr if it's missing.

    `create_all` doesn't add columns to existing tables, so we apply this small
    ALTER directly. Idempotent — only runs when the column isn't present. Works
    on both SQLite (dev) and Postgres (prod).
    """
    insp = inspect(engine)
    if "sub_engineers" not in insp.get_table_names():
        return  # Fresh DB — create_all will include the column.
    columns = {c["name"] for c in insp.get_columns("sub_engineers")}
    if "fee_inr" in columns:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE sub_engineers ADD COLUMN fee_inr INTEGER"))
    logger.info("Added sub_engineers.fee_inr column")
