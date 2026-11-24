"""Снимок зафиксированных данных SQLite, включая WAL."""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic

from chimera.core.config import ConfigError


def sqlite_error_message(error: BaseException | None, fallback: str) -> str:
    code = getattr(error, "sqlite_errorcode", 0) & 0xFF
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return "База SQLite занята. Дождитесь завершения операции и повторите попытку."
    if code == sqlite3.SQLITE_FULL:
        return "Недостаточно свободного места для записи SQLite."
    if code in {sqlite3.SQLITE_READONLY, sqlite3.SQLITE_PERM, sqlite3.SQLITE_CANTOPEN}:
        return "Нет доступа к базе SQLite. Проверьте права на файл и каталог."
    if code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
        return "База SQLite повреждена. Проверьте резервную копию."
    return fallback


def open_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
    configure_functions(connection)
    return connection


def configure_functions(connection: sqlite3.Connection) -> None:
    connection.create_function(
        "chimera_lower",
        1,
        lambda value: value.lower() if value is not None else None,
        deterministic=True,
    )


def check_integrity(connection: sqlite3.Connection) -> None:
    if (
        connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
        or connection.execute("PRAGMA foreign_key_check").fetchone() is not None
    ):
        raise ConfigError("База SQLite не прошла проверку целостности.")


def snapshot(source: Path, target: Path) -> None:
    deadline = monotonic() + 60

    def check_progress(_status: int, _remaining: int, _total: int) -> None:
        if monotonic() > deadline:
            raise ConfigError("Не удалось завершить копирование SQLite за отведённое время.")

    with closing(open_readonly(source)) as reader, closing(sqlite3.connect(target)) as writer:
        pages = reader.execute("PRAGMA page_count").fetchone()[0]
        page_size = reader.execute("PRAGMA page_size").fetchone()[0]
        if shutil.disk_usage(target.parent).free < pages * page_size:
            raise ConfigError("Недостаточно свободного места для резервной копии SQLite.")
        configure_functions(writer)
        reader.backup(writer, pages=256, progress=check_progress, sleep=0.1)
        writer.execute("PRAGMA journal_mode=DELETE")
        check_integrity(writer)
