"""Admin endpoints for the internal staff app.

All routes here require a valid JWT via `get_current_user`. Phase 2.2 adds
acknowledge / assign / warranty actions, plus engineer + event listing.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.installation import Installation, InstallationStatus
from ..models.shipment import TicketShipment, TicketShipmentItem
from ..models.spare import SpareCatalog, TicketSpare
from ..models.sub_engineer import SubEngineer, SubEngineerRoster
from ..models.ticket import ServiceType, Severity, Ticket, TicketEvent, TicketStatus, WorkNote
from ..models.ticket_engineer import TicketEngineer
from ..models.user import User, UserRole, ADMIN_MANAGER_ROLES, ADMIN_ROLES, SUPER_ADMIN_ROLES
from ..schemas.auth import (
    EngineerOption,
    AddSubEngineerRequest,
    AddTicketSpareRequest,
    AddWorkNoteRequest,
    AdditionalEngineerOut,
    BusinessNameSuggestion,
    AssignEngineerRequest,
    ChargesSummary,
    CollectPaymentRequest,
    CreateRosterSubEngineerRequest,
    CreateUserRequest,
    CreateUserResponse,
    DeleteTicketRequest,
    ForceCloseRequest,
    RegisterPushTokenRequest,
    ResolveRequest,
    SpareCatalogItem,
    SubEngineerOut,
    SubEngineerRosterOut,
    TicketEventOut,
    TicketSpareOut,
    UpdateRosterSubEngineerRequest,
    UpdateServiceTypeRequest,
    UpdateThirdPartyInfoRequest,
    UpdateSeverityRequest,
    UpdateServiceFeeRequest,
    UpdateSubEngineerFeeRequest,
    UpdateTicketSpareRequest,
    UnregisterPushTokenRequest,
    UpdateUserActiveRequest,
    UpdateUserRoleRequest,
    UpdateUserSalesRepRequest,
    UpdateWarrantyRequest,
    UserOut,
    WorkNoteOut,
)
from ..schemas.ticket import (
    ClosePreviewOut,
    ShipmentCreate,
    ShipmentOut,
    TicketListItem,
    TicketListPage,
    TicketResponse,
)
from ..services.analytics import compute_analytics
from ..services.reports import compute_ticket_report
from ..services.auth import get_current_user, hash_password, require_role
from ..services.email import send_engineer_assignment
from ..services.push import notify_ticket_assigned, register_token, unregister_token
from ..services.whatsapp import send_engineer_assignment_alert
from ..services.signing import (
    generate_field_sign_link,
    record_customer_signature_via_engineer,
    record_engineer_signature,
)
from ..services.spares import compute_charges, oow_min_service_fee_inr
from ..services.storage import get_storage
from ..services.ticket_workflow import (
    accept,
    acknowledge,
    add_engineer,
    add_work_note,
    assign_engineer,
    collect_payment,
    compute_close_pending,
    end_attempt,
    force_close,
    remove_engineer,
    resolve,
    set_service_type,
    set_third_party_info,
    soft_delete_ticket,
    start_attempt,
    start_work,
    update_severity,
    update_warranty,
)

logger = logging.getLogger("skposcare.admin")

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


# --------------------------- profile + lookups -------------------------- #

@router.get("/me", response_model=UserOut)
def my_profile(user: User = Depends(get_current_user)):
    return user


# --------------------------- push notifications ------------------------- #

@router.post("/push/register", status_code=204)
def register_push_token(
    body: RegisterPushTokenRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Register this device's Expo push token for the logged-in user.

    Called by the mobile app after login (and on token refresh). Idempotent.
    """
    register_token(db, user, body.token, body.platform)
    return None


@router.post("/push/unregister", status_code=204)
def unregister_push_token(
    body: UnregisterPushTokenRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Remove this device's token (called on logout)."""
    unregister_token(db, body.token)
    return None


# --------------------------- user management (Admin-only) --------------- #

@router.get("/users", response_model=List[UserOut])
def list_users(
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(UserRole.SUPER_ADMIN)),
):
    """All staff accounts — Admin only."""
    return db.query(User).order_by(User.created_at.desc()).all()


@router.post("/users", response_model=CreateUserResponse, status_code=201)
def create_user(
    body: CreateUserRequest,
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(UserRole.SUPER_ADMIN)),
):
    """Create a new Admin (Manager) or Engineer account.

    Admin explicitly chooses username + password — no auto-generation, no
    temp-password handoff. Username must be unique across all users
    (including ADMINs and inactive accounts). Email uniqueness is also
    enforced so we never end up with two profiles tied to the same inbox.
    """
    username = body.username.strip().lower()

    # Pydantic already validated the character set; this is just the DB-uniqueness
    # check (case-insensitive: usernames are stored lowercased on create).
    if db.query(User).filter(User.username == username).first():
        raise HTTPException(status_code=409, detail="That username is already taken.")
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(status_code=409, detail="A user with this email already exists.")

    user = User(
        username=username,
        password_hash=hash_password(body.password),
        name=f"{body.first_name} {body.last_name}".strip(),
        first_name=body.first_name,
        last_name=body.last_name,
        phone=body.phone,
        email=body.email,
        role=body.role,
        # District only applies to field engineers.
        district=body.district if body.role == UserRole.ENGINEER.value else None,
        # SALES-role users are implicitly sales reps; the flag lets any other
        # role (e.g. MANAGER) also be credited on installations.
        is_sales_rep=bool(body.is_sales_rep) or body.role == UserRole.SALES.value,
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    logger.info("ADMIN %s created user %s (%s)", _user.username, user.username, user.role)
    return CreateUserResponse(user=UserOut.model_validate(user))


@router.patch("/users/{user_id}/active", response_model=UserOut)
def set_user_active(
    user_id: int,
    body: UpdateUserActiveRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_role(UserRole.SUPER_ADMIN)),
):
    """Activate or deactivate a user. Admins can't deactivate themselves."""
    target = db.query(User).filter(User.id == user_id).one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if target.id == actor.id and not body.active:
        raise HTTPException(status_code=400, detail="You cannot deactivate your own account.")
    target.active = body.active
    db.commit()
    db.refresh(target)
    return target


@router.patch("/users/{user_id}/sales-rep", response_model=UserOut)
def set_user_sales_rep(
    user_id: int,
    body: UpdateUserSalesRepRequest,
    db: Session = Depends(get_db),
    _actor: User = Depends(require_role(UserRole.SUPER_ADMIN)),
):
    """Flag/unflag a user as a sales rep (creditable on installations),
    independent of their role. Admin only."""
    target = db.query(User).filter(User.id == user_id).one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")
    target.is_sales_rep = bool(body.is_sales_rep)
    db.commit()
    db.refresh(target)
    return target


@router.patch("/users/{user_id}/role", response_model=UserOut)
def set_user_role(
    user_id: int,
    body: UpdateUserRoleRequest,
    db: Session = Depends(get_db),
    actor: User = Depends(require_role(UserRole.SUPER_ADMIN)),
):
    """Change a user's role. Super-Admin-only — this is how Admins are promoted
    or demoted (and roles reassigned generally) without touching the DB.

    Guardrails:
      - A Super Admin can't demote themselves out of Super Admin (avoids the
        last owner locking themselves out of the reserved powers).
    """
    target = db.query(User).filter(User.id == user_id).one_or_none()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")

    new_role = body.role
    if target.id == actor.id and new_role not in SUPER_ADMIN_ROLES:
        raise HTTPException(
            status_code=400,
            detail="You cannot remove your own Super Admin role.",
        )

    prev_role = target.role
    target.role = new_role
    # District only applies to engineers; clear it when leaving that role so a
    # promoted engineer doesn't keep a stale district on their profile.
    if new_role != UserRole.ENGINEER.value:
        target.district = None
    # SALES-role users are always creditable as sales reps.
    if new_role == UserRole.SALES.value:
        target.is_sales_rep = True
    db.commit()
    db.refresh(target)
    logger.info(
        "SUPER_ADMIN %s changed role of %s: %s -> %s",
        actor.username, target.username, prev_role, new_role,
    )
    return target


# --------------------------- analytics (Admin/Manager) ------------------ #

@router.get("/analytics")
def get_analytics(
    days: int = Query(default=30, ge=7, le=365),
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(UserRole.ADMIN, UserRole.MANAGER)),
):
    """Aggregated metrics for the analytics dashboard. Admin/Manager."""
    return compute_analytics(db, days)


@router.get("/reports/tickets")
def get_ticket_report(
    date_from: date = Query(..., description="Start of created-date range (IST, inclusive)"),
    date_to: date = Query(..., description="End of created-date range (IST, inclusive)"),
    db: Session = Depends(get_db),
    _user: User = Depends(require_role(UserRole.ADMIN, UserRole.MANAGER)),
):
    """Date-ranged ticket report with SLA escalation analysis (Admin/Manager)."""
    if date_to < date_from:
        raise HTTPException(status_code=400, detail="date_to must be on or after date_from")
    return compute_ticket_report(db, date_from, date_to)


# --------------------------- lookups ------------------------------------ #

@router.get("/engineers", response_model=List[EngineerOption])
def list_engineers(
    include_sales_reps: bool = Query(default=False),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Active engineers for the 'assign to' picker, each annotated with their
    current open workload (open tickets + pending installations) so the UI can
    recommend the least-busy ones.

    Managers are included too: they sometimes work complaints/installations
    themselves, so Admin/Owner and Managers may assign to a Manager as well as
    an Engineer. (Admin/Owner accounts are deliberately NOT listed — they are
    not assignable.) The assignee write path and the engineer workflow already
    accept any active user regardless of role, so a Manager can accept/resolve
    a ticket assigned to them.

    When `include_sales_reps` is set, active SALES-role users are returned too
    (each carrying role="SALES") so Admin/Manager can assign service calls and
    installations to a sales rep as well. Workload counts are keyed on
    `assigned_engineer_id`, so a manager's or sales rep's count reflects work
    assigned to them just like an engineer's."""
    roles = [UserRole.ENGINEER.value, UserRole.MANAGER.value]
    if include_sales_reps:
        roles.append(UserRole.SALES.value)
    engineers = (
        db.query(User)
        .filter(User.role.in_(roles), User.active.is_(True))
        .order_by(User.name)
        .all()
    )
    # Per-engineer count of active (not CLOSED, not deleted) assigned tickets.
    ticket_counts = dict(
        db.query(Ticket.assigned_engineer_id, func.count(Ticket.id))
        .filter(
            Ticket.assigned_engineer_id.isnot(None),
            Ticket.status != TicketStatus.CLOSED.value,
            Ticket.deleted_at.is_(None),
        )
        .group_by(Ticket.assigned_engineer_id)
        .all()
    )
    # Per-engineer count of pending (not CLOSED) assigned installations.
    install_counts = dict(
        db.query(Installation.assigned_engineer_id, func.count(Installation.id))
        .filter(
            Installation.assigned_engineer_id.isnot(None),
            Installation.status != InstallationStatus.CLOSED.value,
        )
        .group_by(Installation.assigned_engineer_id)
        .all()
    )
    out: List[EngineerOption] = []
    for u in engineers:
        item = EngineerOption.model_validate(u)
        item.open_ticket_count = int(ticket_counts.get(u.id, 0)) + int(
            install_counts.get(u.id, 0)
        )
        out.append(item)
    return out


@router.get("/businesses", response_model=List[str])
def list_businesses(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Distinct business names for the inbox "sort by business" picker.

    Scoped to the tickets the caller can see (engineers → their own; sales →
    what they raised) so the dropdown never reveals businesses they can't open.
    Soft-deleted tickets are excluded.
    """
    q = db.query(Ticket.business_name).filter(Ticket.deleted_at.is_(None))
    if user.role == UserRole.ENGINEER.value:
        additional = db.query(TicketEngineer.ticket_id).filter(
            TicketEngineer.engineer_id == user.id
        )
        q = q.filter(
            or_(Ticket.assigned_engineer_id == user.id, Ticket.id.in_(additional))
        )
    elif user.role == UserRole.SALES.value:
        q = q.filter(Ticket.raised_by_id == user.id)
    rows = q.distinct().order_by(Ticket.business_name).all()
    return [r[0] for r in rows if r[0]]


@router.get("/sales-reps", response_model=List[UserOut])
def list_sales_reps(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    """Users creditable as a sales rep — SALES-role users plus anyone flagged
    `is_sales_rep` (e.g. a Manager who also does sales). Feeds the 'sales
    representative' picker. Any signed-in staff member can read it."""
    return (
        db.query(User)
        .filter(
            User.active.is_(True),
            or_(User.role == UserRole.SALES.value, User.is_sales_rep.is_(True)),
        )
        .order_by(User.name)
        .all()
    )


# --------------------------- ticket list + detail ----------------------- #

@router.get("/tickets", response_model=TicketListPage)
def list_tickets(
    status: Optional[str] = Query(default=None),
    severity: Optional[str] = Query(default=None),
    product: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    created_within_days: Optional[int] = Query(default=None, ge=1, le=3650),
    # Inbox "Sort by" control. None = legacy default (status grouping) so other
    # clients are unaffected; the web inbox always sends one of these.
    sort: Optional[str] = Query(
        default=None,
        pattern="^(newest|oldest|severity|status|unassigned)$",
    ),
    # Narrow the inbox to one engineer's tickets (the "Engineer" sort view).
    assigned_engineer_id: Optional[int] = Query(default=None, ge=1),
    # Narrow the inbox to one business (the "Business" sort view). Exact match.
    business_name: Optional[str] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(Ticket).filter(Ticket.deleted_at.is_(None))
    # Engineers only see tickets assigned to them — as primary OR as an
    # additional engineer brought in for the same visit.
    if user.role == UserRole.ENGINEER.value:
        additional = db.query(TicketEngineer.ticket_id).filter(
            TicketEngineer.engineer_id == user.id
        )
        q = q.filter(
            or_(Ticket.assigned_engineer_id == user.id, Ticket.id.in_(additional))
        )
    elif user.role == UserRole.SALES.value:
        # Sales reps see tickets they raised OR that are assigned to them (a
        # service call can now be assigned to a sales rep).
        q = q.filter(
            or_(Ticket.raised_by_id == user.id, Ticket.assigned_engineer_id == user.id)
        )
    if status:
        q = q.filter(Ticket.status == status.upper())
    if severity:
        q = q.filter(Ticket.severity == severity.upper())
    if product:
        q = q.filter(Ticket.product_category == product)
    if assigned_engineer_id:
        q = q.filter(Ticket.assigned_engineer_id == assigned_engineer_id)
    if business_name:
        q = q.filter(Ticket.business_name == business_name)
    if created_within_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=created_within_days)
        q = q.filter(Ticket.created_at >= cutoff)
    if search:
        like = f"%{search.strip()}%"
        like_upper = f"%{search.strip().upper()}%"
        q = q.filter(
            or_(
                Ticket.reference.ilike(like_upper),
                Ticket.business_name.ilike(like),
                Ticket.serial_number.ilike(like_upper),
            )
        )
    # COUNT runs against the same filtered query so totals stay accurate.
    total = q.with_entities(func.count(Ticket.id)).scalar() or 0
    # Default order for the admin inbox: group by workflow status so the
    # tickets needing attention float to the top and finished ones sink to the
    # bottom. Within a status group, most severe first (CRITICAL → … → LOW),
    # then newest-first as the final tiebreaker. The CASEs follow the
    # TicketStatus lifecycle (OPEN → … → CLOSED) and Severity scale; unknown
    # values sort last.
    status_rank = case(
        {
            TicketStatus.OPEN.value: 0,
            TicketStatus.ACKNOWLEDGED.value: 1,
            TicketStatus.ASSIGNED.value: 2,
            TicketStatus.ACCEPTED.value: 3,
            TicketStatus.RESOLVING.value: 4,
            TicketStatus.RESOLVED.value: 5,
            TicketStatus.CLOSED.value: 6,
        },
        value=Ticket.status,
        else_=99,
    )
    severity_rank = case(
        {
            Severity.CRITICAL.value: 0,
            Severity.HIGH.value: 1,
            Severity.MEDIUM.value: 2,
            Severity.LOW.value: 3,
        },
        value=Ticket.severity,
        else_=99,
    )
    # Unassigned-first: tickets with no engineer float to the top.
    unassigned_rank = case((Ticket.assigned_engineer_id.is_(None), 0), else_=1)
    # The "Sort by" control maps to an ORDER BY recipe. Default (sort is None,
    # e.g. legacy/mobile callers) keeps the original status-grouped order. Every
    # recipe ends with created_at DESC as a stable tiebreaker.
    order_by = {
        "newest": [Ticket.created_at.desc()],
        "oldest": [Ticket.created_at.asc()],
        "severity": [severity_rank.asc(), Ticket.created_at.desc()],
        "status": [status_rank.asc(), severity_rank.asc(), Ticket.created_at.desc()],
        "unassigned": [
            unassigned_rank.asc(),
            status_rank.asc(),
            severity_rank.asc(),
            Ticket.created_at.desc(),
        ],
    }.get(sort, [status_rank.asc(), severity_rank.asc(), Ticket.created_at.desc()])
    rows = (
        q.order_by(*order_by)
        .offset(offset)
        .limit(limit)
        .all()
    )
    return TicketListPage(
        items=[TicketListItem.model_validate(r) for r in rows],
        total=int(total),
        limit=limit,
        offset=offset,
    )


@router.get("/tickets/counts")
def ticket_status_counts(
    severity: Optional[str] = Query(default=None),
    product: Optional[str] = Query(default=None),
    search: Optional[str] = Query(default=None),
    created_within_days: Optional[int] = Query(default=None, ge=1, le=3650),
    assigned_engineer_id: Optional[int] = Query(default=None, ge=1),
    business_name: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Per-status ticket counts for the inbox filter pills.

    Deliberately ignores the `status` filter: the pills must show the count for
    every status regardless of which one is currently selected. All other
    filters (severity, product, search, date) are applied so the counts match
    the list the user is looking at.
    """
    q = db.query(Ticket.status, func.count(Ticket.id)).filter(Ticket.deleted_at.is_(None))
    # Engineers only see tickets assigned to them — as primary OR as an
    # additional engineer brought in for the same visit.
    if user.role == UserRole.ENGINEER.value:
        additional = db.query(TicketEngineer.ticket_id).filter(
            TicketEngineer.engineer_id == user.id
        )
        q = q.filter(
            or_(Ticket.assigned_engineer_id == user.id, Ticket.id.in_(additional))
        )
    elif user.role == UserRole.SALES.value:
        q = q.filter(
            or_(Ticket.raised_by_id == user.id, Ticket.assigned_engineer_id == user.id)
        )
    if severity:
        q = q.filter(Ticket.severity == severity.upper())
    if product:
        q = q.filter(Ticket.product_category == product)
    if assigned_engineer_id:
        q = q.filter(Ticket.assigned_engineer_id == assigned_engineer_id)
    if business_name:
        q = q.filter(Ticket.business_name == business_name)
    if created_within_days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=created_within_days)
        q = q.filter(Ticket.created_at >= cutoff)
    if search:
        like = f"%{search.strip()}%"
        like_upper = f"%{search.strip().upper()}%"
        q = q.filter(
            or_(
                Ticket.reference.ilike(like_upper),
                Ticket.business_name.ilike(like),
                Ticket.serial_number.ilike(like_upper),
            )
        )
    rows = q.group_by(Ticket.status).all()
    by_status = {status: int(count) for status, count in rows}
    return {"by_status": by_status, "total": sum(by_status.values())}


@router.get("/tickets/{reference}", response_model=TicketResponse)
def get_ticket(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference, user)
    return ticket


@router.get("/tickets/{reference}/events", response_model=List[TicketEventOut])
def get_ticket_events(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference, user)
    events = (
        db.query(TicketEvent)
        .filter(TicketEvent.ticket_id == ticket.id)
        .order_by(TicketEvent.created_at)
        .all()
    )
    return events


# --------------------------- actions ------------------------------------ #

@router.post("/tickets/{reference}/acknowledge", response_model=TicketResponse)
def acknowledge_ticket(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference, user)
    return acknowledge(db, ticket, user)


@router.post("/tickets/{reference}/assign", response_model=TicketResponse)
def assign_ticket(
    reference: str,
    body: AssignEngineerRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference, user)
    ticket, engineer = assign_engineer(db, ticket, user, body.engineer_id)
    # Notify engineer in the background so the API returns immediately.
    background.add_task(send_engineer_assignment, ticket, engineer, user)
    # Plus a WhatsApp ping to the engineer's phone via Twilio.
    background.add_task(
        send_engineer_assignment_alert, ticket.id, engineer.id, user.id
    )
    # Mobile push to the assigned engineer's device(s).
    background.add_task(notify_ticket_assigned, ticket.id, engineer.id)
    return ticket


@router.post("/tickets/{reference}/self-assign", response_model=TicketResponse)
def self_assign_ticket(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Admin/Manager assigns the ticket to themselves. Convenience wrapper
    around /assign with engineer_id = current_user.id."""
    if user.role not in ADMIN_MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="Only Manager or Admin can self-assign")
    ticket = _load_ticket(db, reference, user)
    ticket, _ = assign_engineer(db, ticket, user, user.id)
    return ticket


@router.post("/tickets/{reference}/engineers", response_model=TicketResponse)
def add_ticket_engineer(
    reference: str,
    body: AssignEngineerRequest,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Add an EXTRA engineer to attend the same visit. Admin/Manager/Owner only.

    The added engineer can view the ticket and is notified, but only the primary
    assignee drives the workflow. Reuses the assignment notification channels.
    """
    ticket = _load_ticket(db, reference, user)
    _, engineer = add_engineer(db, ticket, user, body.engineer_id)
    # Notify the added engineer just like a primary assignment.
    background.add_task(send_engineer_assignment, ticket, engineer, user)
    background.add_task(
        send_engineer_assignment_alert, ticket.id, engineer.id, user.id
    )
    background.add_task(notify_ticket_assigned, ticket.id, engineer.id)
    db.refresh(ticket)
    return ticket


@router.delete("/tickets/{reference}/engineers/{engineer_id}", response_model=TicketResponse)
def remove_ticket_engineer(
    reference: str,
    engineer_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Remove an additional engineer from the ticket. Admin/Manager/Owner only."""
    ticket = _load_ticket(db, reference, user)
    return remove_engineer(db, ticket, user, engineer_id)


@router.get("/tickets/{reference}/close-preview", response_model=ClosePreviewOut)
def close_preview(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.SUPER_ADMIN)),
):
    """Summary + 'what's still pending' shown before an Admin/Owner force-closes."""
    ticket = _load_ticket(db, reference, user)
    out = ClosePreviewOut.model_validate(ticket)
    out.pending = compute_close_pending(ticket)
    return out


@router.post("/tickets/{reference}/force-close", response_model=TicketResponse)
def force_close_ticket(
    reference: str,
    body: ForceCloseRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.SUPER_ADMIN)),
):
    """Admin/Owner override: close a ticket from ANY status (reason required)."""
    ticket = _load_ticket(db, reference, user)
    return force_close(db, ticket, user, body.reason)


@router.post("/tickets/{reference}/collect-payment", response_model=TicketResponse)
def collect_ticket_payment(
    reference: str,
    body: CollectPaymentRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Record the payment collected for an out-of-warranty ticket and close it
    if it's otherwise done. Admin/Manager or the assigned engineer."""
    ticket = _load_ticket(db, reference, user)
    return collect_payment(db, ticket, user, body.amount_collected_inr)


@router.delete("/tickets/{reference}", response_model=TicketResponse)
def delete_ticket(
    reference: str,
    body: Optional[DeleteTicketRequest] = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.SUPER_ADMIN)),
):
    """Admin/Owner override: soft-delete a ticket in ANY status. The row is
    retained (hidden everywhere) for audit/recovery."""
    ticket = _load_ticket(db, reference, user)
    return soft_delete_ticket(db, ticket, user, body.reason if body else None)


@router.patch("/tickets/{reference}/warranty", response_model=TicketResponse)
def patch_warranty(
    reference: str,
    body: UpdateWarrantyRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference, user)
    return update_warranty(db, ticket, user, body.warranty_status.upper())


@router.patch("/tickets/{reference}/severity", response_model=TicketResponse)
def patch_severity(
    reference: str,
    body: UpdateSeverityRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Admin/Manager-only. Set or change ticket severity."""
    ticket = _load_ticket(db, reference, user)
    return update_severity(db, ticket, user, body.severity.upper())


@router.patch("/tickets/{reference}/service-type", response_model=TicketResponse)
def patch_service_type(
    reference: str,
    body: UpdateServiceTypeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Admin/Manager or the assigned engineer can switch between SITE_VISIT and
    REMOTE_SUPPORT. Remote support skips signatures, PDF and spare parts."""
    ticket = _load_ticket(db, reference, user)
    return set_service_type(db, ticket, user, body.service_type.upper())


@router.patch("/tickets/{reference}/third-party-info", response_model=TicketResponse)
def patch_third_party_info(
    reference: str,
    body: UpdateThirdPartyInfoRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Admin/Manager or the assigned engineer set the third-party support
    details (device name / issue info / ticket reference). Editable until the
    ticket is closed."""
    ticket = _load_ticket(db, reference, user)
    return set_third_party_info(
        db, ticket, user,
        body.third_party_device_name,
        body.third_party_issue_info,
        body.third_party_ticket_ref,
    )


# --------------------------- engineer actions --------------------------- #

@router.post("/tickets/{reference}/accept", response_model=TicketResponse)
def accept_ticket(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Engineer claims an assigned ticket. ASSIGNED → ACCEPTED."""
    return accept(db, _load_ticket(db, reference, user), user)


@router.post("/tickets/{reference}/start-work", response_model=TicketResponse)
def start_work_ticket(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Engineer begins active work. ACCEPTED → RESOLVING."""
    return start_work(db, _load_ticket(db, reference, user), user)


@router.post("/tickets/{reference}/attempts", response_model=TicketResponse, status_code=201)
def start_ticket_attempt(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Engineer begins a new work attempt (auto-starts work from ACCEPTED)."""
    ticket = _load_ticket(db, reference, user)
    start_attempt(db, ticket, user)
    db.refresh(ticket)
    return ticket


@router.post("/tickets/{reference}/attempts/{attempt_id}/end", response_model=TicketResponse)
def end_ticket_attempt(
    reference: str,
    attempt_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Engineer ends the given open attempt."""
    ticket = _load_ticket(db, reference, user)
    end_attempt(db, ticket, user, attempt_id)
    db.refresh(ticket)
    return ticket


@router.post("/tickets/{reference}/resolve", response_model=TicketResponse)
def resolve_ticket(
    reference: str,
    body: ResolveRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Engineer marks the ticket fixed and writes a resolution summary.

    RESOLVING → RESOLVED. The engineer then collects both signatures (customer
    + their own) directly on this admin interface — no email link is sent.
    """
    ticket, _sign_url = resolve(db, _load_ticket(db, reference, user), user, body.summary)
    return ticket


@router.post("/tickets/{reference}/sign-customer", response_model=TicketResponse)
async def sign_as_customer_via_engineer(
    reference: str,
    signer_name: str = Form(..., min_length=2, max_length=120),
    signature: UploadFile = File(..., description="PNG of the customer's signature"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Engineer captures the customer's signature on their own device.

    Only the assigned engineer can submit. After this, the engineer can
    countersign via POST /sign-engineer to close the ticket.
    """
    ticket = _load_ticket(db, reference, user)
    image_bytes = await signature.read()
    record_customer_signature_via_engineer(
        db, ticket, user,
        signer_name=signer_name,
        image_bytes=image_bytes,
        content_type=signature.content_type or "image/png",
    )
    db.refresh(ticket)
    return ticket


@router.post("/tickets/{reference}/sign-engineer", response_model=TicketResponse)
async def sign_as_engineer(
    reference: str,
    signature: UploadFile = File(..., description="PNG of the engineer's signature"),
    photo: UploadFile | None = File(None, description="Optional photo of the customer"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Engineer signs the resolution document. Customer must have signed first.

    An optional customer photo, captured at this final sign-off step, is saved
    and embedded in the PDF. On success: PDF is generated, ticket transitions
    RESOLVED → CLOSED.
    """
    ticket = _load_ticket(db, reference, user)
    image_bytes = await signature.read()
    photo_bytes = await photo.read() if photo is not None else None
    record_engineer_signature(
        db, ticket, user, image_bytes,
        content_type=signature.content_type or "image/png",
        photo_bytes=photo_bytes,
        photo_content_type=photo.content_type if photo is not None else None,
    )
    db.refresh(ticket)
    return ticket


@router.post("/tickets/{reference}/field-sign-link")
def create_field_sign_link(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Generate the no-login link a sub-engineer uses to collect both the
    customer's and their own signature off-site.

    Available once the ticket is RESOLVED and has at least one sub-engineer.
    Idempotent — returns the existing link if already generated. Once
    generated, on-site signing in the admin app is locked for this ticket.
    """
    ticket = _load_ticket(db, reference, user)
    resolution, url = generate_field_sign_link(db, ticket, user)
    return {
        "url": url,
        "token": resolution.customer_sign_token,
        "generated_at": resolution.field_sign_link_generated_at,
    }


@router.get("/tickets/{reference}/pdf")
def get_resolution_pdf_url(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return a short-lived URL to the resolution PDF.

    Accessible to the Admin, a Manager, or the engineer the ticket is assigned
    to. We return JSON rather than a redirect because the frontend calls this
    with a JWT Authorization header — browser navigation via plain <a href>
    would drop that header and fail with 401. The frontend opens the returned
    URL in a new tab to download.
    """
    ticket = _load_ticket(db, reference, user)
    if (
        user.role not in ADMIN_MANAGER_ROLES
        and ticket.assigned_engineer_id != user.id
    ):
        raise HTTPException(
            status_code=403, detail="Manager, Admin, or the assigned engineer only"
        )
    res = ticket.resolution
    if res is None or not res.pdf_storage_key:
        raise HTTPException(
            status_code=404,
            detail="PDF not available yet — both signatures must be in place.",
        )
    url = get_storage().public_url(res.pdf_storage_key)
    if not url.startswith("http"):
        # Local mode returns "/uploads/..." — make it absolute.
        url = f"http://localhost:8000{url}"
    return {
        "url": url,
        "filename": f"resolution-{reference}.pdf",
        "generated_at": res.pdf_generated_at,
    }


@router.post("/tickets/{reference}/pdf/regenerate")
def regenerate_resolution_pdf(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Re-render the resolution PDF in place (Admin / Manager only).

    PDFs in storage are snapshots — once written at close-time they don't
    refresh when the template changes. This endpoint re-runs the generator
    against the current ticket data and overwrites the stored file, so any
    template / branding tweaks become visible on already-closed tickets
    without losing their signatures or close history.

    Preserves the original storage key (so existing references to the file
    remain valid) and bumps `pdf_generated_at` to the current time.
    """
    from datetime import datetime, timezone  # local — module-level import already covers timedelta

    from ..services.pdf_generator import generate_resolution_pdf  # avoid module-load cost when unused

    if user.role not in ADMIN_MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="Manager or Admin only")
    ticket = _load_ticket(db, reference, user)
    res = ticket.resolution
    # Need both signatures so the regenerated PDF carries the same legal
    # weight as the original; we never want a "draft-looking" PDF replacing
    # a fully-signed one.
    if res is None or res.customer_signed_at is None or res.engineer_signed_at is None:
        raise HTTPException(
            status_code=409,
            detail="Can only regenerate after both signatures are in place.",
        )

    pdf_bytes = generate_resolution_pdf(ticket, res)
    storage = get_storage()
    filename = f"resolution-{ticket.reference}.pdf"
    meta = storage.save_bytes(pdf_bytes, "application/pdf", ticket.reference, filename)
    res.pdf_storage_key = meta["storage_url"]
    res.pdf_generated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(res)
    logger.info("Regenerated PDF for %s by %s", ticket.reference, user.username)

    url = storage.public_url(res.pdf_storage_key)
    if not url.startswith("http"):
        url = f"http://localhost:8000{url}"
    return {
        "url": url,
        "filename": filename,
        "generated_at": res.pdf_generated_at,
    }


# --------------------------- work notes --------------------------------- #

@router.get("/tickets/{reference}/notes", response_model=List[WorkNoteOut])
def list_notes(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference, user)
    return (
        db.query(WorkNote)
        .filter(WorkNote.ticket_id == ticket.id)
        .order_by(WorkNote.created_at)
        .all()
    )


@router.post("/tickets/{reference}/notes", response_model=WorkNoteOut, status_code=201)
async def add_note(
    reference: str,
    body: str = Form(..., min_length=2, max_length=4000),
    images: List[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Add a work note. Supports optional worksite images (multipart form).

    `images` may be omitted entirely. Each file is validated by storage and
    persisted before the note row is committed.
    """
    ticket = _load_ticket(db, reference, user)
    saved: list[dict] = []
    if images:
        from ..services.storage import save_uploads  # noqa: WPS433
        # Reject obvious non-images early — keep worksite uploads to photos.
        for f in images:
            ct = (f.content_type or "").lower()
            if ct and not ct.startswith("image/"):
                raise HTTPException(
                    status_code=422,
                    detail=f"Only image files allowed (got {ct} for {f.filename})",
                )
        saved = save_uploads(images, ticket.reference)
    return add_work_note(db, ticket, user, body, attachments=saved)


# --------------------------- sub-engineers ------------------------------ #

def _can_manage_sub_engineers(ticket: Ticket, user: User) -> bool:
    """Admin, Manager, or the current assignee can manage sub-engineers."""
    if user.role in ADMIN_MANAGER_ROLES:
        return True
    return ticket.assigned_engineer_id == user.id


def _ensure_roster_entry(
    db: Session, *, name: str, phone: str, district: str, actor: User
) -> SubEngineerRoster:
    """Return the matching active roster contact, creating one if absent.

    Called when a brand-new sub-engineer is added to a ticket, so the contact
    becomes reusable from the district roster. Matched case-insensitively by
    name + district."""
    existing = (
        db.query(SubEngineerRoster)
        .filter(
            func.lower(SubEngineerRoster.name) == name.lower(),
            func.lower(SubEngineerRoster.district) == district.lower(),
            SubEngineerRoster.active.is_(True),
        )
        .first()
    )
    if existing is not None:
        return existing
    entry = SubEngineerRoster(
        name=name, phone=phone, district=district, created_by_user_id=actor.id
    )
    db.add(entry)
    db.flush()
    return entry


@router.get("/business-name-suggestions", response_model=List[BusinessNameSuggestion])
def list_business_name_suggestions(
    q: str = Query(..., min_length=2, max_length=120),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Distinct business names starting with `q`, drawn from past tickets and
    installations — feeds the business-name autocomplete on staff forms so
    the same customer keeps one spelling across their history.

    Each hit carries the category last recorded for that name (ticket
    `business_type` or installation `business_category`, whichever is more
    recent) so the form can pre-fill it when the name is picked.
    """
    like = f"{q.strip()}%"
    limit = 10
    # Prefix + min-2-char filter keeps the candidate set tiny; pulling a few
    # dozen recent rows per table is plenty to find the newest category per name.
    fetch_cap = 40

    def recent_rows(model, name_col, category_col):
        return (
            db.query(name_col, category_col, model.created_at)
            .filter(
                name_col.ilike(like),
                # Sample/test rows are prefixed TEST_ — keep them out of
                # suggestions so staff never reuse them for real customers.
                ~name_col.ilike(r"TEST\_%", escape="\\"),
            )
            .order_by(model.created_at.desc())
            .limit(fetch_cap)
            .all()
        )

    rows = recent_rows(Ticket, Ticket.business_name, Ticket.business_type) + recent_rows(
        Installation, Installation.business_name, Installation.business_category
    )
    # Most recent first so the first time we see a name we take its latest
    # category. Convert to epoch to avoid naive/aware datetime comparisons.
    rows.sort(key=lambda r: r[2].timestamp() if r[2] else 0.0, reverse=True)

    out: List[BusinessNameSuggestion] = []
    seen: set[str] = set()
    for name, category, _ in rows:
        clean = (name or "").strip()
        key = clean.lower()
        if not clean or key in seen:
            continue
        seen.add(key)
        out.append(
            BusinessNameSuggestion(
                business_name=clean, business_type=(category or "").strip()
            )
        )
    out.sort(key=lambda s: s.business_name.lower())
    return out[:limit]


@router.get("/tickets/{reference}/sub-engineers", response_model=List[SubEngineerOut])
def list_sub_engineers(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference, user)
    return (
        db.query(SubEngineer)
        .filter(SubEngineer.ticket_id == ticket.id)
        .order_by(SubEngineer.created_at)
        .all()
    )


@router.get(
    "/tickets/{reference}/sub-engineer-suggestions",
    response_model=List[SubEngineerRosterOut],
)
def list_sub_engineer_suggestions(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Roster contacts for this ticket's district — feeds the add dropdown.

    Returns active roster entries whose district matches the ticket's city,
    minus contacts already on this ticket so the dropdown only offers new
    additions.
    """
    ticket = _load_ticket(db, reference, user)
    city_key = ticket.city.strip()
    if not city_key:
        return []

    rows = (
        db.query(SubEngineerRoster)
        .filter(
            SubEngineerRoster.active.is_(True),
            func.lower(SubEngineerRoster.district) == city_key.lower(),
        )
        .order_by(SubEngineerRoster.name)
        .all()
    )
    already_on_ticket = {
        (s.name.strip().lower(), s.phone.strip())
        for s in ticket.sub_engineers
    }
    return [
        r for r in rows
        if (r.name.strip().lower(), r.phone.strip()) not in already_on_ticket
    ]


@router.post("/tickets/{reference}/sub-engineers", response_model=SubEngineerOut, status_code=201)
def add_sub_engineer(
    reference: str,
    body: AddSubEngineerRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Add a field contractor to the ticket.

    Either pick an existing roster contact (`roster_id`) or supply a new
    contact (`name` + `phone` + `location`) — a new contact is also recorded
    in the district roster so it's reusable later.

    Available after the ticket has been ACKNOWLEDGED. Caller must be the
    current assignee, Admin, or Manager.
    """
    ticket = _load_ticket(db, reference, user)
    if ticket.status in ("OPEN",):
        raise HTTPException(
            status_code=409,
            detail="Sub-engineers can be added after the ticket is acknowledged.",
        )
    if not _can_manage_sub_engineers(ticket, user):
        raise HTTPException(
            status_code=403,
            detail="Only the assignee, Admin, or Manager can add sub-engineers",
        )

    if body.roster_id is not None:
        entry = (
            db.query(SubEngineerRoster)
            .filter(
                SubEngineerRoster.id == body.roster_id,
                SubEngineerRoster.active.is_(True),
            )
            .one_or_none()
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="Roster contact not found")
        name, phone, location = entry.name, entry.phone, entry.district
    else:
        if not (body.name and body.phone and body.location):
            raise HTTPException(
                status_code=422,
                detail="Provide roster_id, or name + phone + location for a new contact.",
            )
        name = body.name.strip()
        phone = body.phone.strip()
        location = body.location.strip()
        # A brand-new contact also joins the district roster for future reuse.
        _ensure_roster_entry(db, name=name, phone=phone, district=location, actor=user)

    sub = SubEngineer(
        ticket_id=ticket.id,
        name=name,
        phone=phone,
        location=location,
        created_by_user_id=user.id,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    logger.info("Added sub-engineer %s to %s by %s", sub.name, ticket.reference, user.username)
    return sub


@router.delete("/tickets/{reference}/sub-engineers/{sub_id}", status_code=204)
def remove_sub_engineer(
    reference: str,
    sub_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference, user)
    if not _can_manage_sub_engineers(ticket, user):
        raise HTTPException(status_code=403, detail="Not allowed to remove sub-engineers on this ticket")
    sub = (
        db.query(SubEngineer)
        .filter(SubEngineer.id == sub_id, SubEngineer.ticket_id == ticket.id)
        .one_or_none()
    )
    if sub is None:
        raise HTTPException(status_code=404, detail="Sub-engineer not found")
    db.delete(sub)
    db.commit()
    return None


@router.patch(
    "/tickets/{reference}/sub-engineers/{sub_id}",
    response_model=SubEngineerOut,
)
def update_sub_engineer_fee(
    reference: str,
    sub_id: int,
    body: UpdateSubEngineerFeeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Record the fee paid to an outsourced sub-engineer for this ticket.

    Internal cost only — it does not affect the customer invoice or the
    resolution PDF. Editable by the assignee, Admin, or Manager at any status.
    """
    ticket = _load_ticket(db, reference, user)
    if not _can_manage_sub_engineers(ticket, user):
        raise HTTPException(
            status_code=403,
            detail="Only the assignee, Admin, or Manager can set sub-engineer fees",
        )
    sub = (
        db.query(SubEngineer)
        .filter(SubEngineer.id == sub_id, SubEngineer.ticket_id == ticket.id)
        .one_or_none()
    )
    if sub is None:
        raise HTTPException(status_code=404, detail="Sub-engineer not found")
    sub.fee_inr = int(body.fee_inr)
    db.commit()
    db.refresh(sub)
    logger.info(
        "Sub-engineer %s fee set to INR %d on %s by %s",
        sub.name, sub.fee_inr, ticket.reference, user.username,
    )
    return sub


# --------------------------- sub-engineer roster ----------------------- #

@router.get("/sub-engineer-roster", response_model=List[SubEngineerRosterOut])
def list_sub_engineer_roster(
    district: Optional[str] = Query(default=None),
    include_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """The district roster of field contractors. Any staff can read it."""
    q = db.query(SubEngineerRoster)
    if not include_inactive:
        q = q.filter(SubEngineerRoster.active.is_(True))
    if district:
        q = q.filter(func.lower(SubEngineerRoster.district) == district.strip().lower())
    return q.order_by(SubEngineerRoster.district, SubEngineerRoster.name).all()


@router.post("/sub-engineer-roster", response_model=SubEngineerRosterOut, status_code=201)
def create_sub_engineer_roster_entry(
    body: CreateRosterSubEngineerRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.MANAGER)),
):
    """Add a contact to the district roster. Admin / Manager only."""
    name = body.name.strip()
    district = body.district.strip()
    existing = (
        db.query(SubEngineerRoster)
        .filter(
            func.lower(SubEngineerRoster.name) == name.lower(),
            func.lower(SubEngineerRoster.district) == district.lower(),
            SubEngineerRoster.active.is_(True),
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="A roster contact with that name already exists in this district.",
        )
    entry = SubEngineerRoster(
        name=name,
        phone=body.phone.strip(),
        district=district,
        created_by_user_id=user.id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    logger.info("Roster contact %s (%s) added by %s", entry.name, entry.district, user.username)
    return entry


@router.patch("/sub-engineer-roster/{entry_id}", response_model=SubEngineerRosterOut)
def update_sub_engineer_roster_entry(
    entry_id: int,
    body: UpdateRosterSubEngineerRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_role(UserRole.ADMIN, UserRole.MANAGER)),
):
    """Edit or deactivate a roster contact. Admin / Manager only.

    Deactivating just hides the contact from the add dropdown — sub-engineers
    already recorded on tickets are untouched.
    """
    entry = (
        db.query(SubEngineerRoster).filter(SubEngineerRoster.id == entry_id).one_or_none()
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="Roster contact not found")
    if body.name is not None:
        entry.name = body.name.strip()
    if body.phone is not None:
        entry.phone = body.phone.strip()
    if body.district is not None:
        entry.district = body.district.strip()
    if body.active is not None:
        entry.active = body.active
    db.commit()
    db.refresh(entry)
    return entry


# --------------------------- spare parts / charges ---------------------- #

def _can_manage_spares(ticket: Ticket, user: User) -> bool:
    """Spares + service fee are editable only while the ticket is RESOLVING.

    Once the engineer marks it RESOLVED the invoice freezes — the figures
    flow into the resolution PDF and the customer's signature attests to
    them, so post-hoc edits (even by Admin/Manager) would break that audit
    chain.
    """
    if ticket.status != "RESOLVING":
        return False
    if user.role in ADMIN_MANAGER_ROLES:
        return True
    return ticket.assigned_engineer_id == user.id


def _can_manage_charges(ticket: Ticket, user: User) -> bool:
    """Who may touch the invoice (spares + service fee) right now.

    A Super Admin is exempt from the freeze above: they may correct the
    figures any time before the ticket is CLOSED, including after RESOLVED
    when the customer has already signed. Regenerate the resolution PDF
    afterwards so the signed document matches the corrected invoice.
    """
    if user.role in SUPER_ADMIN_ROLES:
        return ticket.status != "CLOSED"
    return _can_manage_spares(ticket, user)


@router.get("/spare-catalog", response_model=List[SpareCatalogItem])
def list_spare_catalog(
    product: Optional[str] = Query(default=None, description="Filter by product category"),
    db: Session = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Available spare parts. Optionally filter by product category."""
    q = db.query(SpareCatalog).filter(SpareCatalog.active.is_(True))
    if product:
        q = q.filter(SpareCatalog.product_category == product)
    return q.order_by(SpareCatalog.product_category, SpareCatalog.name).all()


@router.get("/tickets/{reference}/charges", response_model=ChargesSummary)
def get_ticket_charges(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Spares + service fee + computed totals for this ticket."""
    ticket = _load_ticket(db, reference, user)
    return compute_charges(ticket)


@router.post(
    "/tickets/{reference}/spares",
    response_model=TicketSpareOut,
    status_code=201,
)
def add_ticket_spare(
    reference: str,
    body: AddTicketSpareRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Add a spare line item.

    Either pass `catalog_id` to pick from the seeded catalog, or pass
    `name` + `unit_price_inr` for an ad-hoc part. Only the assignee may add
    while the ticket is RESOLVING. Admin/Manager may add at any time.
    """
    ticket = _load_ticket(db, reference, user)
    if ticket.service_type == ServiceType.REMOTE_SUPPORT.value:
        raise HTTPException(
            status_code=409,
            detail="Spare parts don't apply to remote-support tickets.",
        )
    if not _can_manage_charges(ticket, user):
        if ticket.status not in ("RESOLVING",):
            raise HTTPException(
                status_code=409,
                detail=f"Spares are locked once the ticket is {ticket.status.lower()}.",
            )
        raise HTTPException(
            status_code=403,
            detail="Only the assigned engineer (or Admin/Manager) can edit spares.",
        )

    name: str
    unit_price: int
    catalog_id: Optional[int] = None

    if body.catalog_id is not None:
        item = db.query(SpareCatalog).filter(SpareCatalog.id == body.catalog_id).one_or_none()
        if item is None or not item.active:
            raise HTTPException(status_code=404, detail="Spare catalog item not found")
        catalog_id = item.id
        name = item.name
        # Engineer can override the catalog price at add-time; otherwise use the default.
        unit_price = (
            body.unit_price_inr if body.unit_price_inr is not None else item.default_price_inr
        )
    else:
        if not body.name or body.unit_price_inr is None:
            raise HTTPException(
                status_code=422,
                detail="Provide catalog_id, or both name and unit_price_inr for an ad-hoc part.",
            )
        name = body.name.strip()
        unit_price = body.unit_price_inr

    spare = TicketSpare(
        ticket_id=ticket.id,
        catalog_id=catalog_id,
        name=name,
        unit_price_inr=int(unit_price),
        quantity=body.quantity,
        created_by_user_id=user.id,
    )
    db.add(spare)
    db.commit()
    db.refresh(spare)
    logger.info(
        "Added spare '%s' x%d to %s by %s",
        spare.name, spare.quantity, ticket.reference, user.username,
    )
    return spare


@router.patch("/tickets/{reference}/spares/{spare_id}", response_model=TicketSpareOut)
def update_ticket_spare(
    reference: str,
    spare_id: int,
    body: UpdateTicketSpareRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Edit price or quantity of an existing spare line item."""
    ticket = _load_ticket(db, reference, user)
    if not _can_manage_charges(ticket, user):
        raise HTTPException(status_code=403, detail="Not allowed to edit spares on this ticket")
    spare = (
        db.query(TicketSpare)
        .filter(TicketSpare.id == spare_id, TicketSpare.ticket_id == ticket.id)
        .one_or_none()
    )
    if spare is None:
        raise HTTPException(status_code=404, detail="Spare not found")
    if body.unit_price_inr is not None:
        spare.unit_price_inr = int(body.unit_price_inr)
    if body.quantity is not None:
        spare.quantity = int(body.quantity)
    db.commit()
    db.refresh(spare)
    return spare


@router.delete("/tickets/{reference}/spares/{spare_id}", status_code=204)
def remove_ticket_spare(
    reference: str,
    spare_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference, user)
    if not _can_manage_charges(ticket, user):
        raise HTTPException(status_code=403, detail="Not allowed to edit spares on this ticket")
    spare = (
        db.query(TicketSpare)
        .filter(TicketSpare.id == spare_id, TicketSpare.ticket_id == ticket.id)
        .one_or_none()
    )
    if spare is None:
        raise HTTPException(status_code=404, detail="Spare not found")
    db.delete(spare)
    db.commit()
    return None


@router.patch("/tickets/{reference}/service-fee", response_model=ChargesSummary)
def update_service_fee(
    reference: str,
    body: UpdateServiceFeeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference, user)
    new_fee = int(body.service_fee_inr)
    is_super_admin = user.role in SUPER_ADMIN_ROLES
    if not _can_manage_charges(ticket, user):
        if is_super_admin:
            raise HTTPException(
                status_code=403,
                detail="Ticket is closed — charges can no longer be edited.",
            )
        raise HTTPException(status_code=403, detail="Not allowed to edit charges on this ticket")
    # Out-of-warranty tickets carry a per-service-type minimum charge. Anyone
    # who can edit charges may set it at/above that floor; only a Super Admin
    # may go below it (a waiver/discount).
    min_fee = oow_min_service_fee_inr(ticket)
    if new_fee < min_fee and not is_super_admin:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Service charge can't be below ₹{min_fee} for this ticket. "
                "Only a Super Admin can set a lower amount."
            ),
        )
    ticket.service_fee_inr = new_fee
    db.commit()
    db.refresh(ticket)
    return compute_charges(ticket)


# --------------------------- spare-parts shipments ---------------------- #

def _can_ship_parts(ticket: Ticket, user: User) -> bool:
    """Admin, Manager, or the current assignee can record shipments.

    Allowed at any status except CLOSED — once the ticket is closed the
    work is over and logging in-transit parts retroactively is noise.
    """
    if ticket.status == "CLOSED":
        return False
    if user.role in ADMIN_MANAGER_ROLES:
        return True
    return ticket.assigned_engineer_id == user.id


@router.get("/tickets/{reference}/shipments", response_model=List[ShipmentOut])
def list_shipments(
    reference: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ticket = _load_ticket(db, reference, user)
    return (
        db.query(TicketShipment)
        .filter(TicketShipment.ticket_id == ticket.id)
        .order_by(TicketShipment.departed_at.desc())
        .all()
    )


@router.post("/tickets/{reference}/shipments", response_model=ShipmentOut, status_code=201)
def create_shipment(
    reference: str,
    body: ShipmentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Record a courier shipment of spare parts for this ticket.

    Items can either reference a `SpareCatalog.id` (preferred) or carry a
    free-form `name`. Tracking ID is optional — some couriers / own-vehicle
    dispatches don't generate one. Departure timestamp defaults to now.
    """
    ticket = _load_ticket(db, reference, user)
    if ticket.service_type == ServiceType.REMOTE_SUPPORT.value:
        raise HTTPException(
            status_code=409,
            detail="Spare-parts shipments don't apply to remote-support tickets.",
        )
    if not _can_ship_parts(ticket, user):
        if ticket.status == "CLOSED":
            raise HTTPException(
                status_code=409,
                detail="Ticket is closed — shipments can no longer be added.",
            )
        raise HTTPException(
            status_code=403,
            detail="Only the assignee, Admin, or Manager can record shipments.",
        )

    departed = body.departed_at or datetime.now(timezone.utc)
    shipment = TicketShipment(
        ticket_id=ticket.id,
        courier_name=body.courier_name.strip(),
        tracking_id=(body.tracking_id or "").strip() or None,
        departed_at=departed,
        created_by_user_id=user.id,
    )
    db.add(shipment)
    db.flush()  # so shipment.id is available below

    # Resolve each item: prefer catalog_id, fall back to free-form name.
    for item in body.items:
        catalog_id: Optional[int] = None
        name: str
        if item.catalog_id is not None:
            cat = db.query(SpareCatalog).filter(SpareCatalog.id == item.catalog_id).one_or_none()
            if cat is None or not cat.active:
                raise HTTPException(
                    status_code=404,
                    detail=f"Spare catalog item {item.catalog_id} not found",
                )
            catalog_id = cat.id
            name = cat.name
        else:
            if not item.name or not item.name.strip():
                raise HTTPException(
                    status_code=422,
                    detail="Each shipment item needs either catalog_id or a name.",
                )
            name = item.name.strip()
        db.add(
            TicketShipmentItem(
                shipment_id=shipment.id,
                catalog_id=catalog_id,
                name=name,
                quantity=int(item.quantity),
            )
        )

    # Audit trail on the ticket timeline so it shows up next to acks/assigns.
    db.add(
        TicketEvent(
            ticket_id=ticket.id,
            actor_user_id=user.id,
            event_type="PARTS_SHIPPED",
            payload={
                "courier": shipment.courier_name,
                "tracking_id": shipment.tracking_id,
                "item_count": len(body.items),
            },
        )
    )
    db.commit()
    db.refresh(shipment)
    logger.info(
        "Shipment created for %s by %s (%s, %d items)",
        ticket.reference, user.username, shipment.courier_name, len(body.items),
    )
    return shipment


@router.post(
    "/tickets/{reference}/shipments/{shipment_id}/deliver",
    response_model=ShipmentOut,
)
def mark_shipment_delivered(
    reference: str,
    shipment_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Close the shipment — i.e. the parts have arrived at the worksite.

    Idempotent: hitting it twice returns the already-delivered shipment
    rather than erroring (avoids confusing double-clicks).
    """
    ticket = _load_ticket(db, reference, user)
    if not _can_ship_parts(ticket, user):
        if ticket.status == "CLOSED":
            raise HTTPException(
                status_code=409,
                detail="Ticket is closed — shipment status can no longer change.",
            )
        raise HTTPException(
            status_code=403,
            detail="Only the assignee, Admin, or Manager can update shipments.",
        )
    shipment = (
        db.query(TicketShipment)
        .filter(
            TicketShipment.id == shipment_id,
            TicketShipment.ticket_id == ticket.id,
        )
        .one_or_none()
    )
    if shipment is None:
        raise HTTPException(status_code=404, detail="Shipment not found")
    if shipment.delivered_at is not None:
        return shipment  # already delivered — no-op

    shipment.delivered_at = datetime.now(timezone.utc)
    db.add(
        TicketEvent(
            ticket_id=ticket.id,
            actor_user_id=user.id,
            event_type="PARTS_DELIVERED",
            payload={
                "shipment_id": shipment.id,
                "courier": shipment.courier_name,
                "tracking_id": shipment.tracking_id,
            },
        )
    )
    db.commit()
    db.refresh(shipment)
    logger.info(
        "Shipment %d on %s marked delivered by %s",
        shipment.id, ticket.reference, user.username,
    )
    return shipment


# --------------------------- helpers ------------------------------------ #

def _load_ticket(
    db: Session, reference: str, user: Optional[User] = None
) -> Ticket:
    """Load a ticket by reference, applying role-based access control.

    ADMIN and MANAGER can read any ticket. ENGINEER can only read tickets
    assigned to them (`ticket.assigned_engineer_id == user.id`) or that they
    raised themselves (`ticket.raised_by_id == user.id`) — the latter so an
    engineer who opens a ticket can land on its detail page before an
    Admin/Manager assigns it. When the user is not authorised, we return 404
    rather than 403 — leaking "this ticket exists but you can't see it" to
    engineers would let a curious one enumerate the reference space.

    Read-only: this only governs visibility. Every state-changing action
    (acknowledge / assign / accept / resolve / warranty / …) independently
    enforces role or assignee in the workflow service, so being able to see a
    self-raised ticket does not let an engineer act on it.

    `user` is optional only for legacy callers; all admin endpoints should
    pass the authenticated `Depends(get_current_user)` value so the scope
    check fires.
    """
    t = (
        db.query(Ticket)
        .filter(
            Ticket.reference == reference.strip().upper(),
            Ticket.deleted_at.is_(None),  # soft-deleted tickets are invisible
        )
        .one_or_none()
    )
    if t is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    if (
        user is not None
        and user.role == UserRole.ENGINEER.value
        and t.assigned_engineer_id != user.id
        and t.raised_by_id != user.id
        # …or they were added as an additional engineer for the same visit.
        and not any(ae.engineer_id == user.id for ae in t.additional_engineers)
    ):
        raise HTTPException(status_code=404, detail="Ticket not found")
    # Sales reps can open tickets they raised themselves or that are assigned to
    # them (a service call can now be assigned to a sales rep).
    if (
        user is not None
        and user.role == UserRole.SALES.value
        and t.raised_by_id != user.id
        and t.assigned_engineer_id != user.id
    ):
        raise HTTPException(status_code=404, detail="Ticket not found")
    return t
