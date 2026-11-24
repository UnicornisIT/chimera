"""Flask-приложение: фабрика, безопасность запросов и регистрация Blueprints."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from alembic.util.exc import CommandError
from flask import Flask, Response, abort, g, redirect, render_template, request, session, url_for
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from chimera.core import crypto
from chimera.core.config import (
    LOOPBACK,
    AppMode,
    ConfigError,
    Settings,
    load_settings,
    validate_settings,
)
from chimera.core.context import NotFound, PermissionDenied, ServiceError
from chimera.core.db import Database, utcnow
from chimera.core.features import FEATURES, FeatureStatus, get_feature
from chimera.core.metadata import APP_METADATA, app_version
from chimera.core.migrations import SchemaTooNewError, schema_state, upgrade_head
from chimera.core.permissions import ROLE_LABELS, Permission, SystemPermission
from chimera.core.security import (
    CSRF_FORM_FIELD,
    CSRF_HEADER,
    SAFE_METHODS,
    csrf_token,
    csrf_valid,
    security_headers,
)
from chimera.core.sqlite_snapshot import sqlite_error_message
from chimera.services import auth as auth_service
from chimera.services import impersonation
from chimera.services.local_profile import prepare_local_schema
from chimera.web import context as web_context
from chimera.web.local_setup import SETUP_PENDING, register_setup

PACKAGE_DIR = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, *, setup_token: str | None = None) -> Flask:
    settings = load_settings() if settings is None else validate_settings(settings)
    app = Flask(
        "chimera",
        template_folder=str(PACKAGE_DIR / "templates"),
        static_folder=str(PACKAGE_DIR / "static"),
    )
    app.config.update(
        SECRET_KEY=settings.secret_key,
        TESTING=settings.testing,
        MAX_CONTENT_LENGTH=settings.max_upload_bytes,
        SESSION_COOKIE_NAME="chimera_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.session_cookie_secure,
        PERMANENT_SESSION_LIFETIME=timedelta(minutes=settings.session_lifetime_minutes),
        TEMPLATES_AUTO_RELOAD=settings.debug,
    )
    if settings.mode is AppMode.SERVER and not settings.testing:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # type: ignore[method-assign]

    database = Database.from_settings(settings)
    app.extensions["chimera.settings"] = settings
    app.extensions["chimera.db"] = database
    try:
        _prepare_schema(database, settings)
        crypto.configure(settings.data_encryption_key, settings.blind_index_key)
        _bootstrap(database, settings)
        if setup_token and settings.mode is AppMode.DESKTOP_LOCAL:
            register_setup(app, database, settings, setup_token)
    except (ConfigError, SchemaTooNewError, ServiceError) as exc:
        database.dispose()
        raise ConfigError(str(exc)) from None
    except (CommandError, SQLAlchemyError):
        database.dispose()
        raise ConfigError(
            "Не удалось подготовить базу Chimera. Проверьте подключение и схему."
        ) from None

    _register_request_hooks(app, settings)
    _register_template_helpers(app)
    _register_error_handlers(app)

    from chimera.web.admin import bp as admin_bp
    from chimera.web.auth import bp as auth_bp
    from chimera.web.main import bp as main_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp)
    return app


def _prepare_schema(database: Database, settings: Settings) -> None:
    if settings.mode is AppMode.DESKTOP_LOCAL:
        prepare_local_schema(database, settings)
        return
    state = schema_state(database.engine, database.url)
    if state.up_to_date:
        return
    if settings.testing:
        log.info("Обновление схемы базы %s → %s", state.current, state.head)
        upgrade_head(database)
        return
    raise ConfigError(
        f"Схема базы не обновлена (текущая {state.current or 'пусто'}, нужна {state.head}). "
        "Выполните: python -m chimera migrate"
    )


def _bootstrap(database: Database, settings: Settings) -> None:
    with database.transaction() as session:
        admin = auth_service.ensure_initial_admin(session, settings)
        if admin is not None:
            log.warning(
                "Создан начальный администратор %r: смените пароль при входе", admin.username
            )


def _register_request_hooks(app: Flask, settings: Settings) -> None:
    @app.before_request
    def _before() -> Any:
        if (
            settings.mode is AppMode.DESKTOP_LOCAL
            and not settings.testing
            and (
                request.host.split(":", 1)[0] not in {LOOPBACK, "localhost"}
                or request.remote_addr != LOOPBACK
            )
        ):
            return Response("Локальный профиль доступен только на этом компьютере.", 400)
        if request.endpoint == "static":
            return None
        if app.extensions.get(SETUP_PENDING) and request.endpoint not in {None, "local_setup"}:
            return redirect(url_for("local_setup"))
        if request.method not in SAFE_METHODS:
            submitted = request.form.get(CSRF_FORM_FIELD) or request.headers.get(CSRF_HEADER)
            if not csrf_valid(session, submitted):
                abort(400, description="Сессия устарела: обновите страницу и повторите действие.")
        web_context.load_request_context()
        state = web_context.state()
        if (
            state.user is not None
            and state.user.must_change_password
            and request.endpoint not in web_context.PASSWORD_CHANGE_ALLOWED
        ):
            return redirect(url_for("auth.change_password"))
        return None

    @app.after_request
    def _after(response: Response) -> Response:
        for key, value in security_headers(hsts=settings.enable_hsts).items():
            response.headers.setdefault(key, value)
        if request.endpoint != "static":
            response.headers.setdefault("Cache-Control", "no-store")
        ctx = g.get("ctx")
        if (
            ctx is not None
            and ctx.is_acting
            and request.endpoint not in {None, "static"}
            and response.status_code < 500
        ):
            impersonation.record_action(ctx, request.method, request.endpoint, request.path)
            ctx.session.commit()
        if request.method == "POST" and request.headers.get("X-Chimera-Form") == "1":
            if response.status_code in {302, 303} and response.location:
                response.headers["X-Chimera-Location"] = response.location
                del response.headers["Location"]
                response.status_code = 204
            response.headers["X-Chimera-Form"] = "1"
        return response

    @app.teardown_request
    def _teardown(exc: BaseException | None) -> None:
        session_db = g.pop("db", None)
        if session_db is not None:
            if exc is not None:
                session_db.rollback()
            session_db.close()


def _register_template_helpers(app: Flask) -> None:
    @app.context_processor
    def _inject() -> dict[str, Any]:
        state = web_context.state()
        ctx = g.get("ctx")
        return {
            "app_metadata": APP_METADATA,
            "app_version": app_version(),
            "current_user": state.user,
            "current_org": state.organization,
            "user_organizations": state.organizations or [],
            "acting": state.acting,
            "ctx": ctx,
            "t": web_context.current_terms(),
            "role_labels": ROLE_LABELS,
            "features": FEATURES,
            "FeatureStatus": FeatureStatus,
            "P": Permission,
            "SP": SystemPermission,
            "csrf_token": lambda: csrf_token(session),
            "csrf_field_name": CSRF_FORM_FIELD,
            "get_feature": get_feature,
            "now": utcnow,
        }

    @app.template_filter("datetime")
    def _datetime(value: Any, fmt: str = "%d.%m.%Y %H:%M") -> str:
        if value is None:
            return ""
        from zoneinfo import ZoneInfo

        try:
            local = value.astimezone(ZoneInfo("Europe/Moscow"))
        except Exception:  # pragma: no cover - нет базы часовых поясов
            local = value
        return str(local.strftime(fmt))


def _register_error_handlers(app: Flask) -> None:
    def _render(code: int, title: str, message: str) -> tuple[str, int]:
        return render_template("error.html", code=code, title=title, message=message), code

    if app.extensions["chimera.settings"].mode is AppMode.DESKTOP_LOCAL:

        @app.errorhandler(OperationalError)
        def _sqlite_unavailable(exc: OperationalError) -> tuple[str, int]:
            return _render(
                503,
                "База временно недоступна",
                sqlite_error_message(exc.orig, "Не удалось выполнить операцию в SQLite."),
            )

    @app.errorhandler(PermissionDenied)
    def _denied(_exc: PermissionDenied) -> tuple[str, int]:
        return _render(403, "Доступ запрещён", "У вас нет доступа к этому разделу или действию.")

    @app.errorhandler(NotFound)
    def _not_found(_exc: NotFound) -> tuple[str, int]:
        return _render(404, "Не найдено", "Запись не найдена.")

    @app.errorhandler(HTTPException)
    def _http(exc: HTTPException) -> Any:
        if exc.code is None or exc.code < 400:
            return exc
        titles = {
            400: "Некорректный запрос",
            401: "Требуется вход",
            403: "Доступ запрещён",
            404: "Страница не найдена",
            405: "Метод не поддерживается",
            413: "Файл слишком большой",
        }
        messages = {
            403: "У вас нет доступа к этому разделу или действию.",
            408: "Время ожидания ответа истекло.",
            502: "Не удалось связаться с приложением.",
            503: "Приложение временно недоступно.",
            504: "Приложение не ответило вовремя.",
            500: "Не удалось выполнить запрос.",
        }
        # Для запроса без CSRF сохраняем подсказку об обновлении страницы.
        message = (
            exc.description
            if exc.code == 400 and exc.description != type(exc).description
            else messages.get(exc.code, "")
        )
        if request.method == "POST" and (exc.code == 408 or exc.code >= 500):
            message = (
                f"{message or 'Не удалось получить ответ.'} "
                "Проверьте результат в нужном разделе перед повторным действием."
            )
        return _render(exc.code, titles.get(exc.code, "Ошибка"), message or "")
