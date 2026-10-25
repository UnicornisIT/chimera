"""Общие фикстуры: база (SQLite или PostgreSQL), приложение и синтетические данные.

База выбирается переменной `CHIMERA_TEST_DATABASE_URL`; по умолчанию — временный
файл SQLite. Схема создаётся миграциями Alembic один раз на сессию, между тестами
таблицы очищаются.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from faker import Faker
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from chimera.core import crypto
from chimera.core.config import Settings, load_settings
from chimera.core.context import ServiceContext
from chimera.core.db import Base, Database
from chimera.core.migrations import upgrade_head
from chimera.core.permissions import Role
from chimera.core.security import hash_password
from chimera.core.tenancy import bind_organization
from chimera.core.terms import OrgType
from chimera.models import Membership, Organization, User
from chimera.web import create_app

PASSWORD = "Test-pass-123"
# Хэш считается один раз: генерация werkzeug намеренно медленная.
PASSWORD_HASH = hash_password(PASSWORD)
TEST_KEY = Fernet.generate_key().decode()


def _database_url(tmp_root: Path) -> str:
    url = os.environ.get("CHIMERA_TEST_DATABASE_URL")
    if url:
        return url
    return f"sqlite:///{(tmp_root / 'chimera-test.db').as_posix()}"


@pytest.fixture(scope="session")
def tmp_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("chimera")


@pytest.fixture(scope="session")
def settings(tmp_root: Path) -> Settings:
    return load_settings(
        {
            "CHIMERA_TESTING": "1",
            "DATABASE_URL": _database_url(tmp_root),
            "SECRET_KEY": "test-secret-key-" + "x" * 32,
            "DATA_ENCRYPTION_KEY": TEST_KEY,
            "CHIMERA_DATA_DIR": str(tmp_root / "data"),
            "SESSION_COOKIE_SECURE": "0",
            "LOGIN_MAX_ATTEMPTS": "3",
        }
    )


def _drop_everything(database: Database) -> None:
    with database.engine.begin() as conn:
        Base.metadata.drop_all(conn)
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture(scope="session")
def database(settings: Settings) -> Iterator[Database]:
    db = Database(settings.database_url)
    _drop_everything(db)
    upgrade_head(settings.database_url)
    crypto.configure(TEST_KEY)
    yield db
    db.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(request: pytest.FixtureRequest) -> Iterator[None]:
    yield
    if "database" not in request.fixturenames:
        return
    db: Database = request.getfixturevalue("database")
    with db.engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture
def session(database: Database) -> Iterator[Session]:
    s = database.session()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def app(settings: Settings, database: Database) -> Flask:
    application = create_app(settings)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


# --- Синтетические данные -------------------------------------------------------------


@dataclass
class OrgWorld:
    organization: Organization
    users: dict[Role, User] = field(default_factory=dict)


@dataclass
class World:
    university: OrgWorld
    college: OrgWorld
    superadmin: User

    def ctx(self, session: Session, org: OrgWorld, role: Role) -> ServiceContext:
        user = org.users[role]
        bind_organization(session, org.organization.id)
        return ServiceContext.build(
            session,
            role=role,
            user_id=user.id,
            username=user.username,
            organization_id=org.organization.id,
        )


def _make_user(session: Session, fake: Faker, username: str, **extra: object) -> User:
    user = User(
        username=username,
        full_name=fake.name(),
        email=f"{username}@example.org",
        password_hash=PASSWORD_HASH,
        **extra,
    )
    session.add(user)
    session.flush()
    return user


def _make_org(session: Session, fake: Faker, org_type: OrgType, prefix: str) -> OrgWorld:
    org = Organization(
        name=f"{fake.company()} ({prefix})",
        short_name=prefix.upper(),
        org_type=org_type,
        address=fake.address(),
        head_full_name=fake.name(),
        head_position="Директор" if org_type is OrgType.COLLEGE else "Ректор",
    )
    session.add(org)
    session.flush()
    bind_organization(session, org.id)
    world = OrgWorld(org)
    for role in Role:
        user = _make_user(session, fake, f"{prefix}.{role.value}")
        session.add(Membership(user_id=user.id, organization_id=org.id, role=role))
        world.users[role] = user
    session.flush()
    bind_organization(session, None)
    return world


@pytest.fixture
def world(database: Database) -> World:
    fake = Faker("ru_RU")
    fake.seed_instance(20261025)
    with database.transaction() as s:
        university = _make_org(s, fake, OrgType.UNIVERSITY, "uni")
        college = _make_org(s, fake, OrgType.COLLEGE, "col")
        superadmin = _make_user(s, fake, "root", is_superadmin=True)
        bind_organization(s, university.organization.id)
        s.add(
            Membership(
                user_id=superadmin.id,
                organization_id=university.organization.id,
                role=Role.ADMIN,
            )
        )
    return World(university, college, superadmin)


# --- HTTP-помощники --------------------------------------------------------------------

_CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


def csrf(client: FlaskClient, url: str = "/login") -> str:
    response = client.get(url)
    match = _CSRF.search(response.get_data(as_text=True))
    assert match, f"нет CSRF-токена на {url}"
    return match.group(1)


def login(client: FlaskClient, username: str, password: str = PASSWORD) -> None:
    token = csrf(client)
    response = client.post(
        "/login", data={"username": username, "password": password, "csrf_token": token}
    )
    assert response.status_code == 302, response.get_data(as_text=True)[:500]


def post(
    client: FlaskClient, url: str, data: dict[str, object] | None = None, *, csrf_from: str = "/"
) -> object:
    payload = dict(data or {})
    payload["csrf_token"] = csrf(client, csrf_from)
    return client.post(url, data=payload)
