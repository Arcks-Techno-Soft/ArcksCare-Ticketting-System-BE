"""One-time backfill: fold pre-attempts work notes into a synthetic attempt.

Before the work-attempts feature, ticket/installation work notes had no
attempt. The new detail UIs render notes *nested under their attempt*, so those
legacy notes (attempt_id IS NULL) would be invisible even though they're still
in the DB.

This idempotent backfill groups each parent's attempt-less notes into a single
already-ended "Attempt N" (started/ended spanning the notes' timestamps) and
links the notes to it, so they show up naturally under that attempt. It only
ever touches notes whose attempt_id is NULL, so re-running is a no-op.

Runs at startup AFTER create_all (the attempt tables must already exist).
"""
from __future__ import annotations

import logging

from sqlalchemy import func, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ..database import MIGRATION_SCHEMA, SessionLocal
from ..models.installation import Installation, InstallationAttempt, InstallationNote
from ..models.ticket import Ticket, TicketAttempt, WorkNote

logger = logging.getLogger("skposcare.attempt_backfill")


def _backfill_ticket_notes(db: Session) -> int:
    ticket_ids = [
        row[0]
        for row in db.query(WorkNote.ticket_id)
        .filter(WorkNote.ticket_attempt_id.is_(None))
        .distinct()
        .all()
    ]
    migrated = 0
    for tid in ticket_ids:
        notes = (
            db.query(WorkNote)
            .filter(WorkNote.ticket_id == tid, WorkNote.ticket_attempt_id.is_(None))
            .order_by(WorkNote.created_at)
            .all()
        )
        if not notes:
            continue
        ticket = db.get(Ticket, tid)
        if ticket is None:  # orphaned note — skip defensively
            continue
        next_num = (
            db.query(func.max(TicketAttempt.attempt_number))
            .filter(TicketAttempt.ticket_id == tid)
            .scalar()
        ) or 0
        attempt = TicketAttempt(
            ticket_id=tid,
            attempt_number=next_num + 1,
            started_at=notes[0].created_at,
            ended_at=notes[-1].created_at,
            started_by_id=ticket.assigned_engineer_id or notes[0].author_id,
        )
        db.add(attempt)
        db.flush()
        for n in notes:
            n.ticket_attempt_id = attempt.id
        migrated += 1
    return migrated


def _backfill_installation_notes(db: Session) -> int:
    inst_ids = [
        row[0]
        for row in db.query(InstallationNote.installation_id)
        .filter(InstallationNote.installation_attempt_id.is_(None))
        .distinct()
        .all()
    ]
    migrated = 0
    for iid in inst_ids:
        notes = (
            db.query(InstallationNote)
            .filter(
                InstallationNote.installation_id == iid,
                InstallationNote.installation_attempt_id.is_(None),
            )
            .order_by(InstallationNote.created_at)
            .all()
        )
        if not notes:
            continue
        inst = db.get(Installation, iid)
        if inst is None:
            continue
        next_num = (
            db.query(func.max(InstallationAttempt.attempt_number))
            .filter(InstallationAttempt.installation_id == iid)
            .scalar()
        ) or 0
        attempt = InstallationAttempt(
            installation_id=iid,
            attempt_number=next_num + 1,
            started_at=notes[0].created_at,
            ended_at=notes[-1].created_at,
            started_by_id=inst.assigned_engineer_id or notes[0].author_id,
        )
        db.add(attempt)
        db.flush()
        for n in notes:
            n.installation_attempt_id = attempt.id
        migrated += 1
    return migrated


def backfill_legacy_notes_into_attempts(engine: Engine) -> None:
    """Group attempt-less notes into a synthetic ended attempt. Idempotent."""
    insp = inspect(engine)
    tables = set(insp.get_table_names(schema=MIGRATION_SCHEMA))
    # Need both the notes tables and the new attempt tables to exist.
    if not {"work_notes", "ticket_attempts"}.issubset(tables) and not {
        "installation_notes",
        "installation_attempts",
    }.issubset(tables):
        return

    db = SessionLocal()
    try:
        t = _backfill_ticket_notes(db) if {"work_notes", "ticket_attempts"}.issubset(tables) else 0
        i = (
            _backfill_installation_notes(db)
            if {"installation_notes", "installation_attempts"}.issubset(tables)
            else 0
        )
        if t or i:
            db.commit()
            logger.info(
                "Attempt backfill: wrapped legacy notes for %d ticket(s) and %d installation(s)",
                t, i,
            )
    except Exception:  # never block startup on a best-effort backfill
        db.rollback()
        logger.exception("Attempt backfill failed (non-fatal)")
    finally:
        db.close()
