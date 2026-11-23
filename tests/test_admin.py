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
from chimera.core.tenancy import bind_organization, unscoped
from chimera.core.terms import OrgType
from chimera.models import Division, Membership, User
from chimera.services import audit_log, organizations, users
from tests.conftest import PASSWORD, World, csrf, login, post


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


@pytest.mark.parametrize("protected", ["shared", "inactive_membership", "system"])
def test_organization_admin_cannot_change_protected_account(
    client: FlaskClient, session: Session, world: World, protected: str
) -> None:
    user = world.superadmin if protected == "system" else world.university.users[Role.CURATOR]
    if protected == "inactive_membership":
        bind_organization(session, world.university.organization.id)
        other = session.scalars(select(Membership).where(Membership.user_id == user.id)).one()
        other.is_active = False
        session.flush()
    bind_organization(session, world.college.organization.id)
    membership = Membership(
        organization_id=world.college.organization.id, user_id=user.id, role=Role.VIEWER
    )
    session.add(membership)
    session.commit()
    original_hash = user.password_hash
    original_name = user.full_name
    edit = f"/admin/users/{membership.id}"

    login(client, "col.admin")
    page = client.get(edit).get_data(as_text=True)
    assert 'id="full_name" name="full_name"' in page and "readonly" in page
    assert f'action="{edit}/password"' not in page
    token = csrf(client, edit)
    assert (
        client.post(
            edit + "/password", data={"csrf_token": token, "password": PASSWORD, "confirmed": "1"}
        ).status_code
        == 403
    )
    membership_data = {
        "csrf_token": token,
        "role": "curator",
        "is_active": "1",
        "email": user.email or "",
    }
    assert client.post(edit, data={**membership_data, "full_name": "Другое имя"}).status_code == 403
    assert (
        client.post(edit, data={**membership_data, "full_name": original_name}).status_code == 302
    )
    updated_user = session.get(User, user.id)
    assert updated_user is not None
    session.refresh(membership)
    assert (updated_user.password_hash, updated_user.full_name) == (original_hash, original_name)
    assert membership.role is Role.CURATOR


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

    for number in range(51):
        audit.record(ctx, "division.reviewed", message=f"Проверка {number:02d}")
    other = world.ctx(session, world.university, Role.ADMIN)
    audit.record(other, "division.reviewed", message="Проверка другой организации")
    session.commit()
    filters = {"q": "ПРОВЕРКА", "action": "division.reviewed"}
    filtered = client.get("/admin/audit", query_string=filters).get_data(as_text=True)
    assert "Страница 1 из 2" in filtered
    assert "page=2" in filtered and "action=division.reviewed" in filtered
    assert "другой организации" not in filtered
    second = client.get("/admin/audit", query_string={**filters, "page": 2})
    assert "Проверка 00" in second.get_data(as_text=True)
    actors = client.get("/admin/audit", query_string={**filters, "actor": "COL.ADMIN"})
    assert "Страница 1 из 2" in actors.get_data(as_text=True)
    for parameters in (
        {"q": "\0", "action": "\0", "user_id": "999999999999999999999", "page": "-1"},
        {"page": "no", "limit": "-1", "from": "unknown", "to": "9999-12-31"},
        {"actor": "uni.admin", "page": "999"},
    ):
        response = client.get("/admin/audit", query_string=parameters)
        assert response.status_code == 200
        assert "Проверка другой организации" not in response.get_data(as_text=True)
    assert "Ничего не найдено." in response.get_data(as_text=True)


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
    target = f"/admin/divisions/{division.id}/delete"
    token = csrf(client, "/admin/divisions")
    assert client.post(target, data={"csrf_token": token}).status_code == 400
    assert client.post(target, data={"confirmed": "1"}).status_code == 400
    with database.transaction() as s:
        assert s.scalars(unscoped(select(Division))).one().id == division.id
    assert client.get(target).status_code == 405
    assert client.post(target, data={"csrf_token": token, "confirmed": "1"}).status_code == 302
    with database.transaction() as s:
        assert s.scalars(unscoped(select(Division))).all() == []


def test_user_form_error_keeps_nonsecret_fields(client: FlaskClient, world: World) -> None:
    login(client, "col.admin")
    path = "/admin/users/new"
    response = client.post(
        path,
        data={
            "csrf_token": csrf(client, path),
            "username": "synthetic.user",
            "full_name": "Сотрудник",
            "password": "secret",
            "role": "head",
        },
        headers={"X-Chimera-Form": "1"},
    )
    html = response.get_data(as_text=True)
    assert response.headers["X-Chimera-Form"] == "1"
    assert 'value="synthetic.user"' in html
    assert 'value="Сотрудник"' in html
    assert 'value="head" selected' in html
    assert 'aria-describedby="password-hint password-error"' in html
    assert 'value="secret"' not in html


def test_filtered_user_pagination_is_scoped(
    client: FlaskClient, session: Session, world: World
) -> None:
    for organization, count in [(world.college, 51), (world.university, 1)]:
        bind_organization(session, organization.organization.id)
        for number in range(count):
            user = User(
                username=f"page.{organization.organization.id}.{number}",
                full_name=f"Сотрудник {number:02d}",
                email=f"page{number}@example.org",
                password_hash=world.superadmin.password_hash,
            )
            session.add(user)
            session.flush()
            session.add(
                Membership(
                    organization_id=organization.organization.id, user_id=user.id, role=Role.CURATOR
                )
            )
        session.flush()
    session.commit()
    ctx = world.ctx(session, world.college, Role.ADMIN)
    members = users.search_members(ctx, query="СОТРУДНИК", role=Role.CURATOR, page=1)
    assert members.total == 51
    login(client, "col.admin")
    first = client.get("/admin/users", query_string={"q": "СОТРУДНИК", "role": "curator"})
    html = first.get_data(as_text=True)
    assert "Страница 1 из 2" in html
    assert "Сотрудник 49" in html and "Сотрудник 50" not in html
    assert "page=2" in html and "role=curator" in html
    second = client.get(
        "/admin/users", query_string={"q": "сотрудник", "role": "curator", "page": 2}
    ).get_data(as_text=True)
    assert "Сотрудник 50" in second and "Сотрудник 49" not in second
    assert "Ничего не найдено." in client.get("/admin/users?q=%25").get_data(as_text=True)
    malformed = client.get(
        "/admin/users",
        query_string={"q": "сотрудник\0", "role": "unknown", "page": "x", "limit": 1},
    )
    assert malformed.status_code == 200
    assert malformed.get_data(as_text=True).count("<tr>") == 51
    last_page = {"q": "сотрудник", "role": "curator", "page": 999}
    assert "Страница 2 из 2" in client.get("/admin/users", query_string=last_page).get_data(
        as_text=True
    )
    extra = users.search_members(ctx, query="Сотрудник 50", role=Role.CURATOR, page=1).items[0]
    session.delete(extra)
    session.commit()
    after_removal = client.get("/admin/users", query_string=last_page).get_data(as_text=True)
    assert "Страница 2 из 2" not in after_removal
    assert "Сотрудник 49" in after_removal


def test_enhanced_post_preserves_csrf_and_redirect(client: FlaskClient, world: World) -> None:
    login(client, "col.admin")
    path = "/admin/divisions"
    fields = {"name": "Учебное отделение", "kind": "division", "is_active": "1"}
    headers = {"X-Chimera-Form": "1"}
    denied = client.post(path, data=fields, headers=headers)
    assert denied.status_code == 400
    fields["csrf_token"] = csrf(client, path)
    saved = client.post(path, data=fields, headers=headers)
    assert saved.status_code == 204
    assert saved.headers["X-Chimera-Location"] == path
    assert "Подразделение добавлено." in client.get(path).get_data(as_text=True)


def test_failed_user_edit_rolls_back_account_and_membership(
    client: FlaskClient, session: Session, world: World
) -> None:
    context = world.ctx(session, world.college, Role.ADMIN)
    member = next(m for m in users.list_members(context) if m.role is Role.CURATOR)
    profile = member.user
    original = profile.full_name, profile.email, member.role, member.division_id, member.is_active
    path = f"/admin/users/{member.id}"
    login(client, "col.admin")
    response = client.post(
        path,
        data={
            "csrf_token": csrf(client, path),
            "role": "head",
            "full_name": "Изменённое имя",
            "email": "changed@example.org",
            "division_id": "2147483647",
        },
    )
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert 'aria-describedby="division_id-error"' in html
    assert 'value="Изменённое имя"' in html
    assert 'value="changed@example.org"' in html
    session.expire_all()
    assert (
        profile.full_name,
        profile.email,
        member.role,
        member.division_id,
        member.is_active,
    ) == original
    assert session.scalar(select(AuditLog.id).where(AuditLog.action == "user.updated")) is None
