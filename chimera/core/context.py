"""Контекст вызова сервиса: кто действует, в какой организации и в какой роли."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from chimera.core.permissions import Permission, Role, SystemPermission, permissions_for


class PermissionDenied(PermissionError):
    """Действие запрещено для текущей роли."""


class NotFound(LookupError):
    """Запись не найдена (или недоступна в текущей организации)."""


class ServiceError(ValueError):
    """Ошибка бизнес-правила; текст показывается пользователю."""


@dataclass(frozen=True, slots=True)
class ServiceContext:
    session: Session
    user_id: int | None = None
    username: str | None = None
    organization_id: int | None = None
    role: Role | None = None
    """Действующая роль (с учётом режима «работать как»)."""
    real_role: Role | None = None
    """Роль пользователя в организации без учёта режима «работать как»."""
    is_superadmin: bool = False
    division_id: int | None = None
    acting_as_user_id: int | None = None
    ip: str | None = None
    permissions: frozenset[Permission] = field(default=frozenset())

    @classmethod
    def build(
        cls,
        session: Session,
        *,
        role: Role | None,
        real_role: Role | None = None,
        **kwargs: object,
    ) -> ServiceContext:
        return cls(
            session=session,
            role=role,
            real_role=real_role if real_role is not None else role,
            permissions=permissions_for(role),
            **kwargs,  # type: ignore[arg-type]
        )

    @classmethod
    def system(cls, session: Session, organization_id: int | None = None) -> ServiceContext:
        """Контекст служебных операций (миграции, начальная настройка, CLI)."""
        return cls(session=session, organization_id=organization_id, username="system")

    @property
    def is_acting(self) -> bool:
        return self.role is not None and self.real_role is not None and self.role != self.real_role

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        if not self.can(permission):
            raise PermissionDenied(f"Недостаточно прав: {permission.value}")

    def can_system(self, permission: SystemPermission) -> bool:
        del permission  # все системные права пока принадлежат суперадмину
        return self.is_superadmin and not self.is_acting

    def require_system(self, permission: SystemPermission) -> None:
        if not self.can_system(permission):
            raise PermissionDenied(f"Недостаточно прав: {permission.value}")

    def require_organization(self) -> int:
        if self.organization_id is None:
            raise PermissionDenied("Не выбрана организация")
        return self.organization_id
