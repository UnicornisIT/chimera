"""Контракт запуска и изоляции данных Desktop Local и Server."""

from __future__ import annotations

import os
import runpy
import secrets
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, Mock

import psycopg
import pytest
from cryptography.fernet import Fernet
from sqlalchemy.engine import make_url

from chimera import __main__ as cli
from chimera.core import config
from chimera.core.config import AppMode, ConfigError, Settings, load_settings
from chimera.core.db import Database
from chimera.web import create_app


@pytest.fixture
def config_env(tmp_path: Path) -> dict[str, str]:
    return {
        "CHIMERA_DATA_DIR": str(tmp_path / "data"),
        "SECRET_KEY": secrets.token_urlsafe(48),
        "DATA_ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }


@pytest.mark.parametrize(
    ("mode", "key", "value", "expected"),
    [
        ("desktop-local", "DATABASE_URL", "postgresql://u:p@db/name", "SQLite"),
        ("desktop-local", "DATABASE_URL", "sqlite+aiosqlite:///foo", "SQLite"),
        ("desktop-local", "DATABASE_URL", "sqlite:///foo?uri=true", "DATABASE_URL"),
        ("desktop-local", "DATABASE_URL", "sqlite://host/db", "DATABASE_URL"),
        ("desktop-local", "DATABASE_URL", "sqlite://", "DATABASE_URL"),
        ("desktop-local", "DATABASE_URL", "sqlite:///:memory:", "DATABASE_URL"),
        ("desktop-local", "APP_HOST", "0.0.0.0", "APP_HOST"),  # noqa: S104
        ("desktop-local", "APP_HOST", "::", "APP_HOST"),
        ("desktop-local", "APP_HOST", "192.0.2.4", "APP_HOST"),
        ("desktop-local", "APP_HOST", "2001:db8::4", "APP_HOST"),
        ("desktop-local", "APP_HOST", "localhost", "APP_HOST"),
        ("desktop-local", "SESSION_COOKIE_SECURE", "true", "SESSION_COOKIE_SECURE"),
        ("desktop-local", "ENABLE_HSTS", "true", "ENABLE_HSTS"),
        ("server", "DATABASE_URL", None, "DATABASE_URL"),
        ("server", "DATABASE_URL", "", "DATABASE_URL"),
        ("server", "DATABASE_URL", "sqlite:///foo", "PostgreSQL"),
        ("server", "DATABASE_URL", "postgresql+psycopg2://u:p@db/name", "psycopg"),
        ("server", "DATABASE_URL", "postgresql://u:p@db:abc/name", "DATABASE_URL"),
        ("server", "DATABASE_URL", "postgresql://u:p@db:65536/name", "DATABASE_URL"),
        ("server", "DATABASE_URL", "postgresql://u:p%q@db/name", "DATABASE_URL"),
        ("server", "DATABASE_URL", "postgresql://u:p@db/name?unsupported=value", "DATABASE_URL"),
        ("server", "DATABASE_URL", "postgresql://u:p%00q@db/name", "DATABASE_URL"),
        ("server", "DATABASE_URL", "postgresql://u:p@db", "DATABASE_URL"),
        ("server", "DATABASE_URL", "not-a-url", "DATABASE_URL"),
        ("server", "SECRET_KEY", None, "SECRET_KEY"),
        ("server", "SECRET_KEY", "a" * 40, "SECRET_KEY"),
        ("server", "SECRET_KEY", "replace-with-your-signing-key-of-32-chars", "SECRET_KEY"),
        ("server", "DATA_ENCRYPTION_KEY", None, "DATA_ENCRYPTION_KEY"),
        ("server", "DATA_ENCRYPTION_KEY", "not-a-fernet-key", "DATA_ENCRYPTION_KEY"),
        ("server", "SESSION_COOKIE_SECURE", "false", "SESSION_COOKIE_SECURE"),
        ("server", "CHIMERA_DATA_DIR", None, "CHIMERA_DATA_DIR"),
        ("server", "APP_DEBUG", "true", "APP_DEBUG"),
        ("server", "CHIMERA_TESTING", "true", "CHIMERA_TESTING"),
        ("server", "APP_PORT", "-1", "APP_PORT"),
        ("desktop-local", "APP_PORT", "65536", "APP_PORT"),
        ("desktop-local", "APP_PORT", "  ", "APP_PORT"),
        ("desktop-local", "APP_DEBUG", "maybe", "APP_DEBUG"),
        ("desktop-local", "CHIMERA_BACKUP_DIR", "bad\0path", "CHIMERA_BACKUP_DIR"),
        ("server", "CHIMERA_UPLOAD_DIR", "bad\0path", "CHIMERA_UPLOAD_DIR"),
        ("cloud", "APP_PORT", "5000", "CHIMERA_MODE"),
    ],
)
def test_rejected_config_has_no_side_effects(
    tmp_path: Path,
    config_env: dict[str, str],
    mode: str,
    key: str,
    value: str | None,
    expected: str,
) -> None:
    env = {**config_env, "CHIMERA_MODE": mode}
    if mode == "server":
        env["DATABASE_URL"] = "postgresql://u:p@db/name"
    else:
        env.pop("SECRET_KEY")
        env.pop("DATA_ENCRYPTION_KEY")
    if value is None:
        env.pop(key, None)
    else:
        env[key] = value
    with pytest.raises(ConfigError, match=expected):
        load_settings(env)
    assert list(tmp_path.iterdir()) == []


def test_paths_relative_to_checkout_and_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "checkout"
    source.mkdir()
    monkeypatch.setattr(config, "PROJECT_ROOT", source)
    monkeypatch.chdir(tmp_path)
    settings = load_settings(
        {
            "CHIMERA_MODE": "desktop-local",
            "CHIMERA_DATA_DIR": "local",
            "CHIMERA_UPLOAD_DIR": "files/uploads",
            "DATABASE_URL": "sqlite+pysqlite:///relative/db.sqlite",
            "APP_PORT": "65535",
        },
        require_secrets=False,
    )
    assert settings.mode is AppMode.DESKTOP_LOCAL and settings.is_sqlite
    assert settings.host == "127.0.0.1"
    assert settings.data_dir == source / "local"
    assert settings.upload_dir == source / "files" / "uploads"
    assert settings.backup_dir == settings.data_dir / "backups"
    assert make_url(settings.database_url).database == (tmp_path / "relative/db.sqlite").as_posix()
    assert settings.port == 65535
    assert not settings.data_dir.exists()


@pytest.mark.skipif(os.name != "nt", reason="расположение профиля Windows")
def test_windows_profile_and_legacy_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "source"
    monkeypatch.setattr(config, "PROJECT_ROOT", checkout)
    env = {"CHIMERA_MODE": "desktop-local", "LOCALAPPDATA": str(tmp_path / "profile")}
    first = load_settings(env, require_secrets=False)
    assert first.data_dir == tmp_path / "profile" / "Chimera"
    assert not first.data_dir.exists()
    old = checkout / "instance"
    (old / "backups").mkdir(parents=True)
    with pytest.raises(ConfigError, match="CHIMERA_DATA_DIR"):
        load_settings(env)
    db_file = old / "chimera.db"
    db_file.touch()
    with pytest.raises(ConfigError, match="CHIMERA_DATA_DIR"):
        load_settings(env)
    legacy = load_settings({**env, "CHIMERA_DATA_DIR": "instance"}, require_secrets=False)
    assert make_url(legacy.database_url).database == db_file.as_posix()
    assert set(old.iterdir()) == {db_file, old / "backups"}
    assert not first.data_dir.exists()


def test_rejects_a_file_in_place_of_directory(tmp_path: Path, config_env: dict[str, str]) -> None:
    occupied = tmp_path / "file"
    occupied.touch()
    with pytest.raises(ConfigError, match="CHIMERA_BACKUP_DIR"):
        load_settings(
            {
                **config_env,
                "CHIMERA_MODE": "desktop-local",
                "CHIMERA_BACKUP_DIR": str(occupied / "subdir"),
            }
        )
    assert not Path(config_env["CHIMERA_DATA_DIR"]).exists()


def test_rejects_desktop_bundle_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = tmp_path / "_MEI123"
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    with pytest.raises(ConfigError, match="установщика"):
        load_settings({"CHIMERA_MODE": "desktop-local", "CHIMERA_DATA_DIR": str(bundle / "data")})
    assert not bundle.exists()


def test_local_secrets_are_reused_and_corruption_preserved(tmp_path: Path) -> None:
    env = {"CHIMERA_MODE": "desktop-local", "CHIMERA_DATA_DIR": str(tmp_path)}
    first = load_settings(env)
    second = load_settings(env)
    assert first.secret_key == second.secret_key
    assert first.data_encryption_key == second.data_encryption_key
    assert not first.session_cookie_secure
    secret_path = tmp_path / "secret_key"
    secret_path.unlink()
    encryption_path = tmp_path / "data_encryption.key"
    encryption_path.write_text("invalid", encoding="ascii")
    with pytest.raises(ConfigError, match="DATA_ENCRYPTION_KEY"):
        load_settings(env)
    assert not secret_path.exists()
    assert encryption_path.read_text(encoding="ascii") == "invalid"


def test_existing_sqlite_requires_original_encryption_key(tmp_path: Path) -> None:
    env = {"CHIMERA_MODE": "desktop-local", "CHIMERA_DATA_DIR": str(tmp_path)}
    original_key = Fernet.generate_key().decode()
    database = tmp_path / "chimera.db"
    database.write_bytes(b"existing SQLite sentinel")
    with pytest.raises(ConfigError, match="DATA_ENCRYPTION_KEY"):
        load_settings(env)
    assert list(tmp_path.iterdir()) == [database]
    assert database.read_bytes() == b"existing SQLite sentinel"
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        load_settings({**env, "DATA_ENCRYPTION_KEY": original_key})
    assert list(tmp_path.iterdir()) == [database]
    restored = load_settings(
        {**env, "DATA_ENCRYPTION_KEY": original_key, "SECRET_KEY": "synthetic-session-key-" * 2}
    )
    assert restored.data_encryption_key == original_key
    assert not (tmp_path / "data_encryption.key").exists()


@pytest.mark.parametrize("driver", ["postgres", "postgresql", "postgresql+psycopg"])
def test_postgres_url_and_key_rotation(driver: str, config_env: dict[str, str]) -> None:
    raw = f"{driver}://u:p%40%2F%3A%25@[::1]:5432/db?sslmode=verify-full&application_name=A+B"
    rotation = config_env["DATA_ENCRYPTION_KEY"] + ", " + Fernet.generate_key().decode()
    settings = load_settings({**config_env, "DATABASE_URL": raw, "DATA_ENCRYPTION_KEY": rotation})
    url = make_url(settings.database_url)
    assert url.drivername == "postgresql+psycopg"
    assert url.password == "p@/:%"
    assert url.host == "::1" and url.port == 5432
    assert url.query == {"sslmode": "verify-full", "application_name": "A B"}
    assert settings.data_encryption_key == rotation
    assert settings.session_cookie_secure and not settings.is_sqlite
    assert rotation not in repr(settings) and url.password not in repr(settings)


@pytest.mark.parametrize("command", ["run", "status", "migrate", "bootstrap"])
def test_cli_rejects_server_without_url(
    command: str,
    config_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(config, "_read_env", lambda *_: config_env)
    assert cli.main([command]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err
    assert not Path(config_env["CHIMERA_DATA_DIR"]).exists()


@pytest.mark.parametrize("command", ["migrate", "bootstrap", "wsgi", "alembic"])
def test_server_writers_require_web_secrets_before_connecting(
    command: str,
    config_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from alembic import command as alembic
    from alembic.config import Config
    from alembic.util.exc import CommandError

    env = {**config_env, "DATABASE_URL": "postgresql://u:synthetic@127.0.0.1/db"}
    del env["SECRET_KEY"]
    monkeypatch.setattr(config, "_read_env", lambda *_: env)
    connect = Mock()
    monkeypatch.setattr(psycopg, "connect", connect)
    if command == "alembic":
        with pytest.raises(CommandError, match="SECRET_KEY"):
            alembic.upgrade(Config("alembic.ini"), "head")
    elif command == "wsgi":
        with pytest.raises(SystemExit):
            runpy.run_module("chimera.wsgi")
        assert "SECRET_KEY" in capsys.readouterr().err
    else:
        assert cli.main([command]) == 2
        assert "SECRET_KEY" in capsys.readouterr().err
    connect.assert_not_called()


def test_cli_passes_loopback_and_disables_debugger(
    config_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {**config_env, "CHIMERA_MODE": "desktop-local", "APP_DEBUG": "true"}
    monkeypatch.setattr(config, "_read_env", lambda *_: env)
    run = Mock()
    monkeypatch.setattr("flask.Flask.run", run)
    assert cli.main(["run"]) == 0
    run.assert_called_once_with(
        host="127.0.0.1",
        port=5000,
        debug=True,
        use_debugger=False,
        use_reloader=False,
        load_dotenv=False,
    )
    env["APP_HOST"] = "0.0.0.0"  # noqa: S104
    run.reset_mock()
    assert cli.main(["run"]) == 2
    run.assert_not_called()


@pytest.mark.parametrize(
    "overrides",
    [
        {"host": "192.0.2.4"},
        {"mode": AppMode.SERVER},
        {"port": 65536},
        {"data_encryption_key": "invalid"},
        {"database_url": "postgresql://u:p@db/name"},
    ],
)
def test_direct_settings_cannot_bypass_validation(
    config_env: dict[str, str], overrides: dict[str, object]
) -> None:
    settings = load_settings({**config_env, "CHIMERA_MODE": "desktop-local"})
    with pytest.raises(ConfigError):
        create_app(replace(settings, **overrides))
    assert not settings.data_dir.exists()


def test_wsgi_rejects_desktop_before_creating_keys(
    config_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = {
        "CHIMERA_MODE": "desktop-local",
        "CHIMERA_DATA_DIR": config_env["CHIMERA_DATA_DIR"],
    }
    monkeypatch.setattr(config, "_read_env", lambda *_: env)
    with pytest.raises(SystemExit) as error:
        runpy.run_module("chimera.wsgi")
    assert error.value.code == 4
    assert "WSGI" in capsys.readouterr().err
    assert not Path(config_env["CHIMERA_DATA_DIR"]).exists()


@pytest.mark.parametrize("command", ["status", "migrate", "bootstrap", "wsgi"])
def test_incompatible_postgres_is_closed_before_any_queries(
    command: str,
    config_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = {
        **config_env,
        "DATABASE_URL": "postgresql://u:synthetic-password@127.0.0.1/db",
        "ADMIN_DEFAULT_PASSWORD": "Synthetic-9-" + secrets.token_urlsafe(18),
    }
    monkeypatch.setattr(config, "_read_env", lambda *_: env)
    connection = MagicMock()
    connection.info.server_version = 150013
    monkeypatch.setattr(psycopg, "connect", Mock(return_value=connection))
    if command == "wsgi":
        with pytest.raises(SystemExit) as error:
            runpy.run_module("chimera.wsgi")
        assert error.value.code == 4
        message = capsys.readouterr().err
        assert "PostgreSQL 16" in message and "synthetic-password" not in message
    else:
        assert cli.main([command]) == 2
        message = capsys.readouterr().err
        assert "PostgreSQL 16" in message and "synthetic-password" not in message
    connection.close.assert_called_once()
    connection.cursor.assert_not_called()
    assert not Path(config_env["CHIMERA_DATA_DIR"]).exists()


def test_connection_error_redacts_driver_details(
    config_env: dict[str, str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    password = "synthetic-redaction-marker"
    env = {**config_env, "DATABASE_URL": f"postgresql://u:{password}@127.0.0.1/db"}
    monkeypatch.setattr(config, "_read_env", lambda *_: env)
    monkeypatch.setattr(psycopg, "connect", Mock(side_effect=psycopg.OperationalError(password)))
    assert cli.main(["migrate"]) == 2
    message = capsys.readouterr().err
    assert password not in message and "Traceback" not in message


def test_server_wsgi_settings_on_live_postgres(
    database: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    if database.dialect != "postgresql":
        pytest.skip("штатный WSGI проверяется на выделенном PostgreSQL 16")
    env = {
        "CHIMERA_MODE": "server",
        "CHIMERA_DATA_DIR": str(settings.data_dir),
        "DATABASE_URL": settings.database_url,
        "SECRET_KEY": settings.secret_key,
        "DATA_ENCRYPTION_KEY": settings.data_encryption_key,
    }
    monkeypatch.setattr(config, "_read_env", lambda *_: env)
    app = runpy.run_module("chimera.wsgi")["app"]
    try:
        response = app.test_client().get("/login", base_url="https://chimera.example")
        assert response.status_code == 200
        assert "Secure" in response.headers["Set-Cookie"]
        assert not app.testing
        assert cli.main(["status"]) == 0
        assert cli.main(["migrate"]) == 0
    finally:
        app.extensions["chimera.db"].dispose()


def test_pytest_rejects_external_database_url(tmp_path: Path) -> None:
    sentinel = tmp_path / "untouched.db"
    sentinel.write_bytes(b"preserve")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q"],
        env={**os.environ, "CHIMERA_TEST_DATABASE_URL": f"sqlite:///{sentinel.as_posix()}"},
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 4
    assert b"CHIMERA_TEST_DATABASE_URL" in result.stderr
    assert sentinel.read_bytes() == b"preserve"


@pytest.mark.parametrize("command", ["status", "bootstrap"])
def test_read_only_cli_does_not_create_a_missing_sqlite(
    command: str, config_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {**config_env, "CHIMERA_MODE": "desktop-local"}
    monkeypatch.setattr(config, "_read_env", lambda *_: env)
    assert cli.main([command]) == (1 if command == "status" else 2)
    assert not Path(config_env["CHIMERA_DATA_DIR"]).exists()


def test_test_mode_rejects_a_foreign_cluster_before_orm_queries(
    tmp_path: Path, config_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    stand = tmp_path / "postgres"
    cluster = stand / "pgdata"
    cluster.mkdir(parents=True)
    key = "ab" * 32
    (stand / config.TEST_STAND_MARKER).write_text(
        f"chimera-stage-02\n{stand.as_posix()}\n", encoding="ascii"
    )
    (stand / "postgres.key").write_text(key, encoding="ascii")
    (cluster / "PG_VERSION").write_text("16", encoding="ascii")
    name = config.TEST_POSTGRES_NAME
    env = {
        **config_env,
        "DATABASE_URL": f"postgresql://{name}:{key}@127.0.0.1:55439/{name}",
    }
    with pytest.raises(ConfigError, match="выделенный кластер"):
        load_settings(env, testing=True)
    settings = load_settings(env, testing=True, test_postgres_dir=cluster)
    connection = MagicMock()
    connection.info.server_version = 160016
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (str(tmp_path / "foreign"), name, name, "127.0.0.1", 55439)
    monkeypatch.setattr(psycopg, "connect", Mock(return_value=connection))
    with pytest.raises(ConfigError, match="за пределы тестового кластера"):
        create_app(settings)
    connection.close.assert_called_once()
    cursor.execute.assert_called_once()
    assert not settings.data_dir.exists()
