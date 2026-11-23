"""WSGI-точка входа для gunicorn: `gunicorn chimera.wsgi:app`."""

import sys

from chimera.core.config import AppMode, ConfigError, load_settings
from chimera.web import create_app

APP_LOAD_ERROR = 4

try:
    settings = load_settings(require_secrets=False)
    if settings.mode is not AppMode.SERVER:
        raise ConfigError("WSGI предназначен для режима server. Для Desktop Local используйте run.")
    app = create_app(settings)
except ConfigError as exc:
    print(f"Ошибка настройки: {exc}", file=sys.stderr)
    # Gunicorn останавливает мастер при ошибке загрузки вместо перезапуска воркера.
    raise SystemExit(APP_LOAD_ERROR) from None
