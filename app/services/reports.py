"""Reports service — date-ranged ticket report with SLA escalation analysis.

Powers the Admin "Reports" dashboard. Returns one row per ticket in the
selected created-date range, each annotated with any SLA breaches across the
six workflow transitions. The frontend toggles between a plain date-range view
and an "escalation matrix" view that sorts breached tickets to the top.

SLA stages (thresholds in minutes; Site / Remote):
  1 Open        -> Acknowledge   15 / 15
  2 Acknowledge -> Assign        10 / 10
  3 Assign      -> Accept        15 / 15
  4 Accept      -> Start work    90 / 30
  5 Start work  -> Resolved      90 / 30
  6 Resolved    -> Close         30 / N-A   (remote auto-closes on resolve)

A transition that hasn't completed yet is measured against *now* (a live,
ongoing breach). Thresholds live in STAGE_DEFS so they're easy to tune.

Time a ticket spent on hold is excluded from every stage it overlaps — the
wait was a deliberate management decision, not an SLA failure — so a parked
ticket doesn't accumulate an unbounded breach against its engineer. The hold
windows are reconstructed from the HELD / RESUMED events, which handles a
ticket being held and resumed repeatedly.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session, selectinload

from ..models.ticket import ServiceType, Ticket, TicketStatus

# India Standard Time — fixed +05:30 (no DST), so a constant offset is safe and
# avoids a tzdata dependency. The date-range endpoints are interpreted in IST.
IST = timezone(timedelta(hours=5, minutes=30))

# stage, label, start-column, end-column, site-threshold, remote-threshold (min).
# A None remote threshold means the stage does not apply to remote tickets.
STAGE_DEFS = [
    {"stage": 1, "label": "Open → Acknowledge", "start": "created_at", "end": "acknowledged_at", "site": 15, "remote": 15},
    {"stage": 2, "label": "Acknowledge → Assign", "start": "acknowledged_at", "end": "assigned_at", "site": 10, "remote": 10},
    {"stage": 3, "label": "Assign → Accept", "start": "assigned_at", "end": "accepted_at", "site": 15, "remote": 15},
    {"stage": 4, "label": "Accept → Start work", "start": "accepted_at", "end": "resolving_started_at", "site": 90, "remote": 30},
    {"stage": 5, "label": "Start work → Resolved", "start": "resolving_started_at", "end": "resolved_at", "site": 90, "remote": 30},
    {"stage": 6, "label": "Resolved → Close", "start": "resolved_at", "end": "closed_at", "site": 30, "remote": None},
]


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    """Treat naive datetimes (some SQLite rows) as UTC; pass None through."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _person(u) -> Dict[str, Optional[str]]:
    return {"name": u.name, "role": u.role} if u is not None else {"name": None, "role": None}


def _closed_at(t: Ticket) -> Optional[datetime]:
    """Best available close timestamp — Ticket has no closed_at column.

    Remote tickets resolve-and-close in one step, so resolved_at is the close.
    Site tickets close when the PDF is stamped (resolution.pdf_generated_at).
    Force-closed tickets fall back to the CLOSED/FORCE_CLOSED audit event.
    """
    if t.status != TicketStatus.CLOSED.value:
        return None
    if t.service_type == ServiceType.REMOTE_SUPPORT.value:
        return _aware(t.resolved_at)
    res = t.resolution
    if res is not None and res.pdf_generated_at is not None:
        return _aware(res.pdf_generated_at)
    closed_events = [
        e for e in t.events
        if e.to_status == TicketStatus.CLOSED.value
        or e.event_type in ("CLOSED", "FORCE_CLOSED")
    ]
    if closed_events:
        return max(_aware(e.created_at) for e in closed_events)
    return None


def _force_closer(t: Ticket):
    """The admin who force-closed the ticket, if any (for stage-6 attribution)."""
    fc = [e for e in t.events if e.event_type == "FORCE_CLOSED"]
    return fc[-1].actor if fc else None


def _responsible(stage: int, t: Ticket, pending: bool):
    """Who is accountable for a given stage's breach.

    Stages 3-6 sit with the assigned engineer. Stage 1/2 sit with the
    acknowledger / assigner; while still pending, stage 1 has no owner yet
    (triage queue) and stage 2 falls to whoever acknowledged it. A force-closed
    stage 6 is attributed to the admin who closed it.
    """
    if stage == 1:
        return None if pending else t.acknowledged_by
    if stage == 2:
        return t.acknowledged_by if pending else t.assigned_by
    if stage == 6 and not pending:
        return _force_closer(t) or t.assigned_engineer
    return t.assigned_engineer


def _hold_spans(t: Ticket, now: datetime) -> List[tuple]:
    """Every (start, end) window during which the ticket was on hold.

    Reconstructed from the HELD / RESUMED event pairs rather than the
    `held_at` column, because a ticket can be held and resumed any number of
    times and only the log remembers the earlier cycles. A HELD with no
    matching RESUMED is still open, so it runs to `now`.
    """
    spans: List[tuple] = []
    held_from: Optional[datetime] = None
    for e in sorted(t.events, key=lambda ev: _aware(ev.created_at) or now):
        if e.event_type == "HELD":
            if held_from is None:
                held_from = _aware(e.created_at)
        elif e.event_type == "RESUMED" and held_from is not None:
            resumed_at = _aware(e.created_at)
            if resumed_at is not None and resumed_at > held_from:
                spans.append((held_from, resumed_at))
            held_from = None
    if held_from is not None:
        spans.append((held_from, now))
    return spans


def _held_minutes(spans: List[tuple], start: datetime, end: datetime) -> float:
    """How much of [start, end] the ticket spent on hold — the overlap of the
    stage window with each hold span, summed."""
    total = 0.0
    for hold_start, hold_end in spans:
        overlap_start = max(hold_start, start)
        overlap_end = min(hold_end, end)
        if overlap_end > overlap_start:
            total += (overlap_end - overlap_start).total_seconds() / 60.0
    return total


def _ticket_breaches(t: Ticket, closed_at: Optional[datetime], now: datetime) -> List[Dict[str, Any]]:
    is_remote = t.service_type == ServiceType.REMOTE_SUPPORT.value
    timestamps = {
        "created_at": _aware(t.created_at),
        "acknowledged_at": _aware(t.acknowledged_at),
        "assigned_at": _aware(t.assigned_at),
        "accepted_at": _aware(t.accepted_at),
        "resolving_started_at": _aware(t.resolving_started_at),
        "resolved_at": _aware(t.resolved_at),
        "closed_at": closed_at,
    }
    # Time spent on hold is nobody's SLA failure, so it's subtracted from every
    # stage it overlaps. Without this a ticket parked for a fortnight would
    # show a permanent, unbounded breach against its engineer.
    hold_spans = _hold_spans(t, now)
    breaches: List[Dict[str, Any]] = []
    for d in STAGE_DEFS:
        threshold = d["remote"] if is_remote else d["site"]
        if threshold is None:
            continue  # stage N/A for this service type (e.g. remote stage 6)
        start = timestamps[d["start"]]
        if start is None:
            continue  # ticket hasn't reached the start of this transition yet
        end = timestamps[d["end"]]
        if end is not None:
            window_end = end
            pending = False
        else:
            window_end = now
            pending = True
        elapsed = (window_end - start).total_seconds() / 60.0
        held = _held_minutes(hold_spans, start, window_end)
        elapsed = max(0.0, elapsed - held)
        if elapsed <= threshold:
            continue
        owner = _responsible(d["stage"], t, pending)
        person = _person(owner)
        breaches.append({
            "stage": d["stage"],
            "label": d["label"],
            "threshold_min": threshold,
            "elapsed_min": round(elapsed, 1),
            "overage_min": round(elapsed - threshold, 1),
            # How much wall-clock time in this stage was excluded as hold time.
            # Shown in the UI so a low elapsed on an old ticket makes sense.
            "held_min": round(held, 1),
            "pending": pending,
            "responsible_name": person["name"],
            "responsible_role": person["role"],
        })
    return breaches


def _iso(dt: Optional[datetime]) -> Optional[str]:
    aware = _aware(dt)
    return aware.isoformat() if aware else None


def _row(t: Ticket, now: datetime) -> Dict[str, Any]:
    closed_at = _closed_at(t)
    breaches = _ticket_breaches(t, closed_at, now)
    earliest = min((b["stage"] for b in breaches), default=None)
    # Secondary sort key: how far over threshold the earliest breached stage ran.
    primary_overage = next(
        (b["overage_min"] for b in breaches if b["stage"] == earliest), None
    )
    resolution_hours = None
    start = _aware(t.resolving_started_at)
    resolved = _aware(t.resolved_at)
    if start and resolved:
        resolution_hours = round((resolved - start).total_seconds() / 3600.0, 2)

    return {
        "reference": t.reference,
        "status": t.status,
        "service_type": t.service_type,
        "business_name": t.business_name,
        "contact_name": t.contact_name,
        "phone": t.phone,
        "city": t.city,
        "state": t.state,
        "pincode": t.pincode,
        "product_category": t.product_category,
        "serial_number": t.serial_number,
        "issue_category": t.issue_category,
        "severity": t.severity,
        "warranty_status": t.warranty_status,
        "assigned_engineer": t.assigned_engineer.name if t.assigned_engineer else None,
        # Currently parked. Breach times below already have hold time removed,
        # so the flag explains why an old ticket can still read as compliant.
        "on_hold": t.held_at is not None,
        "hold_reason": t.hold_reason,
        "created_at": _iso(t.created_at),
        "acknowledged_at": _iso(t.acknowledged_at),
        "assigned_at": _iso(t.assigned_at),
        "accepted_at": _iso(t.accepted_at),
        "resolving_started_at": _iso(t.resolving_started_at),
        "resolved_at": _iso(t.resolved_at),
        "closed_at": closed_at.isoformat() if closed_at else None,
        "resolution_hours": resolution_hours,
        "resolution_summary": t.resolution_summary,
        "escalated": bool(breaches),
        "earliest_breach_stage": earliest,
        "primary_overage_min": primary_overage,
        "breaches": breaches,
    }


def compute_ticket_report(db: Session, date_from: date, date_to: date) -> Dict[str, Any]:
    """Build the ticket report for tickets created within [date_from, date_to] (IST, inclusive).

    Returns the rows already escalation-sorted (breached first by earliest
    stage, then by overage), plus the stage definitions for the UI legend.
    The frontend re-sorts to created-desc when the escalation toggle is off.
    """
    now = datetime.now(timezone.utc)
    start = datetime.combine(date_from, time.min, tzinfo=IST)
    end = datetime.combine(date_to, time.max, tzinfo=IST)

    tickets = (
        db.query(Ticket)
        .options(selectinload(Ticket.events), selectinload(Ticket.resolution))
        .filter(Ticket.deleted_at.is_(None))
        .all()
    )
    rows = [
        _row(t, now)
        for t in tickets
        if (c := _aware(t.created_at)) is not None and start <= c <= end
    ]

    # Default order = escalation: breached first (earliest stage, then most
    # overdue), compliant tickets after by newest-created.
    def sort_key(r: Dict[str, Any]):
        if r["escalated"]:
            return (0, r["earliest_breach_stage"], -(r["primary_overage_min"] or 0.0))
        return (1, 0, 0.0)

    rows.sort(key=lambda r: (sort_key(r), _neg_created(r)))

    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "total": len(rows),
        "escalated_count": sum(1 for r in rows if r["escalated"]),
        "stages": [
            {"stage": d["stage"], "label": d["label"], "site": d["site"], "remote": d["remote"]}
            for d in STAGE_DEFS
        ],
        "rows": rows,
    }


def _neg_created(r: Dict[str, Any]) -> float:
    """Newest-first tiebreaker: negative epoch seconds of created_at."""
    if not r["created_at"]:
        return 0.0
    return -datetime.fromisoformat(r["created_at"]).timestamp()
