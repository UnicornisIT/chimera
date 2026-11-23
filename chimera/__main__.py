"""Командная строка: `python -m chimera <команда>`."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from alembic.util.exc import CommandError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from chimera.core.config import LOOPBACK, AppMode, ConfigError, load_settings, validate_settings
from chimera.core.context import ServiceError
from chimera.core.db import Database
from chimera.core.metadata import app_version
from chimera.core.migrations import SchemaTooNewError, schema_state, upgrade_head


def _migrate(_args: argparse.Namespace) -> int:
    settings = load_settings()
    database = Database.from_settings(settings)
    try:
        schema_state(database.engine, database.url)
        upgrade_head(database)
    finally:
        database.dispose()
    print("Схема базы обновлена.")
    return 0


def _status(_args: argparse.Namespace) -> int:
    settings = load_settings(require_secrets=False)
    if settings.is_sqlite and not Path(make_url(settings.database_url).database or "").is_file():
        print("Файл SQLite ещё не создан.")
        return 1
    database = Database.from_settings(settings)
    try:
        state = schema_state(database.engine, database.url)
    finally:
        database.dispose()
    print(f"Chimera {app_version()} · режим {settings.mode.value} · БД {database.dialect}")
    print(f"Ревизия схемы: {state.current or 'пусто'}, требуется: {state.head}")
    return 0 if state.up_to_date else 1


def _bootstrap(args: argparse.Namespace) -> int:
    """Создаёт начального администратора (пароль из ADMIN_DEFAULT_PASSWORD или ввод)."""
    from dataclasses import replace

    from chimera.services.auth import ensure_initial_admin

    settings = load_settings(require_secrets=False)
    if settings.is_sqlite and not Path(make_url(settings.database_url).database or "").is_file():
        raise ConfigError("Перед bootstrap выполните python -m chimera migrate")
    if settings.mode is AppMode.SERVER:
        settings = validate_settings(settings)
    database = Database.from_settings(settings)
    try:
        if not schema_state(database.engine, database.url).up_to_date:
            raise ConfigError("Перед bootstrap выполните python -m chimera migrate")
        if not settings.admin_default_password:
            if not sys.stdin.isatty() or args.no_input:
                print("ADMIN_DEFAULT_PASSWORD не задан — начальный администратор не создаётся.")
                return 0
            password = getpass.getpass("Пароль начального администратора: ")
            settings = replace(settings, admin_default_password=password)
        with database.transaction() as session:
            admin = ensure_initial_admin(session, settings)
    except ServiceError as exc:
        print(exc)
        return 2
    finally:
        database.dispose()
    if admin is None:
        print("Пользователи уже существуют — ничего не изменено.")
    else:
        print(f"Создан администратор {admin.username!r}. Смените пароль при первом входе.")
    return 0


def _run(_args: argparse.Namespace) -> int:
    from chimera.web import create_app

    settings = load_settings()
    if settings.mode is not AppMode.DESKTOP_LOCAL:
        raise ConfigError("Режим server запускается через chimera.wsgi за HTTPS-прокси")
    app = create_app(settings)
    app.run(
        host=LOOPBACK,
        port=settings.port,
        debug=settings.debug,
        use_debugger=False,
        use_reloader=False,
        load_dotenv=False,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m chimera", description=__doc__)
    parser.add_argument("--version", action="version", version=f"Chimera {app_version()}")
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser("migrate", help="обновить схему базы (alembic upgrade head)")
    migrate.set_defaults(func=_migrate)
    commands.add_parser("status", help="версия и состояние схемы базы").set_defaults(func=_status)
    bootstrap = commands.add_parser("bootstrap", help="создать начального администратора")
    bootstrap.add_argument("--no-input", action="store_true", help="не спрашивать пароль")
    bootstrap.set_defaults(func=_bootstrap)
    commands.add_parser("run", help="запустить сервер разработки").set_defaults(func=_run)
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ConfigError, SchemaTooNewError) as exc:
        print(f"Ошибка настройки: {exc}", file=sys.stderr)
        return 2
    except (CommandError, SQLAlchemyError):
        print("Ошибка базы: проверьте подключение и ревизию схемы.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
