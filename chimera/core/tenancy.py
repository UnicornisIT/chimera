"""Централизованная изоляция данных организаций.

Каждая предметная модель наследует `OrgScoped`. Когда у сессии задана текущая
организация (`bind_organization`), любой ORM-запрос SELECT/UPDATE/DELETE к таким
моделям автоматически получает фильтр `organization_id = <текущая>`, включая
связанные загрузки. Запрос к `OrgScoped` без организации в сессии — ошибка
программиста: сервис обязан либо привязать организацию, либо явно снять фильтр
через `unscoped(...)` (только для системных операций).

При сохранении новые записи получают `organization_id` текущей организации,
а попытка записать чужую организацию или перенести запись между организациями
отклоняется.
"""

from __future__ import annotations

from typing import Any, TypeVar

from sqlalchemy import ForeignKey, event
from sqlalchemy.orm import (
    Mapped,
    ORMExecuteState,
    Session,
    declared_attr,
    mapped_column,
    with_loader_criteria,
)
from sqlalchemy.orm.attributes import get_history
from sqlalchemy.sql import Executable

ORG_KEY = "chimera_organization_id"
SKIP_OPTION = "chimera_skip_org_filter"

E = TypeVar("E", bound=Executable)


class TenancyError(RuntimeError):
    """Нарушение изоляции организаций."""


class OrgScoped:
    """Миксин модели, принадлежащей организации."""

    @declared_attr
    def organization_id(cls) -> Mapped[int]:
        return mapped_column(
            ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
        )


def bind_organization(session: Session, organization_id: int | None) -> None:
    if organization_id is None:
        session.info.pop(ORG_KEY, None)
    else:
        session.info[ORG_KEY] = organization_id


def current_organization(session: Session) -> int | None:
    value = session.info.get(ORG_KEY)
    return int(value) if value is not None else None


def unscoped(statement: E) -> E:
    """Снимает фильтр организации. Только для системных и межорганизационных операций."""
    return statement.execution_options(**{SKIP_OPTION: True})


def _touches_org_scoped(state: ORMExecuteState) -> bool:
    return any(
        isinstance(mapper.class_, type) and issubclass(mapper.class_, OrgScoped)
        for mapper in state.all_mappers
    )


@event.listens_for(Session, "do_orm_execute")
def _apply_org_filter(state: ORMExecuteState) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if state.is_column_load or state.is_relationship_load:
        return  # критерий уже распространён из исходного запроса
    if state.execution_options.get(SKIP_OPTION):
        return
    if not _touches_org_scoped(state):
        return
    org_id = current_organization(state.session)
    if org_id is None:
        raise TenancyError(
            "Запрос к данным организации без выбранной организации. "
            "Привяжите организацию к сессии или используйте unscoped() для системной операции."
        )
    state.statement = state.statement.options(
        with_loader_criteria(
            OrgScoped,
            lambda cls: cls.organization_id == org_id,
            include_aliases=True,
        )
    )


@event.listens_for(Session, "before_flush")
def _check_org_on_flush(session: Session, _flush_context: Any, _instances: Any) -> None:
    org_id = current_organization(session)
    for obj in session.new:
        if not isinstance(obj, OrgScoped):
            continue
        if obj.organization_id is None:
            if org_id is None:
                raise TenancyError(f"{type(obj).__name__}: не указана организация")
            obj.organization_id = org_id
        elif org_id is not None and obj.organization_id != org_id:
            raise TenancyError(f"{type(obj).__name__}: запись в чужую организацию запрещена")
    for obj in session.dirty:
        if not isinstance(obj, OrgScoped):
            continue
        history = get_history(obj, "organization_id")
        if history.deleted and history.added and history.deleted != history.added:
            raise TenancyError(f"{type(obj).__name__}: перенос между организациями запрещён")
    for obj in session.deleted:
        if isinstance(obj, OrgScoped) and org_id is not None and obj.organization_id != org_id:
            raise TenancyError(f"{type(obj).__name__}: удаление записи чужой организации")
