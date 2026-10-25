"""Панель администратора: организация, подразделения, пользователи, аудит, бэкапы."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, render_template

from chimera.core.permissions import Permission, SystemPermission
from chimera.services import users as users_service
from chimera.web.context import ctx, permission_required

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.get("/")
@permission_required(Permission.USERS_MANAGE)
def index() -> Any:
    current = ctx()
    return render_template(
        "admin/index.html",
        pending_requests=users_service.count_pending_requests(current),
        can_backups=current.can_system(SystemPermission.BACKUPS_MANAGE),
        can_create_orgs=current.can_system(SystemPermission.ORGANIZATIONS_CREATE),
    )


from chimera.web.admin import organizations, system, users  # noqa: E402, F401
