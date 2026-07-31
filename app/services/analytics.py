"""Analytics aggregations for the Admin dashboard.

Pulled out of the router so the math stays testable and the endpoint stays
thin. All times are computed in UTC. `days` defines the trailing window the
dashboard cares about (default 30).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from ..models.installation import Installation, InstallationStatus
from ..models.ticket import ServiceType, Ticket, TicketStatus, WarrantyStatus
from ..models.user import User, UserRole
from .reports import STAGE_DEFS


def _hours_between(start, end) -> float:
    if start is None or end is None:
        return 0.0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0.0, (end - start).total_seconds() / 3600.0)


def compute_analytics(db: Session, days: int = 30) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=days)

    all_tickets: List[Ticket] = (
        db.query(Ticket).filter(Ticket.deleted_at.is_(None)).all()
    )
    window_tickets = [t for t in all_tickets if _aware(t.created_at) >= window_start]

    # ---- KPI cards ----
    total = len(all_tickets)
    open_statuses = {
        TicketStatus.OPEN.value,
        TicketStatus.ACKNOWLEDGED.value,
        TicketStatus.ASSIGNED.value,
        TicketStatus.ACCEPTED.value,
        TicketStatus.RESOLVING.value,
    }
    # Held tickets keep their status, so they'd otherwise inflate "Open" with
    # work nobody can act on. They get their own card instead.
    open_count = sum(
        1 for t in all_tickets if t.status in open_statuses and t.held_at is None
    )
    on_hold_count = sum(
        1 for t in all_tickets if t.status in open_statuses and t.held_at is not None
    )
    resolved_count = sum(1 for t in all_tickets if t.status == TicketStatus.RESOLVED.value)
    closed_count = sum(1 for t in all_tickets if t.status == TicketStatus.CLOSED.value)

    # The whole page reports on ONE cohort: tickets CREATED in the window
    # (window_tickets). "Resolved · Nd" = how many of this period's tickets are
    # resolved; "Avg resolution" = average Resolving→Resolved time over those.
    # Every breakdown below uses the same cohort, so the headline counts and the
    # per-category tables tie out. (The per-day "resolved" line and the
    # resolution-time trend remain time-series keyed on resolved_at — they show
    # WHEN resolutions happened, so they aren't expected to sum to these cards.)
    resolved_in_window = [
        t for t in window_tickets
        if t.resolved_at is not None and t.resolving_started_at is not None
    ]
    avg_resolution_hours = (
        round(sum(_hours_between(t.resolving_started_at, t.resolved_at) for t in resolved_in_window)
              / len(resolved_in_window), 2)
        if resolved_in_window else 0.0
    )

    # ---- Status breakdown (tickets created in the window) ----
    by_status: Dict[str, int] = defaultdict(int)
    for t in window_tickets:
        by_status[t.status] += 1

    # ---- Severity breakdown ----
    by_severity: Dict[str, int] = defaultdict(int)
    for t in window_tickets:
        by_severity[t.severity] += 1

    # ---- Tickets-per-day series (created + resolved) ----
    days_series: List[Dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        day = (now - timedelta(days=offset)).date()
        days_series.append({"date": day.isoformat(), "created": 0, "resolved": 0})
    by_date = {row["date"]: row for row in days_series}

    for t in window_tickets:
        key = _aware(t.created_at).date().isoformat()
        if key in by_date:
            by_date[key]["created"] += 1
    for t in all_tickets:
        if t.resolved_at is None:
            continue
        key = _aware(t.resolved_at).date().isoformat()
        if key in by_date:
            by_date[key]["resolved"] += 1

    # ---- Resolution-time trend (avg hours per day, only days with resolutions) ----
    res_trend_buckets: Dict[str, List[float]] = defaultdict(list)
    for t in all_tickets:
        if t.resolved_at is None or t.resolving_started_at is None:
            continue
        key = _aware(t.resolved_at).date().isoformat()
        if key in by_date:
            res_trend_buckets[key].append(_hours_between(t.resolving_started_at, t.resolved_at))
    resolution_trend = [
        {
            "date": row["date"],
            "avg_hours": round(sum(res_trend_buckets[row["date"]]) / len(res_trend_buckets[row["date"]]), 2)
            if res_trend_buckets.get(row["date"]) else None,
            "count": len(res_trend_buckets.get(row["date"], [])),
        }
        for row in days_series
    ]

    # ---- Avg resolution time per issue category (tickets created in the window) ----
    by_issue: Dict[str, List[float]] = defaultdict(list)
    for t in window_tickets:
        if t.resolved_at is None or t.resolving_started_at is None:
            continue
        by_issue[t.issue_category].append(_hours_between(t.resolving_started_at, t.resolved_at))
    issue_breakdown = sorted(
        [
            {
                "issue_category": cat,
                "avg_hours": round(sum(vals) / len(vals), 2),
                "resolved_count": len(vals),
            }
            for cat, vals in by_issue.items()
        ],
        key=lambda row: row["avg_hours"],
        reverse=True,
    )

    # ---- Per-product breakdown (tickets created in the window) ----
    # Carries the warranty split per product too: a product whose tickets are
    # mostly out-of-warranty is where the service revenue (and the spare-part
    # billing) comes from.
    by_product: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"total": 0, "resolved": 0, "hours": [], "under_warranty": 0, "out_of_warranty": 0}
    )
    for t in window_tickets:
        bucket = by_product[t.product_category]
        bucket["total"] += 1
        if t.warranty_status == WarrantyStatus.UNDER_WARRANTY.value:
            bucket["under_warranty"] += 1
        elif t.warranty_status == WarrantyStatus.OUT_OF_WARRANTY.value:
            bucket["out_of_warranty"] += 1
        if t.resolved_at and t.resolving_started_at:
            bucket["resolved"] += 1
            bucket["hours"].append(_hours_between(t.resolving_started_at, t.resolved_at))
    product_breakdown = sorted(
        [
            {
                "product_category": prod,
                "total": data["total"],
                "resolved": data["resolved"],
                "under_warranty": data["under_warranty"],
                "out_of_warranty": data["out_of_warranty"],
                "avg_hours": round(sum(data["hours"]) / len(data["hours"]), 2) if data["hours"] else 0.0,
            }
            for prod, data in by_product.items()
        ],
        key=lambda row: row["total"],
        reverse=True,
    )

    # ---- Revenue & warranty (tickets created in the window) ----
    # Money figures use the same billing rules as the charges screen
    # (Ticket.amount_due_inr mirrors services.spares.compute_charges), so the
    # dashboard can never disagree with what a ticket actually bills.
    #
    # Scoped to payment-TRACKED tickets only (payment_status non-NULL): legacy
    # tickets pre-date payment tracking, collected their money off the books,
    # and would otherwise inflate "billed" with amounts that can never show as
    # collected — turning the collection rate into fiction.
    tracked = [t for t in window_tickets if t.payment_status is not None]
    billed_inr = sum(t.amount_due_inr for t in tracked)
    collected_inr = sum(t.amount_collected_inr for t in tracked)
    outstanding_inr = sum(t.amount_pending_inr for t in tracked)
    awaiting_verification = sum(1 for t in tracked if t.payment_awaiting_verification)
    revenue = {
        "billed_inr": billed_inr,
        "collected_inr": collected_inr,
        "outstanding_inr": outstanding_inr,
        "collection_rate": round((collected_inr / billed_inr) * 100, 1) if billed_inr else 0.0,
        "awaiting_verification": awaiting_verification,
        "tracked_tickets": len(tracked),
        "untracked_tickets": len(window_tickets) - len(tracked),
    }

    # Warranty mix over the window cohort — UNKNOWN means intake hasn't been
    # triaged yet, so a growing UNKNOWN bar is itself a signal.
    warranty_mix: Dict[str, int] = defaultdict(int)
    for t in window_tickets:
        warranty_mix[t.warranty_status] += 1

    # Site-visit vs remote split: remote resolutions are the cheapest, so the
    # remote share is worth watching as a service-cost lever.
    service_type_mix: Dict[str, int] = defaultdict(int)
    for t in window_tickets:
        service_type_mix[t.service_type] += 1

    # ---- Response time & SLA stage compliance (window cohort) ----
    # Thresholds come from the report's STAGE_DEFS so this page can never
    # disagree with the Reports screen about what "breached" means. Two
    # deliberate simplifications vs the full report: stage 6 (Resolved→Close)
    # is skipped — its end time is derived per-ticket from the resolution PDF
    # and would N+1 the lazy relationships — and time-on-hold is NOT
    # subtracted, so a stage that spanned a hold reads slower here than in the
    # report. Headline numbers here; per-ticket truth on the Reports page.
    sla_stages: List[Dict[str, Any]] = []
    for d in STAGE_DEFS:
        if d["end"] == "closed_at":
            continue  # not a Ticket column — reports derives it per ticket
        minutes: List[float] = []
        breaches = 0
        applicable = 0
        for t in window_tickets:
            start = getattr(t, d["start"])
            end = getattr(t, d["end"])
            if start is None or end is None:
                continue
            threshold = (
                d["remote"]
                if t.service_type == ServiceType.REMOTE_SUPPORT.value
                else d["site"]
            )
            if threshold is None:
                continue
            elapsed = _hours_between(start, end) * 60.0
            minutes.append(elapsed)
            applicable += 1
            if elapsed > threshold:
                breaches += 1
        sla_stages.append({
            "stage": d["stage"],
            "label": d["label"],
            "avg_min": round(sum(minutes) / len(minutes), 1) if minutes else None,
            "breach_rate": round((breaches / applicable) * 100, 1) if applicable else None,
            "measured": applicable,
        })

    # ---- Backlog aging + current holds (live state, NOT the window cohort:
    # the question is "what is sitting in the queue right now") ----
    open_live = [
        t for t in all_tickets if t.status in open_statuses and t.held_at is None
    ]
    aging_order = ["0-2d", "3-7d", "8-14d", "15d+"]
    backlog_aging: Dict[str, int] = {k: 0 for k in aging_order}
    for t in open_live:
        age_days = (now - _aware(t.created_at)).total_seconds() / 86400.0
        if age_days <= 2:
            backlog_aging["0-2d"] += 1
        elif age_days <= 7:
            backlog_aging["3-7d"] += 1
        elif age_days <= 14:
            backlog_aging["8-14d"] += 1
        else:
            backlog_aging["15d+"] += 1

    held_now = sorted(
        (t for t in all_tickets if t.held_at is not None and t.status != TicketStatus.CLOSED.value),
        key=lambda t: _aware(t.held_at),
    )
    holds = [
        {
            "reference": t.reference,
            "business_name": t.business_name,
            "status": t.status,
            "reason": (t.hold_reason or "").strip() or None,
            "days_on_hold": round((now - _aware(t.held_at)).total_seconds() / 86400.0, 1),
        }
        for t in held_now[:20]  # longest-parked first; 20 is plenty for a card
    ]

    # Engineer roster — shared by the ticket and installation per-engineer
    # tables below, so it's fetched once here.
    engineers = (
        db.query(User)
        .filter(User.role == UserRole.ENGINEER.value)
        .all()
    )

    # ---- Installations: a full parallel view (the UI toggles between this and
    # the ticket blocks above, so it mirrors their shape). ----
    all_installs: List[Installation] = db.query(Installation).all()
    window_installs = [i for i in all_installs if _aware(i.created_at) >= window_start]
    completed_in_window = [
        i for i in all_installs
        if i.completed_at is not None and _aware(i.completed_at) >= window_start
    ]
    install_open_statuses = {InstallationStatus.NEW.value, InstallationStatus.ASSIGNED.value}

    assign_to_complete = [
        _hours_between(i.assigned_at, i.completed_at)
        for i in completed_in_window if i.assigned_at is not None
    ]
    create_to_complete = [
        _hours_between(i.created_at, i.completed_at) for i in completed_in_window
    ]

    # Expected-date adherence — the promise made to the customer. Compared on
    # DATES (the expected value is a plain date, not a timestamp).
    today = now.date()
    exp_on_time = exp_late = exp_overdue_open = exp_upcoming = exp_no_date = 0
    for i in window_installs:
        exp = i.expected_installation_date
        if exp is None:
            exp_no_date += 1
        elif i.completed_at is not None:
            if _aware(i.completed_at).date() <= exp:
                exp_on_time += 1
            else:
                exp_late += 1
        elif exp < today:
            exp_overdue_open += 1
        else:
            # Dated, not done, not yet due — still on schedule. Its own bucket
            # so the five states partition window_created exactly.
            exp_upcoming += 1

    install_open_live = [
        i for i in all_installs if i.status in install_open_statuses and i.held_at is None
    ]
    install_aging: Dict[str, int] = {k: 0 for k in aging_order}
    for i in install_open_live:
        age_days = (now - _aware(i.created_at)).total_seconds() / 86400.0
        if age_days <= 2:
            install_aging["0-2d"] += 1
        elif age_days <= 7:
            install_aging["3-7d"] += 1
        elif age_days <= 14:
            install_aging["8-14d"] += 1
        else:
            install_aging["15d+"] += 1

    install_held = sorted(
        (i for i in all_installs
         if i.held_at is not None and i.status != InstallationStatus.CLOSED.value),
        key=lambda i: _aware(i.held_at),
    )

    # Per-day created vs completed, on the same day grid as the ticket series.
    install_days = [
        {"date": row["date"], "created": 0, "completed": 0} for row in days_series
    ]
    install_by_date = {row["date"]: row for row in install_days}
    for i in window_installs:
        key = _aware(i.created_at).date().isoformat()
        if key in install_by_date:
            install_by_date[key]["created"] += 1
    for i in all_installs:
        if i.completed_at is None:
            continue
        key = _aware(i.completed_at).date().isoformat()
        if key in install_by_date:
            install_by_date[key]["completed"] += 1

    install_by_status: Dict[str, int] = defaultdict(int)
    for i in window_installs:
        install_by_status[i.status] += 1

    # Per-engineer installation workload, same shape as the ticket table.
    inst_eng: Dict[int, Dict[str, Any]] = {
        e.id: {"engineer_id": e.id, "name": e.name, "assigned": 0, "completed": 0, "hours": []}
        for e in engineers
    }
    for i in window_installs:
        if i.assigned_engineer_id and i.assigned_engineer_id in inst_eng:
            b = inst_eng[i.assigned_engineer_id]
            b["assigned"] += 1
            if i.completed_at is not None:
                b["completed"] += 1
                if i.assigned_at is not None:
                    b["hours"].append(_hours_between(i.assigned_at, i.completed_at))
    install_engineer_performance = sorted(
        [
            {
                "engineer_id": b["engineer_id"],
                "name": b["name"],
                "assigned": b["assigned"],
                "completed": b["completed"],
                "avg_hours": round(sum(b["hours"]) / len(b["hours"]), 2) if b["hours"] else 0.0,
                "completion_rate": round((b["completed"] / b["assigned"]) * 100, 1) if b["assigned"] else 0.0,
            }
            for b in inst_eng.values()
        ],
        key=lambda row: (-row["completed"], row["avg_hours"]),
    )

    # Where installations land — business category is the closest thing to a
    # customer segment on an installation.
    inst_by_category: Dict[str, int] = defaultdict(int)
    for i in window_installs:
        inst_by_category[i.business_category or "Uncategorised"] += 1
    install_category_breakdown = sorted(
        [{"category": c, "total": n} for c, n in inst_by_category.items()],
        key=lambda row: row["total"],
        reverse=True,
    )

    installations = {
        "kpis": {
            "window_created": len(window_installs),
            "window_completed": len(completed_in_window),
            "open_now": len(install_open_live),
            "on_hold": len(install_held),
            "closed_total": sum(
                1 for i in all_installs if i.status == InstallationStatus.CLOSED.value
            ),
            "avg_assign_to_complete_hours": (
                round(sum(assign_to_complete) / len(assign_to_complete), 2)
                if assign_to_complete else 0.0
            ),
            "avg_create_to_complete_hours": (
                round(sum(create_to_complete) / len(create_to_complete), 2)
                if create_to_complete else 0.0
            ),
            "overdue_open": exp_overdue_open,
        },
        "expected_date": {
            "on_time": exp_on_time,
            "late": exp_late,
            "overdue_open": exp_overdue_open,
            "upcoming": exp_upcoming,
            "no_date": exp_no_date,
        },
        "per_day": install_days,
        "by_status": dict(install_by_status),
        "backlog_aging": install_aging,
        "holds": [
            {
                "reference": i.reference,
                "business_name": i.business_name,
                "status": i.status,
                "reason": (i.hold_reason or "").strip() or None,
                "days_on_hold": round((now - _aware(i.held_at)).total_seconds() / 86400.0, 1),
            }
            for i in install_held[:20]
        ],
        "engineer_performance": install_engineer_performance,
        "category_breakdown": install_category_breakdown,
    }

    # ---- Repeat businesses (window cohort) ----
    # A site raising several tickets in one window is a machine, environment,
    # or training problem — five tickets are rarely five coincidences.
    by_business: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"tickets": 0, "products": defaultdict(int), "open": 0}
    )
    for t in window_tickets:
        b = by_business[t.business_name]
        b["tickets"] += 1
        b["products"][t.product_category] += 1
        if t.status in open_statuses:
            b["open"] += 1
    repeat_businesses = sorted(
        [
            {
                "business_name": name,
                "tickets": data["tickets"],
                "open_now": data["open"],
                "top_product": max(data["products"].items(), key=lambda kv: kv[1])[0],
            }
            for name, data in by_business.items()
            if data["tickets"] >= 2
        ],
        key=lambda row: row["tickets"],
        reverse=True,
    )[:10]

    # ---- Per-engineer performance (roster fetched above) ----
    eng_by_id = {e.id: e for e in engineers}
    eng_buckets: Dict[int, Dict[str, Any]] = {
        e.id: {
            "engineer_id": e.id, "name": e.name, "assigned": 0, "resolved": 0,
            "hours": [], "installs_assigned": 0, "installs_completed": 0,
        }
        for e in engineers
    }
    for t in window_tickets:
        if t.assigned_engineer_id and t.assigned_engineer_id in eng_buckets:
            b = eng_buckets[t.assigned_engineer_id]
            b["assigned"] += 1
            if t.resolved_at and t.resolving_started_at:
                b["resolved"] += 1
                b["hours"].append(_hours_between(t.resolving_started_at, t.resolved_at))
    # Installations count toward workload too — without this, an engineer who
    # spent the window installing looks idle on this table.
    for i in window_installs:
        if i.assigned_engineer_id and i.assigned_engineer_id in eng_buckets:
            b = eng_buckets[i.assigned_engineer_id]
            b["installs_assigned"] += 1
            if i.completed_at is not None:
                b["installs_completed"] += 1
    engineer_performance = sorted(
        [
            {
                "engineer_id": b["engineer_id"],
                "name": b["name"],
                "assigned": b["assigned"],
                "resolved": b["resolved"],
                "installs_assigned": b["installs_assigned"],
                "installs_completed": b["installs_completed"],
                "avg_hours": round(sum(b["hours"]) / len(b["hours"]), 2) if b["hours"] else 0.0,
                "completion_rate": round((b["resolved"] / b["assigned"]) * 100, 1) if b["assigned"] else 0.0,
            }
            for b in eng_buckets.values()
        ],
        key=lambda row: (-row["resolved"], row["avg_hours"]),
    )

    return {
        "window_days": days,
        "kpis": {
            "total_tickets": total,
            "open_tickets": open_count,
            "on_hold_tickets": on_hold_count,
            "resolved_tickets": resolved_count,
            "closed_tickets": closed_count,
            "window_tickets": len(window_tickets),
            "window_resolved": len(resolved_in_window),
            "avg_resolution_hours": avg_resolution_hours,
        },
        "by_status": dict(by_status),
        "by_severity": dict(by_severity),
        "tickets_per_day": days_series,
        "resolution_trend": resolution_trend,
        "issue_breakdown": issue_breakdown,
        "product_breakdown": product_breakdown,
        "engineer_performance": engineer_performance,
        "revenue": revenue,
        "warranty_mix": dict(warranty_mix),
        "service_type_mix": dict(service_type_mix),
        "sla_stages": sla_stages,
        "backlog_aging": backlog_aging,
        "holds": holds,
        "installations": installations,
        "repeat_businesses": repeat_businesses,
    }


def _aware(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC (SQLite stores some as naive)."""
    if dt is None:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
