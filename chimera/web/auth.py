"""Вход, выход, саморегистрация, смена пароля и профиль."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from chimera.core.context import ServiceError
from chimera.core.security import PASSWORD_MIN_LENGTH
from chimera.services import auth as auth_service
from chimera.web.context import (
    client_ip,
    ctx,
    db,
    form_int,
    login_required,
    login_user,
    refresh_auth_version,
    require_user,
    state,
)

bp = Blueprint("auth", __name__)


def _safe_next(target: str | None) -> str | None:
    if not target:
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//"):
        return None
    return target


@bp.route("/login", methods=["GET", "POST"])
def login() -> Any:
    if state().user is not None:
        return redirect(url_for("main.dashboard"))
    username = request.form.get("username", "")
    if request.method == "POST":
        settings = current_app.extensions["chimera.settings"]
        session_db = db()
        try:
            user = auth_service.authenticate(
                session_db,
                username,
                request.form.get("password", ""),
                ip=client_ip(),
                policy=auth_service.LoginPolicy.from_settings(settings),
            )
        except ServiceError as exc:
            session_db.commit()  # неудачная попытка учитывается в ограничении
            flash(str(exc), "error")
        else:
            session_db.commit()
            login_user(user)
            if user.must_change_password:
                return redirect(url_for("auth.change_password"))
            return redirect(_safe_next(request.args.get("next")) or url_for("main.dashboard"))
    return render_template("auth/login.html", username=username)


@bp.post("/logout")
def logout() -> Any:
    from flask import session

    current = ctx()
    if current.user_id is not None:
        from chimera.core import audit

        audit.record(current, "auth.logout", entity_type="User", entity_id=current.user_id)
        db().commit()
    session.clear()
    flash("Вы вышли из системы.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/register", methods=["GET", "POST"])
def register() -> Any:
    if state().user is not None:
        return redirect(url_for("main.dashboard"))
    session_db = db()
    organizations = auth_service.registration_organizations(session_db)
    form = request.form
    if request.method == "POST":
        if form.get("password") != form.get("password_confirm"):
            flash("Пароли не совпадают.", "error")
        else:
            try:
                auth_service.register(
                    session_db,
                    username=form.get("username", ""),
                    full_name=form.get("full_name", ""),
                    email=form.get("email"),
                    password=form.get("password", ""),
                    organization_id=form_int("organization_id") or 0,
                    comment=form.get("comment"),
                    ip=client_ip(),
                )
            except ServiceError as exc:
                session_db.rollback()
                flash(str(exc), "error")
            else:
                session_db.commit()
                flash("Заявка отправлена. Войти можно после одобрения администратором.", "success")
                return redirect(url_for("auth.login"))
    return render_template(
        "auth/register.html",
        organizations=organizations,
        form=form,
        password_min_length=PASSWORD_MIN_LENGTH,
    )


@bp.route("/password", methods=["GET", "POST"])
def change_password() -> Any:
    user = state().user
    if user is None:
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        new = request.form.get("new_password", "")
        if new != request.form.get("new_password_confirm", ""):
            flash("Новые пароли не совпадают.", "error")
        else:
            try:
                auth_service.change_password(
                    ctx(), user, request.form.get("current_password", ""), new
                )
            except ServiceError as exc:
                db().rollback()
                flash(str(exc), "error")
            else:
                db().commit()
                refresh_auth_version(user)
                flash("Пароль изменён.", "success")
                return redirect(url_for("main.dashboard"))
    return render_template(
        "auth/change_password.html",
        forced=user.must_change_password,
        password_min_length=PASSWORD_MIN_LENGTH,
    )


@bp.get("/profile")
@login_required
def profile() -> Any:
    user = require_user()
    memberships = auth_service.user_memberships(db(), user)
    return render_template("auth/profile.html", user=user, memberships=memberships)
