"""Просмотр журнала аудита с фильтрами и поиском."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, TypeVar

from sqlalchemy import Select, String, cast, or_, select

from chimera.core.audit import AuditLog
from chimera.core.context import ServiceContext
from chimera.core.permissions import Permission
from chimera.core.validation import clean_text
from chimera.services.listing import Page, contains_text, paginate


@dataclass(slots=True)
class AuditFilter:
    query: str | None = None
    action: str | None = None
    user_id: int | None = None
    actor: str | None = None
    acting_only: bool = False
    date_from: date | None = None
    date_to: date | None = None
    page: int = 1


# Параметр типа Select различается в SQLAlchemy 2.0 (tuple[...]) и 2.1 (TypeVarTuple),
# поэтому тип запроса сохраняется через TypeVar, а не записывается явно.
_Stmt = TypeVar("_Stmt", bound=Select[Any])


def _apply(stmt: _Stmt, ctx: ServiceContext, f: AuditFilter) -> _Stmt:
    # Изоляция: только записи текущей организации, даже для системного администратора.
    stmt = stmt.where(AuditLog.organization_id == ctx.require_organization())
    if f.action:
        stmt = stmt.where(AuditLog.action.startswith(f.action, autoescape=True))
    if f.actor:
        stmt = stmt.where(AuditLog.username == f.actor)
    elif f.user_id:
        stmt = stmt.where(AuditLog.user_id == f.user_id)
    if f.acting_only:
        stmt = stmt.where(
            AuditLog.acting_role.is_not(None), AuditLog.acting_role != AuditLog.real_role
        )
    if f.date_from:
        stmt = stmt.where(AuditLog.created_at >= datetime.combine(f.date_from, time.min, UTC))
    if f.date_to and f.date_to < date.max:
        end = datetime.combine(f.date_to + timedelta(days=1), time.min, UTC)
        stmt = stmt.where(AuditLog.created_at < end)
    if f.query and (text := clean_text(f.query, max_length=100)):
        stmt = stmt.where(
            or_(
                contains_text(ctx.session, AuditLog.username, text),
                contains_text(ctx.session, AuditLog.action, text),
                contains_text(ctx.session, AuditLog.message, text),
                contains_text(ctx.session, AuditLog.entity_type, text),
                AuditLog.entity_id == f.query.strip(),
                contains_text(ctx.session, cast(AuditLog.changes, String), text),
            )
        )
    return stmt


def search(ctx: ServiceContext, f: AuditFilter) -> Page[AuditLog]:
    ctx.require(Permission.AUDIT_VIEW)
    base = _apply(select(AuditLog), ctx, f)
    return paginate(
        ctx.session, base.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()), f.page
    )


def suggested_actors(ctx: ServiceContext) -> list[str]:
    ctx.require(Permission.AUDIT_VIEW)
    stmt = (
        select(AuditLog.username)
        .where(
            AuditLog.organization_id == ctx.require_organization(), AuditLog.username.is_not(None)
        )
        .distinct()
        .order_by(AuditLog.username)
        .limit(50)
    )
    return [name for name in ctx.session.scalars(stmt) if name]


def known_actions(ctx: ServiceContext) -> list[str]:
    ctx.require(Permission.AUDIT_VIEW)
    stmt = (
        select(AuditLog.action)
        .where(AuditLog.organization_id == ctx.require_organization())
        .distinct()
        .order_by(AuditLog.action)
    )
    return list(ctx.session.scalars(stmt))
