"""Журнал аудита и резервные копии."""

from __future__ import annotations

from datetime import date
from typing import Any

from flask import current_app, flash, redirect, render_template, request, send_file, url_for

from chimera.core.context import ServiceError
from chimera.core.migrations import schema_state
from chimera.core.permissions import Permission, SystemPermission
from chimera.services import audit_log
from chimera.services import backup as backup_service
from chimera.services import users as users_service
from chimera.web.admin import bp
from chimera.web.context import ctx, database, db, permission_required, system_required


def _date_arg(name: str) -> date | None:
    try:
        return date.fromisoformat(request.args.get(name, ""))
    except ValueError:
        return None


def _int_arg(name: str, default: int | None = None) -> int | None:
    try:
        return int(request.args.get(name, ""))
    except ValueError:
        return default


@bp.get("/audit")
@permission_required(Permission.AUDIT_VIEW)
def audit() -> Any:
    current = ctx()
    filters = audit_log.AuditFilter(
        query=request.args.get("q") or None,
        action=request.args.get("action") or None,
        user_id=_int_arg("user_id"),
        acting_only=request.args.get("acting") == "1",
        date_from=_date_arg("from"),
        date_to=_date_arg("to"),
        page=_int_arg("page", 1) or 1,
    )
    return render_template(
        "admin/audit.html",
        page=audit_log.search(current, filters),
        filters=filters,
        actions=audit_log.known_actions(current),
        members=users_service.list_members(current),
        args=request.args,
    )


@bp.route("/backups", methods=["GET", "POST"])
@system_required(SystemPermission.BACKUPS_MANAGE)
def backups() -> Any:
    settings = current_app.extensions["chimera.settings"]
    current = ctx()
    if request.method == "POST":
        try:
            created = backup_service.create_backup(current, database(), settings)
        except ServiceError as exc:
            db().rollback()
            flash(str(exc), "error")
        else:
            db().commit()
            flash(f"Резервная копия {created.name} создана.", "success")
        return redirect(url_for("admin.backups"))
    db_engine = database()
    pg_dump_missing = db_engine.dialect == "postgresql" and not backup_service.find_pg_dump(
        settings.pg_dump_path
    )
    return render_template(
        "admin/backups.html",
        backups=backup_service.list_backups(current, settings),
        dialect=db_engine.dialect,
        schema=schema_state(db_engine.engine, db_engine.url),
        pg_dump_missing=pg_dump_missing,
        pg_dump_path=settings.pg_dump_path,
        retention_days=settings.backup_retention_days,
        backup_dir=settings.backup_dir,
    )


@bp.get("/backups/<name>")
@system_required(SystemPermission.BACKUPS_MANAGE)
def backup_download(name: str) -> Any:
    settings = current_app.extensions["chimera.settings"]
    current = ctx()
    path = backup_service.backup_path(current, settings, name)
    from chimera.core import audit as audit_core

    audit_core.record(current, "backup.downloaded", entity_type="Backup", entity_id=name)
    db().commit()
    return send_file(path, as_attachment=True, download_name=name)


@bp.post("/backups/<name>/delete")
@system_required(SystemPermission.BACKUPS_MANAGE)
def backup_delete(name: str) -> Any:
    settings = current_app.extensions["chimera.settings"]
    backup_service.delete_backup(ctx(), settings, name)
    db().commit()
    flash("Резервная копия удалена.", "success")
    return redirect(url_for("admin.backups"))
