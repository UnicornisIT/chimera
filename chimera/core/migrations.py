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
from sqlalchemy import Engine

from chimera.core.config import PROJECT_ROOT

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


class SchemaTooNewError(RuntimeError):
    """База обновлена более новой версией Chimera: запуск старой версии опасен."""


def alembic_config(database_url: str, *, migrations_dir: Path = MIGRATIONS_DIR) -> Config:
    config = Config()
    config.set_main_option("script_location", str(migrations_dir))
    # ConfigParser интерпретирует «%», поэтому экранируем (пароли в URL).
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["chimera_database_url"] = database_url
    return config


def upgrade_head(database_url: str) -> None:
    command.upgrade(alembic_config(database_url), "head")


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


def schema_state(engine: Engine, database_url: str) -> SchemaState:
    """Текущая и целевая ревизии. Неизвестная коду ревизия — `SchemaTooNewError`."""
    script = ScriptDirectory.from_config(alembic_config(database_url))
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    if current is not None:
        try:
            known = script.get_revision(current) is not None
        except (ResolutionError, CommandError):
            known = False
        if not known:
            raise SchemaTooNewError(
                f"Схема базы ({current}) создана более новой версией Chimera. Обновите "
                "приложение или восстановите резервную копию, совместимую с этой версией."
            )
    return SchemaState(current, script.get_current_head())
