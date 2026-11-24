"""Однократная настройка администратора из окна, открытого локальным запуском."""

from __future__ import annotations

import secrets
from threading import Lock
from typing import Any

from flask import Flask, abort, flash, redirect, render_template, request, url_for

from chimera.core.config import LOOPBACK, Settings
from chimera.core.context import ServiceError
from chimera.core.db import Database
from chimera.core.security import PASSWORD_MIN_LENGTH
from chimera.services.auth import ensure_initial_admin, local_setup_needed

SETUP_PENDING = "chimera.local_setup_pending"


def register_setup(app: Flask, database: Database, settings: Settings, token: str) -> bool:
    with database.session() as db:
        if not local_setup_needed(db):
            return False

    lock = Lock()
    app.extensions[SETUP_PENDING] = True
    authority = LOOPBACK if settings.port == 80 else f"{LOOPBACK}:{settings.port}"

    @app.route("/local-setup", methods=["GET", "POST"])
    def local_setup() -> Any:
        if request.remote_addr != LOOPBACK or request.host != authority:
            abort(403)
        with lock:
            if not app.extensions[SETUP_PENDING]:
                return redirect(url_for("auth.login"))
            error = None
            submitted = ""
            if request.method == "POST":
                submitted = request.form.get("setup_token", "")
                if (
                    request.headers.get("Origin") != f"http://{authority}"
                    or not submitted.isascii()
                    or not secrets.compare_digest(submitted, token)
                ):
                    abort(403)
                password = request.form.get("password", "")
                if password != request.form.get("password_confirm"):
                    error = "Пароли не совпадают."
                else:
                    try:
                        with database.transaction() as db:
                            admin = ensure_initial_admin(db, settings, chosen_password=password)
                    except ServiceError as exc:
                        error = str(exc)
                    else:
                        app.extensions[SETUP_PENDING] = False
                        if admin is not None:
                            flash("Администратор создан. Войдите с новым паролем.", "success")
                        return redirect(url_for("auth.login"), code=303)
            return render_template(
                "auth/local_setup.html",
                username=settings.admin_default_username,
                setup_token=submitted,
                error=error,
                password_min_length=PASSWORD_MIN_LENGTH,
            )

    return True
