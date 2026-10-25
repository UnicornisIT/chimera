"""Просмотр журнала аудита с фильтрами и поиском."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Select, String, cast, func, or_, select

from chimera.core.audit import AuditLog
from chimera.core.context import ServiceContext
from chimera.core.permissions import Permission
from chimera.core.validation import clean_text

PAGE_SIZE = 50


@dataclass(slots=True)
class AuditFilter:
    query: str | None = None
    action: str | None = None
    user_id: int | None = None
    acting_only: bool = False
    date_from: date | None = None
    date_to: date | None = None
    page: int = 1


@dataclass(frozen=True, slots=True)
class AuditPage:
    items: list[AuditLog]
    total: int
    page: int
    pages: int


def _apply(
    stmt: Select[tuple[AuditLog]], ctx: ServiceContext, f: AuditFilter
) -> Select[tuple[AuditLog]]:
    # Изоляция: только записи текущей организации, даже для системного администратора.
    stmt = stmt.where(AuditLog.organization_id == ctx.require_organization())
    if f.action:
        stmt = stmt.where(AuditLog.action.startswith(f.action))
    if f.user_id:
        stmt = stmt.where(AuditLog.user_id == f.user_id)
    if f.acting_only:
        stmt = stmt.where(
            AuditLog.acting_role.is_not(None), AuditLog.acting_role != AuditLog.real_role
        )
    if f.date_from:
        stmt = stmt.where(AuditLog.created_at >= datetime.combine(f.date_from, time.min, UTC))
    if f.date_to:
        end = datetime.combine(f.date_to + timedelta(days=1), time.min, UTC)
        stmt = stmt.where(AuditLog.created_at < end)
    if f.query:
        pattern = f"%{clean_text(f.query, max_length=100).lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(AuditLog.username).like(pattern),
                func.lower(AuditLog.action).like(pattern),
                func.lower(AuditLog.message).like(pattern),
                func.lower(AuditLog.entity_type).like(pattern),
                AuditLog.entity_id == f.query.strip(),
                func.lower(cast(AuditLog.changes, String)).like(pattern),
            )
        )
    return stmt


def search(ctx: ServiceContext, f: AuditFilter) -> AuditPage:
    ctx.require(Permission.AUDIT_VIEW)
    base = _apply(select(AuditLog), ctx, f)
    total = ctx.session.scalar(select(func.count()).select_from(base.subquery())) or 0
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, f.page), pages)
    items = list(
        ctx.session.scalars(
            base.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(PAGE_SIZE)
            .offset((page - 1) * PAGE_SIZE)
        )
    )
    return AuditPage(items, total, page, pages)


def known_actions(ctx: ServiceContext) -> list[str]:
    ctx.require(Permission.AUDIT_VIEW)
    stmt = (
        select(AuditLog.action)
        .where(AuditLog.organization_id == ctx.require_organization())
        .distinct()
        .order_by(AuditLog.action)
    )
    return list(ctx.session.scalars(stmt))
