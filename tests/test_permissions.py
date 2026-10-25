"""Матрица прав ролей: декларация и проверка в сервисах."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from chimera.core.context import PermissionDenied, ServiceContext
from chimera.core.permissions import (
    ROLE_PERMISSIONS,
    Permission,
    Role,
    SystemPermission,
    has_permission,
    permissions_for,
)
from chimera.services import audit_log, organizations, users
from tests.conftest import World

P = Permission

# Ожидаемая матрица записана явно: любое изменение прав должно менять и этот тест.
EXPECTED: dict[Permission, set[Role]] = {
    P.DASHBOARD_VIEW: set(Role),
    P.PROFILE_EDIT: set(Role),
    P.ORG_MANAGE: {Role.ADMIN},
    P.USERS_MANAGE: {Role.ADMIN},
    P.REQUESTS_APPROVE: {Role.ADMIN},
    P.AUDIT_VIEW: {Role.ADMIN},
    P.ROLE_ACT_AS: {Role.ADMIN},
    P.GROUPS_VIEW_ALL: {Role.ADMIN},
    P.GROUPS_VIEW_DIVISION: {Role.HEAD},
    P.GROUPS_VIEW_OWN: {Role.CURATOR, Role.STAROSTA},
    P.STUDENTS_EDIT: {Role.CURATOR},
    P.DOCUMENTS_GENERATE: {Role.HEAD, Role.CURATOR},
    P.ASSIGNMENTS_CREATE: {Role.HEAD},
    P.ASSIGNMENTS_VIEW: {Role.ADMIN, Role.HEAD, Role.CURATOR},
    P.DUTY_MARK: {Role.STAROSTA},
    P.REFERENCES_VIEW: {Role.ADMIN, Role.HEAD, Role.CURATOR, Role.VIEWER},
    P.REFERENCES_MANAGE: {Role.ADMIN},
}


def test_matrix_covers_every_permission() -> None:
    assert set(EXPECTED) == set(Permission)
    assert set(ROLE_PERMISSIONS) == set(Role)


@pytest.mark.parametrize("permission", list(Permission))
@pytest.mark.parametrize("role", list(Role))
def test_role_matrix(role: Role, permission: Permission) -> None:
    assert has_permission(role, permission) is (role in EXPECTED[permission])


def test_no_role_has_no_permissions() -> None:
    assert permissions_for(None) == frozenset()


def test_system_permissions_need_superadmin(session: Session) -> None:
    admin = ServiceContext.build(session, role=Role.ADMIN, organization_id=1)
    root = ServiceContext.build(session, role=Role.ADMIN, organization_id=1, is_superadmin=True)
    assert not admin.can_system(SystemPermission.BACKUPS_MANAGE)
    assert root.can_system(SystemPermission.BACKUPS_MANAGE)
    acting_root = ServiceContext.build(
        session, role=Role.CURATOR, real_role=Role.ADMIN, organization_id=1, is_superadmin=True
    )
    assert not acting_root.can_system(SystemPermission.BACKUPS_MANAGE)


@pytest.mark.parametrize("role", [Role.HEAD, Role.CURATOR, Role.STAROSTA, Role.VIEWER])
def test_services_enforce_permissions(session: Session, world: World, role: Role) -> None:
    ctx = world.ctx(session, world.college, role)
    with pytest.raises(PermissionDenied):
        users.list_members(ctx)
    with pytest.raises(PermissionDenied):
        audit_log.search(ctx, audit_log.AuditFilter())
    with pytest.raises(PermissionDenied):
        organizations.create_division(
            ctx, organizations.DivisionData(name="Тестовое", kind="division")
        )
    with pytest.raises(PermissionDenied):
        organizations.create_organization(
            ctx, organizations.OrganizationData(name="X", short_name="X", org_type="college")
        )


def test_admin_cannot_create_organization_without_superadmin(
    session: Session, world: World
) -> None:
    ctx = world.ctx(session, world.college, Role.ADMIN)
    with pytest.raises(PermissionDenied):
        organizations.create_organization(
            ctx, organizations.OrganizationData(name="X", short_name="X", org_type="college")
        )
