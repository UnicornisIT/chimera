"""Flask-приложение: фабрика, безопасность запросов и регистрация Blueprints."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import Flask, Response, abort, g, render_template, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from chimera.core import crypto
from chimera.core.config import AppMode, ConfigError, Settings, load_settings
from chimera.core.context import NotFound, PermissionDenied
from chimera.core.db import Database, utcnow
from chimera.core.features import FEATURES, FeatureStatus, get_feature
from chimera.core.metadata import APP_METADATA, app_version
from chimera.core.migrations import schema_state, upgrade_head
from chimera.core.permissions import ROLE_LABELS, Permission, SystemPermission
from chimera.core.security import (
    CSRF_FORM_FIELD,
    CSRF_HEADER,
    SAFE_METHODS,
    csrf_token,
    csrf_valid,
    security_headers,
)
from chimera.services import auth as auth_service
from chimera.services import impersonation
from chimera.web import context as web_context

PACKAGE_DIR = Path(__file__).resolve().parent.parent
log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or load_settings()
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

    if settings.data_encryption_key:
        crypto.configure(settings.data_encryption_key, settings.blind_index_key)

    database = Database(settings.database_url)
    app.extensions["chimera.settings"] = settings
    app.extensions["chimera.db"] = database
    _prepare_schema(database, settings)
    _bootstrap(database, settings)

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
    state = schema_state(database.engine, database.url)
    if state.up_to_date:
        return
    if settings.mode is AppMode.DESKTOP_LOCAL or settings.testing:
        log.info("Обновление схемы базы %s → %s", state.current, state.head)
        upgrade_head(database.url)
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
        if request.endpoint == "static":
            return None
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
            from flask import redirect, url_for

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

    @app.errorhandler(PermissionDenied)
    def _denied(exc: PermissionDenied) -> tuple[str, int]:
        return _render(403, "Доступ запрещён", str(exc) or "Недостаточно прав.")

    @app.errorhandler(NotFound)
    def _not_found(exc: NotFound) -> tuple[str, int]:
        return _render(404, "Не найдено", str(exc) or "Запись не найдена.")

    @app.errorhandler(HTTPException)
    def _http(exc: HTTPException) -> Any:
        if exc.code is None or exc.code < 400:
            return exc
        titles = {
            400: "Некорректный запрос",
            403: "Доступ запрещён",
            404: "Страница не найдена",
            405: "Метод не поддерживается",
            413: "Файл слишком большой",
        }
        return _render(exc.code, titles.get(exc.code, "Ошибка"), exc.description or "")
