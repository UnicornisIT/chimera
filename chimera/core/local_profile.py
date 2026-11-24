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


def sqlite_path(settings: Settings) -> Path:
    return Path(make_url(settings.database_url).database or "")


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
        if not database.is_file():
            marker = settings.data_dir / PROFILE_MARKER
            has_uploads = settings.upload_dir.is_dir() and any(settings.upload_dir.iterdir())
            if marker.exists() or has_uploads:
                raise ConfigError(
                    "База локального профиля отсутствует. Проверьте путь и резервную копию."
                )
        yield
