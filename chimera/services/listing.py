"""Общие операции для серверных списков."""

from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import SQLColumnExpression

PAGE_SIZE = 50
Row = TypeVar("Row")


@dataclass(frozen=True, slots=True)
class Page(Generic[Row]):
    items: list[Row]
    total: int
    page: int
    pages: int


def paginate(session: Session, statement: Select[Any], requested: int) -> Page[Row]:
    total = (
        session.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
    )
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, requested), pages)
    items: list[Row] = list(
        session.scalars(statement.limit(PAGE_SIZE).offset((page - 1) * PAGE_SIZE))
    )
    return Page(items, total, page, pages)


def contains_text(
    session: Session, column: SQLColumnExpression[str] | SQLColumnExpression[str | None], query: str
) -> SQLColumnExpression[bool]:
    if session.get_bind().dialect.name == "sqlite":
        lowered = func.chimera_lower(column)
    else:
        # Локаль C может сворачивать регистр только для ASCII.
        capitals = "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
        lowered = func.translate(func.lower(column), capitals, capitals.lower())
    return lowered.contains(query.lower(), autoescape=True)
