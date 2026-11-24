"""Командная строка: `python -m chimera <команда>`."""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
import webbrowser
from contextlib import nullcontext
from pathlib import Path
from typing import cast

from alembic.util.exc import CommandError
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.serving import ThreadedWSGIServer, make_server

from chimera.core.config import (
    LOOPBACK,
    AppMode,
    ConfigError,
    Settings,
    load_local_keys,
    load_settings,
    validate_settings,
)
from chimera.core.context import ServiceError
from chimera.core.db import Database
from chimera.core.local_profile import own_local_profile, sqlite_path
from chimera.core.metadata import app_version
from chimera.core.migrations import SchemaTooNewError, schema_state, upgrade_head


def _migrate(_args: argparse.Namespace) -> int:
    from chimera.services.local_profile import prepare_local_schema

    settings = load_settings(require_secrets=False)
    desktop = settings.mode is AppMode.DESKTOP_LOCAL
    with own_local_profile(settings) if desktop else nullcontext():
        settings = load_local_keys(settings) if desktop else validate_settings(settings)
        database = Database.from_settings(settings)
        try:
            if desktop:
                prepare_local_schema(database, settings)
            elif not schema_state(database.engine, database.url).up_to_date:
                upgrade_head(database)
        finally:
            database.dispose()
    print("Схема базы обновлена.")
    return 0


def _status(_args: argparse.Namespace) -> int:
    settings = load_settings(require_secrets=False)
    if settings.is_sqlite and not sqlite_path(settings).is_file():
        print("Файл SQLite ещё не создан.")
        return 1
    with own_local_profile(settings) if settings.is_sqlite else nullcontext():
        database = Database.from_settings(settings)
        try:
            state = schema_state(database.engine, database.url)
        finally:
            database.dispose()
    print(f"Chimera {app_version()} · режим {settings.mode.value} · БД {database.dialect}")
    print(f"Ревизия схемы: {state.current or 'пусто'}, требуется: {state.head}")
    return 0 if state.up_to_date else 1


def _bootstrap(args: argparse.Namespace) -> int:
    settings = load_settings(require_secrets=False)
    if settings.is_sqlite and not sqlite_path(settings).is_file():
        raise ConfigError("Перед bootstrap выполните python -m chimera migrate")
    with own_local_profile(settings) if settings.is_sqlite else nullcontext():
        return _bootstrap_admin(args, settings)


def _bootstrap_admin(args: argparse.Namespace, settings: Settings) -> int:
    """Создаёт начального администратора (пароль из ADMIN_DEFAULT_PASSWORD или ввод)."""
    from dataclasses import replace

    from chimera.services.auth import ensure_initial_admin

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
    from chimera.web.local_setup import SETUP_PENDING

    settings = load_settings(require_secrets=False)
    if settings.mode is not AppMode.DESKTOP_LOCAL:
        validate_settings(settings)
        raise ConfigError("Режим server запускается через chimera.wsgi за HTTPS-прокси")
    with own_local_profile(settings):
        token = secrets.token_urlsafe(32)
        settings = load_local_keys(settings)
        app = create_app(settings, setup_token=token)
        app.debug = settings.debug
        try:
            server = cast(
                ThreadedWSGIServer, make_server(LOOPBACK, settings.port, app, threaded=True)
            )
            with server:
                server.daemon_threads = False
                if app.extensions.get(SETUP_PENDING):
                    address = f"http://{LOOPBACK}:{settings.port}/local-setup#{token}"
                    try:
                        launched = webbrowser.open(address)
                    except webbrowser.Error:
                        launched = False
                    if not launched:
                        raise ConfigError("Не удалось открыть окно настройки. Проверьте браузер.")
                server.serve_forever()
        finally:
            app.extensions["chimera.db"].dispose()
    return 0


def _backup_profile(_args: argparse.Namespace) -> int:
    from chimera.services.local_backup import copy_profile

    settings = _local_settings()
    if not sqlite_path(settings).is_file():
        raise ConfigError("Для резервирования требуется существующий локальный профиль.")
    with own_local_profile(settings):
        target = copy_profile(load_local_keys(settings))
    print(f"Полная копия создана: {target.name}.")
    print("Копия содержит конфиденциальные данные и ключи. Храните её с ограниченным доступом.")
    return 0


def _restore_profile(args: argparse.Namespace) -> int:
    from chimera.services.local_backup import restore_profile

    if not args.confirm:
        raise ConfigError("Подтвердите проверочное восстановление параметром --confirm.")
    restore_profile(_local_settings(), args.source, args.destination)
    print("Проверочный профиль восстановлен.")
    return 0


def _local_settings() -> Settings:
    settings = load_settings(require_secrets=False)
    if settings.mode is not AppMode.DESKTOP_LOCAL:
        raise ConfigError("Команда доступна в режиме desktop-local.")
    return settings


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
    commands.add_parser("run", help="запустить Desktop Local").set_defaults(func=_run)
    commands.add_parser(
        "backup-profile", help="создать полную копию локального профиля"
    ).set_defaults(func=_backup_profile)
    restore = commands.add_parser("restore-profile", help="восстановить профиль для проверки")
    restore.add_argument("source", type=Path, help="каталог полной копии")
    restore.add_argument("destination", type=Path, help="новый проверочный каталог")
    restore.add_argument("--confirm", action="store_true", help="подтвердить восстановление")
    restore.set_defaults(func=_restore_profile)
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
