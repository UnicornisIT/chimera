"""Нормализация и проверка простых полей форм (по мотивам email_validation.py Manticore)."""

from __future__ import annotations

import re
import unicodedata

_EMAIL_LOCAL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}$")
_EMAIL_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
_USERNAME = re.compile(r"^[A-Za-z0-9._-]{3,150}$")

# Частые опечатки в доменах почты, которые пользователи вводят вручную.
EMAIL_DOMAIN_TYPOS = {
    "gmail.ru": "gmail.com",
    "gmial.com": "gmail.com",
    "gamil.com": "gmail.com",
    "yandex.ry": "yandex.ru",
    "yandeх.ru": "yandex.ru",
    "mail.ry": "mail.ru",
    "maill.ru": "mail.ru",
    "outlok.com": "outlook.com",
}


def clean_text(value: str | None, *, max_length: int | None = None) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    text = " ".join(text.split())
    if max_length is not None:
        text = text[:max_length]
    return text


def optional_text(value: str | None, *, max_length: int | None = None) -> str | None:
    text = clean_text(value, max_length=max_length)
    return text or None


def normalize_username(value: str | None) -> str:
    return clean_text(value).lower()


def username_problem(value: str) -> str | None:
    if not _USERNAME.fullmatch(value):
        return "Логин: от 3 символов, латинские буквы, цифры, точка, дефис или подчёркивание."
    return None


def normalize_email(value: str | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    local, sep, domain = text.rpartition("@")
    if not sep:
        return text
    return f"{local}@{domain.lower()}"


def email_problem(value: str | None) -> str | None:
    """Синтаксическая проверка адреса; возвращает текст ошибки или подсказку."""
    if not value:
        return None
    local, sep, domain = value.rpartition("@")
    if not sep or not local or not domain:
        return "Адрес почты должен содержать «@» и домен."
    if not _EMAIL_LOCAL.fullmatch(local) or local.startswith(".") or ".." in local:
        return "Некорректная часть адреса до «@»."
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return "Некорректный домен почты."
    labels = ascii_domain.split(".")
    if len(labels) < 2 or not all(_EMAIL_LABEL.fullmatch(label) for label in labels):
        return "Некорректный домен почты."
    if domain in EMAIL_DOMAIN_TYPOS:
        return f"Возможно, опечатка в домене: имелось в виду {EMAIL_DOMAIN_TYPOS[domain]}?"
    return None
