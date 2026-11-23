"""Окружение Alembic для Chimera (PostgreSQL и SQLite)."""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any, Literal

from alembic import context
from alembic.runtime.migration import MigrationContext
from alembic.util.exc import CommandError
from sqlalchemy import Connection
from sqlalchemy.exc import SQLAlchemyError

import chimera.models  # noqa: F401 - регистрирует все таблицы
from chimera.core.config import AppMode, ConfigError, load_settings, validate_settings
from chimera.core.crypto import EncryptedString
from chimera.core.db import Base, Database, UTCDateTime
from chimera.core.migrations import check_unversioned_schema

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _render_item(type_: str, obj: Any, autogen_context: Any) -> str | Literal[False]:
    """Свои TypeDecorator в миграциях записываются базовыми типами: миграции не
    должны зависеть от кода приложения, который со временем меняется."""
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    if type_ == "type" and isinstance(obj, EncryptedString):
        return "sa.Text()"
    return False


def _database_url() -> str:
    url = config.attributes.get("chimera_database_url")
    if url:
        return str(url)
    return load_settings(require_secrets=False).database_url


def run_migrations_offline() -> None:
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=url.startswith("sqlite"),
        compare_type=True,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    settings = load_settings(require_secrets=False)
    if settings.mode is AppMode.SERVER:
        settings = validate_settings(settings)
    database = Database.from_settings(settings)
    try:
        with database.engine.connect() as conn:
            _run(conn)
    finally:
        database.dispose()


def _run(connection: Connection) -> None:
    def check_schema() -> None:
        current = MigrationContext.configure(connection).get_current_revision()
        check_unversioned_schema(connection, current)

    if connection.in_transaction():
        check_schema()
    else:
        with connection.begin():
            check_schema()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


try:
    if context.is_offline_mode():
        run_migrations_offline()
    else:
        run_migrations_online()
except ConfigError as exc:
    raise CommandError(str(exc)) from None
except SQLAlchemyError:
    raise CommandError("Не удалось подключиться к базе или выполнить миграцию") from None
