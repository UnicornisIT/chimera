"""Пользователи, членства в организациях, заявки и попытки входа."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from chimera.core.db import Base, TimestampMixin, UTCDateTime, enum_column, utcnow
from chimera.core.permissions import Role
from chimera.core.tenancy import OrgScoped
from chimera.models.organization import Division, Organization


class UserStatus(StrEnum):
    PENDING = "pending"
    """Саморегистрация: ждёт одобрения администратора."""
    ACTIVE = "active"
    DISABLED = "disabled"
    REJECTED = "rejected"


class User(TimestampMixin, Base):
    """Учётная запись. Один пользователь может работать в нескольких организациях."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(150), unique=True)
    full_name: Mapped[str] = mapped_column(String(300))
    email: Mapped[str | None] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(512), info={"sensitive": True})
    status: Mapped[UserStatus] = mapped_column(
        enum_column(UserStatus, "user_status"), default=UserStatus.ACTIVE
    )
    is_superadmin: Mapped[bool] = mapped_column(Boolean, default=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    password_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    memberships: Mapped[list[Membership]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="Membership.user_id",
    )

    @property
    def is_active(self) -> bool:
        return self.status is UserStatus.ACTIVE

    def __repr__(self) -> str:
        return f"<User {self.id} {self.username!r}>"


class Membership(OrgScoped, TimestampMixin, Base):
    """Пользователь × организация × роль."""

    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "organization_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[Role] = mapped_column(enum_column(Role, "role"))
    division_id: Mapped[int | None] = mapped_column(
        ForeignKey("divisions.id", ondelete="SET NULL"), index=True
    )
    """Подразделение, в котором работает руководитель или куратор."""
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped[User] = relationship(back_populates="memberships", foreign_keys=[user_id])
    organization: Mapped[Organization] = relationship()
    division: Mapped[Division | None] = relationship()

    def __repr__(self) -> str:
        return f"<Membership u={self.user_id} org={self.organization_id} {self.role.value}>"


class AccessRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class AccessRequest(OrgScoped, TimestampMixin, Base):
    """Заявка на доступ к организации, поданная при саморегистрации."""

    __tablename__ = "access_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[AccessRequestStatus] = mapped_column(
        enum_column(AccessRequestStatus, "access_request_status"),
        default=AccessRequestStatus.PENDING,
    )
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    user: Mapped[User] = relationship(foreign_keys=[user_id])


class LoginAttempt(Base):
    """Неудачные попытки входа: ограничение работает между процессами gunicorn."""

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(150), index=True)
    ip: Mapped[str | None] = mapped_column(String(45), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    note: Mapped[str | None] = mapped_column(Text)
