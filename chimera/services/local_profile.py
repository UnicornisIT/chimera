"""Подготовка схемы и каталогов Desktop Local под блокировкой профиля."""

from __future__ import annotations

import sqlite3
from contextlib import closing

from sqlalchemy.exc import DBAPIError

from chimera.core.config import ConfigError, Settings
from chimera.core.db import Database
from chimera.core.local_profile import PROFILE_MARKER, sqlite_path
from chimera.core.migrations import schema_state, upgrade_head
from chimera.core.sqlite_snapshot import check_integrity, open_readonly, sqlite_error_message
from chimera.services.local_backup import copy_profile


def prepare_local_schema(database: Database, settings: Settings) -> None:
    path = sqlite_path(settings)
    try:
        settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        settings.upload_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        settings.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_file():
            with closing(open_readonly(path)) as connection:
                check_integrity(connection)
        state = schema_state(database.engine, database.url)
        marker = settings.data_dir / PROFILE_MARKER
        if state.empty and marker.exists():
            raise ConfigError("Ранее настроенная SQLite пуста. Проверьте путь и резервную копию.")
        if not state.up_to_date:
            if state.current is not None:
                copy_profile(settings)
            upgrade_head(database)
        try:
            with marker.open("x"):
                pass
        except FileExistsError:
            pass
    except (sqlite3.Error, DBAPIError) as exc:
        error = exc.orig if isinstance(exc, DBAPIError) else exc
        raise ConfigError(
            sqlite_error_message(
                error, "Не удалось открыть SQLite. Проверьте целостность и доступ к базе."
            )
        ) from None
    except OSError:
        raise ConfigError(
            "Не удалось подготовить локальный профиль. Проверьте права и место."
        ) from None
