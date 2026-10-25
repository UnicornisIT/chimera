"""Организации и их подразделения."""

from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from chimera.core.db import Base, TimestampMixin, enum_column
from chimera.core.tenancy import OrgScoped
from chimera.core.terms import OrgType, StudentTerm, Terms, terms_for


class Organization(TimestampMixin, Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(500))
    short_name: Mapped[str] = mapped_column(String(150))
    org_type: Mapped[OrgType] = mapped_column(enum_column(OrgType, "org_type"))
    student_term: Mapped[StudentTerm | None] = mapped_column(
        enum_column(StudentTerm, "student_term")
    )
    founder_name: Mapped[str | None] = mapped_column(String(500))
    address: Mapped[str | None] = mapped_column(Text)
    head_full_name: Mapped[str | None] = mapped_column(String(300))
    head_position: Mapped[str | None] = mapped_column(String(300))
    document_logo_path: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    divisions: Mapped[list[Division]] = relationship(
        back_populates="organization", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def terms(self) -> Terms:
        return terms_for(self.org_type, self.student_term)

    def __repr__(self) -> str:
        return f"<Organization {self.id} {self.short_name!r}>"


class DivisionKind(StrEnum):
    DIVISION = "division"
    """Факультет (вуз) или отделение (колледж)."""
    DEPARTMENT = "department"
    """Кафедра (вуз) или цикловая комиссия (колледж)."""


class Division(OrgScoped, TimestampMixin, Base):
    """Подразделение: организация → факультет/отделение → кафедра/цикловая комиссия."""

    __tablename__ = "divisions"
    __table_args__ = (UniqueConstraint("organization_id", "parent_id", "name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("divisions.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[DivisionKind] = mapped_column(enum_column(DivisionKind, "division_kind"))
    name: Mapped[str] = mapped_column(String(300))
    short_name: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    organization: Mapped[Organization] = relationship(back_populates="divisions")
    parent: Mapped[Division | None] = relationship(
        remote_side="Division.id", back_populates="children"
    )
    children: Mapped[list[Division]] = relationship(
        back_populates="parent", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:
        return f"<Division {self.id} {self.kind.value} {self.name!r}>"


__all__ = ["Division", "DivisionKind", "Organization"]
