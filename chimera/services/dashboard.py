"""Карточки главной страницы: какие модули доступны и что ещё в разработке."""

from __future__ import annotations

from dataclasses import dataclass

from chimera.core.context import ServiceContext
from chimera.core.features import Feature, get_feature
from chimera.core.permissions import Permission
from chimera.core.terms import Terms


@dataclass(frozen=True, slots=True)
class ModuleCard:
    feature: Feature
    title: str
    description: str
    endpoint: str | None = None


def module_cards(ctx: ServiceContext, terms: Terms) -> list[ModuleCard]:
    cards: list[ModuleCard] = [
        ModuleCard(
            get_feature("groups"),
            f"Группы и {terms.student.nom_pl.lower()}",
            f"Учебные группы, досье {terms.student.gen_pl}, контакты и история обучения.",
        ),
        ModuleCard(get_feature("documents"), "Документы", get_feature("documents").description),
        ModuleCard(get_feature("assignments"), "Поручения", get_feature("assignments").description),
        ModuleCard(
            get_feature("curator_hours"),
            "Кураторские часы и дежурства",
            get_feature("curator_hours").description,
        ),
        ModuleCard(
            get_feature("references"),
            "Справочники",
            f"{terms.program.nom_pl}, учебные годы, типовые формулировки.",
        ),
        ModuleCard(get_feature("import"), "Импорт таблиц", get_feature("import").description),
    ]
    if ctx.can(Permission.USERS_MANAGE):
        cards.append(
            ModuleCard(
                get_feature("administration"),
                "Администрирование",
                f"Реквизиты организации, {terms.division.nom_pl.lower()}, пользователи, "
                "заявки и журнал аудита.",
                "admin.index",
            )
        )
    return cards
