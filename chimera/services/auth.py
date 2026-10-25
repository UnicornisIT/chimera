"""Вход, пароли, саморегистрация и начальная настройка администратора."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from chimera.core import audit
from chimera.core.config import Settings
from chimera.core.context import ServiceContext, ServiceError
from chimera.core.db import utcnow
from chimera.core.permissions import Role
from chimera.core.security import hash_password, password_problems, verify_password
from chimera.core.tenancy import bind_organization, unscoped
from chimera.core.terms import OrgType
from chimera.core.validation import (
    clean_text,
    email_problem,
    normalize_email,
    normalize_username,
    optional_text,
    username_problem,
)
from chimera.models import (
    AccessRequest,
    LoginAttempt,
    Membership,
    Organization,
    User,
    UserStatus,
)

# Заранее посчитанный хэш: проверка несуществующего логина занимает то же время.
_DUMMY_HASH = hash_password("chimera-timing-equaliser")


class LoginThrottled(ServiceError):
    pass


class AuthenticationFailed(ServiceError):
    pass


@dataclass(frozen=True, slots=True)
class LoginPolicy:
    max_attempts: int
    window_seconds: int

    @classmethod
    def from_settings(cls, settings: Settings) -> LoginPolicy:
        return cls(settings.login_max_attempts, settings.login_window_seconds)


def _recent_failures(session: Session, username: str, ip: str | None, window: int) -> int:
    since = utcnow() - timedelta(seconds=window)
    by_user = session.scalar(
        select(func.count(LoginAttempt.id)).where(
            LoginAttempt.username == username, LoginAttempt.created_at >= since
        )
    )
    by_ip = 0
    if ip:
        by_ip = (
            session.scalar(
                select(func.count(LoginAttempt.id)).where(
                    LoginAttempt.ip == ip, LoginAttempt.created_at >= since
                )
            )
            or 0
        ) // 3  # с одного адреса допускается больше попыток (общий NAT)
    return max(by_user or 0, by_ip)


def authenticate(
    session: Session,
    username: str,
    password: str,
    *,
    ip: str | None,
    policy: LoginPolicy,
) -> User:
    """Проверяет логин и пароль.

    Неудачная попытка тоже пишется в БД, поэтому вызывающий код фиксирует
    транзакцию (commit) и при ошибке входа.
    """
    login = normalize_username(username)
    if _recent_failures(session, login, ip, policy.window_seconds) >= policy.max_attempts:
        raise LoginThrottled("Слишком много неудачных попыток. Повторите вход позже.")

    user = session.scalar(select(User).where(User.username == login))
    valid = verify_password(user.password_hash if user else _DUMMY_HASH, password)
    if user is None or not valid:
        session.add(LoginAttempt(username=login, ip=ip))
        audit.record(
            ServiceContext(session=session, username=login, ip=ip),
            "auth.login_failed",
            entity_type="User",
            entity_id=user.id if user else None,
        )
        raise AuthenticationFailed("Неверный логин или пароль.")
    if user.status is UserStatus.PENDING:
        raise AuthenticationFailed("Заявка на регистрацию ещё не одобрена администратором.")
    if user.status is not UserStatus.ACTIVE:
        raise AuthenticationFailed("Учётная запись отключена. Обратитесь к администратору.")

    session.execute(delete(LoginAttempt).where(LoginAttempt.username == login))
    user.last_login_at = utcnow()
    audit.record(
        ServiceContext(session=session, user_id=user.id, username=user.username, ip=ip),
        "auth.login",
        entity=user,
    )
    return user


def change_password(ctx: ServiceContext, user: User, current: str, new: str) -> None:
    if not verify_password(user.password_hash, current):
        raise ServiceError("Текущий пароль указан неверно.")
    if current == new:
        raise ServiceError("Новый пароль должен отличаться от текущего.")
    set_password(ctx, user, new, must_change=False, action="auth.password_changed")


def set_password(
    ctx: ServiceContext,
    user: User,
    new: str,
    *,
    must_change: bool,
    action: str = "user.password_reset",
) -> None:
    problems = password_problems(new, username=user.username)
    if problems:
        raise ServiceError(" ".join(problems))
    before = audit.snapshot(user)
    user.password_hash = hash_password(new)
    user.must_change_password = must_change
    user.password_changed_at = utcnow()
    audit.record(ctx, action, entity=user, before=before, after=audit.snapshot(user))


def _org_ids_open_for_registration(session: Session) -> set[int]:
    return set(session.scalars(select(Organization.id).where(Organization.is_active.is_(True))))


def register(
    session: Session,
    *,
    username: str,
    full_name: str,
    email: str | None,
    password: str,
    organization_id: int,
    comment: str | None,
    ip: str | None,
) -> User:
    """Саморегистрация: пользователь без роли и заявка, которую одобряет администратор."""
    login = normalize_username(username)
    problems = [p for p in (username_problem(login),) if p]
    name = clean_text(full_name, max_length=300)
    if not name:
        problems.append("Укажите ФИО.")
    mail = normalize_email(email)
    if (problem := email_problem(mail)) is not None:
        problems.append(problem)
    problems.extend(password_problems(password, username=login))
    if organization_id not in _org_ids_open_for_registration(session):
        problems.append("Выберите организацию из списка.")
    if session.scalar(select(User.id).where(User.username == login)) is not None:
        problems.append("Такой логин уже занят.")
    if problems:
        raise ServiceError(" ".join(problems))

    user = User(
        username=login,
        full_name=name,
        email=mail,
        password_hash=hash_password(password),
        status=UserStatus.PENDING,
    )
    session.add(user)
    session.flush()
    bind_organization(session, organization_id)
    request = AccessRequest(
        user_id=user.id, organization_id=organization_id, comment=optional_text(comment)
    )
    session.add(request)
    session.flush()
    ctx = ServiceContext(
        session=session,
        user_id=user.id,
        username=user.username,
        organization_id=organization_id,
        ip=ip,
    )
    audit.record(ctx, "auth.registration_requested", entity=request)
    return user


def registration_organizations(session: Session) -> list[Organization]:
    return list(
        session.scalars(
            select(Organization)
            .where(Organization.is_active.is_(True))
            .order_by(Organization.short_name)
        )
    )


def user_memberships(session: Session, user: User) -> list[Membership]:
    """Активные членства пользователя во всех организациях (для переключателя)."""
    stmt = (
        select(Membership)
        .join(Organization, Organization.id == Membership.organization_id)
        .where(
            Membership.user_id == user.id,
            Membership.is_active.is_(True),
            Organization.is_active.is_(True),
        )
        .order_by(Organization.short_name)
    )
    return list(session.scalars(unscoped(stmt)))


def available_organizations(session: Session, user: User) -> list[Organization]:
    if user.is_superadmin:
        return list(session.scalars(select(Organization).order_by(Organization.short_name)))
    return [m.organization for m in user_memberships(session, user)]


@dataclass(frozen=True, slots=True)
class OrgAccess:
    organization: Organization
    role: Role
    division_id: int | None


def resolve_access(session: Session, user: User, organization_id: int | None) -> OrgAccess | None:
    """Роль пользователя в организации. Суперадмин — администратор любой организации."""
    if organization_id is None:
        return None
    organization = session.get(Organization, organization_id)
    if organization is None:
        return None
    membership = session.scalar(
        unscoped(
            select(Membership).where(
                Membership.user_id == user.id,
                Membership.organization_id == organization_id,
                Membership.is_active.is_(True),
            )
        )
    )
    if user.is_superadmin:
        division_id = membership.division_id if membership else None
        return OrgAccess(organization, Role.ADMIN, division_id)
    if membership is None or not organization.is_active:
        return None
    return OrgAccess(organization, membership.role, membership.division_id)


def ensure_initial_admin(session: Session, settings: Settings) -> User | None:
    """Создаёт первую организацию и администратора, если пользователей ещё нет."""
    if session.scalar(select(func.count(User.id))):
        return None
    if not settings.admin_default_password:
        return None
    problems = password_problems(settings.admin_default_password)
    if problems:
        raise ServiceError("ADMIN_DEFAULT_PASSWORD: " + " ".join(problems))

    organization = session.scalar(select(Organization).order_by(Organization.id))
    if organization is None:
        organization = Organization(
            name=settings.default_organization_name,
            short_name=settings.default_organization_name[:150],
            org_type=OrgType(settings.default_organization_type),
        )
        session.add(organization)
        session.flush()
    admin = User(
        username=normalize_username(settings.admin_default_username),
        full_name="Администратор",
        password_hash=hash_password(settings.admin_default_password),
        is_superadmin=True,
        must_change_password=True,
    )
    session.add(admin)
    session.flush()
    bind_organization(session, organization.id)
    session.add(Membership(user_id=admin.id, organization_id=organization.id, role=Role.ADMIN))
    ctx = ServiceContext.system(session, organization.id)
    audit.record(ctx, "system.initial_admin_created", entity=admin)
    session.flush()
    return admin
