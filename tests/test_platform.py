"""Миграции, бэкапы, заголовки безопасности, CSRF и метаданные."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from flask.testing import FlaskClient
from sqlalchemy.orm import Session

from chimera.core.config import Settings
from chimera.core.context import PermissionDenied
from chimera.core.db import Base, Database
from chimera.core.migrations import SchemaTooNewError, alembic_config, schema_state
from chimera.core.permissions import Role
from chimera.services import backup
from tests.conftest import World, login, post


def test_migrations_are_at_head_and_match_models(database: Database) -> None:
    state = schema_state(database.engine, database.url)
    assert state.up_to_date and state.current is not None
    with database.engine.connect() as conn:
        diff = compare_metadata(
            MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata
        )
    assert diff == []


def test_downgrade_and_upgrade_roundtrip(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'roundtrip.db').as_posix()}"
    config = alembic_config(url)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    db = Database(url)
    try:
        assert schema_state(db.engine, url).up_to_date
    finally:
        db.dispose()


def test_unknown_revision_blocks_start(tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'future.db').as_posix()}"
    command.upgrade(alembic_config(url), "head")
    db = Database(url)
    try:
        with db.engine.begin() as conn:
            conn.exec_driver_sql("UPDATE alembic_version SET version_num = 'ffff_future'")
        with pytest.raises(SchemaTooNewError):
            schema_state(db.engine, url)
    finally:
        db.dispose()


def test_security_headers(client: FlaskClient) -> None:
    response = client.get("/login")
    csp = response.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    cookie = response.headers.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie


def test_templates_have_no_inline_scripts_or_styles() -> None:
    root = Path(__file__).resolve().parents[1] / "chimera" / "templates"
    for path in root.rglob("*.html"):
        source = path.read_text(encoding="utf-8")
        assert "<script>" not in source and "<script " not in source.replace("<script src=", ""), (
            path
        )
        assert " style=" not in source, path
        assert " onclick=" not in source and " onchange=" not in source, path


def test_csrf_is_required(client: FlaskClient, world: World) -> None:
    response = client.post("/login", data={"username": "col.admin", "password": "x"})
    assert response.status_code == 400
    login(client, "col.admin")
    assert (
        client.post("/admin/divisions", data={"name": "X", "kind": "division"}).status_code == 400
    )


def test_anonymous_is_redirected_to_login(client: FlaskClient) -> None:
    response = client.get("/admin/users")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_sqlite_backup(
    session: Session, world: World, database: Database, settings: Settings, tmp_path: Path
) -> None:
    if database.dialect != "sqlite":
        pytest.skip("копия файла — только для SQLite")
    configured = replace(settings, backup_dir=tmp_path / "backups")
    ctx = world.ctx(session, world.university, Role.ADMIN)
    with pytest.raises(PermissionDenied):
        backup.create_backup(ctx, database, configured)
    root = replace(ctx, is_superadmin=True)
    created = backup.create_backup(root, database, configured)
    assert created.size > 0
    assert [b.name for b in backup.list_backups(root, configured)] == [created.name]
    with pytest.raises(Exception, match="не найдена"):
        backup.backup_path(root, configured, "../../etc/passwd")
    backup.delete_backup(root, configured, created.name)
    assert backup.list_backups(root, configured) == []


def test_postgres_backup_without_pg_dump(
    session: Session, world: World, settings: Settings, tmp_path: Path
) -> None:
    configured = replace(
        settings, backup_dir=tmp_path / "pg", pg_dump_path="definitely-missing-pg_dump"
    )
    fake_pg = Database.__new__(Database)
    fake_pg.url = "postgresql+psycopg://user:secret@localhost:5432/chimera"
    ctx = replace(world.ctx(session, world.university, Role.ADMIN), is_superadmin=True)

    class _Dialect:
        name = "postgresql"

    class _Engine:
        dialect = _Dialect()

    fake_pg.engine = _Engine()  # type: ignore[assignment]
    with pytest.raises(backup.BackupError, match="pg_dump"):
        backup.create_backup(ctx, fake_pg, configured)


def test_backup_page_for_superadmin(client: FlaskClient, world: World) -> None:
    login(client, "root")
    page = client.get("/admin/backups")
    assert page.status_code == 200
    assert "Ревизия схемы" in page.get_data(as_text=True)


def test_readme_contacts_are_in_sync() -> None:
    from sync_app_metadata import sync_readme

    assert sync_readme(check=True)


def test_dashboard_marks_planned_modules(client: FlaskClient, world: World) -> None:
    login(client, "col.curator")
    page = client.get("/").get_data(as_text=True)
    assert "Скоро" in page
    assert "Администрирование" not in page
    post(client, "/logout")
    login(client, "col.admin")
    assert "Администрирование" in client.get("/").get_data(as_text=True)
