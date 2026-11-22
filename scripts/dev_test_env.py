"""Управление изолированными стендами SQLite и PostgreSQL 16 из checkout."""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg
from cryptography.fernet import Fernet
from psycopg import sql
from sqlalchemy.exc import SQLAlchemyError

from chimera.core.config import PROJECT_ROOT, Settings, load_settings
from chimera.core.db import Database
from chimera.core.migrations import schema_state, upgrade_head
from chimera.web import create_app
from scripts.dev_test_fixtures import COLLEGE, seed

STATE_ROOT = PROJECT_ROOT / ".chimera-test-envs"
MARKER = ".stage-02"
LOOPBACK = "127.0.0.1"
PG_PORT = 55432
PG_ROLE = "chimera_stage02"
PG_DATABASE = "chimera_stage02"


class StandError(RuntimeError):
    """Стенд не прошёл проверку или не готов к операции."""


def file_info(path: Path) -> os.stat_result:
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
        or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)
    ):
        raise StandError("В каталоге стенда обнаружена ссылка, точка перенаправления или спецфайл.")
    return info


def check_mounts(path: Path, *, descendants: bool = False) -> None:
    if os.path.ismount(path):
        raise StandError("Каталог стенда является точкой монтирования.")
    if sys.platform == "linux":
        # is_mount() не распознаёт bind mount на той же файловой системе.
        for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
            encoded = line.split()[4]
            mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), encoded))
            if mount == path or (descendants and mount.is_relative_to(path)):
                raise StandError("В каталоге стенда обнаружена точка монтирования.")


def check_tree(path: Path) -> None:
    if stat.S_ISDIR(file_info(path).st_mode):
        for child in path.iterdir():
            check_tree(child)


@contextmanager
def locked_stand(backend: str, *, create: bool = False) -> Iterator[Path]:
    if backend not in {"sqlite", "postgres"}:
        raise StandError("Ожидалось имя тестового стенда sqlite или postgres.")
    if create:
        STATE_ROOT.mkdir(mode=0o700, exist_ok=True)
    info = file_info(STATE_ROOT)
    if not stat.S_ISDIR(info.st_mode):
        raise StandError("Ожидался отдельный каталог тестовых стендов.")
    if sys.platform != "win32" and (info.st_uid != os.getuid() or info.st_mode & 0o022):
        raise StandError("Корневой каталог стендов должен принадлежать текущему пользователю.")
    check_mounts(STATE_ROOT)
    path = STATE_ROOT / backend
    lock = STATE_ROOT / f"{backend}.lock"
    try:
        lock.mkdir(mode=0o700)
    except FileExistsError:
        raise StandError(
            "Стенд занят другой командой или осталась блокировка прошлого запуска."
        ) from None
    try:
        marker = path / MARKER
        expected = f"chimera-stage-02\n{path.as_posix()}\n"
        if create:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                pass
            else:
                with marker.open("x", encoding="utf-8") as stream:
                    stream.write(expected)
        check_mounts(path, descendants=True)
        check_tree(path)
        if (
            not stat.S_ISREG(file_info(marker).st_mode)
            or marker.read_text(encoding="utf-8") != expected
        ):
            raise StandError("Маркер этого стенда отсутствует или не соответствует каталогу.")
        yield path
    finally:
        lock.rmdir()


def persistent_value(path: Path, value: str) -> str:
    try:
        with path.open("x", encoding="ascii") as stream:
            stream.write(value + "\n")
    except FileExistsError:
        pass
    return path.read_text(encoding="ascii").strip()


def pg_tool(
    tool: str, *args: str, allowed: tuple[int, ...] = (0,), capture: bool = True
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(tool)
    if not executable:
        raise StandError("Добавьте утилиты PostgreSQL 16 в PATH.")
    output = subprocess.PIPE if capture else subprocess.DEVNULL
    try:
        result = subprocess.run(  # noqa: S603 - фиксированные утилиты с проверкой версии
            [executable, *args],
            stdout=output,
            stderr=output,
            text=True,
            errors="replace",
            check=False,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise StandError(f"Истекло время ожидания {tool}. Проверьте журнал стенда.") from None
    if result.returncode not in allowed:
        raise StandError(
            f"Утилита {tool} завершилась с кодом {result.returncode}. Проверьте журнал стенда."
        )
    return result


def check_pg_tool(tool: str) -> None:
    if not re.search(r"\(PostgreSQL\) 16(?:\.|\s|$)", pg_tool(tool, "--version").stdout):
        raise StandError(f"Для стенда требуется {tool} из PostgreSQL 16.")


def pg_state(path: Path) -> bool:
    if (path / "pgdata" / "PG_VERSION").read_text(encoding="ascii").strip() != "16":
        raise StandError("Каталог кластера должен быть инициализирован PostgreSQL 16.")
    check_pg_tool("pg_ctl")
    return pg_tool("pg_ctl", "status", "-D", str(path / "pgdata"), allowed=(0, 3)).returncode == 0


def pg_connection(path: Path, *, create_database: bool = False) -> int:
    password = (path / "postgres.key").read_text(encoding="ascii").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", password):
        raise StandError("Ключ выделенного кластера имеет неверный формат.")
    with psycopg.connect(
        host=LOOPBACK,
        port=PG_PORT,
        user=PG_ROLE,
        password=password,
        dbname="postgres",
        connect_timeout=3,
        autocommit=True,
    ) as connection:
        row = connection.execute(
            "SELECT current_setting('data_directory'), current_setting('server_version_num')"
        ).fetchone()
        if row is None or Path(row[0]).resolve() != path / "pgdata" or int(row[1]) // 10000 != 16:
            raise StandError("По тестовому порту отвечает другой кластер PostgreSQL.")
        if (
            create_database
            and connection.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (PG_DATABASE,)
            ).fetchone()
            is None
        ):
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(PG_DATABASE)))
        return int(row[1])


def start_postgres(path: Path) -> None:
    data = path / "pgdata"
    if not data.exists():
        check_pg_tool("initdb")
        check_pg_tool("pg_ctl")
        persistent_value(path / "postgres.key", secrets.token_hex(32))
        pg_tool(
            "initdb",
            "-D",
            str(data),
            "-U",
            PG_ROLE,
            "--auth=scram-sha-256",
            "--encoding=UTF8",
            "--no-locale",
            f"--pwfile={path / 'postgres.key'}",
        )
    if not pg_state(path):
        # Windows postmaster может унаследовать PIPE от pg_ctl и держать его открытым.
        pg_tool(
            "pg_ctl",
            "start",
            "-D",
            str(data),
            "-l",
            str(path / "postgres.log"),
            "-w",
            "-t",
            "30",
            "-o",
            f"-h {LOOPBACK} -p {PG_PORT} -c unix_socket_directories=",
            capture=False,
        )


def stop_postgres(path: Path) -> None:
    data = path / "pgdata"
    if not data.exists():
        return
    if not (data / "PG_VERSION").exists() and not (data / "postmaster.pid").exists():
        return  # прерванный initdb до создания кластера
    if pg_state(path):
        pg_connection(path)
        pg_tool("pg_ctl", "stop", "-D", str(data), "-m", "fast", "-w", "-t", "30")


def settings_for(path: Path, backend: str, *, prepare: bool) -> Settings:
    postgres = backend == "postgres"
    if postgres:
        version = pg_connection(path, create_database=prepare)
        print(f"PostgreSQL {version // 10000}.{version % 10000}")
        key = (path / "postgres.key").read_text(encoding="ascii").strip()
        url = f"postgresql+psycopg://{PG_ROLE}:{key}@{LOOPBACK}:{PG_PORT}/{PG_DATABASE}"
    else:
        db_file = path / "chimera.db"
        if "?" in db_file.as_posix():
            raise StandError("Путь checkout для стенда SQLite не должен содержать знак вопроса.")
        if not prepare and not db_file.is_file():
            raise StandError("SQLite ещё не подготовлена.")
        url = f"sqlite:///{db_file.as_posix()}"
    env = {
        "CHIMERA_MODE": "server" if postgres else "desktop-local",
        "DATABASE_URL": url,
        "CHIMERA_DATA_DIR": str(path),
        "APP_HOST": LOOPBACK,
        "APP_PORT": "5802" if postgres else "5801",
        "SESSION_COOKIE_SECURE": "0",
        "CHIMERA_DEFAULT_ORG_NAME": COLLEGE,
    }
    if prepare:
        env["ADMIN_DEFAULT_PASSWORD"] = persistent_value(
            path / "fixture_password.key", secrets.token_hex(24) + "!"
        )
        env["SECRET_KEY"] = persistent_value(path / "session.key", secrets.token_urlsafe(48))
        env["DATA_ENCRYPTION_KEY"] = persistent_value(
            path / "data_encryption.key", Fernet.generate_key().decode("ascii")
        )
    return load_settings(env, require_secrets=prepare)


def prepare_or_status(path: Path, backend: str, *, prepare: bool) -> Settings:
    if prepare and backend == "postgres":
        start_postgres(path)
    settings = settings_for(path, backend, prepare=prepare)
    database = Database(settings.database_url)
    try:
        state = schema_state(database.engine, database.url)
        if prepare:
            upgrade_head(database.url)
            with database.transaction() as session:
                seed(session, settings)
            state = schema_state(database.engine, database.url)
        print(
            f"{settings.mode.value} · {database.dialect} · "
            f"Alembic {state.current or 'пусто'} / {state.head}"
        )
        if state.head is None or not state.up_to_date:
            raise StandError("Схема стенда не соответствует текущей ревизии Alembic.")
    finally:
        database.dispose()
    return settings


def clean_stand(path: Path, backend: str) -> None:
    if backend == "postgres":
        stop_postgres(path)
    # Маркер остаётся на месте при ошибке удаления файлов и позволяет повторить очистку.
    for child in path.iterdir():
        if child.name != MARKER:
            if stat.S_ISDIR(file_info(child).st_mode):
                shutil.rmtree(child)
            else:
                child.unlink()
    (path / MARKER).unlink()
    path.rmdir()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backend", choices=("sqlite", "postgres"))
    parser.add_argument("action", choices=("prepare", "run", "status", "stop", "clean"))
    parser.add_argument("--confirm", action="store_true", help="подтвердить удаление стенда")
    args = parser.parse_args(argv)
    if args.action == "clean" and not args.confirm:
        parser.error("Для удаления тестового стенда требуется --confirm.")
    if args.action == "stop" and args.backend != "postgres":
        parser.error("Остановите приложение SQLite сочетанием Ctrl+C.")
    # libpq читает PG* даже при явном DSN, в том числе параметры служб и SSL.
    for name in tuple(os.environ):
        if name.upper().startswith("PG"):
            del os.environ[name]
    prepare = args.action in {"prepare", "run"}
    try:
        with locked_stand(args.backend, create=prepare) as path:
            if args.action == "clean":
                clean_stand(path, args.backend)
                print("Тестовый стенд удалён.")
            elif args.action == "stop":
                stop_postgres(path)
                print("Тестовый PostgreSQL остановлен.")
            else:
                settings = prepare_or_status(path, args.backend, prepare=prepare)
                if args.action == "run":
                    create_app(settings).run(
                        host=LOOPBACK, port=settings.port, debug=False, load_dotenv=False
                    )
    except StandError as exc:
        print(exc, file=sys.stderr)
        return 1
    except FileNotFoundError:
        print(
            "Не найден каталог, маркер или файл стенда. Проверьте состояние стенда.",
            file=sys.stderr,
        )
        return 1
    except (OSError, SQLAlchemyError, psycopg.Error) as exc:
        print(f"Операция стенда завершилась ошибкой ({type(exc).__name__}).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
