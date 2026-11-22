"""Начальные синтетические данные для изолированных стендов."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from chimera.core.config import Settings
from chimera.core.permissions import Role
from chimera.core.security import hash_password
from chimera.core.tenancy import bind_organization
from chimera.core.terms import OrgType
from chimera.models import Membership, Organization, User
from chimera.services.auth import ensure_initial_admin

COLLEGE = "Учебный колледж «Пример»"


def seed(session: Session, settings: Settings) -> None:
    password = settings.admin_default_password
    if not password:
        return
    # Bootstrap и фикстуры фиксируются одной транзакцией. При повторном запуске
    # изменения учёток, организаций и паролей сохраняются.
    admin = ensure_initial_admin(session, settings)
    if admin is None:
        return
    college_id = session.scalars(select(Organization.id).where(Organization.name == COLLEGE)).one()
    university = Organization(
        name="Учебный университет «Образец»", short_name="Образец", org_type=OrgType.UNIVERSITY
    )
    session.add(university)
    session.flush()

    password_hash = hash_password(password)
    for username, name, role, org_id in (
        ("head", "Тестовый руководитель", Role.HEAD, college_id),
        ("curator1", "Тестовый куратор колледжа", Role.CURATOR, college_id),
        ("curator2", "Тестовый куратор университета", Role.CURATOR, university.id),
        ("viewer", "Тестовый наблюдатель", Role.VIEWER, university.id),
    ):
        bind_organization(session, org_id)
        user = User(username=username, full_name=name, password_hash=password_hash)
        session.add(user)
        session.flush()
        session.add(Membership(user_id=user.id, organization_id=org_id, role=role))
        session.flush()
