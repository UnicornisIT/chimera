"""Роли и разрешения. Единственное место, где роль сопоставляется с правами."""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType


class Role(StrEnum):
    ADMIN = "admin"
    HEAD = "head"
    CURATOR = "curator"
    STAROSTA = "starosta"
    VIEWER = "viewer"


ROLE_LABELS = MappingProxyType(
    {
        Role.ADMIN: "Администратор",
        Role.HEAD: "Руководитель подразделения",
        Role.CURATOR: "Куратор",
        Role.STAROSTA: "Староста",
        Role.VIEWER: "Наблюдатель",
    }
)

ROLE_DESCRIPTIONS = MappingProxyType(
    {
        Role.ADMIN: "Настраивает организацию, пользователей и роли, видит журнал аудита.",
        Role.HEAD: "Зав. кафедрой или зам. декана по воспитательной работе: видит все группы "
        "своего подразделения и ставит поручения.",
        Role.CURATOR: "Работает только со своими текущими группами.",
        Role.STAROSTA: "Отмечает дежурства и другие события по своей группе.",
        Role.VIEWER: "Только просмотр разрешённых разделов.",
    }
)

# Роли, в режим которых может перейти администратор.
ACTABLE_ROLES = (Role.HEAD, Role.CURATOR, Role.STAROSTA)


class Permission(StrEnum):
    DASHBOARD_VIEW = "dashboard.view"
    PROFILE_EDIT = "profile.edit"
    ORG_MANAGE = "org.manage"
    USERS_MANAGE = "users.manage"
    REQUESTS_APPROVE = "requests.approve"
    AUDIT_VIEW = "audit.view"
    ROLE_ACT_AS = "role.act_as"
    GROUPS_VIEW_ALL = "groups.view_all"
    GROUPS_VIEW_DIVISION = "groups.view_division"
    GROUPS_VIEW_OWN = "groups.view_own"
    STUDENTS_EDIT = "students.edit"
    DOCUMENTS_GENERATE = "documents.generate"
    ASSIGNMENTS_CREATE = "assignments.create"
    ASSIGNMENTS_VIEW = "assignments.view"
    DUTY_MARK = "duty.mark"
    REFERENCES_VIEW = "references.view"
    REFERENCES_MANAGE = "references.manage"


class SystemPermission(StrEnum):
    """Права системного администратора (не привязаны к организации)."""

    ORGANIZATIONS_CREATE = "system.organizations.create"
    BACKUPS_MANAGE = "system.backups.manage"


P = Permission

_BASE = frozenset({P.DASHBOARD_VIEW, P.PROFILE_EDIT})

ROLE_PERMISSIONS: MappingProxyType[Role, frozenset[Permission]] = MappingProxyType(
    {
        Role.ADMIN: _BASE
        | {
            P.ORG_MANAGE,
            P.USERS_MANAGE,
            P.REQUESTS_APPROVE,
            P.AUDIT_VIEW,
            P.ROLE_ACT_AS,
            P.GROUPS_VIEW_ALL,
            P.ASSIGNMENTS_VIEW,
            P.REFERENCES_VIEW,
            P.REFERENCES_MANAGE,
        },
        Role.HEAD: _BASE
        | {
            P.GROUPS_VIEW_DIVISION,
            P.ASSIGNMENTS_CREATE,
            P.ASSIGNMENTS_VIEW,
            P.DOCUMENTS_GENERATE,
            P.REFERENCES_VIEW,
        },
        Role.CURATOR: _BASE
        | {
            P.GROUPS_VIEW_OWN,
            P.STUDENTS_EDIT,
            P.DOCUMENTS_GENERATE,
            P.ASSIGNMENTS_VIEW,
            P.REFERENCES_VIEW,
        },
        Role.STAROSTA: _BASE | {P.GROUPS_VIEW_OWN, P.DUTY_MARK},
        Role.VIEWER: _BASE | {P.REFERENCES_VIEW},
    }
)


def permissions_for(role: Role | None) -> frozenset[Permission]:
    if role is None:
        return frozenset()
    return ROLE_PERMISSIONS[role]


def has_permission(role: Role | None, permission: Permission) -> bool:
    return permission in permissions_for(role)
