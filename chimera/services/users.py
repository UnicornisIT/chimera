"""Пользователи организации, их роли и заявки на доступ."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import joinedload

from chimera.core import audit
from chimera.core.context import NotFound, ServiceContext, ServiceError
from chimera.core.db import utcnow
from chimera.core.permissions import Permission, Role
from chimera.core.security import hash_password, password_problems
from chimera.core.tenancy import unscoped
from chimera.core.validation import (
    clean_text,
    email_problem,
    normalize_email,
    normalize_username,
    username_problem,
)
from chimera.models import (
    AccessRequest,
    AccessRequestStatus,
    Division,
    Membership,
    User,
    UserStatus,
)

DIVISION_ROLES = frozenset({Role.HEAD, Role.CURATOR})


def list_members(
    ctx: ServiceContext, *, query: str | None = None, role: Role | None = None
) -> list[Membership]:
    ctx.require(Permission.USERS_MANAGE)
    stmt = (
        select(Membership)
        .join(User, User.id == Membership.user_id)
        .options(joinedload(Membership.user), joinedload(Membership.division))
        .order_by(User.full_name)
    )
    if role is not None:
        stmt = stmt.where(Membership.role == role)
    if query:
        pattern = f"%{clean_text(query).lower()}%"
        stmt = stmt.where(
            or_(
                User.username.ilike(pattern),
                User.full_name.ilike(pattern),
                User.email.ilike(pattern),
            )
        )
    return list(ctx.session.scalars(stmt).unique())


def members_with_role(ctx: ServiceContext, role: Role) -> list[Membership]:
    """Активные участники с ролью — для выбора в режиме «работать как»."""
    stmt = (
        select(Membership)
        .join(User, User.id == Membership.user_id)
        .options(joinedload(Membership.user))
        .where(
            Membership.role == role,
            Membership.is_active.is_(True),
            User.status == UserStatus.ACTIVE,
        )
        .order_by(User.full_name)
    )
    return list(ctx.session.scalars(stmt).unique())


def get_membership(ctx: ServiceContext, membership_id: int) -> Membership:
    ctx.require(Permission.USERS_MANAGE)
    membership = ctx.session.scalar(
        select(Membership)
        .options(joinedload(Membership.user))
        .where(Membership.id == membership_id)
    )
    if membership is None:
        raise NotFound("Пользователь не найден в организации")
    return membership


def _division_for(ctx: ServiceContext, role: Role, division_id: int | None) -> int | None:
    if role not in DIVISION_ROLES or not division_id:
        return None
    division = ctx.session.scalar(select(Division.id).where(Division.id == division_id))
    if division is None:
        raise ServiceError("Подразделение не найдено.")
    return division


@dataclass(slots=True)
class NewUserData:
    username: str
    full_name: str
    email: str | None
    password: str
    role: str
    division_id: int | None = None


def _parse_role(raw: str) -> Role:
    try:
        return Role(raw)
    except ValueError as exc:
        raise ServiceError("Выберите роль.") from exc


def create_member(ctx: ServiceContext, data: NewUserData) -> Membership:
    """Создаёт пользователя или добавляет существующего (по логину) в организацию."""
    ctx.require(Permission.USERS_MANAGE)
    org_id = ctx.require_organization()
    role = _parse_role(data.role)
    login = normalize_username(data.username)
    if (problem := username_problem(login)) is not None:
        raise ServiceError(problem)

    user = ctx.session.scalar(select(User).where(User.username == login))
    if user is None:
        problems: list[str] = []
        full_name = clean_text(data.full_name, max_length=300)
        if not full_name:
            problems.append("Укажите ФИО.")
        email = normalize_email(data.email)
        if (email_issue := email_problem(email)) is not None:
            problems.append(email_issue)
        problems.extend(password_problems(data.password, username=login))
        if problems:
            raise ServiceError(" ".join(problems))
        user = User(
            username=login,
            full_name=full_name,
            email=email,
            password_hash=hash_password(data.password),
            must_change_password=True,
        )
        ctx.session.add(user)
        ctx.session.flush()
        audit.record(ctx, "user.created", entity=user, after=audit.snapshot(user))
    elif ctx.session.scalar(select(Membership.id).where(Membership.user_id == user.id)) is not None:
        raise ServiceError("Пользователь уже состоит в этой организации.")

    membership = Membership(
        user_id=user.id,
        organization_id=org_id,
        role=role,
        division_id=_division_for(ctx, role, data.division_id),
    )
    ctx.session.add(membership)
    ctx.session.flush()
    audit.record(ctx, "membership.created", entity=membership, after=audit.snapshot(membership))
    return membership


@dataclass(slots=True)
class MembershipData:
    role: str
    division_id: int | None
    is_active: bool
    full_name: str
    email: str | None


def update_member(ctx: ServiceContext, membership_id: int, data: MembershipData) -> Membership:
    membership = get_membership(ctx, membership_id)
    role = _parse_role(data.role)
    is_self = membership.user_id == ctx.user_id
    if is_self and (role is not Role.ADMIN or not data.is_active) and not ctx.is_superadmin:
        raise ServiceError("Нельзя снять с себя роль администратора или отключить себя.")

    user = membership.user
    full_name = clean_text(data.full_name, max_length=300)
    email = normalize_email(data.email)
    if not full_name:
        raise ServiceError("Укажите ФИО.")
    if (problem := email_problem(email)) is not None:
        raise ServiceError(problem)

    user_before = audit.snapshot(user)
    user.full_name = full_name
    user.email = email
    user_changes = audit.diff(user_before, audit.snapshot(user))
    if user_changes:
        audit.record(ctx, "user.updated", entity=user, changes=user_changes)

    before = audit.snapshot(membership)
    membership.role = role
    membership.division_id = _division_for(ctx, role, data.division_id)
    membership.is_active = bool(data.is_active)
    ctx.session.flush()
    changes = audit.diff(before, audit.snapshot(membership))
    if changes:
        audit.record(ctx, "membership.updated", entity=membership, changes=changes)
    return membership


def reset_password(ctx: ServiceContext, membership_id: int, new_password: str) -> None:
    from chimera.services.auth import set_password

    membership = get_membership(ctx, membership_id)
    if membership.user.is_superadmin and not ctx.is_superadmin:
        raise ServiceError("Пароль системного администратора меняет только он сам.")
    set_password(ctx, membership.user, new_password, must_change=True)


# --- Заявки ------------------------------------------------------------------------


def list_requests(
    ctx: ServiceContext, *, status: AccessRequestStatus | None = AccessRequestStatus.PENDING
) -> list[AccessRequest]:
    ctx.require(Permission.REQUESTS_APPROVE)
    stmt = (
        select(AccessRequest)
        .options(joinedload(AccessRequest.user))
        .order_by(AccessRequest.created_at.desc())
    )
    if status is not None:
        stmt = stmt.where(AccessRequest.status == status)
    return list(ctx.session.scalars(stmt).unique())


def count_pending_requests(ctx: ServiceContext) -> int:
    if not ctx.can(Permission.REQUESTS_APPROVE):
        return 0
    return len(list_requests(ctx))


def _get_pending_request(ctx: ServiceContext, request_id: int) -> AccessRequest:
    ctx.require(Permission.REQUESTS_APPROVE)
    request = ctx.session.scalar(
        select(AccessRequest)
        .options(joinedload(AccessRequest.user))
        .where(AccessRequest.id == request_id)
    )
    if request is None:
        raise NotFound("Заявка не найдена")
    if request.status is not AccessRequestStatus.PENDING:
        raise ServiceError("Заявка уже рассмотрена.")
    return request


def approve_request(
    ctx: ServiceContext, request_id: int, role: str, division_id: int | None = None
) -> Membership:
    request = _get_pending_request(ctx, request_id)
    parsed_role = _parse_role(role)
    user = request.user
    user_before = audit.snapshot(user)
    if user.status in {UserStatus.PENDING, UserStatus.REJECTED}:
        user.status = UserStatus.ACTIVE
    request.status = AccessRequestStatus.APPROVED
    request.decided_by_id = ctx.user_id
    request.decided_at = utcnow()
    membership = Membership(
        user_id=user.id,
        organization_id=ctx.require_organization(),
        role=parsed_role,
        division_id=_division_for(ctx, parsed_role, division_id),
    )
    ctx.session.add(membership)
    ctx.session.flush()
    audit.record(ctx, "request.approved", entity=request, message=f"Роль: {parsed_role.value}")
    audit.record(ctx, "user.updated", entity=user, before=user_before, after=audit.snapshot(user))
    audit.record(ctx, "membership.created", entity=membership, after=audit.snapshot(membership))
    return membership


def reject_request(ctx: ServiceContext, request_id: int) -> AccessRequest:
    request = _get_pending_request(ctx, request_id)
    request.status = AccessRequestStatus.REJECTED
    request.decided_by_id = ctx.user_id
    request.decided_at = utcnow()
    user = request.user
    other_open = ctx.session.scalar(
        unscoped(
            select(AccessRequest.id).where(
                AccessRequest.user_id == user.id,
                AccessRequest.status == AccessRequestStatus.PENDING,
                AccessRequest.id != request.id,
            )
        )
    )
    has_membership = ctx.session.scalar(
        unscoped(select(Membership.id).where(Membership.user_id == user.id))
    )
    if user.status is UserStatus.PENDING and other_open is None and has_membership is None:
        user.status = UserStatus.REJECTED
    ctx.session.flush()
    audit.record(ctx, "request.rejected", entity=request)
    return request
