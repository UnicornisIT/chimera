"""Полная копия остановленного локального профиля и проверочное восстановление."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from pathlib import Path
from uuid import uuid4

from alembic.script import ScriptDirectory
from alembic.script.revision import ResolutionError
from alembic.util.exc import CommandError

from chimera.core.config import ConfigError, Settings, load_settings
from chimera.core.local_profile import PROFILE_MARKER, sqlite_path
from chimera.core.migrations import alembic_config
from chimera.core.private_directory import make_private_directory
from chimera.core.sqlite_snapshot import check_integrity, open_readonly, snapshot

MANIFEST = "manifest.json"
PROFILE_FILES = {"chimera.db", "secret_key", "data_encryption.key", "profile-settings.json"}


def _safe_path(path: Path) -> Path:
    path = path.absolute()
    if ".." in path.parts:
        raise ConfigError("Укажите путь без переходов в родительский каталог.")
    for entry in (*reversed(path.parents), path):
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ConfigError(
                "Для копирования и восстановления запрещены ссылки и точки подключения."
            )
    return path.resolve()


def _files(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for entry in root.iterdir():
        _safe_path(entry)
        if entry.is_dir():
            for relative, child in _files(entry).items():
                result[f"{entry.name}/{relative}"] = child
        elif entry.is_file():
            result[entry.name] = entry
        else:
            raise ConfigError("Копия содержит неподдерживаемый тип файла.")
    return result


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _directory_chain(path: Path) -> tuple[tuple[int, int], ...]:
    _safe_path(path)
    return tuple(_identity(part.stat()) for part in (*reversed(path.parents), path))


@contextmanager
def _private_stage(
    parent: Path, parent_identity: tuple[tuple[int, int], ...]
) -> Iterator[tuple[Path, tuple[tuple[int, int], ...]]]:
    if _directory_chain(parent) != parent_identity:
        raise ConfigError("Родительский каталог изменился.")
    stage = parent / f".chimera-profile-{uuid4().hex}"
    make_private_directory(stage)
    identity = _directory_chain(stage)
    try:
        yield stage, identity
    finally:
        with suppress(OSError, ConfigError):
            if _directory_chain(stage) == identity:
                shutil.rmtree(stage)


def _copy_tree(source: Path, target: Path) -> None:
    source_identity = _identity(_safe_path(source).stat())
    for entry in source.iterdir():
        _safe_path(entry)
        info = entry.lstat()
        if _identity(source.stat()) != source_identity:
            raise ConfigError("Каталог источника изменился во время копирования.")
        output = target / entry.name
        if stat.S_ISDIR(info.st_mode):
            output.mkdir(mode=0o700)
            _copy_tree(entry, output)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            with entry.open("rb") as reader, output.open("xb") as writer:
                current = os.fstat(reader.fileno())
                if _identity(current) != _identity(info) or current.st_nlink != 1:
                    raise ConfigError("Файл источника изменился во время копирования.")
                shutil.copyfileobj(reader, writer)
        else:
            raise ConfigError("Копия содержит неподдерживаемый тип файла или жёсткую ссылку.")


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _revision(path: Path, settings: Settings) -> str:
    with closing(open_readonly(path)) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
        if not {"users", "memberships", "organizations", "audit_log", "login_attempts"} <= tables:
            raise ConfigError("В базе копии отсутствуют обязательные таблицы профиля.")
        check_integrity(connection)
        rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    script = ScriptDirectory.from_config(alembic_config(settings.database_url))
    try:
        if len(rows) == 1 and isinstance(rows[0][0], str):
            current = rows[0][0]
            revision = script.get_revision(current)
            if revision is not None and revision.revision == current:
                return current
    except (ResolutionError, CommandError):
        pass
    raise ConfigError("Ревизия копии не поддерживается этой версией Chimera.")


def _write_private(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(value)


def copy_profile(settings: Settings) -> Path:
    try:
        source = _safe_path(sqlite_path(settings))
        uploads = _safe_path(settings.upload_dir)
        directory = _safe_path(settings.backup_dir)
        if any(path.is_relative_to(uploads) for path in (directory, source, settings.data_dir)):
            raise ConfigError("Каталог вложений должен быть отделён от базы и резервных копий.")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_identity = _directory_chain(directory)
        with _private_stage(directory, parent_identity) as (stage, stage_identity):
            snapshot(source, stage / "chimera.db")
            revision = _revision(stage / "chimera.db", settings)
            _write_private(stage / "secret_key", settings.secret_key)
            _write_private(stage / "data_encryption.key", settings.data_encryption_key)
            _write_private(
                stage / "profile-settings.json",
                json.dumps({"blind_index_key": settings.blind_index_key}),
            )
            (stage / "uploads").mkdir(mode=0o700)
            _copy_tree(uploads, stage / "uploads")
            manifest = {
                "format": 1,
                "revision": revision,
                "files": {name: _digest(path) for name, path in _files(stage).items()},
            }
            _write_private(stage / MANIFEST, json.dumps(manifest, sort_keys=True))
            _verified_profile(stage, settings)
            target = directory / f"profile-{uuid4().hex}"
            if (
                _directory_chain(directory) != parent_identity
                or _directory_chain(stage) != stage_identity
            ):
                raise ConfigError("Каталог копии изменился. Повторите операцию в личном каталоге.")
            stage.rename(target)
            return target
    except (OSError, sqlite3.Error, RecursionError):
        raise ConfigError(
            "Не удалось создать полную копию. Проверьте профиль, доступ и место."
        ) from None


def _unique_manifest_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ConfigError("Манифест содержит повторяющиеся поля или пути.")
    return fields


def _verified_profile(stage: Path, settings: Settings) -> None:
    manifest = json.loads(
        (stage / MANIFEST).read_text(encoding="utf-8"),
        object_pairs_hook=_unique_manifest_fields,
    )
    files = _files(stage)
    del files[MANIFEST]
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != 1
        or not files.keys() >= PROFILE_FILES
        or not (stage / "uploads").is_dir()
        or {p.name for p in stage.iterdir()} != PROFILE_FILES | {"uploads", MANIFEST}
        or {name: _digest(path) for name, path in files.items()} != manifest.get("files")
    ):
        raise ConfigError("Полная копия неполна или повреждена.")
    if _revision(stage / "chimera.db", settings) != manifest.get("revision"):
        raise ConfigError("Ревизия базы не совпадает с описанием копии.")
    restored = load_settings({"CHIMERA_MODE": "desktop-local", "CHIMERA_DATA_DIR": str(stage)})
    local_options = json.loads((stage / "profile-settings.json").read_text(encoding="utf-8"))
    if (
        not restored.secret_key
        or not isinstance(local_options, dict)
        or not isinstance(local_options.get("blind_index_key"), (str, type(None)))
        or set(local_options) != {"blind_index_key"}
    ):
        raise ConfigError("Настройки полной копии повреждены.")


def restore_profile(settings: Settings, source: Path, destination: Path) -> None:
    try:
        source = _safe_path(source)
        requested_destination = destination.absolute()
        destination = _safe_path(requested_destination)
        protected = (settings.data_dir, settings.upload_dir, settings.backup_dir, source)
        if destination == Path.cwd().resolve() or any(
            destination.is_relative_to(path) or path.is_relative_to(destination)
            for path in (*protected, sqlite_path(settings))
        ):
            raise ConfigError("Выберите новый каталог вне рабочего профиля и резервных копий.")
        parent_identity = _directory_chain(requested_destination.parent)

        def check_destination() -> None:
            if (
                _safe_path(requested_destination) != destination
                or _directory_chain(requested_destination.parent) != parent_identity
            ):
                raise ConfigError("Родительский каталог назначения изменился.")
            if destination.exists():
                raise ConfigError("Каталог назначения уже существует.")

        check_destination()
        if {entry.name for entry in source.iterdir()} != PROFILE_FILES | {"uploads", MANIFEST}:
            raise ConfigError("Полная копия неполна или повреждена.")
        with _private_stage(destination.parent, parent_identity) as (stage, stage_identity):
            _copy_tree(source, stage)
            _verified_profile(stage, settings)
            (stage / MANIFEST).unlink()
            _write_private(stage / PROFILE_MARKER, "")
            if _directory_chain(stage) != stage_identity:
                raise ConfigError("Временный каталог восстановления изменился.")
            check_destination()
            if os.name == "nt":
                # Windows не заменяет существующий каталог при rename.
                stage.rename(destination)
            else:
                destination.mkdir(mode=0o700)
                try:
                    stage.replace(destination)
                except OSError:
                    destination.rmdir()
                    raise
    except (OSError, sqlite3.Error, UnicodeError, ValueError, RecursionError):
        raise ConfigError(
            "Не удалось восстановить профиль. Проверьте копию, доступ и место."
        ) from None
