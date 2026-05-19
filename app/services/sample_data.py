"""First-boot seed of realistic sample tickets — for analytics demo.

Generates ~50 tickets spread over the trailing 30 days, varied across status,
severity, product, issue category, and engineer assignment. Resolved tickets
have realistic resolving_started_at / resolved_at gaps so the analytics
dashboard has meaningful curves to draw.

Idempotent: only runs when zero tickets exist.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from ..models.ticket import Severity, Ticket, TicketStatus, WarrantyStatus
from ..models.user import User, UserRole

logger = logging.getLogger("skposcare.sample_data")


PRODUCTS = ["POS Terminal", "Receipt Printer", "Kitchen Display", "UPS", "Self-Service Kiosk", "CCTV"]
ISSUE_CATEGORIES = [
    "Won't power on", "Network connectivity", "Display flickering",
    "Print head jammed", "Battery not charging", "Touchscreen unresponsive",
    "Loud fan noise", "Software crash",
]
BUSINESS_TYPES = ["Restaurant", "Retail", "Hotel", "Cafe", "Pharmacy"]
CITIES = [
    ("Bengaluru", "Karnataka", "560001"),
    ("Mumbai", "Maharashtra", "400001"),
    ("Chennai", "Tamil Nadu", "600001"),
    ("Hyderabad", "Telangana", "500001"),
    ("Pune", "Maharashtra", "411001"),
    ("Delhi", "Delhi", "110001"),
]
BUSINESS_NAMES = [
    "Spice Garden", "Cafe Aroma", "Quickmart", "Hotel Maple", "Pizza House",
    "Greenleaf Pharmacy", "Sunset Diner", "Urban Bites", "Cornerstone Retail",
    "Royal Kitchen", "Midnight Cafe", "FreshMart", "Blue Lagoon", "Tandoor Hub",
]


def _reference_for(seq: int, year: int, state: Optional[str] = None) -> str:
    from ..utils.ticket_id import make_reference  # local import avoids cycle
    return make_reference(seq, state=state, year=year)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def seed_sample_tickets(db: Session) -> None:
    """Generate sample tickets if the table is empty."""
    if db.query(Ticket).count() > 0:
        return

    engineers: List[User] = (
        db.query(User).filter(User.role == UserRole.ENGINEER.value, User.active.is_(True)).all()
    )
    manager = db.query(User).filter(User.role == UserRole.MANAGER.value).first()
    owner = db.query(User).filter(User.role == UserRole.OWNER.value).first()

    if not engineers:
        logger.info("No engineers seeded — skipping sample tickets.")
        return

    rng = random.Random(42)  # deterministic seed
    now = datetime.now(timezone.utc)
    year = now.year

    # Status distribution chosen to make all analytics buckets non-empty.
    status_plan = (
        [TicketStatus.OPEN.value] * 4
        + [TicketStatus.ACKNOWLEDGED.value] * 3
        + [TicketStatus.ASSIGNED.value] * 4
        + [TicketStatus.ACCEPTED.value] * 3
        + [TicketStatus.RESOLVING.value] * 4
        + [TicketStatus.RESOLVED.value] * 18
        + [TicketStatus.CLOSED.value] * 12
    )
    rng.shuffle(status_plan)
    total = len(status_plan)

    created = 0
    for i, status in enumerate(status_plan):
        # Spread across the past 30 days, weighted toward more recent.
        days_ago = int(rng.triangular(0, 30, 4))
        hours_offset = rng.randint(0, 23)
        created_at = now - timedelta(days=days_ago, hours=hours_offset)

        city, state, pincode = rng.choice(CITIES)
        product = rng.choice(PRODUCTS)
        issue = rng.choice(ISSUE_CATEGORIES)
        severity = rng.choices(
            [Severity.LOW.value, Severity.MEDIUM.value, Severity.HIGH.value, Severity.CRITICAL.value],
            weights=[2, 5, 3, 1],
        )[0]
        warranty = rng.choices(
            [WarrantyStatus.UNDER_WARRANTY.value, WarrantyStatus.OUT_OF_WARRANTY.value,
             WarrantyStatus.UNKNOWN.value],
            weights=[4, 3, 1],
        )[0]

        engineer = rng.choice(engineers)
        assigned_at = created_at + timedelta(hours=rng.randint(1, 6))
        accepted_at = assigned_at + timedelta(minutes=rng.randint(10, 180))
        resolving_started_at = accepted_at + timedelta(minutes=rng.randint(30, 360))
        # Resolution time depends on severity, with noise: 1h..36h
        base_hours = {
            Severity.LOW.value: 1.5,
            Severity.MEDIUM.value: 4,
            Severity.HIGH.value: 8,
            Severity.CRITICAL.value: 12,
        }[severity]
        resolve_hours = max(0.5, rng.gauss(base_hours, base_hours * 0.4))
        resolved_at = resolving_started_at + timedelta(hours=resolve_hours)

        # Apply status-dependent timestamp truncation
        ack_at = assigned_at if status != TicketStatus.OPEN.value else None
        asg_at = assigned_at if status in {
            TicketStatus.ASSIGNED.value, TicketStatus.ACCEPTED.value,
            TicketStatus.RESOLVING.value, TicketStatus.RESOLVED.value,
            TicketStatus.CLOSED.value,
        } else None
        acc_at = accepted_at if status in {
            TicketStatus.ACCEPTED.value, TicketStatus.RESOLVING.value,
            TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value,
        } else None
        rs_at = resolving_started_at if status in {
            TicketStatus.RESOLVING.value, TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value,
        } else None
        r_at = resolved_at if status in {
            TicketStatus.RESOLVED.value, TicketStatus.CLOSED.value,
        } else None

        # Assigned engineer / acker / assigner population
        eng_id = engineer.id if asg_at else None
        acked_by_id = (manager.id if manager else owner.id if owner else None) if ack_at else None
        assigned_by_id = acked_by_id if asg_at else None

        ticket = Ticket(
            reference=_reference_for(i + 1, year, state=state),
            business_name=rng.choice(BUSINESS_NAMES),
            contact_name=rng.choice(["Anita Rao", "Vikram Shah", "Priya Iyer", "Rahul Mehta",
                                     "Neha Gupta", "Arjun Reddy", "Sneha Kapoor"]),
            phone=f"+9198{rng.randint(10000000, 99999999)}",
            email=f"contact{i+1}@example.com",
            business_type=rng.choice(BUSINESS_TYPES),
            address_line1=f"{rng.randint(1, 999)} {rng.choice(['MG Road', 'Park Street', 'Brigade Road', 'Linking Road'])}",
            city=city,
            state=state,
            pincode=pincode,
            product_category=product,
            serial_number=f"{product.split()[0][:3].upper()}-{rng.randint(10000, 99999)}",
            issue_category=issue,
            severity=severity,
            description=f"Customer reports: {issue.lower()} on the {product}. "
                        f"Issue started during business hours; affecting daily operations.",
            status=status,
            warranty_status=warranty,
            acknowledged_by_id=acked_by_id,
            acknowledged_at=ack_at,
            assigned_by_id=assigned_by_id,
            assigned_engineer_id=eng_id,
            assigned_at=asg_at,
            accepted_at=acc_at,
            resolving_started_at=rs_at,
            resolved_at=r_at,
            resolution_summary=(
                f"Replaced faulty component and validated full functionality. "
                f"Customer signed off after on-site test."
                if r_at else None
            ),
            service_fee_inr=rng.choice([300, 500, 750, 1000]),
            created_at=created_at,
            updated_at=r_at or rs_at or acc_at or asg_at or ack_at or created_at,
        )
        db.add(ticket)
        created += 1

    db.commit()
    logger.info("Seeded %d sample tickets for analytics.", created)
