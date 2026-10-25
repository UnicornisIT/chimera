# Установка и настройка

## Требования

- CPython **3.11** (ровно; 3.12 не поддерживается — совместимость с Manticore);
- для сервера — PostgreSQL 16 и клиент `pg_dump` той же версии;
- для Docker-установки — Docker Engine 24+ с плагином Compose.

## Вариант 1. Docker Compose (рекомендуется для сервера)

1. Скопируйте пример настроек и заполните секреты:

   ```bash
   cp .env.example .env
   python -c "import secrets; print(secrets.token_urlsafe(48))"            # SECRET_KEY
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"  # DATA_ENCRYPTION_KEY
   ```

   Обязательно задайте `SECRET_KEY`, `DATA_ENCRYPTION_KEY`, `POSTGRES_PASSWORD` и
   `ADMIN_DEFAULT_PASSWORD` (не короче 10 символов, буквы и цифры).
   Укажите `CHIMERA_DEFAULT_ORG_NAME` и `CHIMERA_DEFAULT_ORG_TYPE`
   (`university` или `college`) — так будет создана первая организация.

2. Соберите и запустите:

   ```bash
   docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
   ```

   Контейнер приложения при старте выполняет миграции (`python -m chimera migrate`)
   и создаёт начального администратора, если пользователей ещё нет.

3. Откройте `http://<сервер>` и войдите под `admin` (или `ADMIN_DEFAULT_USERNAME`).
   Система потребует сменить пароль. После этого `ADMIN_DEFAULT_PASSWORD`
   можно удалить из `.env`.

Для доступа из сети настройте HTTPS (см. [развёртывание](deployment.md)) и
оставьте `SESSION_COOKIE_SECURE=true`.

## Вариант 2. Ручная установка

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-prod.lock   # Linux, точные версии
.venv/bin/python -m pip install --no-deps -e .
cp .env.example .env     # DATABASE_URL=postgresql+psycopg://chimera:пароль@localhost:5432/chimera
.venv/bin/python -m chimera migrate
.venv/bin/python -m chimera bootstrap   # спросит пароль, если ADMIN_DEFAULT_PASSWORD не задан
.venv/bin/gunicorn --config deploy/gunicorn.conf.py chimera.wsgi:app
```

На Windows используйте `py -3.11 -m venv .venv` и `.venv\Scripts\python.exe`.

## Локальный режим (одна машина, SQLite)

Для разработки или работы на одном компьютере:

```env
CHIMERA_MODE=desktop-local
ADMIN_DEFAULT_PASSWORD=СложныйПароль123
```

В этом режиме база `instance/chimera.db` создаётся и обновляется автоматически,
а `SECRET_KEY` и `DATA_ENCRYPTION_KEY` генерируются и сохраняются в
`instance/` (файлы `secret_key` и `data_encryption.key`). **Сделайте копию ключа
шифрования**: без него зашифрованные поля не восстановить.

```bash
python -m chimera run      # http://127.0.0.1:5000
```

## Команды

| Команда | Назначение |
|---|---|
| `python -m chimera migrate` | обновить схему базы до последней версии |
| `python -m chimera status` | версия приложения и ревизия схемы |
| `python -m chimera bootstrap` | создать первую организацию и администратора |
| `python -m chimera run` | сервер разработки |

## Переменные окружения

Полный список с комментариями — в [`.env.example`](../.env.example). Главные:

| Переменная | Смысл |
|---|---|
| `CHIMERA_MODE` | `server` или `desktop-local` |
| `DATABASE_URL` | строка подключения; пусто — SQLite в `CHIMERA_DATA_DIR` |
| `SECRET_KEY` | подпись сессий, ≥ 32 символов |
| `DATA_ENCRYPTION_KEY` | ключ Fernet для персональных данных; несколько через запятую — ротация |
| `CHIMERA_DATA_DIR` | каталог данных: SQLite, загрузки, бэкапы |
| `PG_DUMP_PATH` | путь к `pg_dump` для резервных копий PostgreSQL |
| `LOGIN_MAX_ATTEMPTS`, `LOGIN_WINDOW_SECONDS` | ограничение попыток входа |
