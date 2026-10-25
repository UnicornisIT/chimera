"""WSGI-точка входа для gunicorn: `gunicorn chimera.wsgi:app`."""

from chimera.web import create_app

app = create_app()
