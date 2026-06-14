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
    ADMIN = "ADMIN"          # full access, can edit warranty, close tickets, manage team
    MANAGER = "MANAGER"      # can acknowledge + assign + download PDFs
    ENGINEER = "ENGINEER"    # can accept, add work notes, mark resolved (on own tickets)


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
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
