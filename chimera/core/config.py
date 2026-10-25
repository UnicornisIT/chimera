"""Типизированная загрузка настроек из окружения и файла `.env`."""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIN_SECRET_KEY_LENGTH = 32


class ConfigError(RuntimeError):
    """Настройки неполны или противоречивы."""


class AppMode(StrEnum):
    SERVER = "server"
    DESKTOP_LOCAL = "desktop-local"


_TRUE = {"1", "true", "yes", "on", "да"}
_FALSE = {"0", "false", "no", "off", "нет", ""}


def _bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"Ожидалось логическое значение, получено: {raw!r}")


def _int(raw: str | None, default: int, *, name: str, minimum: int = 0) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}: ожидалось целое число, получено {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name}: значение должно быть не меньше {minimum}")
    return value


def _path(raw: str | None, default: Path, base: Path) -> Path:
    if raw is None or not raw.strip():
        return default
    path = Path(raw).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    mode: AppMode
    database_url: str
    secret_key: str
    data_encryption_key: str
    blind_index_key: str | None
    data_dir: Path
    upload_dir: Path
    backup_dir: Path
    max_upload_mb: int
    backup_retention_days: int
    pg_dump_path: str
    admin_default_username: str
    admin_default_password: str | None
    default_organization_name: str
    default_organization_type: str
    session_cookie_secure: bool
    session_lifetime_minutes: int
    login_max_attempts: int
    login_window_seconds: int
    enable_hsts: bool
    host: str
    port: int
    debug: bool
    testing: bool = False

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024


def _read_env(env: Mapping[str, str] | None, env_file: Path | None) -> dict[str, str]:
    if env is not None:
        return dict(env)
    values: dict[str, str] = {}
    candidate = env_file if env_file is not None else PROJECT_ROOT / ".env"
    if candidate.is_file():
        values.update({k: v for k, v in dotenv_values(candidate).items() if v is not None})
    values.update(os.environ)
    return values


def _persistent_secret(data_dir: Path, name: str) -> str:
    """Секрет desktop-режима: создаётся один раз и хранится в каталоге данных."""
    path = data_dir / name
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(48)
    path.write_text(value, encoding="utf-8")
    with contextlib.suppress(OSError):  # Windows без POSIX-прав
        path.chmod(0o600)
    return value


def _persistent_fernet_key(data_dir: Path) -> str:
    from cryptography.fernet import Fernet

    path = data_dir / "data_encryption.key"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    data_dir.mkdir(parents=True, exist_ok=True)
    value = Fernet.generate_key().decode("ascii")
    path.write_text(value, encoding="utf-8")
    with contextlib.suppress(OSError):  # Windows без POSIX-прав
        path.chmod(0o600)
    return value


def load_settings(
    env: Mapping[str, str] | None = None,
    *,
    env_file: Path | None = None,
    require_secrets: bool = True,
) -> Settings:
    """Собирает настройки. `env` задаётся в тестах, иначе читаются `.env` и окружение.

    `require_secrets=False` — для служебных команд (миграции), которым ключи не нужны.
    """
    values = _read_env(env, env_file)
    get = values.get

    try:
        mode = AppMode(get("CHIMERA_MODE", AppMode.SERVER.value).strip())
    except ValueError as exc:
        raise ConfigError("CHIMERA_MODE должен быть 'server' или 'desktop-local'") from exc

    data_dir = _path(get("CHIMERA_DATA_DIR"), PROJECT_ROOT / "instance", PROJECT_ROOT)
    upload_dir = _path(get("CHIMERA_UPLOAD_DIR"), data_dir / "uploads", PROJECT_ROOT)
    backup_dir = _path(get("CHIMERA_BACKUP_DIR"), data_dir / "backups", PROJECT_ROOT)

    database_url = (get("DATABASE_URL") or "").strip()
    if not database_url:
        database_url = f"sqlite:///{(data_dir / 'chimera.db').as_posix()}"
    if database_url.startswith("postgres://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix("postgres://")
    elif database_url.startswith("postgresql://"):
        database_url = "postgresql+psycopg://" + database_url.removeprefix("postgresql://")
    if mode is AppMode.DESKTOP_LOCAL and not database_url.startswith("sqlite"):
        raise ConfigError("В режиме desktop-local поддерживается только SQLite")

    testing = _bool(get("CHIMERA_TESTING"), False)
    secret_key = (get("SECRET_KEY") or "").strip()
    encryption_key = (get("DATA_ENCRYPTION_KEY") or "").strip()
    if mode is AppMode.DESKTOP_LOCAL and require_secrets:
        secret_key = secret_key or _persistent_secret(data_dir, "secret_key")
        encryption_key = encryption_key or _persistent_fernet_key(data_dir)
    if require_secrets and not testing:
        if len(secret_key) < MIN_SECRET_KEY_LENGTH:
            raise ConfigError(
                f"SECRET_KEY не задан или короче {MIN_SECRET_KEY_LENGTH} символов. "
                'Сгенерируйте: python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        if not encryption_key:
            raise ConfigError(
                "DATA_ENCRYPTION_KEY не задан. Сгенерируйте: python -c "
                '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )

    org_type = (get("CHIMERA_DEFAULT_ORG_TYPE") or "college").strip()
    if org_type not in {"university", "college"}:
        raise ConfigError("CHIMERA_DEFAULT_ORG_TYPE должен быть 'university' или 'college'")

    return Settings(
        mode=mode,
        database_url=database_url,
        secret_key=secret_key,
        data_encryption_key=encryption_key,
        blind_index_key=(get("BLIND_INDEX_KEY") or "").strip() or None,
        data_dir=data_dir,
        upload_dir=upload_dir,
        backup_dir=backup_dir,
        max_upload_mb=_int(get("MAX_UPLOAD_SIZE_MB"), 16, name="MAX_UPLOAD_SIZE_MB", minimum=1),
        backup_retention_days=_int(
            get("BACKUP_RETENTION_DAYS"), 30, name="BACKUP_RETENTION_DAYS", minimum=1
        ),
        pg_dump_path=(get("PG_DUMP_PATH") or "pg_dump").strip(),
        admin_default_username=(get("ADMIN_DEFAULT_USERNAME") or "admin").strip(),
        admin_default_password=get("ADMIN_DEFAULT_PASSWORD") or None,
        default_organization_name=(
            get("CHIMERA_DEFAULT_ORG_NAME") or "Моя образовательная организация"
        ).strip(),
        default_organization_type=org_type,
        session_cookie_secure=_bool(get("SESSION_COOKIE_SECURE"), mode is AppMode.SERVER),
        session_lifetime_minutes=_int(
            get("SESSION_LIFETIME_MINUTES"), 480, name="SESSION_LIFETIME_MINUTES", minimum=5
        ),
        login_max_attempts=_int(get("LOGIN_MAX_ATTEMPTS"), 5, name="LOGIN_MAX_ATTEMPTS", minimum=1),
        login_window_seconds=_int(
            get("LOGIN_WINDOW_SECONDS"), 600, name="LOGIN_WINDOW_SECONDS", minimum=10
        ),
        enable_hsts=_bool(get("ENABLE_HSTS"), False),
        host=(get("APP_HOST") or "127.0.0.1").strip(),
        port=_int(get("APP_PORT"), 5000, name="APP_PORT", minimum=1),
        debug=_bool(get("APP_DEBUG"), False),
        testing=testing,
    )
