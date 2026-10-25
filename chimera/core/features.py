"""Фича-флаги: что готово, что в бета-версии, а что только запланировано.

Незавершённая функциональность не прячется молча: модуль регистрируется здесь,
а интерфейс показывает бейдж «Бета» или «Скоро» через макрос `feature_badge`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class FeatureStatus(StrEnum):
    STABLE = "stable"
    BETA = "beta"
    PLANNED = "planned"


BADGE_LABELS = MappingProxyType(
    {FeatureStatus.STABLE: "", FeatureStatus.BETA: "Бета", FeatureStatus.PLANNED: "Скоро"}
)
BADGE_TONES = MappingProxyType(
    {FeatureStatus.STABLE: "success", FeatureStatus.BETA: "info", FeatureStatus.PLANNED: "warning"}
)


@dataclass(frozen=True, slots=True)
class Feature:
    key: str
    title: str
    status: FeatureStatus
    description: str = ""

    @property
    def available(self) -> bool:
        return self.status is not FeatureStatus.PLANNED

    @property
    def badge(self) -> str:
        return BADGE_LABELS[self.status]

    @property
    def badge_tone(self) -> str:
        return BADGE_TONES[self.status]


_FEATURES = (
    Feature("dashboard", "Главная", FeatureStatus.STABLE),
    Feature("administration", "Администрирование", FeatureStatus.STABLE),
    Feature(
        "groups",
        "Группы и студенты",
        FeatureStatus.PLANNED,
        "Учебные группы, досье, контакты и история обучения.",
    ),
    Feature(
        "documents",
        "Документы",
        FeatureStatus.PLANNED,
        "Характеристики, ведомости, объяснительные, протоколы УВК и отчёты из шаблонов.",
    ),
    Feature(
        "assignments",
        "Поручения",
        FeatureStatus.PLANNED,
        "Задания от руководства подразделения кураторам с контролем сроков.",
    ),
    Feature(
        "references",
        "Справочники",
        FeatureStatus.PLANNED,
        "Специальности, учебные годы, типовые формулировки.",
    ),
    Feature(
        "import",
        "Импорт таблиц",
        FeatureStatus.PLANNED,
        "Загрузка списков из Excel с сопоставлением колонок.",
    ),
    Feature(
        "curator_hours",
        "Кураторские часы и дежурства",
        FeatureStatus.PLANNED,
        "Планы кураторских часов, графики дежурств и уборки, проверки общежитий.",
    ),
    Feature(
        "act_as_starosta_group",
        "Режим старосты по группе",
        FeatureStatus.PLANNED,
        "Выбор группы появится вместе с модулем групп; пока выбирается пользователь-староста.",
    ),
    Feature("desktop", "Windows-клиент", FeatureStatus.PLANNED),
)

FEATURES: MappingProxyType[str, Feature] = MappingProxyType({f.key: f for f in _FEATURES})


def get_feature(key: str) -> Feature:
    try:
        return FEATURES[key]
    except KeyError as exc:
        raise KeyError(f"Неизвестный фича-флаг: {key}") from exc


def is_enabled(key: str) -> bool:
    return get_feature(key).available
