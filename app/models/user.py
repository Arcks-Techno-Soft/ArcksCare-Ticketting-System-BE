"""User model — staff accounts for the admin app (Admin / Manager / Engineer).

Customers don't have user records; they're identified only by their submitted
ticket details. This table is exclusively for internal team members who need
to access /admin.
"""
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base


class UserRole(str, Enum):
    SUPER_ADMIN = "SUPER_ADMIN"  # top tier (was OWNER). Full access PLUS the
                                 # reserved powers: manage users/roles, delete &
                                 # force-close tickets, waive charges below the min.
    ADMIN = "ADMIN"          # full access EXCEPT the reserved super-admin powers
    MANAGER = "MANAGER"      # can acknowledge + assign + download PDFs
    ENGINEER = "ENGINEER"    # can accept, add work notes, mark resolved (on own tickets)
    SALES = "SALES"          # field sales rep — raises tickets/installations; gets
                             # credited as the sales rep on installations they source.
    # DEPRECATED legacy alias: pre-rename accounts carried role "OWNER". It is
    # treated as SUPER_ADMIN everywhere until those rows are migrated. Kept so a
    # not-yet-migrated OWNER row is never locked out mid-deploy.
    # TODO: remove once no OWNER rows remain (see scripts/migrate_owner_to_super_admin).
    OWNER = "OWNER"


# --- Authorization tiers (single source of truth) -----------------------
# SUPER_ADMIN sits above ADMIN. Legacy OWNER is folded into SUPER_ADMIN so an
# un-migrated owner keeps full access. Each tier INCLUDES the ones above it.
SUPER_ADMIN_ROLES = (UserRole.SUPER_ADMIN.value, UserRole.OWNER.value)
# "Admin-level" = plain Admin plus everyone above (Super Admin / legacy Owner).
ADMIN_ROLES = (UserRole.ADMIN.value,) + SUPER_ADMIN_ROLES
# "Admin or Manager level" — the common gate for assign/acknowledge/etc.
ADMIN_MANAGER_ROLES = ADMIN_ROLES + (UserRole.MANAGER.value,)

# Back-compat alias (was: ADMIN + OWNER). Now admin-level = ADMIN + SUPER_ADMIN.
ADMIN_ROLE_VALUES = ADMIN_ROLES


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(120))
    first_name: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    role: Mapped[str] = mapped_column(String(20), default=UserRole.ENGINEER.value, index=True)
    # District an Engineer covers. Used to surface district-matched engineers
    # first in the ticket assignment dropdown. NULL for Admin/Manager.
    district: Mapped[Optional[str]] = mapped_column(String(80), nullable=True, index=True)
    # Capability flag, independent of `role`: when True the user can be credited
    # as the sales representative on an installation (appears in the sales-rep
    # picker) regardless of their role. Lets e.g. a Manager also act as a sales
    # rep without giving up Manager access. SALES-role users are always eligible.
    is_sales_rep: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
