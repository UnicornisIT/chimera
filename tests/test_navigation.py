"""Ссылки оболочки соответствуют серверному доступу в текущей организации."""

import pytest
from flask.testing import FlaskClient
from sqlalchemy.orm import Session

from chimera.core.permissions import Role
from chimera.core.tenancy import bind_organization
from chimera.models import Membership
from tests.conftest import World, csrf, login

MANAGEMENT = (
    "/admin/",
    "/admin/organizations",
    "/admin/divisions",
    "/admin/users",
    "/admin/requests",
    "/admin/audit",
    "/admin/act-as",
)
SYSTEM = ("/admin/organizations/new", "/admin/backups")


def sidebar(client: FlaskClient, path: str = "/") -> str:
    response = client.get(path)
    assert response.status_code == 200
    return response.get_data(as_text=True).split('id="app-sidebar"', 1)[1].split("</nav>", 1)[0]


def check_access(client: FlaskClient, routes: tuple[str, ...], allowed: bool) -> None:
    navigation = sidebar(client)
    for route in routes:
        assert (f'href="{route}"' in navigation) == allowed
        assert client.get(route).status_code == (200 if allowed else 403)


@pytest.mark.parametrize("role", list(Role))
def test_navigation_by_role(client: FlaskClient, world: World, role: Role) -> None:
    login(client, world.college.users[role].username)
    assert 'href="/"' in sidebar(client)
    assert client.get("/profile").status_code == 200
    check_access(client, MANAGEMENT, role is Role.ADMIN)
    check_access(client, SYSTEM, False)


def test_navigation_uses_effective_role_for_system_admin(client: FlaskClient, world: World) -> None:
    login(client, world.superadmin.username)
    response = client.post(
        "/organization/switch",
        data={"csrf_token": csrf(client, "/"), "organization_id": world.university.organization.id},
    )
    assert response.status_code == 302
    check_access(client, MANAGEMENT + SYSTEM, True)
    assert 'aria-current="page"' in sidebar(client, "/admin/users")
    assert 'aria-current="location"' in sidebar(client, "/admin/users/new")

    response = client.post(
        "/admin/act-as",
        data={
            "csrf_token": csrf(client, "/admin/act-as"),
            "role": "curator",
            "subject_user_id": world.university.users[Role.CURATOR].id,
        },
    )
    assert response.status_code == 302
    assert 'class="role-banner"' in client.get("/").get_data(as_text=True)
    check_access(client, MANAGEMENT + SYSTEM, False)
    assert client.post("/act-as/stop", data={"csrf_token": csrf(client, "/")}).status_code == 302
    check_access(client, MANAGEMENT + SYSTEM, True)


def test_navigation_refreshes_after_organization_switch(
    client: FlaskClient, session: Session, world: World
) -> None:
    admin = world.college.users[Role.ADMIN]
    university = world.university.organization
    bind_organization(session, university.id)
    session.add(Membership(user_id=admin.id, organization_id=university.id, role=Role.VIEWER))
    session.commit()
    login(client, admin.username)

    for org, can_manage in ((world.college.organization, True), (university, False)):
        response = client.post(
            "/organization/switch",
            data={"csrf_token": csrf(client, "/"), "organization_id": org.id},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert f'<option value="{org.id}" selected>' in response.get_data(as_text=True)
        check_access(client, MANAGEMENT, can_manage)
