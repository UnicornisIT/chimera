"""Контекст запроса: пользователь, текущая организация, роль и режим «работать как»."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, TypeVar, cast

from flask import abort, current_app, g, redirect, request, session, url_for
from sqlalchemy.orm import Session

from chimera.core.context import ServiceContext
from chimera.core.db import Database
from chimera.core.permissions import Permission, Role, SystemPermission
from chimera.core.tenancy import bind_organization
from chimera.core.terms import Terms, terms_for
from chimera.models import Organization, User
from chimera.services import auth as auth_service
from chimera.services.impersonation import ActAs

F = TypeVar("F", bound=Callable[..., Any])

SESSION_USER = "user_id"
SESSION_ORG = "organization_id"
SESSION_ACT_AS = "act_as"
SESSION_AUTH_VERSION = "auth_version"

# Эти страницы доступны до обязательной смены пароля.
PASSWORD_CHANGE_ALLOWED = frozenset({"auth.change_password", "auth.logout", "static"})


@dataclass(slots=True)
class RequestState:
    user: User | None = None
    organization: Organization | None = None
    organizations: list[Organization] | None = None
    acting: ActAs | None = None
    terms: Terms | None = None


def database() -> Database:
    return cast(Database, current_app.extensions["chimera.db"])


def db() -> Session:
    if "db" not in g:
        g.db = database().session()
    return cast(Session, g.db)


def state() -> RequestState:
    if "chimera_state" not in g:
        g.chimera_state = RequestState()
    return cast(RequestState, g.chimera_state)


def ctx() -> ServiceContext:
    if "ctx" not in g:
        g.ctx = ServiceContext(session=db(), ip=client_ip())
    return cast(ServiceContext, g.ctx)


def client_ip() -> str | None:
    # ProxyFix (при работе за nginx) уже подставляет адрес клиента в remote_addr.
    return request.remote_addr


def _auth_version(user: User) -> str:
    # Смена пароля делает недействительными прочие сессии пользователя.
    stamp = user.password_changed_at.isoformat() if user.password_changed_at else ""
    return f"{user.id}:{stamp}"


def login_user(user: User) -> None:
    session.clear()
    session.permanent = True
    session[SESSION_USER] = user.id
    session[SESSION_AUTH_VERSION] = _auth_version(user)


def refresh_auth_version(user: User) -> None:
    session[SESSION_AUTH_VERSION] = _auth_version(user)


def load_request_context() -> None:
    """Вызывается в before_request: строит `ServiceContext` для сервисов."""
    st = state()
    session_db = db()
    user_id = session.get(SESSION_USER)
    user = session_db.get(User, user_id) if isinstance(user_id, int) else None
    if user is not None and (
        not user.is_active or session.get(SESSION_AUTH_VERSION) != _auth_version(user)
    ):
        session.clear()
        user = None
    if user is None:
        g.ctx = ServiceContext(session=session_db, ip=client_ip())
        return

    st.user = user
    organizations = auth_service.available_organizations(session_db, user)
    st.organizations = organizations
    org_id = session.get(SESSION_ORG)
    access = auth_service.resolve_access(
        session_db, user, org_id if isinstance(org_id, int) else None
    )
    if access is None:
        for candidate in organizations:
            access = auth_service.resolve_access(session_db, user, candidate.id)
            if access is not None:
                break
        session.pop(SESSION_ACT_AS, None)
    if access is None:
        session.pop(SESSION_ORG, None)
        g.ctx = ServiceContext(
            session=session_db,
            user_id=user.id,
            username=user.username,
            is_superadmin=user.is_superadmin,
            ip=client_ip(),
        )
        return

    session[SESSION_ORG] = access.organization.id
    bind_organization(session_db, access.organization.id)
    st.organization = access.organization
    st.terms = access.organization.terms

    acting = ActAs.from_session(session.get(SESSION_ACT_AS)) if access.role is Role.ADMIN else None
    if acting is None:
        session.pop(SESSION_ACT_AS, None)
    st.acting = acting
    g.ctx = ServiceContext.build(
        session_db,
        role=acting.role if acting else access.role,
        real_role=access.role,
        user_id=user.id,
        username=user.username,
        organization_id=access.organization.id,
        is_superadmin=user.is_superadmin,
        division_id=acting.division_id if acting else access.division_id,
        acting_as_user_id=acting.subject_user_id if acting else None,
        ip=client_ip(),
    )


def require_user() -> User:
    user = state().user
    if user is None:
        abort(401)
    return user


def current_terms() -> Terms:
    st = state()
    if st.terms is not None:
        return st.terms
    return terms_for("college")


def login_required(view: F) -> F:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if state().user is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        if ctx().organization_id is None and request.endpoint != "main.no_organization":
            return redirect(url_for("main.no_organization"))
        return view(*args, **kwargs)

    return cast(F, wrapped)


def permission_required(permission: Permission) -> Callable[[F], F]:
    def decorator(view: F) -> F:
        @login_required
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not ctx().can(permission):
                abort(403)
            return view(*args, **kwargs)

        return cast(F, wrapped)

    return decorator


def system_required(permission: SystemPermission) -> Callable[[F], F]:
    def decorator(view: F) -> F:
        @login_required
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not ctx().can_system(permission):
                abort(403)
            return view(*args, **kwargs)

        return cast(F, wrapped)

    return decorator


def form_int(name: str) -> int | None:
    raw = request.form.get(name, "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def form_bool(name: str) -> bool:
    return request.form.get(name) in {"1", "on", "true", "yes"}
