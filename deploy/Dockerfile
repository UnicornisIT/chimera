# Образ сервера Chimera: Python 3.11, LibreOffice (конвертация docx → PDF), шрифты,
# клиент PostgreSQL 16 для резервного копирования через pg_dump.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CHIMERA_MODE=server \
    CHIMERA_DATA_DIR=/var/lib/chimera

# Репозиторий PGDG нужен, чтобы версия pg_dump совпадала с сервером PostgreSQL 16.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && . /etc/os-release \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      postgresql-client-16 \
      libreoffice-writer-nogui \
      fontconfig \
      fonts-dejavu \
      fonts-liberation \
      fonts-liberation2 \
      fonts-crosextra-carlito \
      fonts-crosextra-caladea \
      fonts-paratype \
 && apt-get purge -y --auto-remove gnupg \
 && rm -rf /var/lib/apt/lists/*

# Дополнительные шрифты (например, PT Astra Serif) кладутся в deploy/fonts/.
COPY deploy/fonts/ /usr/local/share/fonts/chimera/
RUN fc-cache -f

RUN useradd --system --create-home --home-dir /home/chimera --uid 10001 chimera \
 && install -d -o chimera -g chimera -m 0700 /var/lib/chimera

WORKDIR /app
COPY requirements-prod.lock ./
RUN python -m pip install -r requirements-prod.lock

COPY --chown=chimera:chimera . .
RUN python -m pip install --no-deps . \
 && chmod +x deploy/docker-entrypoint.sh

USER chimera
EXPOSE 8000
VOLUME ["/var/lib/chimera"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/login', timeout=4)"]
ENTRYPOINT ["deploy/docker-entrypoint.sh"]
CMD ["gunicorn", "--config", "deploy/gunicorn.conf.py", "chimera.wsgi:app"]
