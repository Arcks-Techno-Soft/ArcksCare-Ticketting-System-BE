"""Spare-parts catalog seed + ticket charge calculation.

The catalog values here are placeholder market-typical prices in INR — the
ops team will replace them with real SKUs and prices later. Seeding is
idempotent: rows with the same (product_category, name) won't be duplicated.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Tuple

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..database import MIGRATION_SCHEMA, qualify
from ..models.spare import SpareCatalog
from ..models.ticket import Ticket, WarrantyStatus

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
            text(f"ALTER TABLE {qualify('tickets')} ADD COLUMN service_fee_inr INTEGER NOT NULL DEFAULT 500")
        )
    logger.info("Added tickets.service_fee_inr column")


# --------------------------- charge calculation -------------------------- #

def compute_charges(ticket: Ticket) -> Dict[str, object]:
    """Return the billing summary for a ticket.

    Warranty rule: under warranty, BOTH the spare line items and the service
    fee bill at zero. The stored values are still surfaced (so the customer
    sees the goodwill amount) but `*_billable_inr` and `grand_total_inr`
    reflect what they actually owe.
    """
    is_warranty = ticket.warranty_status == WarrantyStatus.UNDER_WARRANTY.value
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
                "billable": not is_warranty,
            }
        )
    spares_billable_total = 0 if is_warranty else spares_list_price_total
    service_fee = int(ticket.service_fee_inr or 0)
    service_fee_billable = 0 if is_warranty else service_fee
    return {
        "warranty_status": ticket.warranty_status,
        "is_warranty": is_warranty,
        "service_fee_inr": service_fee,
        "service_fee_billable_inr": service_fee_billable,
        "spares_list_price_total_inr": spares_list_price_total,
        "spares_billable_total_inr": spares_billable_total,
        "grand_total_inr": spares_billable_total + service_fee_billable,
        "items": line_items,
    }
