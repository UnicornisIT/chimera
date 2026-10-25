"""Пароли, CSRF и заголовки безопасности HTTP."""

from __future__ import annotations

import hmac
import secrets
from collections.abc import MutableMapping
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

PASSWORD_MIN_LENGTH = 10
CSRF_SESSION_KEY = "_csrf_token"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)


def hash_password(password: str) -> str:
    # Тот же формат werkzeug, что и в Manticore: хэши переносимы между приложениями.
    return generate_password_hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    if not password_hash:
        return False
    return check_password_hash(password_hash, password)


def password_problems(password: str, *, username: str | None = None) -> list[str]:
    problems: list[str] = []
    if len(password) < PASSWORD_MIN_LENGTH:
        problems.append(f"Пароль должен быть не короче {PASSWORD_MIN_LENGTH} символов.")
    if password.strip() != password:
        problems.append("Пароль не должен начинаться или заканчиваться пробелом.")
    if username and password.casefold() == username.casefold():
        problems.append("Пароль не должен совпадать с логином.")
    if password.isdigit() or password.isalpha():
        problems.append("Пароль должен содержать и буквы, и цифры или другие символы.")
    return problems


def csrf_token(session: MutableMapping[str, Any]) -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def csrf_valid(session: MutableMapping[str, Any], submitted: str | None) -> bool:
    expected = session.get(CSRF_SESSION_KEY)
    if not isinstance(expected, str) or not expected or not submitted:
        return False
    return hmac.compare_digest(expected, submitted)


def security_headers(*, hsts: bool) -> dict[str, str]:
    headers = {
        "Content-Security-Policy": CONTENT_SECURITY_POLICY,
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        "Cross-Origin-Opener-Policy": "same-origin",
    }
    if hsts:
        headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return headers
