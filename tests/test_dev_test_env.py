"""Проверки повторного запуска и границ очистки DEV-стендов."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy import select

from chimera.core.db import Database
from chimera.core.tenancy import unscoped
from chimera.models import Membership, Organization, User
from chimera.web import create_app
from scripts import dev_test_env as stand
from tests.conftest import csrf


@pytest.fixture
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / ".chimera-test-envs"
    monkeypatch.setattr(stand, "STATE_ROOT", root)
    return root


@pytest.mark.parametrize("backend", ["sqlite", "postgres"])
def test_prepare_preserves_accounts_and_passwords(
    backend: str, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if backend == "postgres" and os.environ.get("CHIMERA_STAGE02_TEST_POSTGRES") != "1":
        pytest.skip(
            "Для отдельного кластера PostgreSQL 16 задайте CHIMERA_STAGE02_TEST_POSTGRES=1."
        )

    outside = state_root.parent / "outside"
    monkeypatch.setenv("CHIMERA_DATA_DIR", str(outside))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(outside / 'production.db').as_posix()}")
    monkeypatch.setenv("APP_HOST", "0.0.0.0")  # noqa: S104 - проверка игнорирования APP_HOST
    monkeypatch.setenv("PGSERVICE", "production")
    assert stand.main([backend, "prepare"]) == 0
    path = state_root / backend
    try:
        assert not outside.exists()
        assert stand.main([backend, "status"]) == 0
        settings = stand.settings_for(path, backend, prepare=True)
        initial_password = (path / "fixture_password.key").read_text(encoding="ascii").strip()
        assert settings.admin_default_password == initial_password
        changed_password = secrets.token_hex(24) + "!"
        assert settings.host == "127.0.0.1"
        assert settings.is_sqlite == (backend == "sqlite")
        database = Database(settings.database_url)
        try:
            with database.transaction() as session:
                users = list(session.scalars(select(User).order_by(User.username)))
                assert [user.username for user in users] == [
                    "admin",
                    "curator1",
                    "curator2",
                    "head",
                    "viewer",
                ]
                ids = [user.id for user in users]
                assert users[0].must_change_password
                assert sorted(session.scalars(unscoped(select(Membership.role)))) == [
                    "admin",
                    "curator",
                    "curator",
                    "head",
                    "viewer",
                ]
                assert sorted(session.scalars(select(Organization.org_type))) == [
                    "college",
                    "university",
                ]
                users[1].full_name = "Сохранённое имя"
        finally:
            database.dispose()

        app = create_app(settings)
        try:
            with app.test_client() as client:
                login = client.post(
                    "/login",
                    data={
                        "username": "admin",
                        "password": initial_password,
                        "csrf_token": csrf(client),
                    },
                )
                assert login.location == "/password"
                changed = client.post(
                    "/password",
                    data={
                        "current_password": initial_password,
                        "new_password": changed_password,
                        "new_password_confirm": changed_password,
                        "csrf_token": csrf(client, "/password"),
                    },
                )
                assert changed.status_code == 302
                assert client.get("/admin/").status_code == 200
        finally:
            app.extensions["chimera.db"].dispose()

        if backend == "postgres":
            assert stand.main([backend, "stop"]) == 0
        assert stand.main([backend, "prepare"]) == 0
        reloaded = stand.settings_for(path, backend, prepare=True)
        assert reloaded.secret_key == settings.secret_key
        assert reloaded.data_encryption_key == settings.data_encryption_key
        assert reloaded.admin_default_password == initial_password
        database = Database(reloaded.database_url)
        try:
            with database.transaction() as session:
                users = list(session.scalars(select(User).order_by(User.username)))
                assert [user.id for user in users] == ids
                assert users[1].full_name == "Сохранённое имя"
                assert not users[0].must_change_password
                assert len(list(session.scalars(select(Organization.id)))) == 2
        finally:
            database.dispose()

        app = create_app(reloaded)
        try:
            with app.test_client() as client:
                login = client.post(
                    "/login",
                    data={
                        "username": "admin",
                        "password": changed_password,
                        "csrf_token": csrf(client),
                    },
                )
                assert login.status_code == 302
                assert client.get("/admin/").status_code == 200
        finally:
            app.extensions["chimera.db"].dispose()
    finally:
        assert stand.main([backend, "clean", "--confirm"]) == 0
        assert not path.exists()


def test_cleanup_rejects_paths_and_requires_confirmation(
    state_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for argv in (["../production", "clean", "--confirm"], ["sqlite", "clean"]):
        with pytest.raises(SystemExit) as exc:
            stand.main(argv)
        assert exc.value.code == 2
    assert "--confirm" in capsys.readouterr().err
    assert not state_root.exists()


@pytest.mark.parametrize("marker", [None, "another checkout\n"])
def test_cleanup_refuses_unowned_data(state_root: Path, marker: str | None) -> None:
    path = state_root / "sqlite"
    path.mkdir(parents=True)
    sentinel = path / "production.db"
    sentinel.write_bytes(b"keep")
    if marker is not None:
        (path / stand.MARKER).write_text(marker, encoding="utf-8")
    assert stand.main(["sqlite", "clean", "--confirm"]) == 1
    assert sentinel.read_bytes() == b"keep"


@pytest.mark.parametrize("level", ["root", "backend", "nested"])
def test_cleanup_refuses_redirected_directories(state_root: Path, level: str) -> None:
    outside = state_root.parent / "production"
    outside.mkdir()
    sentinel = outside / "document"
    sentinel.write_bytes(b"keep")
    if level == "root":
        link = state_root
    elif level == "backend":
        state_root.mkdir()
        link = state_root / "sqlite"
    else:
        with stand.locked_stand("sqlite", create=True) as path:
            link = path / "uploads"
    if os.name == "nt":
        cmd = shutil.which("cmd.exe")
        assert cmd is not None
        subprocess.run(  # noqa: S603 - тестовая junction внутри tmp_path
            [cmd, "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            check=True,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    assert stand.main(["sqlite", "clean", "--confirm"]) == 1
    assert sentinel.read_bytes() == b"keep"


def test_cleanup_refuses_hard_link_and_active_stand(state_root: Path) -> None:
    outside = state_root.parent / "production.db"
    outside.write_bytes(b"keep")
    with stand.locked_stand("sqlite", create=True) as path:
        assert stand.main(["sqlite", "clean", "--confirm"]) == 1
        os.link(outside, path / "chimera.db")
    assert stand.main(["sqlite", "clean", "--confirm"]) == 1
    assert outside.read_bytes() == b"keep"


def test_postgres_cleanup_rejects_foreign_cluster(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign = state_root.parent / "production-pgdata"
    with stand.locked_stand("postgres", create=True) as path:
        (path / "pgdata").mkdir()
        (path / "postgres.key").write_text(secrets.token_hex(32), encoding="ascii")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = (str(foreign), "160016")
    connect = Mock(return_value=connection)
    pg_tool = Mock()
    monkeypatch.setattr(stand.psycopg, "connect", connect)
    monkeypatch.setattr(stand, "pg_tool", pg_tool)
    monkeypatch.setattr(stand, "pg_state", lambda _: True)
    (path / "pgdata" / "PG_VERSION").write_text("16", encoding="ascii")
    monkeypatch.setenv("DATABASE_URL", "postgresql://production.example/chimera")
    monkeypatch.setenv("PGHOST", "production.example")

    assert stand.main(["postgres", "clean", "--confirm"]) == 1
    assert connect.call_args.kwargs["host"] == "127.0.0.1"
    assert "PGHOST" not in os.environ
    assert connection.execute.call_count == 1
    assert "current_setting('data_directory')" in connection.execute.call_args.args[0]
    pg_tool.assert_not_called()
    assert (path / "pgdata" / "PG_VERSION").is_file()


def test_sqlite_cleanup_ignores_postgres_and_external_paths(
    state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = state_root.parent / "production.db"
    outside.write_bytes(b"keep")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{outside.as_posix()}")
    monkeypatch.setenv("CHIMERA_DATA_DIR", str(outside.parent))
    with stand.locked_stand("postgres", create=True) as postgres:
        (postgres / "postgres.key").write_bytes(b"keep")
    with stand.locked_stand("sqlite", create=True) as sqlite:
        (sqlite / "chimera.db").touch()

    assert stand.main(["sqlite", "clean", "--confirm"]) == 0
    assert not sqlite.exists()
    assert (postgres / "postgres.key").read_bytes() == b"keep"
    assert outside.read_bytes() == b"keep"
