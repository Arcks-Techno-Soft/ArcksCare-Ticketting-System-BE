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

from sqlalchemy import func
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


# District-coverage test data: field engineers in distinct districts plus one
# open ticket from each. Lets the district-aware assignment dropdown be tested
# end-to-end. `city` is set equal to the district so matching works (the
# assignment dropdown matches engineer.district against ticket.city).
# Tuple: (district, state, pincode, first, last, username, password,
#         business_name, contact_name, product, issue, serial)
DISTRICT_SEED = [
    ("Mysuru", "Karnataka", "570001", "Suresh", "Gowda",
     "suresh.mysuru", "suresh123", "Mysuru Test Diner", "Latha Prasad",
     "POS Machine", "Display Issue", "DISTSEED-MYS-01"),
    ("Mangaluru", "Karnataka", "575001", "Prakash", "Shetty",
     "prakash.mangaluru", "prakash123", "Mangaluru Test Cafe", "Deepak Pai",
     "Printer", "Printing Issue", "DISTSEED-MNG-01"),
    ("Coimbatore", "Tamil Nadu", "641001", "Karthik", "Raman",
     "karthik.coimbatore", "karthik123", "Coimbatore Test Hotel", "Meena Subramani",
     "Kitchen Display Screen", "Software Crash", "DISTSEED-CBE-01"),
    ("Vijayawada", "Andhra Pradesh", "520001", "Naveen", "Reddy",
     "naveen.vijayawada", "naveen123", "Vijayawada Test Mart", "Ramesh Babu",
     "UPS", "Not Powering On", "DISTSEED-VJA-01"),
    ("Nashik", "Maharashtra", "422001", "Amol", "Patil",
     "amol.nashik", "amol123", "Nashik Test Kitchen", "Sanjay Jadhav",
     "CCTV", "Connectivity", "DISTSEED-NSK-01"),
]

# Ad-hoc field contractors for each district, attached to that district's
# seeded ticket. Sub-engineers always belong to a ticket; once recorded they
# also surface as suggestions for future tickets in the same city.
# Keyed by district -> list of (name, phone).
DISTRICT_SUB_ENGINEERS = {
    "Mysuru": [("Ravi Kumar", "+919812345001"), ("Manjunath Bhat", "+919812345002")],
    "Mangaluru": [("Vasanth Rao", "+919812345003"), ("Ganesh Hegde", "+919812345004")],
    "Coimbatore": [("Senthil Murugan", "+919812345005"), ("Arun Prakash", "+919812345006")],
    "Vijayawada": [("Srinivas Chowdary", "+919812345007"), ("Kiran Varma", "+919812345008")],
    "Nashik": [("Rohit Pawar", "+919812345009"), ("Sachin Deshmukh", "+919812345010")],
}


def seed_district_test_data(db: Session) -> None:
    """Seed field engineers in distinct districts, one acknowledged ticket from
    each, and a per-district sub-engineer roster — so the district-aware
    assignment dropdown and the sub-engineer roster can be tested.

    Idempotent and additive: every row is guarded by its own existence check,
    so the seed self-heals and never duplicates across restarts.
    """
    from ..models.sub_engineer import SubEngineer, SubEngineerRoster  # local import avoids a cycle
    from ..services.auth import hash_password
    from ..utils.ticket_id import make_reference

    acker = (
        db.query(User).filter(User.role == UserRole.MANAGER.value).first()
        or db.query(User).filter(User.role == UserRole.OWNER.value).first()
    )

    now = datetime.now(timezone.utc)
    year = now.year
    n_eng = n_tic = n_roster = n_removed = 0

    for (district, state, pincode, first, last, username, password,
         business_name, contact_name, product, issue, serial) in DISTRICT_SEED:
        # --- engineer covering this district ---
        if db.query(User).filter(User.username == username).first() is None:
            db.add(User(
                username=username,
                password_hash=hash_password(password),
                name=f"{first} {last}",
                first_name=first,
                last_name=last,
                phone=f"+9190{random.Random(username).randint(10000000, 99999999)}",
                email=f"{username}@arckscare.example",
                role=UserRole.ENGINEER.value,
                district=district,
                active=True,
            ))
            n_eng += 1

        # --- one acknowledged ticket raised from that district ---
        ticket = db.query(Ticket).filter(Ticket.serial_number == serial).first()
        if ticket is None:
            ticket = Ticket(
                reference=f"SEED-{serial}",  # temp unique value; replaced after flush
                business_name=business_name,
                contact_name=contact_name,
                phone="+919800000000",
                email=f"{serial.lower()}@example.com",
                business_type="Restaurant",
                address_line1="1 Test Road",
                city=district,  # city == district so the matching works
                state=state,
                pincode=pincode,
                product_category=product,
                serial_number=serial,
                issue_category=issue,
                severity=Severity.MEDIUM.value,
                description=(
                    f"Sample ticket from {district} for testing district-based "
                    f"engineer assignment. Reported: {issue.lower()} on the {product}."
                ),
                status=TicketStatus.ACKNOWLEDGED.value,
                warranty_status=WarrantyStatus.UNKNOWN.value,
                acknowledged_by_id=acker.id if acker else None,
                acknowledged_at=now,
                created_at=now,
                updated_at=now,
            )
            db.add(ticket)
            db.flush()  # assigns ticket.id
            ticket.reference = make_reference(ticket.id, state=state, year=year)
            n_tic += 1

        # --- district roster of field contractors (reusable across tickets) ---
        for sub_name, sub_phone in DISTRICT_SUB_ENGINEERS.get(district, []):
            in_roster = (
                db.query(SubEngineerRoster)
                .filter(
                    func.lower(SubEngineerRoster.name) == sub_name.lower(),
                    func.lower(SubEngineerRoster.district) == district.lower(),
                )
                .first()
            )
            if in_roster is None:
                db.add(SubEngineerRoster(
                    name=sub_name,
                    phone=sub_phone,
                    district=district,
                    created_by_user_id=acker.id if acker else None,
                ))
                n_roster += 1
            # Drop the earlier per-ticket seed rows so the roster is the single
            # source and every district ticket's add-dropdown is populated.
            for stale in (
                db.query(SubEngineer)
                .filter(SubEngineer.ticket_id == ticket.id, SubEngineer.name == sub_name)
                .all()
            ):
                db.delete(stale)
                n_removed += 1

    db.commit()
    logger.info(
        "Seeded district test data: %d engineers, %d tickets, %d roster contacts "
        "(%d stale ticket rows removed)",
        n_eng, n_tic, n_roster, n_removed,
    )


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
