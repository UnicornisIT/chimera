"""Первый запуск, владение профилем и восстановление на синтетической SQLite."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from time import monotonic, sleep

import pytest
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy import delete, func, select

from chimera import __main__ as cli
from chimera.core.audit import AuditLog
from chimera.core.config import ConfigError, Settings, load_local_keys, load_settings
from chimera.core.crypto import get_cipher
from chimera.core.db import Database
from chimera.core.local_profile import (
    INITIALIZING_MARKER,
    PROFILE_MARKER,
    own_local_profile,
    sqlite_path,
)
from chimera.core.migrations import SchemaState, SchemaTooNewError, upgrade_head
from chimera.core.sqlite_snapshot import open_readonly, snapshot
from chimera.models import Organization, User
from chimera.services import local_backup, local_profile
from chimera.services.auth import INITIAL_ADMIN_CREATED
from chimera.services.local_backup import copy_profile, restore_profile
from chimera.web import create_app

PASSWORD = "Local-Setup-456"
CHANGED = "Local-Changed-789"
TOKEN = "synthetic-test-token"
ORIGIN = "http://127.0.0.1:5000"
PROCESS_TIMEOUT = 120


def profile_env(path: Path) -> dict[str, str]:
    return {"CHIMERA_MODE": "desktop-local", "CHIMERA_DATA_DIR": str(path)}


def _child_env(path: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}
    }
    env.update(
        profile_env(path),
        PYTHONIOENCODING="utf-8",
        APP_HOST="127.0.0.1",
        DATABASE_URL="",
        CHIMERA_UPLOAD_DIR=str(path / "uploads"),
        CHIMERA_BACKUP_DIR=str(path / "backups"),
        SECRET_KEY="",
        DATA_ENCRYPTION_KEY="",
        BLIND_INDEX_KEY="",
        ADMIN_DEFAULT_PASSWORD="",
    )
    return env


def test_setup_competing_posts_and_durable_completion(tmp_path: Path) -> None:
    settings = load_settings(profile_env(tmp_path))
    app = create_app(settings, setup_token=TOKEN)
    db = app.extensions["chimera.db"]
    start = Barrier(2, timeout=30)

    def submit() -> int:
        client = app.test_client()
        page = client.get("/local-setup", base_url=ORIGIN).get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page)
        assert token
        payload = {"setup_token": TOKEN, "password": PASSWORD, "password_confirm": PASSWORD}
        assert (
            client.post(
                "/local-setup",
                base_url=ORIGIN,
                headers={"Origin": ORIGIN},
                data=payload,
            ).status_code
            == 400
        )
        start.wait()
        return client.post(
            "/local-setup",
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
            data={**payload, "csrf_token": token[1]},
        ).status_code

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(submit) for _ in range(2)]
            assert sorted(call.result(timeout=60) for call in calls) == [302, 303]
        with db.transaction() as session:
            assert session.scalar(select(func.count(User.id))) == 1
            assert (
                session.scalar(
                    select(func.count(AuditLog.id)).where(AuditLog.action == INITIAL_ADMIN_CREATED)
                )
                == 1
            )
            session.execute(delete(User))
    finally:
        db.dispose()

    restarted = create_app(replace(settings, admin_default_password=PASSWORD), setup_token=TOKEN)
    try:
        client = restarted.test_client()
        assert client.get("/local-setup", base_url=ORIGIN).status_code == 404
        with restarted.extensions["chimera.db"].session() as session:
            assert session.scalar(select(func.count(User.id))) == 0
    finally:
        restarted.extensions["chimera.db"].dispose()


def test_first_launch_restart_and_full_restore(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = profile_env(tmp_path / "working")
    settings = load_settings(env)
    app = create_app(settings, setup_token=TOKEN)
    app.config.update(TESTING=True)
    client = app.test_client()
    db = app.extensions["chimera.db"]
    try:
        assert client.get("/login", base_url=ORIGIN).location == "/local-setup"
        assert client.get("/local-setup", base_url="http://other.test").status_code == 400
        page = client.get("/local-setup", base_url=ORIGIN).get_data(as_text=True)
        assert TOKEN not in page
        match = re.search(r'name="csrf_token" value="([^"]+)"', page)
        assert match
        payload = {
            "csrf_token": match[1],
            "setup_token": TOKEN,
            "password": PASSWORD,
            "password_confirm": PASSWORD,
        }
        assert client.post("/local-setup", base_url=ORIGIN, data=payload).status_code == 403
        response = client.post(
            "/local-setup",
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
            data={**payload, "setup_token": "invalid"},
        )
        assert response.status_code == 403
        response = client.post(
            "/local-setup",
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
            data={**payload, "password_confirm": CHANGED},
        )
        assert response.status_code == 200
        assert PASSWORD not in response.get_data(as_text=True)
        response = client.post(
            "/local-setup",
            base_url=ORIGIN,
            headers={"Origin": ORIGIN},
            data=payload,
        )
        assert response.status_code == 303 and response.location == "/login"
        assert (
            client.post(
                "/local-setup",
                base_url=ORIGIN,
                headers={"Origin": ORIGIN},
                data=payload,
            ).status_code
            == 302
        )

        def log_in(password: str) -> None:
            page = client.get("/login", base_url=ORIGIN).get_data(as_text=True)
            token = re.search(r'name="csrf_token" value="([^"]+)"', page)
            assert token
            response = client.post(
                "/login",
                base_url=ORIGIN,
                data={"username": "admin", "password": password, "csrf_token": token[1]},
            )
            assert response.status_code == 302 and response.location == "/"

        log_in(PASSWORD)
        page = client.get("/password", base_url=ORIGIN).get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page)
        assert token
        assert (
            client.post(
                "/password",
                base_url=ORIGIN,
                data={
                    "current_password": PASSWORD,
                    "new_password": CHANGED,
                    "new_password_confirm": CHANGED,
                    "csrf_token": token[1],
                },
            ).status_code
            == 302
        )
        with db.transaction() as session:
            assert session.scalar(select(Organization)).name == settings.default_organization_name
            assert session.scalar(select(User)).username == "admin"
        encrypted = get_cipher().encrypt("синтетическое значение")
        (settings.upload_dir / "example.txt").write_text(encrypted, encoding="ascii")
    finally:
        db.dispose()

    fresh = load_settings(env)
    assert fresh.secret_key == settings.secret_key
    assert fresh.data_encryption_key == settings.data_encryption_key
    app = create_app(fresh, setup_token=TOKEN)
    client = app.test_client()
    try:
        log_in(CHANGED)
        assert get_cipher().decrypt(encrypted) == "синтетическое значение"
    finally:
        app.extensions["chimera.db"].dispose()
    assert PASSWORD not in caplog.text and CHANGED not in caplog.text

    with own_local_profile(settings):
        backup = copy_profile(settings)
    original = sqlite_path(settings).read_bytes()
    destination = tmp_path / "restored"
    restore_profile(settings, backup, destination)
    with pytest.raises(ConfigError, match="существует"):
        restore_profile(settings, backup, destination)
    with pytest.raises(ConfigError, match="вне рабочего профиля"):
        restore_profile(settings, backup, settings.data_dir)
    restored = load_settings(profile_env(destination))
    assert restored.secret_key == settings.secret_key
    assert restored.data_encryption_key == settings.data_encryption_key
    app = create_app(restored)
    client = app.test_client()
    try:
        log_in(CHANGED)
        assert get_cipher().decrypt((restored.upload_dir / "example.txt").read_text()) == (
            "синтетическое значение"
        )
    finally:
        app.extensions["chimera.db"].dispose()
    assert sqlite_path(settings).read_bytes() == original

    for missing in ("data_encryption.key", "uploads", "chimera.db"):
        broken = tmp_path / ("broken-" + missing)
        shutil.copytree(backup, broken)
        path = broken / missing
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        with pytest.raises(ConfigError):
            restore_profile(settings, broken, tmp_path / "rejected")
    (backup / "chimera.db").write_bytes(b"invalid")
    with pytest.raises(ConfigError, match="повреждена"):
        restore_profile(settings, backup, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()


def test_wal_snapshot_excludes_uncommitted_write(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    target = tmp_path / "copy.db"
    with closing(sqlite3.connect(source)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("CREATE TABLE notes (label TEXT)")
        writer.execute("INSERT INTO notes VALUES ('committed')")
        writer.commit()
        writer.execute("INSERT INTO notes VALUES ('uncommitted')")
        with ThreadPoolExecutor() as pool:
            pool.submit(snapshot, source, target).result(timeout=10)
        with closing(open_readonly(target)) as reader:
            assert reader.execute("SELECT label FROM notes").fetchall() == [("committed",)]
    with closing(open_readonly(source)) as reader:
        assert reader.execute("SELECT label FROM notes").fetchall() == [("committed",)]


def test_process_lock_released_after_kill(tmp_path: Path) -> None:
    path = tmp_path / "local"
    env = _child_env(path)
    code = """
import os
from threading import Event
from chimera.core.config import load_settings, load_local_keys
from chimera.core.local_profile import own_local_profile, sqlite_path
from chimera.web import create_app
settings = load_settings(os.environ, require_secrets=False)
with own_local_profile(settings):
    app = create_app(load_local_keys(settings))
    connection = app.extensions['chimera.db'].engine.connect()
    connection.exec_driver_sql('CREATE TABLE marker (value TEXT)')
    connection.exec_driver_sql("INSERT INTO marker VALUES ('saved')")
    connection.commit()
    connection.exec_driver_sql("INSERT INTO marker VALUES ('lost')")
    print('ready', flush=True)
    Event().wait()
"""
    with subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", code],
        env=env,
        stdout=subprocess.PIPE,
    ) as owner:
        try:
            assert owner.stdout is not None
            with ThreadPoolExecutor() as pool:
                ready = pool.submit(owner.stdout.readline)
                try:
                    assert ready.result(timeout=PROCESS_TIMEOUT).strip() == b"ready"
                except TimeoutError:
                    owner.kill()
                    raise
            loser = subprocess.run(
                [sys.executable, "-m", "chimera", "run"],
                env=env,
                capture_output=True,
                timeout=PROCESS_TIMEOUT,
                check=False,
            )
            assert loser.returncode == 2 and "другим процессом".encode() in loser.stderr
        finally:
            owner.kill()
            owner.wait(timeout=10)
    settings = load_settings(profile_env(path))
    with own_local_profile(settings), closing(open_readonly(sqlite_path(settings))) as db:
        assert db.execute("SELECT value FROM marker").fetchall() == [("saved",)]


def test_two_cli_first_runs_keep_one_owner(tmp_path: Path) -> None:
    profile = tmp_path / "local"
    env = _child_env(profile)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        env["APP_PORT"] = str(probe.getsockname()[1])
    code = """
from pathlib import Path
import sys
import time
from chimera import __main__ as cli
root = Path(sys.argv[1])
number = sys.argv[2]
(root / ("armed-" + number)).touch()
while not (root / "start").is_file():
    time.sleep(0.02)
def launch(_url):
    (root / ("owner-" + number)).touch()
    return True
cli.webbrowser.open = launch
raise SystemExit(cli.main(["run"]))
"""
    with ExitStack() as stack:
        processes = []
        for number in ("0", "1"):
            proc = stack.enter_context(
                subprocess.Popen(  # noqa: S603
                    [sys.executable, "-c", code, str(tmp_path), number],
                    env=env,
                    stderr=subprocess.PIPE,
                )
            )
            stack.callback(proc.kill)
            processes.append(proc)
        deadline = monotonic() + PROCESS_TIMEOUT
        while len(list(tmp_path.glob("armed-*"))) != 2 and monotonic() < deadline:
            sleep(0.02)
        assert len(list(tmp_path.glob("armed-*"))) == 2
        (tmp_path / "start").touch()

        deadline = monotonic() + PROCESS_TIMEOUT
        while not list(tmp_path.glob("owner-*")) and monotonic() < deadline:
            sleep(0.02)
        (winner,) = tmp_path.glob("owner-*")
        owner = int(winner.name.rsplit("-", 1)[1])
        loser = processes[1 - owner]
        _, errors = loser.communicate(timeout=PROCESS_TIMEOUT)
        assert loser.returncode == 2 and "другим процессом".encode() in errors

        blocked = subprocess.run(
            [sys.executable, "-m", "chimera", "backup-profile"],
            env=env,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert blocked.returncode == 2 and "другим процессом".encode() in blocked.stderr
        assert not list((profile / "backups").iterdir())
        assert processes[owner].poll() is None
        processes[owner].kill()
        processes[owner].wait(timeout=15)
    recovered = subprocess.run(
        [sys.executable, "-m", "chimera", "backup-profile"],
        env=env,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert recovered.returncode == 0
    assert "конфиденциальные данные и ключи".encode() in recovered.stdout
    for name in ("secret_key", "data_encryption.key"):
        assert (profile / name).read_bytes() not in recovered.stdout + recovered.stderr


def test_missing_database_after_initialization_is_not_recreated(tmp_path: Path) -> None:
    settings = load_settings(profile_env(tmp_path))
    (tmp_path / PROFILE_MARKER).touch()
    with pytest.raises(ConfigError, match="отсутствует"), own_local_profile(settings):
        pytest.fail("missing database")
    assert not sqlite_path(settings).exists()


@pytest.mark.parametrize("action", ("run", "migrate"))
def test_legacy_profile_with_backup_refuses_to_recreate_database(
    action: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env = profile_env(tmp_path)
    settings = load_settings(env)
    database = Database.from_settings(settings)
    settings.upload_dir.mkdir()
    settings.backup_dir.mkdir()
    backup = settings.backup_dir / "chimera-20261124-120000.sqlite3"
    try:
        upgrade_head(database)
        with database.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            conn.exec_driver_sql("VACUUM INTO ?", (str(backup),))
    finally:
        database.dispose()
    assert not (tmp_path / PROFILE_MARKER).exists()
    sqlite_path(settings).rename(tmp_path / "moved.db")
    originals = {
        path: path.read_bytes()
        for path in (backup, tmp_path / "secret_key", tmp_path / "data_encryption.key")
    }
    monkeypatch.setattr("chimera.core.config._read_env", lambda *_: env)
    monkeypatch.setattr(cli, "make_server", lambda *_a, **_kw: pytest.fail("setup was started"))
    assert cli.main([action]) == 2
    assert "восстановите копию в отдельный каталог" in capsys.readouterr().err
    assert not sqlite_path(settings).exists()
    assert not (tmp_path / PROFILE_MARKER).exists()
    assert {path: path.read_bytes() for path in originals} == originals


@pytest.mark.parametrize(
    "evidence",
    (
        "backups/profile-synthetic/manifest.json",
        "uploads/document.txt",
        "profile-settings.json",
        "chimera.db-wal",
        "chimera.db-shm",
        "chimera.db-journal",
    ),
)
def test_missing_database_with_evidence_refuses_initialization(
    tmp_path: Path, evidence: str
) -> None:
    witness = tmp_path / evidence
    witness.parent.mkdir(parents=True, exist_ok=True)
    witness.touch()
    settings = load_settings(
        {
            **profile_env(tmp_path),
            "CHIMERA_BACKUP_DIR": str(tmp_path / "new-backups"),
            "CHIMERA_UPLOAD_DIR": str(tmp_path / "new-uploads"),
        },
        require_secrets=False,
    )
    with pytest.raises(ConfigError, match="отсутствует"), own_local_profile(settings):
        pytest.fail("profile was recreated")
    assert witness.exists()
    assert not sqlite_path(settings).exists()


@pytest.mark.parametrize("interrupted", (False, True))
def test_new_and_interrupted_profiles_allow_initialization(
    tmp_path: Path, interrupted: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "profile"
    env = profile_env(root)
    if interrupted:
        pending = load_settings(env, require_secrets=False)
        with own_local_profile(pending):
            load_local_keys(pending)
        assert (root / INITIALIZING_MARKER).is_file()
        (root / "uploads").mkdir()
        (root / "backups").mkdir()
    before = {p.name: p.read_bytes() for p in root.glob("*key")}
    monkeypatch.setattr("chimera.core.config._read_env", lambda *_: env)
    assert cli.main(["migrate"]) == 0
    assert (root / "chimera.db").is_file()
    assert (root / PROFILE_MARKER).is_file()
    assert not (root / INITIALIZING_MARKER).exists()
    assert {name: (root / name).read_bytes() for name in before} == before


def test_unmarked_key_only_profile_requires_explicit_choice(tmp_path: Path) -> None:
    settings = load_settings(profile_env(tmp_path))
    keys = {p.name: p.read_bytes() for p in tmp_path.glob("*key")}
    with pytest.raises(ConfigError, match="состояние не определено"), own_local_profile(settings):
        pytest.fail("ambiguous profile was accepted")
    assert not sqlite_path(settings).exists()
    assert not (tmp_path / INITIALIZING_MARKER).exists()
    assert {name: (tmp_path / name).read_bytes() for name in keys} == keys


def test_standalone_alembic_respects_active_profile_lock(tmp_path: Path) -> None:
    settings = load_settings(profile_env(tmp_path))
    create_app(settings).extensions["chimera.db"].dispose()

    def upgrade() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env=_child_env(tmp_path),
            capture_output=True,
            timeout=PROCESS_TIMEOUT,
            check=False,
        )

    with own_local_profile(settings):
        result = upgrade()
        assert result.returncode != 0
        assert "другим процессом".encode() in result.stdout + result.stderr
    assert upgrade().returncode == 0


def test_backup_failure_logs_sanitized_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    settings = load_settings(profile_env(tmp_path))
    create_app(settings).extensions["chimera.db"].dispose()
    sensitive = "synthetic path, key or database contents"

    def unavailable(_source: Path, _target: Path) -> None:
        error = sqlite3.OperationalError(sensitive)
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error

    monkeypatch.setattr(local_backup, "snapshot", unavailable)
    with own_local_profile(settings), pytest.raises(ConfigError):
        copy_profile(settings)
    assert "phase=sqlite elapsed_ms=" in caplog.text
    assert "sqlite_code=5 sqlite_busy=True" in caplog.text
    assert sensitive not in caplog.text
    assert not list(settings.backup_dir.iterdir())


def test_profile_lock_rejects_hardlinked_database_alias(tmp_path: Path) -> None:
    settings = load_settings(profile_env(tmp_path / "working"))
    app = create_app(settings)
    app.extensions["chimera.db"].dispose()
    alternate = tmp_path / "alternate.db"
    alias_settings = load_settings(
        {
            **profile_env(tmp_path / "alternate-profile"),
            "DATABASE_URL": f"sqlite:///{alternate.as_posix()}",
        },
        require_secrets=False,
    )
    with own_local_profile(settings):
        os.link(sqlite_path(settings), alternate)
        with pytest.raises(ConfigError, match="жёсткие ссылки"), own_local_profile(alias_settings):
            pytest.fail("hardlinked SQLite was accepted")
        assert not alternate.with_suffix(".db.lock").exists()


def test_restore_rejects_link_in_destination(tmp_path: Path, settings: Settings) -> None:
    link = tmp_path / "link"
    target = tmp_path / "target"
    target.mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        result = subprocess.run(  # noqa: S603
            [
                str(Path(os.environ["SYSTEMROOT"]) / "System32/cmd.exe"),
                "/d",
                "/c",
                "mklink",
                "/J",
                str(link),
                str(target),
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            pytest.skip("Windows не разрешила создание ссылки или junction на тестовом томе")
    with pytest.raises(ConfigError, match=r"[сС]сылки"):
        restore_profile(settings, tmp_path / "backup", link / "new")
    assert not (target / "new").exists()


@pytest.fixture
def local_profile_copy(tmp_path: Path) -> tuple[Settings, Path]:
    settings = load_settings(profile_env(tmp_path / "working"))
    app = create_app(settings)
    app.extensions["chimera.db"].dispose()
    (settings.upload_dir / "attachment.txt").write_text("synthetic", encoding="utf-8")
    with own_local_profile(settings):
        return settings, copy_profile(settings)


def test_restore_rechecks_destination_after_validation(
    tmp_path: Path,
    local_profile_copy: tuple[Settings, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, source = local_profile_copy
    destination = tmp_path / "destination"
    verify = local_backup._verified_profile

    def occupy_destination(stage: Path, settings: Settings) -> None:
        verify(stage, settings)
        destination.mkdir()
        (destination / "existing.txt").write_text("keep", encoding="utf-8")

    monkeypatch.setattr(local_backup, "_verified_profile", occupy_destination)
    with pytest.raises(ConfigError, match="уже существует"):
        restore_profile(settings, source, destination)
    assert sorted(p.name for p in destination.iterdir()) == ["existing.txt"]
    assert not list(tmp_path.glob(".chimera-profile-*"))


def test_restore_rejects_parent_replaced_after_validation(
    tmp_path: Path, local_profile_copy: tuple[Settings, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, source = local_profile_copy
    parent = tmp_path / "parent"
    parent.mkdir()
    moved = tmp_path / "moved-parent"
    verify = local_backup._verified_profile

    def replace_parent(stage: Path, settings: Settings) -> None:
        verify(stage, settings)
        parent.rename(moved)
        parent.mkdir()

    monkeypatch.setattr(local_backup, "_verified_profile", replace_parent)
    with pytest.raises(ConfigError):
        restore_profile(settings, source, parent / "restored")
    assert not list(parent.iterdir())
    assert not (moved / "restored").exists()


def test_backup_rejects_missing_uploads_and_links_to_external_files(
    tmp_path: Path, local_profile_copy: tuple[Settings, Path]
) -> None:
    settings, existing = local_profile_copy
    (settings.upload_dir / "attachment.txt").unlink()
    settings.upload_dir.rmdir()
    with own_local_profile(settings), pytest.raises(ConfigError):
        copy_profile(settings)
    settings.upload_dir.mkdir()
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("outside the profile", encoding="utf-8")
    os.link(unrelated, settings.upload_dir / "linked.txt")
    with own_local_profile(settings), pytest.raises(ConfigError, match="жёсткую ссылку"):
        copy_profile(settings)
    assert list(settings.backup_dir.iterdir()) == [existing]


@pytest.mark.parametrize("path", ("../escaped", "/escaped", "C:/escaped"))
def test_restore_rejects_paths_in_manifest(
    tmp_path: Path, local_profile_copy: tuple[Settings, Path], path: str
) -> None:
    settings, source = local_profile_copy
    manifest_path = source / local_backup.MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][path] = manifest["files"]["uploads/attachment.txt"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    destination = tmp_path / "restored"
    with pytest.raises(ConfigError, match="повреждена"):
        restore_profile(settings, source, destination)
    assert not destination.exists()
    assert not (tmp_path / "escaped").exists()


def test_restore_rejects_duplicate_manifest_path(
    tmp_path: Path, local_profile_copy: tuple[Settings, Path]
) -> None:
    settings, source = local_profile_copy
    manifest_path = source / local_backup.MANIFEST
    manifest = manifest_path.read_text(encoding="utf-8")
    name = '"uploads/attachment.txt":'
    manifest_path.write_text(
        manifest.replace(name, f'{name} "different hash", {name}', 1), encoding="utf-8"
    )
    destination = tmp_path / "restored"
    with pytest.raises(ConfigError, match="повторяющиеся"):
        restore_profile(settings, source, destination)
    assert not destination.exists()


def test_restore_rejects_valid_sqlite_without_profile_tables(
    tmp_path: Path, local_profile_copy: tuple[Settings, Path]
) -> None:
    settings, source = local_profile_copy
    db = source / "chimera.db"
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("DROP TABLE users")
    manifest_path = source / local_backup.MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["chimera.db"] = hashlib.sha256(db.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ConfigError, match="обязательные таблицы"):
        restore_profile(settings, source, tmp_path / "restored")


def test_interrupted_restore_is_not_published(
    tmp_path: Path, local_profile_copy: tuple[Settings, Path]
) -> None:
    settings, source = local_profile_copy
    target = tmp_path / "restored"
    code = """
import os
import sys
from chimera import __main__ as cli
from chimera.services import local_backup
verify = local_backup._verified_profile
def interrupt(stage, settings):
    verify(stage, settings)
    os._exit(17)
local_backup._verified_profile = interrupt
raise SystemExit(cli.main(["restore-profile", *sys.argv[1:], "--confirm"]))
"""
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code, str(source), str(target)],
        env=_child_env(settings.data_dir),
        check=False,
        timeout=60,
    )
    assert completed.returncode == 17
    assert not target.exists()
    (stage,) = tmp_path.glob(".chimera-profile-*")
    assert not (stage / PROFILE_MARKER).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="DACL Windows")
def test_windows_profile_copy_and_restore_have_private_acl(tmp_path: Path) -> None:
    settings = load_settings(profile_env(tmp_path / "working"))
    app = create_app(settings)
    app.extensions["chimera.db"].dispose()
    powershell = Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    allow_read = """
$ErrorActionPreference = 'Stop'
$path = $env:CHIMERA_TEST_ACL_PATH
$acl = [System.IO.Directory]::GetAccessControl($path)
$everyone = [System.Security.Principal.SecurityIdentifier]::new('S-1-1-0')
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new(
    $everyone, 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow'
)
$acl.AddAccessRule($rule)
[System.IO.Directory]::SetAccessControl($path, $acl)
"""

    def acl_script(path: Path, script: str) -> str:
        return subprocess.check_output(  # noqa: S603
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
            env={**os.environ, "CHIMERA_TEST_ACL_PATH": str(path)},
            text=True,
            timeout=60,
        )

    acl_script(settings.backup_dir, allow_read)
    acl_script(tmp_path, allow_read)
    with own_local_profile(settings):
        archive = copy_profile(settings)
    restored = tmp_path / "restored"
    restore_profile(settings, archive, restored)
    read_dacl = """
$ErrorActionPreference = 'Stop'
$path = $env:CHIMERA_TEST_ACL_PATH
if ([System.IO.Directory]::Exists($path)) {
    $acl = [System.IO.Directory]::GetAccessControl($path)
} else {
    $acl = [System.IO.File]::GetAccessControl($path)
}
$acl.AreAccessRulesProtected
$acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]) |
    ForEach-Object { $_.IdentityReference.Value }
"""
    assert "S-1-1-0" in acl_script(settings.backup_dir, read_dacl).splitlines()
    for private in (archive, restored):
        for path in (private, private / "secret_key", private / "chimera.db"):
            protected, *sids = acl_script(path, read_dacl).splitlines()
            assert set(sids) == {"S-1-3-4", "S-1-5-18", "S-1-5-32-544"}
            if path == private:
                assert protected == "True"


def test_interrupted_schema_creation_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings(profile_env(tmp_path))
    db = Database.from_settings(settings)

    def interrupted(config: Config, _revision: str) -> None:
        config.attributes["connection"].exec_driver_sql("CREATE TABLE incomplete (id INTEGER)")
        raise CommandError("synthetic failure")

    try:
        with monkeypatch.context() as context:
            context.setattr("chimera.core.migrations.command.upgrade", interrupted)
            with pytest.raises(CommandError):
                upgrade_head(db)
        with closing(open_readonly(sqlite_path(settings))) as connection:
            assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []
        local_profile.prepare_local_schema(db, settings)
        assert (tmp_path / PROFILE_MARKER).is_file()
    finally:
        db.dispose()


def test_existing_schema_is_backed_up_before_upgrade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings(profile_env(tmp_path))
    db = Database.from_settings(settings)
    try:
        local_profile.prepare_local_schema(db, settings)
        monkeypatch.setattr(local_profile, "schema_state", lambda *_: SchemaState("0001", "0002"))
        upgrades = []

        def check_backup(_db: Database) -> None:
            (copy,) = settings.backup_dir.glob("profile-*")
            assert (copy / "manifest.json").is_file()
            assert (copy / "data_encryption.key").read_text() == settings.data_encryption_key
            upgrades.append(copy)

        monkeypatch.setattr(local_profile, "upgrade_head", check_backup)
        local_profile.prepare_local_schema(db, settings)
        assert len(upgrades) == 1

        def unavailable(_settings: Settings) -> Path:
            raise ConfigError("Нет места для копии.")

        monkeypatch.setattr(local_profile, "copy_profile", unavailable)
        with pytest.raises(ConfigError, match="Нет места"):
            local_profile.prepare_local_schema(db, settings)
        assert len(upgrades) == 1
    finally:
        db.dispose()


def test_corruption_and_unknown_revision_are_preserved(tmp_path: Path) -> None:
    settings = load_settings(profile_env(tmp_path))
    db = Database.from_settings(settings)
    try:
        local_profile.prepare_local_schema(db, settings)
        for revision in ("future", "head", "000"):
            with db.engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE alembic_version SET version_num = ?", (revision,)
                )
            with pytest.raises(SchemaTooNewError):
                local_profile.prepare_local_schema(db, settings)
    finally:
        db.dispose()
    path = sqlite_path(settings)
    path.write_bytes(b"synthetic damaged database")
    with pytest.raises(ConfigError, match="повреждена"):
        create_app(load_local_keys(settings), setup_token=TOKEN)
    assert path.read_bytes() == b"synthetic damaged database"
