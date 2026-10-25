"""Вход, выход, смена пароля, ограничение попыток, саморегистрация, начальный admin."""

from __future__ import annotations

from dataclasses import replace

import pytest
from flask.testing import FlaskClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from chimera.core.audit import AuditLog
from chimera.core.config import Settings
from chimera.core.context import ServiceError
from chimera.core.db import Database
from chimera.core.permissions import Role
from chimera.core.security import verify_password
from chimera.core.tenancy import unscoped
from chimera.models import AccessRequest, Membership, Organization, User, UserStatus
from chimera.services import auth as auth_service
from chimera.services import users as users_service
from tests.conftest import PASSWORD, World, csrf, login, post


def test_login_and_logout(client: FlaskClient, world: World) -> None:
    login(client, "col.curator")
    page = client.get("/")
    assert page.status_code == 200
    assert world.college.users[Role.CURATOR].full_name in page.get_data(as_text=True)
    post(client, "/logout")
    assert client.get("/").status_code == 302


def test_wrong_password(client: FlaskClient, world: World) -> None:
    token = csrf(client)
    response = client.post(
        "/login", data={"username": "col.curator", "password": "wrong-pass-1", "csrf_token": token}
    )
    assert response.status_code == 200
    assert "Неверный логин или пароль" in response.get_data(as_text=True)


def test_login_is_throttled(client: FlaskClient, world: World) -> None:
    for _ in range(3):
        token = csrf(client)
        client.post(
            "/login", data={"username": "col.viewer", "password": "nope-12345", "csrf_token": token}
        )
    token = csrf(client)
    response = client.post(
        "/login", data={"username": "col.viewer", "password": PASSWORD, "csrf_token": token}
    )
    assert "Слишком много неудачных попыток" in response.get_data(as_text=True)


def test_failed_logins_are_audited(client: FlaskClient, database: Database, world: World) -> None:
    token = csrf(client)
    client.post("/login", data={"username": "ghost", "password": "x", "csrf_token": token})
    with database.transaction() as s:
        assert (
            s.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == "auth.login_failed"))
            == 1
        )


def test_open_redirect_is_blocked(client: FlaskClient, world: World) -> None:
    token = csrf(client)
    response = client.post(
        "/login?next=https://evil.example/",
        data={"username": "col.curator", "password": PASSWORD, "csrf_token": token},
    )
    assert response.headers["Location"] == "/"


def test_disabled_user_cannot_login(client: FlaskClient, database: Database, world: World) -> None:
    with database.transaction() as s:
        user = s.get(User, world.college.users[Role.VIEWER].id)
        assert user is not None
        user.status = UserStatus.DISABLED
    token = csrf(client)
    response = client.post(
        "/login", data={"username": "col.viewer", "password": PASSWORD, "csrf_token": token}
    )
    assert "отключена" in response.get_data(as_text=True)


def test_forced_password_change(client: FlaskClient, database: Database, world: World) -> None:
    with database.transaction() as s:
        user = s.get(User, world.college.users[Role.HEAD].id)
        assert user is not None
        user.must_change_password = True
    login(client, "col.head")
    assert client.get("/").headers["Location"].endswith("/password")
    assert client.get("/admin/").headers["Location"].endswith("/password")
    response = post(
        client,
        "/password",
        {
            "current_password": PASSWORD,
            "new_password": "Brand-new-456",
            "new_password_confirm": "Brand-new-456",
        },
        csrf_from="/password",
    )
    assert response.headers["Location"] == "/"  # type: ignore[attr-defined]
    assert client.get("/").status_code == 200
    with database.transaction() as s:
        user = s.get(User, world.college.users[Role.HEAD].id)
        assert user is not None
        assert not user.must_change_password
        assert verify_password(user.password_hash, "Brand-new-456")


def test_password_change_invalidates_other_sessions(
    app: object, client: FlaskClient, world: World
) -> None:
    other = client.application.test_client()
    login(client, "col.starosta")
    login(other, "col.starosta")
    post(
        client,
        "/password",
        {
            "current_password": PASSWORD,
            "new_password": "Another-789x",
            "new_password_confirm": "Another-789x",
        },
        csrf_from="/password",
    )
    assert client.get("/").status_code == 200
    assert other.get("/").status_code == 302


def test_registration_and_approval_flow(
    client: FlaskClient, database: Database, session: Session, world: World
) -> None:
    org_id = world.college.organization.id
    token = csrf(client, "/register")
    response = client.post(
        "/register",
        data={
            "csrf_token": token,
            "organization_id": org_id,
            "full_name": "Сидорова Анна Викторовна",
            "username": "sidorova",
            "email": "sidorova@example.org",
            "password": "Register-123",
            "password_confirm": "Register-123",
            "comment": "Куратор 1 курса",
        },
    )
    assert response.status_code == 302

    token = csrf(client)
    pending = client.post(
        "/login", data={"username": "sidorova", "password": "Register-123", "csrf_token": token}
    )
    assert "ещё не одобрена" in pending.get_data(as_text=True)

    ctx = world.ctx(session, world.college, Role.ADMIN)
    requests = users_service.list_requests(ctx)
    assert [r.user.username for r in requests] == ["sidorova"]
    users_service.approve_request(ctx, requests[0].id, "curator")
    session.commit()

    login(client, "sidorova", "Register-123")
    assert client.get("/").status_code == 200
    with database.transaction() as s:
        membership = s.scalar(
            unscoped(select(Membership).join(User).where(User.username == "sidorova"))
        )
        assert membership is not None
        assert membership.role is Role.CURATOR
        assert membership.organization_id == org_id


def test_registration_validation(session: Session, world: World) -> None:
    with pytest.raises(ServiceError, match="уже занят"):
        auth_service.register(
            session,
            username="col.admin",
            full_name="Тест",
            email=None,
            password="Register-123",
            organization_id=world.college.organization.id,
            comment=None,
            ip=None,
        )
    with pytest.raises(ServiceError, match="организацию"):
        auth_service.register(
            session,
            username="newbie",
            full_name="Тест",
            email=None,
            password="Register-123",
            organization_id=999999,
            comment=None,
            ip=None,
        )


def test_reject_request(session: Session, world: World) -> None:
    auth_service.register(
        session,
        username="rejectme",
        full_name="Отказ Тестович",
        email=None,
        password="Register-123",
        organization_id=world.college.organization.id,
        comment=None,
        ip=None,
    )
    session.commit()
    ctx = world.ctx(session, world.college, Role.ADMIN)
    request = users_service.list_requests(ctx)[0]
    users_service.reject_request(ctx, request.id)
    session.commit()
    user = session.scalar(select(User).where(User.username == "rejectme"))
    assert user is not None and user.status is UserStatus.REJECTED
    with pytest.raises(ServiceError, match="рассмотрена"):
        users_service.reject_request(ctx, request.id)
    assert session.scalar(unscoped(select(func.count(AccessRequest.id)))) == 1


def test_initial_admin_bootstrap(database: Database, settings: Settings) -> None:
    configured = replace(
        settings,
        admin_default_password="Initial-pass-1",
        default_organization_name="Тестовый колледж",
    )
    with database.transaction() as s:
        admin = auth_service.ensure_initial_admin(s, configured)
        assert admin is not None
        assert admin.is_superadmin and admin.must_change_password
        org = s.scalar(select(Organization))
        assert org is not None and org.name == "Тестовый колледж"
    with database.transaction() as s:
        assert auth_service.ensure_initial_admin(s, configured) is None


def test_initial_admin_requires_strong_password(database: Database, settings: Settings) -> None:
    with database.transaction() as s, pytest.raises(ServiceError):
        auth_service.ensure_initial_admin(s, replace(settings, admin_default_password="123"))
