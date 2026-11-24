"""Alembic из кода: обновление схемы и проверка её версии (по мотивам db_safety.py)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.script.revision import ResolutionError
from alembic.util.exc import CommandError
from sqlalchemy import Connection, Engine, inspect

from chimera.core.config import ConfigError
from chimera.core.db import Database

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"
_REQUIRED_RESOURCES = ("env.py", "script.py.mako", "versions/20261025_0001_initial_schema.py")


class SchemaTooNewError(RuntimeError):
    """База обновлена более новой версией Chimera: запуск старой версии опасен."""


def alembic_config(database_url: str, *, migrations_dir: Path = MIGRATIONS_DIR) -> Config:
    if any(not (migrations_dir / resource).is_file() for resource in _REQUIRED_RESOURCES):
        raise ConfigError("В установке Chimera отсутствуют ресурсы миграций Alembic.")
    config = Config()
    # ConfigParser интерпретирует «%», поэтому экранируем (пароли в URL).
    config.set_main_option("script_location", str(migrations_dir.resolve()).replace("%", "%%"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["chimera_database_url"] = database_url
    return config


def upgrade_head(database: Database) -> None:
    config = alembic_config(database.url)
    with database.engine.begin() as connection:
        if database.dialect == "sqlite":
            # Драйвер SQLite не начинает транзакцию автоматически при DDL.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


@dataclass(frozen=True, slots=True)
class SchemaState:
    current: str | None
    head: str | None

    @property
    def up_to_date(self) -> bool:
        return self.current == self.head

    @property
    def empty(self) -> bool:
        return self.current is None


def check_unversioned_schema(connection: Connection, current: str | None) -> None:
    if current is None and set(inspect(connection).get_table_names()) - {"alembic_version"}:
        raise ConfigError(
            "В базе есть таблицы без ревизии Alembic. Укажите отдельную базу Chimera."
        )


def schema_state(engine: Engine, database_url: str) -> SchemaState:
    """Текущая и целевая ревизии. Неизвестная коду ревизия — `SchemaTooNewError`."""
    script = ScriptDirectory.from_config(alembic_config(database_url))
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
        check_unversioned_schema(connection, current)
    if current is not None:
        try:
            revision = script.get_revision(current)
            known = revision is not None and revision.revision == current
        except (ResolutionError, CommandError):
            known = False
        if not known:
            raise SchemaTooNewError(
                "Ревизия схемы базы неизвестна этой версии Chimera. Обновите "
                "приложение или восстановите резервную копию, совместимую с этой версией."
            )
    return SchemaState(current, script.get_current_head())
