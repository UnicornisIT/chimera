"""Типизированная загрузка настроек из окружения и файла `.env`."""

from __future__ import annotations

import base64
import binascii
import os
import re
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path, PureWindowsPath

from cryptography.fernet import Fernet
from dotenv import dotenv_values
from psycopg import ProgrammingError
from psycopg.conninfo import make_conninfo
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIN_SECRET_KEY_LENGTH = 32
LOOPBACK = "127.0.0.1"
POSTGRES_MAJOR = 16
TEST_POSTGRES_NAME = "chimera_stage02"
TEST_STAND_MARKER = ".stage-02"


class ConfigError(RuntimeError):
    """Настройки неполны или противоречивы."""


class AppMode(StrEnum):
    SERVER = "server"
    DESKTOP_LOCAL = "desktop-local"


_TRUE = {"1", "true", "yes", "on", "да"}
_FALSE = {"0", "false", "no", "off", "нет"}


def _bool(raw: str | None, default: bool, *, name: str) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ConfigError(f"{name}: ожидалось логическое значение")


def _int(raw: str | None, default: int, *, name: str, minimum: int = 0) -> int:
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name}: ожидалось целое число") from None
    if value < minimum:
        raise ConfigError(f"{name}: значение должно быть не меньше {minimum}")
    return value


def _path(raw: str | None, default: Path, base: Path, *, name: str) -> Path:
    if raw is not None and not raw.strip():
        raise ConfigError(f"{name}: путь пуст")
    value = raw if raw is not None else str(default)
    windows = PureWindowsPath(value)
    if (
        any(ord(char) < 32 for char in value)
        or (windows.drive and (os.name != "nt" or not windows.is_absolute()))
        or (os.name == "nt" and windows.root and not windows.drive)
        or (
            os.name == "nt"
            and any(
                PureWindowsPath(part).is_reserved()
                or part.endswith((" ", "."))
                or any(char in part for char in '<>:"|?*')
                for part in windows.parts[bool(windows.anchor) :]
                if part != ".."
            )
        )
    ):
        raise ConfigError(f"{name}: недопустимый путь")
    try:
        return (base / Path(value).expanduser()).resolve()
    except (OSError, ValueError, RuntimeError):
        raise ConfigError(f"{name}: не удалось разрешить путь") from None


def _check_path(path: Path, *, name: str, directory: bool = True, writable: bool = True) -> None:
    try:
        candidate = path
        while True:
            try:
                candidate.stat()
                break
            except FileNotFoundError:
                if candidate == candidate.parent:
                    raise
                candidate = candidate.parent
        valid_type = candidate.is_dir() if directory or candidate != path else candidate.is_file()
        if not valid_type or not os.access(candidate, os.R_OK | (os.W_OK if writable else 0)):
            raise ConfigError(f"{name}: путь недоступен или имеет неверный тип")
    except (OSError, ValueError):
        raise ConfigError(f"{name}: путь недоступен") from None


def _check_desktop_location(path: Path, *, name: str) -> None:
    roots = [PROJECT_ROOT / "build", PROJECT_ROOT / "dist"]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        roots.append(Path(bundle))
    if any(path.is_relative_to(root.resolve()) for root in roots):
        raise ConfigError(f"{name}: данные нельзя хранить в каталоге сборки или установщика")


def _desktop_dir(values: Mapping[str, str]) -> Path:
    legacy = PROJECT_ROOT / "instance"
    if os.name != "nt" and (PROJECT_ROOT / "pyproject.toml").is_file():
        return legacy
    legacy_entries = ("chimera.db", "secret_key", "data_encryption.key", "uploads", "backups")
    if any((legacy / name).exists() for name in legacy_entries):
        raise ConfigError(
            "Обнаружен прежний каталог Desktop Local. "
            "Укажите его явно в CHIMERA_DATA_DIR, чтобы продолжить с прежними данными."
        )
    if os.name == "nt":
        local = values.get("LOCALAPPDATA")
        return (Path(local) if local else Path.home() / "AppData" / "Local") / "Chimera"
    xdg_data = Path(values.get("XDG_DATA_HOME", ""))
    return (xdg_data if xdg_data.is_absolute() else Path.home() / ".local" / "share") / "Chimera"


def _directory_base() -> Path:
    # В установленном пакете PROJECT_ROOT может указывать на общий site-packages.
    return PROJECT_ROOT if (PROJECT_ROOT / "pyproject.toml").is_file() else Path.home()


def _database_url(raw: str, mode: AppMode, data_dir: Path) -> str:
    if not raw:
        if mode is AppMode.SERVER:
            raise ConfigError("DATABASE_URL обязателен в режиме server")
        raw = URL.create("sqlite", database=(data_dir / "chimera.db").as_posix()).render_as_string(
            hide_password=False
        )
    try:
        if any(ord(char) < 32 for char in raw):
            raise ValueError
        url = make_url(raw)
    except (ArgumentError, ValueError):
        raise ConfigError("DATABASE_URL: некорректный URL базы данных") from None

    if mode is AppMode.DESKTOP_LOCAL:
        if url.drivername not in {"sqlite", "sqlite+pysqlite"}:
            raise ConfigError("В режиме desktop-local поддерживается только SQLite (pysqlite)")
        if (
            not url.database
            or url.database == ":memory:"
            or url.database.startswith("file:")
            or "?" in raw
            or any(part is not None for part in (url.host, url.port, url.username, url.password))
        ):
            raise ConfigError("DATABASE_URL: требуется URL файла SQLite без параметров")
        path = _path(url.database, data_dir / "chimera.db", Path.cwd(), name="DATABASE_URL")
        _check_desktop_location(path, name="DATABASE_URL")
        if "?" in str(path):
            raise ConfigError("DATABASE_URL: недопустимый путь SQLite")
        _check_path(path, name="DATABASE_URL", directory=False)
        _check_path(path.parent, name="DATABASE_URL")
        url = URL.create(url.drivername, database=path.as_posix())
    else:
        if url.drivername not in {"postgres", "postgresql", "postgresql+psycopg"}:
            raise ConfigError("В режиме server поддерживается только PostgreSQL через psycopg")
        if (
            not (url.host or url.query.get("host"))
            or not url.database
            or "@" in (url.host or "")
            or any(char.isspace() for char in (url.host or ""))
            or re.search(r"%(?![0-9a-fA-F]{2})", raw)
            or ("?" in raw and not url.query)
            or (url.port is not None and not 1 <= url.port <= 65535)
        ):
            raise ConfigError("DATABASE_URL: некорректный адрес PostgreSQL")
        url = url.set(drivername="postgresql+psycopg")
        try:
            _, connect_args = url.get_dialect()().create_connect_args(url)
            if any("\x00" in str(value) for value in connect_args.values()):
                raise ValueError
            make_conninfo(**connect_args)
        except (ArgumentError, ProgrammingError, ValueError):
            raise ConfigError("DATABASE_URL: некорректные параметры PostgreSQL") from None
    return url.render_as_string(hide_password=False)


@dataclass(frozen=True, slots=True)
class Settings:
    mode: AppMode
    database_url: str = field(repr=False)
    secret_key: str = field(repr=False)
    data_encryption_key: str = field(repr=False)
    blind_index_key: str | None = field(repr=False)
    data_dir: Path
    upload_dir: Path
    backup_dir: Path
    max_upload_mb: int
    backup_retention_days: int
    pg_dump_path: str
    admin_default_username: str
    admin_default_password: str | None = field(repr=False)
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
    test_postgres_dir: Path | None = None

    @property
    def is_sqlite(self) -> bool:
        return make_url(self.database_url).get_backend_name() == "sqlite"

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


def _check_secrets(settings: Settings, *, required: bool) -> None:
    secret = settings.secret_key
    server = settings.mode is AppMode.SERVER
    if required or secret:
        weak = server and (
            len(set(secret)) < 8
            or any(
                marker in secret.casefold() for marker in ("change-me", "changeme", "replace-with")
            )
        )
        if len(secret) < MIN_SECRET_KEY_LENGTH or weak or not secret.isprintable():
            raise ConfigError("SECRET_KEY: задайте случайный ключ длиной от 32 символов")
    if required or settings.data_encryption_key:
        for part in settings.data_encryption_key.split(","):
            try:
                key = part.strip().encode("ascii")
                decoded = base64.b64decode(key, altchars=b"-_", validate=True)
                if base64.urlsafe_b64encode(decoded) != key or (server and len(set(decoded)) < 2):
                    raise ValueError
                Fernet(key)
            except (ValueError, binascii.Error):
                raise ConfigError(
                    "DATA_ENCRYPTION_KEY: требуется действительный ключ Fernet"
                ) from None


def validate_settings(settings: Settings, *, require_secrets: bool = True) -> Settings:
    """Проверяет в том числе настройки, созданные Python-кодом."""
    if not isinstance(settings.mode, AppMode):
        raise ConfigError("CHIMERA_MODE должен быть 'server' или 'desktop-local'")
    if type(settings.port) is not int or not 1 <= settings.port <= 65535:
        raise ConfigError("APP_PORT: допустимый диапазон 1–65535")
    for name, value in (
        ("APP_DEBUG", settings.debug),
        ("SESSION_COOKIE_SECURE", settings.session_cookie_secure),
        ("ENABLE_HSTS", settings.enable_hsts),
        ("CHIMERA_TESTING", settings.testing),
    ):
        if type(value) is not bool:
            raise ConfigError(f"{name}: ожидалось логическое значение")
    for name, number, minimum in (
        ("MAX_UPLOAD_SIZE_MB", settings.max_upload_mb, 1),
        ("BACKUP_RETENTION_DAYS", settings.backup_retention_days, 1),
        ("SESSION_LIFETIME_MINUTES", settings.session_lifetime_minutes, 5),
        ("LOGIN_MAX_ATTEMPTS", settings.login_max_attempts, 1),
        ("LOGIN_WINDOW_SECONDS", settings.login_window_seconds, 10),
    ):
        if type(number) is not int or number < minimum:
            raise ConfigError(f"{name}: значение должно быть не меньше {minimum}")
    if not settings.host or any(char.isspace() or not char.isprintable() for char in settings.host):
        raise ConfigError("APP_HOST: недопустимое значение")
    desktop = settings.mode is AppMode.DESKTOP_LOCAL
    if desktop or settings.testing:
        if settings.host != LOOPBACK:
            raise ConfigError("APP_HOST: разрешён только 127.0.0.1")
        if settings.session_cookie_secure or settings.enable_hsts:
            raise ConfigError("SESSION_COOKIE_SECURE и ENABLE_HSTS должны быть выключены для HTTP")
    if desktop:
        if settings.test_postgres_dir is not None:
            raise ConfigError("Проверка тестового кластера предназначена для PostgreSQL")
    else:
        if settings.testing != (settings.test_postgres_dir is not None):
            raise ConfigError("Для тестового Server требуется выделенный кластер PostgreSQL")
        if not settings.testing and not settings.session_cookie_secure:
            raise ConfigError("SESSION_COOKIE_SECURE должен быть включён в режиме server")
    if settings.debug and (not desktop or settings.testing):
        raise ConfigError("APP_DEBUG недоступен в режиме server и при тестировании")

    paths = {}
    for field_name, path in (
        ("data_dir", settings.data_dir),
        ("upload_dir", settings.upload_dir),
        ("backup_dir", settings.backup_dir),
    ):
        name = f"CHIMERA_{field_name.upper()}"
        path = _path(str(path), path, _directory_base(), name=name)
        _check_path(path, name=name)
        if desktop:
            _check_desktop_location(path, name=name)
        paths[field_name] = path

    settings = replace(
        settings,
        data_dir=paths["data_dir"],
        upload_dir=paths["upload_dir"],
        backup_dir=paths["backup_dir"],
        database_url=_database_url(settings.database_url, settings.mode, paths["data_dir"]),
    )
    if settings.test_postgres_dir is not None:
        url = make_url(settings.database_url)
        if (
            url.host != LOOPBACK
            or url.query
            or url.port is None
            or url.username != TEST_POSTGRES_NAME
            or url.database != TEST_POSTGRES_NAME
            or not re.fullmatch(r"[0-9a-f]{64}", url.password or "")
        ):
            raise ConfigError("Тестовый PostgreSQL требует адрес и ключ выделенного стенда")
        cluster = settings.test_postgres_dir.resolve()
        stand = cluster.parent
        marker = stand / TEST_STAND_MARKER
        try:
            valid = (
                cluster.name == "pgdata"
                and stand.name == "postgres"
                and marker.read_text(encoding="utf-8") == f"chimera-stage-02\n{stand.as_posix()}\n"
                and (stand / "postgres.key").read_text(encoding="ascii").strip() == url.password
                and (cluster / "PG_VERSION").read_text(encoding="ascii").strip()
                == str(POSTGRES_MAJOR)
            )
        except (OSError, UnicodeError):
            valid = False
        if not valid:
            raise ConfigError("Не удалось подтвердить каталог выделенного кластера PostgreSQL")
        settings = replace(settings, test_postgres_dir=cluster)
    _check_secrets(settings, required=require_secrets)
    return settings


def _read_local_key(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError):
        raise ConfigError("Не удалось прочитать локальный ключ") from None


def _create_local_key(path: Path, value: str) -> str:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(value)
    except FileExistsError:
        return _read_local_key(path) or ""
    except OSError:
        raise ConfigError("Не удалось сохранить локальный ключ") from None
    return value


def load_settings(
    env: Mapping[str, str] | None = None,
    *,
    env_file: Path | None = None,
    require_secrets: bool = True,
    testing: bool = False,
    test_postgres_dir: Path | None = None,
) -> Settings:
    """Служебные команды без доступа к зашифрованным данным могут пропустить ключи."""
    try:
        values = _read_env(env, env_file)
    except (OSError, UnicodeError):
        raise ConfigError("Не удалось прочитать файл настроек") from None
    get = values.get
    try:
        mode = AppMode(get("CHIMERA_MODE", AppMode.SERVER.value).strip())
    except ValueError:
        raise ConfigError("CHIMERA_MODE должен быть 'server' или 'desktop-local'") from None
    desktop = mode is AppMode.DESKTOP_LOCAL
    if _bool(get("CHIMERA_TESTING"), False, name="CHIMERA_TESTING"):
        raise ConfigError("CHIMERA_TESTING нельзя включать через окружение")
    if not desktop and not (get("DATABASE_URL") or "").strip():
        raise ConfigError("DATABASE_URL обязателен в режиме server")
    if not desktop and get("CHIMERA_DATA_DIR") is None:
        raise ConfigError("CHIMERA_DATA_DIR обязателен в режиме server")
    base_dir = _directory_base()
    default_dir = _desktop_dir(values) if desktop and get("CHIMERA_DATA_DIR") is None else base_dir
    data_dir = _path(get("CHIMERA_DATA_DIR"), default_dir, base_dir, name="CHIMERA_DATA_DIR")
    upload_dir = _path(
        get("CHIMERA_UPLOAD_DIR"), data_dir / "uploads", base_dir, name="CHIMERA_UPLOAD_DIR"
    )
    backup_dir = _path(
        get("CHIMERA_BACKUP_DIR"), data_dir / "backups", base_dir, name="CHIMERA_BACKUP_DIR"
    )

    org_type = (get("CHIMERA_DEFAULT_ORG_TYPE") or "college").strip()
    if org_type not in {"university", "college"}:
        raise ConfigError("CHIMERA_DEFAULT_ORG_TYPE должен быть 'university' или 'college'")

    settings = Settings(
        mode=mode,
        database_url=(get("DATABASE_URL") or "").strip(),
        secret_key=(get("SECRET_KEY") or "").strip(),
        data_encryption_key=(get("DATA_ENCRYPTION_KEY") or "").strip(),
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
        session_cookie_secure=_bool(
            get("SESSION_COOKIE_SECURE"), not desktop and not testing, name="SESSION_COOKIE_SECURE"
        ),
        session_lifetime_minutes=_int(
            get("SESSION_LIFETIME_MINUTES"), 480, name="SESSION_LIFETIME_MINUTES", minimum=5
        ),
        login_max_attempts=_int(get("LOGIN_MAX_ATTEMPTS"), 5, name="LOGIN_MAX_ATTEMPTS", minimum=1),
        login_window_seconds=_int(
            get("LOGIN_WINDOW_SECONDS"), 600, name="LOGIN_WINDOW_SECONDS", minimum=10
        ),
        enable_hsts=_bool(get("ENABLE_HSTS"), False, name="ENABLE_HSTS"),
        host=get("APP_HOST", LOOPBACK).strip(),
        port=_int(get("APP_PORT"), 5000, name="APP_PORT", minimum=1),
        debug=_bool(get("APP_DEBUG"), False, name="APP_DEBUG"),
        testing=testing,
        test_postgres_dir=test_postgres_dir,
    )
    local_keys = desktop and require_secrets
    settings = validate_settings(settings, require_secrets=require_secrets and not local_keys)
    if not local_keys:
        return settings

    secret_path = settings.data_dir / "secret_key"
    encryption_path = settings.data_dir / "data_encryption.key"
    secret = settings.secret_key or _read_local_key(secret_path)
    encryption = settings.data_encryption_key or _read_local_key(encryption_path)
    sqlite_file = Path(make_url(settings.database_url).database or "")
    if sqlite_file.exists():
        if encryption is None:
            raise ConfigError(
                "DATA_ENCRYPTION_KEY: для существующей SQLite восстановите исходный ключ "
                "или задайте его в настройках."
            )
        if secret is None:
            raise ConfigError(
                "SECRET_KEY: для существующей SQLite восстановите ключ подписи "
                "или задайте его явно в настройках."
            )
    # Проверяем существующие ключи до создания любого нового файла.
    new_secret = secret if secret is not None else secrets.token_urlsafe(48)
    new_encryption = encryption if encryption is not None else Fernet.generate_key().decode("ascii")
    _check_secrets(
        replace(settings, secret_key=new_secret, data_encryption_key=new_encryption), required=True
    )
    if secret is None:
        new_secret = _create_local_key(secret_path, new_secret)
    if encryption is None:
        new_encryption = _create_local_key(encryption_path, new_encryption)
    settings = replace(settings, secret_key=new_secret, data_encryption_key=new_encryption)
    _check_secrets(settings, required=True)
    return settings
