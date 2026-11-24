"""Владение локальным профилем на время работы процесса."""

from __future__ import annotations

import errno
import os
import sys
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from sqlalchemy.engine import make_url

from chimera.core.config import ConfigError, Settings

PROFILE_MARKER = ".sqlite-initialized"
INITIALIZING_MARKER = ".sqlite-initializing"


def sqlite_path(settings: Settings) -> Path:
    return Path(make_url(settings.database_url).database or "")


def _entries(directory: Path) -> Iterator[Path]:
    try:
        yield from directory.iterdir()
    except FileNotFoundError:
        return


def _previous_database_evidence(settings: Settings, database: Path) -> bool:
    default_database = settings.data_dir / "chimera.db"
    markers = (settings.data_dir / PROFILE_MARKER, settings.data_dir / "profile-settings.json")
    if any(path.exists() for path in markers) or default_database.exists():
        return True
    for path in {database, default_database}:
        if any(
            path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
        ):
            return True
    if any(
        next(_entries(directory), None) is not None
        for directory in {settings.upload_dir, settings.data_dir / "uploads"}
    ):
        return True
    return any(
        (name.startswith("chimera-") and name.endswith(".sqlite3"))
        or name.startswith(("profile-", ".chimera-profile-"))
        for directory in {settings.backup_dir, settings.data_dir / "backups"}
        for name in (entry.name.casefold() for entry in _entries(directory))
    )


@contextmanager
def _lock(path: Path) -> Iterator[None]:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        raise ConfigError("Не удалось открыть каталог профиля. Проверьте права и место.") from None
    with os.fdopen(fd, "rb+") as stream:
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise ConfigError("Локальный профиль уже открыт другим процессом.") from None
            raise ConfigError("Не удалось заблокировать локальный профиль.") from None
        # Закрытие дескриптора освобождает блокировку, включая аварийное завершение.
        yield


@contextmanager
def own_local_profile(settings: Settings) -> Iterator[None]:
    database = sqlite_path(settings)
    try:
        info = database.stat()
    except FileNotFoundError:
        info = None
    except OSError:
        raise ConfigError("Не удалось проверить файл локальной SQLite.") from None
    # Разные имена жёсткой ссылки дали бы разные блокировки и файлы WAL.
    if info is not None and info.st_nlink > 1:
        raise ConfigError("Локальная SQLite не поддерживает жёсткие ссылки на файл базы.")
    locks = {settings.data_dir / ".profile.lock", database.with_name(database.name + ".lock")}
    with ExitStack() as stack:
        for lock in sorted(locks):
            stack.enter_context(_lock(lock))
        try:
            if not database.is_file():
                if _previous_database_evidence(settings, database):
                    raise ConfigError(
                        "База локального профиля отсутствует. Проверьте путь к исходной SQLite "
                        "или восстановите копию в отдельный каталог."
                    )
                initializing = settings.data_dir / INITIALIZING_MARKER
                if not initializing.is_file():
                    if any(
                        (settings.data_dir / name).exists()
                        for name in ("secret_key", "data_encryption.key")
                    ):
                        raise ConfigError(
                            "Есть ключи профиля без SQLite; состояние не определено. "
                            "Укажите исходную базу или выберите отдельный каталог нового профиля."
                        )
                    initializing.touch(exist_ok=False)
        except OSError:
            raise ConfigError("Не удалось проверить состояние локального профиля.") from None
        yield
