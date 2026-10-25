"""Окружение Alembic для Chimera (PostgreSQL и SQLite)."""

from __future__ import annotations

from logging.config import fileConfig
from typing import Any

from alembic import context

import chimera.models  # noqa: F401 - регистрирует все таблицы
from chimera.core.config import load_settings
from chimera.core.crypto import EncryptedString
from chimera.core.db import Base, UTCDateTime, make_engine

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _render_item(type_: str, obj: Any, autogen_context: Any) -> str | bool:
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
    engine = make_engine(_database_url())
    try:
        with engine.connect() as conn:
            _run(conn)
    finally:
        engine.dispose()


def _run(connection: object) -> None:
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",  # type: ignore[attr-defined]
        compare_type=True,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
