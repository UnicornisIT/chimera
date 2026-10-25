"""Сервисы и роуты администрирования: организации, подразделения, пользователи, аудит."""

from __future__ import annotations

import pytest
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from chimera.core import audit
from chimera.core.audit import AuditLog, diff, snapshot
from chimera.core.context import ServiceError
from chimera.core.db import Database
from chimera.core.permissions import Role
from chimera.core.tenancy import unscoped
from chimera.core.terms import OrgType
from chimera.models import Division, User
from chimera.services import audit_log, organizations, users
from tests.conftest import PASSWORD, World, login, post


def test_division_hierarchy_rules(session: Session, world: World) -> None:
    ctx = world.ctx(session, world.university, Role.ADMIN)
    faculty = organizations.create_division(
        ctx, organizations.DivisionData(name="Факультет математики", kind="division")
    )
    chair = organizations.create_division(
        ctx,
        organizations.DivisionData(name="Кафедра алгебры", kind="department", parent_id=faculty.id),
    )
    with pytest.raises(ServiceError, match="верхнего уровня"):
        organizations.create_division(
            ctx,
            organizations.DivisionData(name="Вложенный", kind="division", parent_id=faculty.id),
        )
    with pytest.raises(ServiceError, match="только внутри"):
        organizations.create_division(
            ctx,
            organizations.DivisionData(name="Ещё", kind="department", parent_id=chair.id),
        )
    with pytest.raises(ServiceError, match="уже есть"):
        organizations.create_division(
            ctx, organizations.DivisionData(name="Факультет математики", kind="division")
        )
    with pytest.raises(ServiceError, match="вложенные"):
        organizations.delete_division(ctx, faculty.id)
    tree = organizations.division_tree(ctx)
    assert [n.division.name for n in tree] == ["Факультет математики"]
    assert [c.division.name for c in tree[0].children] == ["Кафедра алгебры"]


def test_audit_records_before_and_after(session: Session, world: World) -> None:
    ctx = world.ctx(session, world.college, Role.ADMIN)
    division = organizations.create_division(
        ctx, organizations.DivisionData(name="Отделение туризма", kind="division")
    )
    organizations.update_division(
        ctx,
        division.id,
        organizations.DivisionData(name="Отделение сервиса и туризма", kind="division"),
    )
    session.commit()
    entry = session.scalars(select(AuditLog).where(AuditLog.action == "division.updated")).one()
    assert entry.changes == {
        "name": {"old": "Отделение туризма", "new": "Отделение сервиса и туризма"}
    }
    assert entry.organization_id == world.college.organization.id
    assert entry.real_role == "admin"
    assert entry.username == "col.admin"


def test_sensitive_fields_are_masked_in_audit(session: Session, world: World) -> None:
    user = session.get(User, world.college.users[Role.VIEWER].id)
    assert user is not None
    before = snapshot(user)
    user.password_hash = "changed"
    changes = diff(before, snapshot(user))
    assert changes == {"password_hash": dict(audit.SENSITIVE_MARK)}


def test_organization_update_and_terms(session: Session, world: World) -> None:
    ctx = world.ctx(session, world.college, Role.ADMIN)
    org = organizations.update_organization(
        ctx,
        world.college.organization.id,
        organizations.OrganizationData(
            name="Колледж связи",
            short_name="КС",
            org_type="university",
            student_term="learner",
            head_full_name="Иванов И. И.",
            head_position="Директор",
        ),
    )
    assert org.org_type is OrgType.UNIVERSITY
    assert str(org.terms.division) == "Факультет"
    assert str(org.terms.student) == "Обучающийся"
    with pytest.raises(ServiceError, match="тип"):
        organizations.update_organization(
            ctx,
            org.id,
            organizations.OrganizationData(name="X", short_name="X", org_type="school"),
        )


def test_superadmin_creates_organization(
    client: FlaskClient, database: Database, world: World
) -> None:
    login(client, "root")
    response = post(
        client,
        "/admin/organizations/new",
        {
            "name": "Новый технический колледж",
            "short_name": "НТК",
            "org_type": "college",
            "is_active": "1",
        },
        csrf_from="/admin/organizations/new",
    )
    assert response.status_code == 302  # type: ignore[attr-defined]
    page = client.get("/admin/organizations").get_data(as_text=True)
    assert "НТК" in page
    switcher = client.get("/").get_data(as_text=True)
    assert "org-switcher" in switcher


def test_regular_admin_cannot_create_organization(client: FlaskClient, world: World) -> None:
    login(client, "col.admin")
    assert client.get("/admin/organizations/new").status_code == 403
    assert client.get("/admin/backups").status_code == 403


def test_terms_follow_current_organization(client: FlaskClient, world: World) -> None:
    login(client, "uni.admin")
    assert "Факультеты и кафедры" in client.get("/admin/divisions").get_data(as_text=True)
    client2 = client.application.test_client()
    login(client2, "col.admin")
    page = client2.get("/admin/divisions").get_data(as_text=True)
    assert "Отделения и цикловые комиссии" in page


def test_create_member_and_role_change(session: Session, world: World) -> None:
    ctx = world.ctx(session, world.college, Role.ADMIN)
    division = organizations.create_division(
        ctx, organizations.DivisionData(name="Отделение медицины", kind="division")
    )
    member = users.create_member(
        ctx,
        users.NewUserData(
            username="new.curator",
            full_name="Новикова Ольга Игоревна",
            email="new@example.org",
            password=PASSWORD,
            role="curator",
            division_id=division.id,
        ),
    )
    assert member.division_id == division.id
    assert member.user.must_change_password
    updated = users.update_member(
        ctx,
        member.id,
        users.MembershipData(
            role="viewer",
            division_id=division.id,
            is_active=True,
            full_name="Новикова Ольга Игоревна",
            email=None,
        ),
    )
    assert updated.role is Role.VIEWER
    assert updated.division_id is None  # наблюдателю подразделение не назначается
    with pytest.raises(ServiceError, match="уже состоит"):
        users.create_member(
            ctx,
            users.NewUserData("new.curator", "", None, "", "curator"),
        )


def test_existing_user_can_join_second_organization(session: Session, world: World) -> None:
    ctx = world.ctx(session, world.college, Role.ADMIN)
    member = users.create_member(ctx, users.NewUserData("uni.curator", "", None, "", "head"))
    assert member.user_id == world.university.users[Role.CURATOR].id
    assert member.role is Role.HEAD


def test_admin_cannot_demote_self(session: Session, world: World) -> None:
    ctx = world.ctx(session, world.college, Role.ADMIN)
    own = next(m for m in users.list_members(ctx) if m.user.username == "col.admin")
    with pytest.raises(ServiceError, match="себя"):
        users.update_member(
            ctx,
            own.id,
            users.MembershipData("curator", None, True, own.user.full_name, None),
        )


def test_audit_page_filters(client: FlaskClient, session: Session, world: World) -> None:
    ctx = world.ctx(session, world.college, Role.ADMIN)
    organizations.create_division(
        ctx, organizations.DivisionData(name="Отделение дизайна", kind="division")
    )
    session.commit()
    login(client, "col.admin")
    page = client.get("/admin/audit?action=division.created").get_data(as_text=True)
    assert "division.created" in page
    assert "Отделение дизайна" in page
    result = audit_log.search(ctx, audit_log.AuditFilter(action="division"))
    assert result.total == 1


def test_admin_pages_render(client: FlaskClient, session: Session, world: World) -> None:
    ctx = world.ctx(session, world.college, Role.ADMIN)
    division = organizations.create_division(
        ctx, organizations.DivisionData(name="Отделение права", kind="division")
    )
    session.commit()
    member_id = users.list_members(ctx)[0].id
    login(client, "col.admin")
    for url in [
        "/admin/",
        "/admin/organizations",
        f"/admin/organizations/{world.college.organization.id}",
        "/admin/divisions",
        f"/admin/divisions/{division.id}",
        "/admin/users",
        "/admin/users/new",
        f"/admin/users/{member_id}",
        "/admin/requests",
        "/admin/audit",
        "/admin/act-as",
        "/profile",
    ]:
        assert client.get(url).status_code == 200, url


def test_division_routes(client: FlaskClient, database: Database, world: World) -> None:
    login(client, "col.admin")
    post(
        client,
        "/admin/divisions",
        {"name": "Отделение ИТ", "kind": "division", "is_active": "1"},
        csrf_from="/admin/divisions",
    )
    with database.transaction() as s:
        division = s.scalars(unscoped(select(Division))).one()
    post(client, f"/admin/divisions/{division.id}/delete", csrf_from="/admin/divisions")
    with database.transaction() as s:
        assert s.scalars(unscoped(select(Division))).all() == []
