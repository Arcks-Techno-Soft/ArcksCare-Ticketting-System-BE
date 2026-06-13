"""Expo push notification tokens registered by the mobile app.

Each logged-in device registers its Expo push token so the backend can send
system notifications (new ticket raised → owners/managers; ticket assigned →
that engineer). A user may have several tokens (multiple devices); a token is
unique and re-registering it just re-points it at the current user.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base


class DevicePushToken(Base):
    __tablename__ = "device_push_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # The Expo push token, e.g. "ExponentPushToken[xxxxxxxx]". Unique so the
    # same physical device never gets duplicated rows.
    token: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # "ios" | "android" — informational, helps debugging delivery issues.
    platform: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped["User"] = relationship(lazy="joined")


# Resolve the forward reference cleanly.
from .user import User  # noqa: E402,F401
