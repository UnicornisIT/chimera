"""Режим «работать как»: права, плашка и аудит с реальной ролью."""

from __future__ import annotations

import pytest
from flask.testing import FlaskClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from chimera.core.audit import AuditLog
from chimera.core.context import PermissionDenied, ServiceError
from chimera.core.db import Database
from chimera.core.permissions import Role
from chimera.models import DivisionKind
from chimera.services import impersonation, organizations
from tests.conftest import World, login, post


def _audit(database: Database, action: str) -> list[AuditLog]:
    with database.transaction() as s:
        return list(
            s.scalars(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id))
        )


def test_act_as_curator_flow(client: FlaskClient, database: Database, world: World) -> None:
    curator = world.college.users[Role.CURATOR]
    login(client, "col.admin")
    response = post(
        client,
        "/admin/act-as",
        {"role": "curator", "subject_user_id": curator.id},
        csrf_from="/admin/act-as",
    )
    assert response.status_code == 302  # type: ignore[attr-defined]

    page = client.get("/").get_data(as_text=True)
    assert "Вы работаете как" in page
    assert curator.full_name in page
    assert "Администрирование</summary>" not in page.replace("\n", "").replace(" ", "")
    assert client.get("/admin/users").status_code == 403

    started = _audit(database, "act_as.started")
    assert len(started) == 1 and started[0].real_role == "admin"
    actions = _audit(database, "act_as.request")
    assert actions
    assert all(a.real_role == "admin" and a.acting_role == "curator" for a in actions)
    assert all(a.acting_as_user_id == curator.id for a in actions)

    post(client, "/act-as/stop")
    assert len(_audit(database, "act_as.stopped")) == 1
    assert "Вы работаете как" not in client.get("/").get_data(as_text=True)
    assert client.get("/admin/users").status_code == 200


def test_act_as_head_requires_division(session: Session, world: World) -> None:
    ctx = world.ctx(session, world.university, Role.ADMIN)
    with pytest.raises(ServiceError, match="подразделение"):
        impersonation.start(ctx, "head")
    division = organizations.create_division(
        ctx, organizations.DivisionData(name="Исторический факультет", kind="division")
    )
    acting = impersonation.start(ctx, "head", division_id=division.id)
    assert acting.role is Role.HEAD and acting.division_id == division.id
    assert "Исторический факультет" in acting.label
    assert division.kind is DivisionKind.DIVISION


def test_act_as_rejects_foreign_subject(session: Session, world: World) -> None:
    ctx = world.ctx(session, world.college, Role.ADMIN)
    foreign = world.university.users[Role.CURATOR]
    with pytest.raises(ServiceError):
        impersonation.start(ctx, "curator", subject_user_id=foreign.id)


def test_act_as_cannot_become_admin(session: Session, world: World) -> None:
    ctx = world.ctx(session, world.college, Role.ADMIN)
    with pytest.raises(ServiceError):
        impersonation.start(ctx, "admin")


@pytest.mark.parametrize("role", [Role.HEAD, Role.CURATOR, Role.VIEWER])
def test_only_admin_can_act_as(session: Session, world: World, role: Role) -> None:
    ctx = world.ctx(session, world.college, role)
    with pytest.raises(PermissionDenied):
        impersonation.start(ctx, "starosta", subject_user_id=1)


def test_act_as_session_payload_is_validated() -> None:
    assert impersonation.ActAs.from_session({"role": "admin"}) is None
    assert impersonation.ActAs.from_session("garbage") is None
    parsed = impersonation.ActAs.from_session({"role": "starosta", "subject_user_id": 5})
    assert parsed is not None and parsed.subject_user_id == 5


def test_non_admin_cannot_forge_act_as_session(client: FlaskClient, world: World) -> None:
    login(client, "col.curator")
    with client.session_transaction() as sess:
        sess["act_as"] = {"role": "head", "division_id": 1}
    page = client.get("/").get_data(as_text=True)
    assert "Вы работаете как" not in page
