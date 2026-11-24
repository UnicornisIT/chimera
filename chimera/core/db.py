"""Движок и сессии SQLAlchemy, общие для сервера (PostgreSQL) и desktop (SQLite)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import DateTime, Engine, Enum, MetaData, create_engine, event
from sqlalchemy.engine import Dialect, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from chimera.core.config import POSTGRES_MAJOR, ConfigError, Settings, validate_settings
from chimera.core.sqlite_snapshot import configure_functions

# Явные имена ограничений нужны Alembic для batch-миграций SQLite.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

SQLITE_BUSY_TIMEOUT_MS = 5000


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Время всегда хранится и возвращается в UTC (SQLite не хранит смещение)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Ожидалось время с часовым поясом")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def enum_column(enum_cls: type[StrEnum], name: str) -> Enum:
    """Строковое перечисление без нативного ENUM: схема одинакова для SQLite и PostgreSQL."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        values_callable=lambda cls: [member.value for member in cls],
        validate_strings=True,
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


def _configure_sqlite(engine: Engine, *, wal: bool) -> None:
    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection: Any, _record: Any) -> None:
        configure_functions(dbapi_connection)
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            if wal:
                cursor.execute("PRAGMA journal_mode=WAL")
                if cursor.fetchone() != ("wal",):
                    raise ConfigError("Не удалось включить режим WAL для SQLite.")
                cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def make_engine(
    database_url: str, *, echo: bool = False, test_postgres_dir: Path | None = None
) -> Engine:
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        database = url.database or ""
        in_memory = database in {"", ":memory:"}
        if not in_memory:
            try:
                Path(database).parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                raise ConfigError("DATABASE_URL: не удалось подготовить каталог SQLite") from None
        engine = create_engine(
            url,
            echo=echo,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool if in_memory else None,
        )
        _configure_sqlite(engine, wal=not in_memory)
        return engine
    engine = create_engine(url, echo=echo, pool_pre_ping=True, hide_parameters=True)
    if url.drivername == "postgresql+psycopg":

        @event.listens_for(engine, "do_connect")
        def _connect(
            _dialect: Dialect, _record: Any, args: list[Any], kwargs: dict[str, Any]
        ) -> Any:
            import psycopg

            connection: psycopg.Connection[Any] | None = None
            try:
                if test_postgres_dir is not None:
                    kwargs["hostaddr"] = url.host
                connection = psycopg.connect(*args, **kwargs)
                if connection.info.server_version // 10000 != POSTGRES_MAJOR:
                    raise ConfigError(f"Требуется PostgreSQL {POSTGRES_MAJOR}")
                if test_postgres_dir is not None:
                    # Проверяем каждое физическое подключение до передачи его ORM/Alembic.
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT current_setting('data_directory'), current_database(), "
                            "session_user, host(inet_server_addr()), inet_server_port()"
                        )
                        row = cursor.fetchone()
                    if (
                        row is None
                        or Path(row[0]).resolve() != test_postgres_dir.resolve()
                        or tuple(row[1:]) != (url.database, url.username, url.host, url.port)
                    ):
                        raise ConfigError("Подключение ведёт за пределы тестового кластера")
                    connection.rollback()
                return connection
            except (OSError, psycopg.Error):
                if connection is not None:
                    connection.close()
                raise ConfigError(
                    "Не удалось подключиться к PostgreSQL или проверить подключение"
                ) from None
            except ConfigError:
                if connection is not None:
                    connection.close()
                raise

    return engine


class Database:
    """Держит движок и фабрику сессий одного приложения."""

    def __init__(
        self, database_url: str, *, echo: bool = False, test_postgres_dir: Path | None = None
    ) -> None:
        self.url = database_url
        self.engine = make_engine(database_url, echo=echo, test_postgres_dir=test_postgres_dir)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    @classmethod
    def from_settings(cls, settings: Settings) -> Database:
        settings = validate_settings(settings, require_secrets=False)
        return cls(settings.database_url, test_postgres_dir=settings.test_postgres_dir)

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    def session(self) -> Session:
        return self.session_factory()

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        session = self.session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
