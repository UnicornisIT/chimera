"""Резервные копии базы: файл SQLite или `pg_dump` для PostgreSQL.

Копия содержит данные всех организаций, поэтому управление бэкапами доступно
только системному администратору.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from psycopg import pq
from sqlalchemy.engine import make_url

from chimera.core import audit
from chimera.core.config import ConfigError, Settings
from chimera.core.context import NotFound, ServiceContext, ServiceError
from chimera.core.db import Database, utcnow
from chimera.core.permissions import SystemPermission
from chimera.core.sqlite_snapshot import snapshot, sqlite_error_message

PG_DUMP_TIMEOUT_SECONDS = 1800
_NAME = re.compile(r"^chimera-\d{8}-\d{6}(?:-\d+)?\.(?:sqlite3|dump)$")


class BackupError(ServiceError):
    pass


@dataclass(frozen=True, slots=True)
class BackupFile:
    name: str
    size: int
    created_at: datetime


def _restrict(path: Path, mode: int = 0o600) -> None:
    with contextlib.suppress(OSError):  # Windows без POSIX-прав
        path.chmod(mode)


def _target(directory: Path, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    _restrict(directory, 0o700)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    candidate = directory / f"chimera-{stamp}.{suffix}"
    counter = 1
    while candidate.exists():
        candidate = directory / f"chimera-{stamp}-{counter}.{suffix}"
        counter += 1
    return candidate


def _sqlite_backup(database: Database, directory: Path) -> Path:
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            pending = Path(temporary) / "backup.sqlite3"
            snapshot(Path(make_url(database.url).database or ""), pending)
            _restrict(pending)
            while True:
                target = _target(directory, "sqlite3")
                try:
                    if os.name == "nt":
                        pending.rename(target)
                    else:
                        os.link(pending, target)
                except FileExistsError:
                    continue
                return target
    except ConfigError as exc:
        raise BackupError(str(exc)) from None
    except sqlite3.Error as exc:
        raise BackupError(sqlite_error_message(exc, "Не удалось сохранить копию SQLite.")) from None
    except OSError:
        raise BackupError("Не удалось сохранить копию SQLite. Проверьте доступ и место.") from None


def find_pg_dump(configured: str) -> str | None:
    if os.path.sep in configured or (os.path.altsep and os.path.altsep in configured):
        return configured if Path(configured).is_file() else None
    return shutil.which(configured)


def _postgres_backup(database_url: str, directory: Path, pg_dump: str) -> Path:
    executable = find_pg_dump(pg_dump)
    if executable is None:
        raise BackupError(
            f"Не найдена программа pg_dump ({pg_dump}). Установите клиент PostgreSQL той же "
            "основной версии, что и сервер, или укажите полный путь в PG_DUMP_PATH."
        )
    # libpq должен получить те же параметры маршрутизации и TLS, что и приложение.
    url = make_url(database_url)
    _, connect_args = url.get_dialect()().create_connect_args(url)
    libpq_env = {
        option.keyword.decode("ascii"): option.envvar.decode("ascii")
        for option in pq.Conninfo.get_defaults()
        if option.keyword and option.envvar
    }
    env = dict(os.environ)
    for name, value in connect_args.items():
        if name not in libpq_env:
            raise BackupError("Параметры подключения PostgreSQL несовместимы с запуском pg_dump.")
        env[libpq_env[name]] = str(value)
    target = _target(directory, "dump")
    args = [executable, "--format=custom", "--no-owner", "--no-password", f"--file={target}"]
    try:
        completed = subprocess.run(  # noqa: S603
            args,
            env=env,
            capture_output=True,
            text=True,
            timeout=PG_DUMP_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        target.unlink(missing_ok=True)
        raise BackupError("pg_dump не завершился за отведённое время.") from None
    except OSError:
        target.unlink(missing_ok=True)
        raise BackupError("Не удалось запустить pg_dump.") from None
    if completed.returncode != 0:
        target.unlink(missing_ok=True)
        raise BackupError("pg_dump завершился с ошибкой. Проверьте подключение и права PostgreSQL.")
    _restrict(target)
    return target


def create_backup(ctx: ServiceContext, database: Database, settings: Settings) -> BackupFile:
    ctx.require_system(SystemPermission.BACKUPS_MANAGE)
    if database.dialect == "sqlite":
        path = _sqlite_backup(database, settings.backup_dir)
    elif database.dialect == "postgresql":
        path = _postgres_backup(database.url, settings.backup_dir, settings.pg_dump_path)
    else:  # pragma: no cover
        raise BackupError(f"Резервное копирование для {database.dialect} не поддерживается.")
    info = _describe(path)
    audit.record(
        ctx,
        "backup.created",
        entity_type="Backup",
        entity_id=info.name,
        message=f"{info.size} байт",
    )
    prune(ctx, settings)
    return info


def _describe(path: Path) -> BackupFile:
    stat = path.stat()
    return BackupFile(path.name, stat.st_size, datetime.fromtimestamp(stat.st_mtime, UTC))


def list_backups(ctx: ServiceContext, settings: Settings) -> list[BackupFile]:
    ctx.require_system(SystemPermission.BACKUPS_MANAGE)
    if not settings.backup_dir.is_dir():
        return []
    files = [_describe(p) for p in settings.backup_dir.iterdir() if _NAME.fullmatch(p.name)]
    return sorted(files, key=lambda f: f.created_at, reverse=True)


def backup_path(ctx: ServiceContext, settings: Settings, name: str) -> Path:
    ctx.require_system(SystemPermission.BACKUPS_MANAGE)
    if not _NAME.fullmatch(name):
        raise NotFound("Копия не найдена")
    path = settings.backup_dir / name
    if not path.is_file():
        raise NotFound("Копия не найдена")
    return path


def delete_backup(ctx: ServiceContext, settings: Settings, name: str) -> None:
    path = backup_path(ctx, settings, name)
    path.unlink()
    audit.record(ctx, "backup.deleted", entity_type="Backup", entity_id=name)


def prune(ctx: ServiceContext, settings: Settings) -> list[str]:
    """Удаляет копии старше срока хранения, но всегда оставляет три последние."""
    files = list_backups(ctx, settings)
    cutoff = utcnow() - timedelta(days=settings.backup_retention_days)
    removed: list[str] = []
    for item in files[3:]:
        if item.created_at < cutoff:
            (settings.backup_dir / item.name).unlink(missing_ok=True)
            removed.append(item.name)
    if removed:
        audit.record(ctx, "backup.pruned", entity_type="Backup", changes={"removed": removed})
    return removed
