"""Who may close, force-close and delete a ticket.

These three gates are the whole of the reserved-power policy for tickets, and
they have moved twice (force-close: Super-Admin → Admin 2026-09-04 → Manager
2026-09-21). Pinning them here means a future change to one tier can't quietly
shift another.
"""
import pytest
from fastapi import HTTPException

from app.services.ticket_workflow import (
    _require_admin_level,
    _require_manager_level,
    _require_super_admin,
)

ALL_ROLES = ["SUPER_ADMIN", "OWNER", "ADMIN", "MANAGER", "ENGINEER", "SALES"]


class _Actor:
    def __init__(self, role):
        self.role = role
        self.id = 1
        self.username = f"{role.lower()}-test"


def _allows(gate, role):
    try:
        gate(_Actor(role), "do the thing")
        return True
    except HTTPException as exc:
        assert exc.status_code == 403
        return False


@pytest.mark.parametrize("role", ALL_ROLES)
def test_manager_level_gate_admits_manager_and_above(role):
    """Closing a ticket. OWNER is the legacy alias for SUPER_ADMIN."""
    expected = role in {"SUPER_ADMIN", "OWNER", "ADMIN", "MANAGER"}
    assert _allows(_require_manager_level, role) is expected


@pytest.mark.parametrize("role", ALL_ROLES)
def test_admin_level_gate_still_excludes_manager(role):
    """The Admin-level gate did not move when closing became Manager-level."""
    expected = role in {"SUPER_ADMIN", "OWNER", "ADMIN"}
    assert _allows(_require_admin_level, role) is expected


@pytest.mark.parametrize("role", ALL_ROLES)
def test_super_admin_gate_is_unchanged(role):
    """Soft-delete stays the reserved power it has always been."""
    expected = role in {"SUPER_ADMIN", "OWNER"}
    assert _allows(_require_super_admin, role) is expected


def test_the_three_tiers_nest():
    """Each tier must be a superset of the one above it — a Super Admin can do
    anything an Admin can, and an Admin anything a Manager can."""
    for role in ALL_ROLES:
        if _allows(_require_super_admin, role):
            assert _allows(_require_admin_level, role)
        if _allows(_require_admin_level, role):
            assert _allows(_require_manager_level, role)
