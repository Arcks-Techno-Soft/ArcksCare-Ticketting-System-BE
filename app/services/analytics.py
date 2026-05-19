"""Analytics aggregations for the Owner dashboard.

Pulled out of the router so the math stays testable and the endpoint stays
thin. All times are computed in UTC. `days` defines the trailing window the
dashboard cares about (default 30).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from ..models.ticket import Ticket, TicketStatus
from ..models.user import User, UserRole


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

    all_tickets: List[Ticket] = db.query(Ticket).all()
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
    open_count = sum(1 for t in all_tickets if t.status in open_statuses)
    resolved_count = sum(1 for t in all_tickets if t.status == TicketStatus.RESOLVED.value)
    closed_count = sum(1 for t in all_tickets if t.status == TicketStatus.CLOSED.value)

    resolved_in_window = [
        t for t in window_tickets
        if t.resolved_at is not None and t.resolving_started_at is not None
    ]
    avg_resolution_hours = (
        round(sum(_hours_between(t.resolving_started_at, t.resolved_at) for t in resolved_in_window)
              / len(resolved_in_window), 2)
        if resolved_in_window else 0.0
    )

    # ---- Status breakdown ----
    by_status: Dict[str, int] = defaultdict(int)
    for t in all_tickets:
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

    # ---- Avg resolution time per issue category ----
    by_issue: Dict[str, List[float]] = defaultdict(list)
    for t in all_tickets:
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

    # ---- Per-product breakdown ----
    by_product: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"total": 0, "resolved": 0, "hours": []})
    for t in all_tickets:
        bucket = by_product[t.product_category]
        bucket["total"] += 1
        if t.resolved_at and t.resolving_started_at:
            bucket["resolved"] += 1
            bucket["hours"].append(_hours_between(t.resolving_started_at, t.resolved_at))
    product_breakdown = sorted(
        [
            {
                "product_category": prod,
                "total": data["total"],
                "resolved": data["resolved"],
                "avg_hours": round(sum(data["hours"]) / len(data["hours"]), 2) if data["hours"] else 0.0,
            }
            for prod, data in by_product.items()
        ],
        key=lambda row: row["total"],
        reverse=True,
    )

    # ---- Per-engineer performance ----
    engineers = (
        db.query(User)
        .filter(User.role == UserRole.ENGINEER.value)
        .all()
    )
    eng_by_id = {e.id: e for e in engineers}
    eng_buckets: Dict[int, Dict[str, Any]] = {
        e.id: {"engineer_id": e.id, "name": e.name, "assigned": 0, "resolved": 0, "hours": []}
        for e in engineers
    }
    for t in all_tickets:
        if t.assigned_engineer_id and t.assigned_engineer_id in eng_buckets:
            b = eng_buckets[t.assigned_engineer_id]
            b["assigned"] += 1
            if t.resolved_at and t.resolving_started_at:
                b["resolved"] += 1
                b["hours"].append(_hours_between(t.resolving_started_at, t.resolved_at))
    engineer_performance = sorted(
        [
            {
                "engineer_id": b["engineer_id"],
                "name": b["name"],
                "assigned": b["assigned"],
                "resolved": b["resolved"],
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
    }


def _aware(dt: datetime) -> datetime:
    """Treat naive datetimes as UTC (SQLite stores some as naive)."""
    if dt is None:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
