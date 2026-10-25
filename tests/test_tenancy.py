"""Изоляция организаций: сервисы, ORM-фильтр и HTTP-роуты."""

from __future__ import annotations

import pytest
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from chimera.core import audit
from chimera.core.context import NotFound
from chimera.core.permissions import Role
from chimera.core.tenancy import TenancyError, bind_organization, unscoped
from chimera.models import Division, DivisionKind, Membership
from chimera.services import audit_log, organizations, users
from tests.conftest import World, login


def _division(session: Session, world: World, which: str, name: str) -> int:
    org = getattr(world, which)
    ctx = world.ctx(session, org, Role.ADMIN)
    division = organizations.create_division(
        ctx, organizations.DivisionData(name=name, kind="division")
    )
    session.commit()
    return division.id


def test_divisions_are_invisible_across_organizations(session: Session, world: World) -> None:
    uni_id = _division(session, world, "university", "Факультет физики")
    _division(session, world, "college", "Отделение экономики")

    ctx = world.ctx(session, world.college, Role.ADMIN)
    names = [d.name for d in organizations.list_divisions(ctx)]
    assert names == ["Отделение экономики"]
    with pytest.raises(NotFound):
        organizations.get_division(ctx, uni_id)
    session.expunge_all()
    assert session.get(Division, uni_id) is None


def test_orm_filter_applies_to_plain_select(session: Session, world: World) -> None:
    _division(session, world, "university", "Факультет химии")
    session.expunge_all()
    bind_organization(session, world.college.organization.id)
    assert session.scalars(select(Division)).all() == []
    assert len(session.scalars(unscoped(select(Division))).all()) == 1


def test_query_without_organization_is_an_error(session: Session, world: World) -> None:
    bind_organization(session, None)
    with pytest.raises(TenancyError):
        session.scalars(select(Division)).all()


def test_insert_into_foreign_organization_is_rejected(session: Session, world: World) -> None:
    bind_organization(session, world.college.organization.id)
    session.add(
        Division(
            organization_id=world.university.organization.id,
            kind=DivisionKind.DIVISION,
            name="Чужой факультет",
        )
    )
    with pytest.raises(TenancyError):
        session.flush()


def test_new_records_get_current_organization(session: Session, world: World) -> None:
    bind_organization(session, world.college.organization.id)
    division = Division(kind=DivisionKind.DIVISION, name="Новое отделение")
    session.add(division)
    session.flush()
    assert division.organization_id == world.college.organization.id


def test_moving_between_organizations_is_rejected(session: Session, world: World) -> None:
    div_id = _division(session, world, "college", "Отделение права")
    bind_organization(session, world.college.organization.id)
    division = session.scalars(select(Division).where(Division.id == div_id)).one()
    division.organization_id = world.university.organization.id
    with pytest.raises(TenancyError):
        session.flush()


def test_members_list_is_isolated(session: Session, world: World) -> None:
    ctx = world.ctx(session, world.college, Role.ADMIN)
    usernames = {m.user.username for m in users.list_members(ctx)}
    assert usernames
    assert all(name.startswith("col.") for name in usernames)


def test_membership_of_other_org_is_not_found(session: Session, world: World) -> None:
    bind_organization(session, world.university.organization.id)
    uni_membership = session.scalars(select(Membership)).first()
    assert uni_membership is not None
    ctx = world.ctx(session, world.college, Role.ADMIN)
    with pytest.raises(NotFound):
        users.get_membership(ctx, uni_membership.id)


def test_audit_log_is_isolated(session: Session, world: World) -> None:
    uni = world.ctx(session, world.university, Role.ADMIN)
    audit.record(uni, "test.secret_event", message="только вуз")
    session.commit()
    college = world.ctx(session, world.college, Role.ADMIN)
    page = audit_log.search(college, audit_log.AuditFilter(query="secret_event"))
    assert page.total == 0
    uni = world.ctx(session, world.university, Role.ADMIN)
    assert audit_log.search(uni, audit_log.AuditFilter(query="secret_event")).total == 1


def test_routes_do_not_leak_other_organization(
    client: FlaskClient, session: Session, world: World
) -> None:
    uni_div = _division(session, world, "university", "Секретный факультет")
    bind_organization(session, world.university.organization.id)
    uni_member = session.scalars(select(Membership)).first()
    assert uni_member is not None
    login(client, "col.admin")
    assert client.get(f"/admin/divisions/{uni_div}").status_code == 404
    assert client.get(f"/admin/users/{uni_member.id}").status_code == 404
    assert client.get(f"/admin/organizations/{world.university.organization.id}").status_code == 404
    page = client.get("/admin/divisions").get_data(as_text=True)
    assert "Секретный факультет" not in page
    users_page = client.get("/admin/users").get_data(as_text=True)
    assert "uni.curator" not in users_page


def test_organization_switch_requires_membership(client: FlaskClient, world: World) -> None:
    from tests.conftest import post

    login(client, "col.curator")
    response = post(
        client, "/organization/switch", {"organization_id": world.university.organization.id}
    )
    assert response.status_code == 403  # type: ignore[attr-defined]
