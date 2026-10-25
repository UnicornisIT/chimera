"""Журнал аудита: кто, в какой роли, в какой организации, что, было/стало.

Сервисы вызывают `record(ctx, ...)` в той же транзакции, что и само изменение,
поэтому запись журнала и изменение данных фиксируются или откатываются вместе.
Значения зашифрованных и служебных полей (`info={"sensitive": True}`) в журнал
не попадают — только факт изменения.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, inspect
from sqlalchemy.orm import Mapped, mapped_column

from chimera.core.context import ServiceContext
from chimera.core.db import Base, UTCDateTime, utcnow

SENSITIVE_MARK = {"changed": True}


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    username: Mapped[str | None] = mapped_column(String(150))
    real_role: Mapped[str | None] = mapped_column(String(32))
    acting_role: Mapped[str | None] = mapped_column(String(32))
    acting_as_user_id: Mapped[int | None] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity_type: Mapped[str | None] = mapped_column(String(64))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    changes: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    message: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[str | None] = mapped_column(String(45))

    @property
    def is_acting(self) -> bool:
        return bool(self.acting_role and self.acting_role != self.real_role)


@dataclass(frozen=True, slots=True)
class Snapshot:
    values: dict[str, Any]
    sensitive: frozenset[str]


def _jsonable(value: Any) -> Any:
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


SNAPSHOT_EXCLUDE = frozenset({"created_at", "updated_at"})


def snapshot(obj: Any, *, exclude: frozenset[str] = SNAPSHOT_EXCLUDE) -> Snapshot:
    """Значения колонок модели; чувствительные хранятся только для сравнения."""
    mapper = inspect(type(obj))
    values: dict[str, Any] = {}
    sensitive: set[str] = set()
    for attr in mapper.column_attrs:
        if attr.key in exclude:
            continue
        column = attr.columns[0]
        if getattr(column.type, "is_encrypted", False) or column.info.get("sensitive"):
            sensitive.add(attr.key)
        values[attr.key] = getattr(obj, attr.key)
    return Snapshot(values, frozenset(sensitive))


def diff(before: Snapshot | None, after: Snapshot | None) -> dict[str, Any]:
    old = before.values if before else {}
    new = after.values if after else {}
    sensitive = (before.sensitive if before else frozenset()) | (
        after.sensitive if after else frozenset()
    )
    result: dict[str, Any] = {}
    for key in sorted(old.keys() | new.keys()):
        old_value, new_value = old.get(key), new.get(key)
        if old_value == new_value:
            continue
        if key in sensitive:
            result[key] = dict(SENSITIVE_MARK)
        else:
            result[key] = {"old": _jsonable(old_value), "new": _jsonable(new_value)}
    return result


def record(
    ctx: ServiceContext,
    action: str,
    *,
    entity: Any = None,
    entity_type: str | None = None,
    entity_id: Any = None,
    before: Snapshot | None = None,
    after: Snapshot | None = None,
    changes: dict[str, Any] | None = None,
    message: str | None = None,
    organization_id: int | None = None,
) -> AuditLog:
    if entity is not None:
        entity_type = entity_type or type(entity).__name__
        if entity_id is None:
            entity_id = getattr(entity, "id", None)
    if changes is None and (before is not None or after is not None):
        changes = diff(before, after)
    entry = AuditLog(
        organization_id=organization_id if organization_id is not None else ctx.organization_id,
        user_id=ctx.user_id,
        username=ctx.username,
        real_role=ctx.real_role.value if ctx.real_role else None,
        acting_role=ctx.role.value if ctx.role else None,
        acting_as_user_id=ctx.acting_as_user_id,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id) if entity_id is not None else None,
        changes=changes or None,
        message=message,
        ip=ctx.ip,
    )
    ctx.session.add(entry)
    return entry
