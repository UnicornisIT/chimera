"""Режим «работать как» для администратора.

Администратор выбирает роль (руководитель подразделения, куратор, староста) и
субъекта: подразделение, чьи группы или пользователя-старосту. Пока режим
активен, права вычисляются по выбранной роли, а каждое действие пишется в аудит
с пометкой реальной роли.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from chimera.core import audit
from chimera.core.context import PermissionDenied, ServiceContext, ServiceError
from chimera.core.permissions import ACTABLE_ROLES, ROLE_LABELS, Permission, Role
from chimera.models import Division, DivisionKind, Membership, User
from chimera.services.users import members_with_role


@dataclass(frozen=True, slots=True)
class ActAs:
    role: Role
    subject_user_id: int | None = None
    division_id: int | None = None
    label: str = ""

    def to_session(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "subject_user_id": self.subject_user_id,
            "division_id": self.division_id,
            "label": self.label,
        }

    @classmethod
    def from_session(cls, raw: object) -> ActAs | None:
        if not isinstance(raw, dict):
            return None
        try:
            role = Role(str(raw.get("role")))
        except ValueError:
            return None
        if role not in ACTABLE_ROLES:
            return None
        subject = raw.get("subject_user_id")
        division = raw.get("division_id")
        return cls(
            role=role,
            subject_user_id=subject if isinstance(subject, int) else None,
            division_id=division if isinstance(division, int) else None,
            label=str(raw.get("label") or ROLE_LABELS[role]),
        )


@dataclass(frozen=True, slots=True)
class ActAsChoices:
    divisions: list[Division]
    curators: list[Membership]
    starostas: list[Membership]


def choices(ctx: ServiceContext) -> ActAsChoices:
    ctx.require(Permission.ROLE_ACT_AS)
    divisions = list(
        ctx.session.scalars(
            select(Division)
            .where(Division.kind == DivisionKind.DIVISION, Division.is_active.is_(True))
            .order_by(Division.name)
        )
    )
    return ActAsChoices(
        divisions=divisions,
        curators=members_with_role(ctx, Role.CURATOR),
        starostas=members_with_role(ctx, Role.STAROSTA),
    )


def _member_label(ctx: ServiceContext, role: Role, user_id: int | None) -> tuple[int, str]:
    if not user_id:
        raise ServiceError(f"Выберите пользователя с ролью «{ROLE_LABELS[role]}».")
    membership = next((m for m in members_with_role(ctx, role) if m.user_id == user_id), None)
    if membership is None:
        raise ServiceError("Пользователь с такой ролью не найден в организации.")
    return membership.user_id, membership.user.full_name


def start(
    ctx: ServiceContext,
    role: str,
    *,
    subject_user_id: int | None = None,
    division_id: int | None = None,
) -> ActAs:
    if ctx.is_acting:
        raise ServiceError("Сначала вернитесь в свою роль.")
    if ctx.real_role is not Role.ADMIN:
        raise PermissionDenied("Режим «работать как» доступен только администратору.")
    ctx.require(Permission.ROLE_ACT_AS)
    try:
        target = Role(role)
    except ValueError as exc:
        raise ServiceError("Выберите роль.") from exc
    if target not in ACTABLE_ROLES:
        raise ServiceError("В эту роль переключиться нельзя.")

    label = ROLE_LABELS[target]
    subject: int | None = None
    division: int | None = None
    if target is Role.HEAD:
        found = ctx.session.scalar(
            select(Division).where(
                Division.id == (division_id or 0), Division.kind == DivisionKind.DIVISION
            )
        )
        if found is None:
            raise ServiceError("Выберите подразделение.")
        division = found.id
        label = f"{label} · {found.name}"
    else:
        subject, name = _member_label(ctx, target, subject_user_id)
        label = f"{label} · {name}"

    acting = ActAs(target, subject, division, label)
    audit.record(
        ctx,
        "act_as.started",
        entity_type="User",
        entity_id=subject,
        changes={"role": target.value, "subject_user_id": subject, "division_id": division},
        message=label,
    )
    return acting


def stop(ctx: ServiceContext, acting: ActAs) -> None:
    audit.record(
        ctx,
        "act_as.stopped",
        entity_type="User",
        entity_id=acting.subject_user_id,
        message=acting.label,
    )


def record_action(ctx: ServiceContext, method: str, endpoint: str | None, path: str) -> None:
    """Каждый запрос в режиме «работать как» фиксируется с реальной ролью."""
    audit.record(
        ctx,
        "act_as.request",
        changes={"method": method, "endpoint": endpoint, "path": path[:300]},
    )


def subject_name(ctx: ServiceContext, user_id: int | None) -> str | None:
    if not user_id:
        return None
    user = ctx.session.get(User, user_id)
    return user.full_name if user else None
