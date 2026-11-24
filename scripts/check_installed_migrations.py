# ruff: noqa: S101
"""Сборка и проверка установленных миграций вне checkout."""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import venv
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = "chimera/migrations/"
PG_PORT = 55441
SITE_PROBE = """
import sys
import sysconfig
from pathlib import Path

import chimera
from alembic.script import ScriptDirectory
from chimera.core.migrations import MIGRATIONS_DIR, alembic_config

checkout = Path(sys.argv[1])
site = Path(sysconfig.get_path("purelib")).resolve()
assert not any(Path(entry).resolve().is_relative_to(checkout) for entry in sys.path)
assert Path(chimera.__file__).resolve().is_relative_to(site)
assert MIGRATIONS_DIR == site / "chimera" / "migrations"
script = ScriptDirectory.from_config(alembic_config("sqlite:///unused.db"))
assert script.get_revision("0001") is not None
head = script.get_current_head()
assert head is not None
print("Пакет:", chimera.__file__)
print("Миграции:", MIGRATIONS_DIR)
print(head)
print(site)
"""

LOCAL_RUN = """
import os
import runpy
import sys
import threading
import time
from urllib.request import ProxyHandler, build_opener

def probe():
    url = "http://127.0.0.1:" + os.environ["APP_PORT"] + "/login"
    opener = build_opener(ProxyHandler({}))
    for _ in range(120):
        try:
            with opener.open(url, timeout=1) as response:
                if response.status == 200:
                    print("PASS Desktop Local: HTTP 200", flush=True)
                    os._exit(0)
        except OSError:
            time.sleep(0.25)

threading.Thread(target=probe, daemon=True).start()
sys.argv = ["chimera", "run"]
runpy.run_module("chimera", run_name="__main__")
"""

LOCAL_FIRST_RUN = """
import http.cookiejar
import os
import re
import runpy
import secrets
import sys
import threading
import webbrowser
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

ready = threading.Event()
launch_url = []

def open_setup(url):
    launch_url.append(url)
    ready.set()
    return True

def probe():
    try:
        if not ready.wait(45):
            raise TimeoutError("Ожидание запуска")
        address = urlsplit(launch_url[0])
        origin = f"{address.scheme}://{address.netloc}"
        setup = origin + address.path
        opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))

        def token(url):
            with opener.open(url, timeout=10) as response:
                return re.search(rb'name="csrf_token" value="([^"]+)"', response.read())[1].decode()

        password = "Wheel-9-" + secrets.token_urlsafe(18)
        body = urlencode({
            "setup_token": address.fragment,
            "csrf_token": token(setup),
            "password": password,
            "password_confirm": password,
        }).encode()
        with opener.open(Request(setup, body, {"Origin": origin}), timeout=10) as response:
            assert response.url == origin + "/login"
        body = urlencode({
            "username": os.environ["ADMIN_DEFAULT_USERNAME"],
            "csrf_token": token(origin + "/login"),
            "password": password,
        }).encode()
        with opener.open(origin + "/login", body, timeout=10) as response:
            assert response.url == origin + "/"
        print("PASS Desktop Local: первый запуск, настройка и вход по HTTP", flush=True)
    except Exception as error:
        print(f"FAIL Desktop Local: настройка или авторизация ({type(error).__name__})", flush=True)
        os._exit(1)
    os._exit(0)

webbrowser.open = open_setup
threading.Thread(target=probe, daemon=True).start()
sys.argv = ["chimera", "run"]
runpy.run_module("chimera", run_name="__main__")
"""
WSGI_RUN = """
from chimera.wsgi import app
response = app.test_client().get("/login", base_url="https://chimera.example")
assert response.status_code == 200 and "Secure" in response.headers["Set-Cookie"]
app.extensions["chimera.db"].dispose()
print("PASS Server: WSGI HTTP 200, Secure cookie")
"""


def run(
    args: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
    expect: int = 0,
    timeout: int = 300,
) -> str:
    result = subprocess.run(  # noqa: S603 - только локальный Python и известные команды
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    output = result.stdout + result.stderr
    if env is not None:
        for name in ("DATABASE_URL", "SECRET_KEY", "DATA_ENCRYPTION_KEY", "ADMIN_DEFAULT_PASSWORD"):
            if secret := env.get(name):
                output = output.replace(secret, "[скрыто]")
    if result.returncode != expect:
        raise RuntimeError(f"Код завершения {result.returncode}, ожидался {expect}\n{output}")
    return output


def resource_contents(paths: dict[str, bytes]) -> dict[str, bytes]:
    return {
        name: data
        for name, data in paths.items()
        if name.startswith(MIGRATIONS) and name.endswith((".py", ".mako"))
    }


def verify_resources(name: str, actual: dict[str, bytes], expected: dict[str, bytes]) -> None:
    resources = resource_contents(actual)
    different = sorted(
        path
        for path in resources.keys() | expected.keys()
        if resources.get(path) != expected.get(path)
    )
    assert not different, f"{name}: отсутствующие или отличающиеся ресурсы {different}"


def build_artifacts(work: Path) -> tuple[Path, Path, Path]:
    direct = work / "direct"
    rebuilt = work / "rebuilt"
    wheelhouse = work / "dependencies"
    expected = {
        path.relative_to(ROOT).as_posix(): path.read_bytes()
        for path in (ROOT / MIGRATIONS).rglob("*")
        if path.is_file() and path.suffix in {".py", ".mako"}
    }
    run(
        [sys.executable, "-m", "build", "--sdist", "--wheel", "--outdir", str(direct), str(ROOT)],
        cwd=work,
        timeout=900,
    )
    (sdist,) = direct.glob("*.tar.gz")
    (wheel,) = direct.glob("*.whl")
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(rebuilt),
            str(sdist),
        ],
        cwd=work,
        timeout=900,
    )
    (second_wheel,) = rebuilt.glob("*.whl")
    assert {
        MIGRATIONS + "__init__.py",
        MIGRATIONS + "env.py",
        MIGRATIONS + "script.py.mako",
        MIGRATIONS + "versions/20261025_0001_initial_schema.py",
    } <= expected.keys()
    with tarfile.open(sdist) as archive:
        source = {}
        for entry in archive.getmembers():
            if entry.isfile():
                name = entry.name.partition("/")[2]
                if name.startswith(MIGRATIONS) and (stream := archive.extractfile(entry)):
                    source[name] = stream.read()
        verify_resources(sdist.name, source, expected)
    for artifact in (wheel, second_wheel):
        with ZipFile(artifact) as archive:
            resources = {
                name: archive.read(name)
                for name in archive.namelist()
                if name.startswith(MIGRATIONS)
            }
            verify_resources(artifact.name, resources, expected)
    print(f"PASS: sdist и оба wheel содержат {len(expected)} ресурсов Alembic", flush=True)
    run(
        [sys.executable, "-m", "pip", "download", "--dest", str(wheelhouse), str(wheel)],
        cwd=work,
        timeout=900,
    )
    return wheel, second_wheel, wheelhouse


def isolated_env(profile: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LANG", "LC_ALL"}
    }
    env.update(
        HOME=str(profile),
        USERPROFILE=str(profile),
        LOCALAPPDATA=str(profile / "Local"),
        XDG_DATA_HOME=str(profile / "data"),
        CHIMERA_MODE="desktop-local",
        ADMIN_DEFAULT_USERNAME="wheeladmin",
        ADMIN_DEFAULT_PASSWORD="Wheel-9-" + secrets.token_urlsafe(18),
    )
    return env


def sqlite_state(db: Path, head: str) -> tuple[str, ...]:
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (head,)
        return tuple(conn.iterdump())


def verify_sqlite(python: list[str], cwd: Path, env: dict[str, str], head: str) -> None:
    cli = [*python, "-m", "chimera"]
    print(run([*cli, "migrate"], cwd=cwd, env=env).strip(), flush=True)
    status = run([*cli, "status"], cwd=cwd, env=env)
    assert head in status
    print(status.strip(), flush=True)
    profile_root = Path(env["LOCALAPPDATA"] if os.name == "nt" else env["XDG_DATA_HOME"])
    data = profile_root / "Chimera"
    database = data / "chimera.db"
    with closing(sqlite3.connect(database)) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"alembic_version", "organizations", "users", "memberships"} <= tables
    run([*cli, "bootstrap", "--no-input"], cwd=cwd, env=env)
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT username FROM users").fetchall() == [("wheeladmin",)]
    existing = sqlite_state(database, head)
    names = ("secret_key", "data_encryption.key")
    keys = tuple((data / name).read_bytes() for name in names)
    run([*cli, "migrate"], cwd=cwd, env=env)
    assert head in run([*cli, "status"], cwd=cwd, env=env)
    assert sqlite_state(database, head) == existing
    assert tuple((data / name).read_bytes() for name in names) == keys
    print(f"PASS: существующая SQLite {head}, схема, данные и ключи сохранены", flush=True)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    print(
        run(
            [*python, "-c", LOCAL_RUN], cwd=cwd, env={**env, "APP_PORT": str(port)}, timeout=45
        ).strip()
    )
    first_run_env = {**env, "APP_PORT": str(port), "CHIMERA_DATA_DIR": str(cwd / "first-run")}
    first_run_env.pop("ADMIN_DEFAULT_PASSWORD")
    print(run([*python, "-c", LOCAL_FIRST_RUN], cwd=cwd, env=first_run_env, timeout=90).strip())
    with closing(sqlite3.connect(cwd / "first-run" / "chimera.db")) as conn:
        assert conn.execute("SELECT username FROM users").fetchall() == [("wheeladmin",)]

    future = cwd / "future.db"
    with closing(sqlite3.connect(database)) as source, closing(sqlite3.connect(future)) as target:
        source.backup(target)
        with target:
            target.execute("UPDATE alembic_version SET version_num = 'future_revision'")
    future_env = {
        **env,
        "CHIMERA_DATA_DIR": str(data),
        "DATABASE_URL": f"sqlite:///{future.as_posix()}",
    }
    for action in ("status", "migrate"):
        output = run([*cli, action], cwd=cwd, env=future_env, expect=2)
        assert "Ревизия схемы базы неизвестна" in output
    with closing(sqlite3.connect(future)) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "future_revision",
        )
    print("PASS: неизвестная ревизия отклонена", flush=True)


def verify_postgres(
    python: list[str], cwd: Path, env: dict[str, str], root: Path, head: str
) -> None:
    from cryptography.fernet import Fernet

    from chimera.core.db import Database
    from scripts import dev_test_env as stand

    with stand.locked_stand("postgres", create=True, root=root) as path:
        try:
            stand.start_postgres(path, port=PG_PORT)
            stand.pg_connection(path, create_database=True, port=PG_PORT)
            settings = stand.settings_for(path, "postgres", prepare=False, port=PG_PORT)
            database = Database.from_settings(settings)
            try:
                with database.engine.connect() as connection:
                    assert not connection.exec_driver_sql(
                        "SELECT to_regclass('alembic_version')"
                    ).scalar_one()
                pg_env = {
                    **env,
                    "CHIMERA_MODE": "server",
                    "DATABASE_URL": settings.database_url,
                    "CHIMERA_DATA_DIR": str(cwd / "server-data"),
                    "SECRET_KEY": secrets.token_urlsafe(48),
                    "DATA_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
                }
                cli = [*python, "-m", "chimera"]
                run([*cli, "migrate"], cwd=cwd, env=pg_env)
                run([*cli, "bootstrap", "--no-input"], cwd=cwd, env=pg_env)
                run([*cli, "migrate"], cwd=cwd, env=pg_env)
                assert head in run([*cli, "status"], cwd=cwd, env=pg_env)
                with database.engine.connect() as connection:
                    assert (
                        connection.exec_driver_sql(
                            "SELECT version_num FROM alembic_version"
                        ).scalar_one()
                        == head
                    )
                    assert (
                        connection.exec_driver_sql("SELECT username FROM users").scalar_one()
                        == "wheeladmin"
                    )
                print(run([*python, "-c", WSGI_RUN], cwd=cwd, env=pg_env).strip(), flush=True)
            finally:
                database.dispose()
        finally:
            stand.stop_postgres(path, port=PG_PORT)


def tree_snapshot(site: Path) -> dict[Path, tuple[int, int]]:
    return {
        path.relative_to(site): (info.st_size, info.st_mtime_ns)
        for path in site.rglob("*")
        if (info := path.stat())
    }


def verify_wheel(wheel: Path, wheelhouse: Path, work: Path, *, postgres: bool = False) -> None:
    work.mkdir()
    virtual = work / "Среда wheel"
    cwd = work / "Рабочий каталог"
    cwd.mkdir()
    venv.EnvBuilder(with_pip=True).create(virtual)
    executable = virtual / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python = [str(executable), "-I", "-B", "-X", "utf8"]
    env = isolated_env(work / "profile")
    run(
        [
            *python,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            str(wheel),
        ],
        cwd=cwd,
        env=env,
    )
    *details, head, site_path = run(
        [*python, "-c", SITE_PROBE, str(ROOT)], cwd=cwd, env=env
    ).splitlines()
    print("\n".join(details), flush=True)
    site = Path(site_path)
    before = tree_snapshot(site)
    verify_sqlite(python, cwd, env, head)
    if postgres:
        verify_postgres(python, cwd, env, work.parent / "pg-stand", head)
    assert tree_snapshot(site) == before, "Команды Chimera изменили site-packages"
    print("PASS: site-packages без записей во время запуска", flush=True)


def main() -> None:
    if not __debug__:
        raise RuntimeError("Проверка упаковки требует включённых assert")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres", action="store_true", help="проверить выделенный PostgreSQL 16"
    )
    args = parser.parse_args()
    if args.postgres:
        for key in tuple(os.environ):
            if key.upper().startswith("PG"):
                del os.environ[key]
    with tempfile.TemporaryDirectory(prefix="chimera-wheel-") as directory:
        work = Path(directory).resolve()
        if work.is_relative_to(ROOT):
            raise RuntimeError("Временный каталог теста должен находиться вне checkout")
        wheel, second_wheel, wheelhouse = build_artifacts(work)
        verify_wheel(wheel, wheelhouse, work / "direct-install", postgres=args.postgres)
        verify_wheel(second_wheel, wheelhouse, work / "sdist-install")


if __name__ == "__main__":
    main()
