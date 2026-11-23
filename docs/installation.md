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

   Обязательно задайте `DATABASE_URL`, `SECRET_KEY`, `DATA_ENCRYPTION_KEY`, `POSTGRES_PASSWORD` и
   `ADMIN_DEFAULT_PASSWORD` (не короче 10 символов, буквы и цифры).
   В `DATABASE_URL` укажите хост `db` и порт `5432`. Имя базы, роль и пароль
   должны совпадать с настройками контейнера PostgreSQL. Пароль в URL нужен
   в percent-encoding, например `a%40b%3Ac` для `a@b:c`.
   Укажите `CHIMERA_DEFAULT_ORG_NAME` и `CHIMERA_DEFAULT_ORG_TYPE`
   (`university` или `college`) — так будет создана первая организация.

2. Соберите и запустите:

   ```bash
   docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
   ```

   Контейнер приложения перед миграцией проверяет ключи и PostgreSQL 16
   и создаёт начального администратора, если пользователей ещё нет.

3. Откройте адрес доверенного HTTPS-прокси и войдите под `admin` (или `ADMIN_DEFAULT_USERNAME`).
   Система потребует сменить пароль. После этого `ADMIN_DEFAULT_PASSWORD`
   можно удалить из `.env`.

Для Server настройте HTTPS по [инструкции](deployment.md). Защитите доступ к
файлу `.env` и храните резервные копии ключей отдельно от базы.

## Вариант 2. Ручная установка

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-prod.lock   # Linux, точные версии
.venv/bin/python -m pip install --no-deps -e .
cp .env.example .env     # задайте URL PostgreSQL, ключи и CHIMERA_DATA_DIR
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

Оставьте `DATABASE_URL` пустым и используйте `APP_HOST=127.0.0.1`.
В новом профиле Windows база `chimera.db` и файлы ключей `secret_key` и
`data_encryption.key` создаются в `%LOCALAPPDATA%/Chimera`. Linux из исходников
по умолчанию использует `instance`. При обнаружении прежней базы, ключей,
загрузок или копий в `instance` явно укажите `CHIMERA_DATA_DIR=instance`, чтобы продолжить
работу с этими файлами. Явно настроенный каталог сохраняет приоритет.
Сделайте отдельную копию ключа шифрования. Переключение режимов оставляет
данные в прежних каталогах, переноса и синхронизации нет.

Если файл SQLite уже существует, запуск требует ключ подписи и исходный
ключ шифрования. Восстановите `secret_key` и `data_encryption.key` из копии
или укажите ключи явно в `SECRET_KEY` и `DATA_ENCRYPTION_KEY`. Для нового
профиля ключи создаёт команда `run` или `migrate`.

```bash
python -m chimera run      # http://127.0.0.1:5000
```

## Команды

| Команда | Назначение |
|---|---|
| `python -m chimera migrate` | обновить схему базы до последней версии |
| `python -m chimera status` | версия приложения и ревизия схемы |
| `python -m chimera bootstrap` | создать первую организацию и администратора |
| `python -m chimera run` | HTTP-запуск Desktop Local на loopback |

При установке из wheel миграции поставляются в `chimera/migrations/`.
Для `migrate`, `status` и `bootstrap --no-input` достаточно активировать
окружение пакета и передать настройки режима. Эти команды можно запустить
из любого каталога. Файл `alembic.ini` используется в checkout для разработки.
Из другого каталога передайте `python -m alembic -c /путь/к/checkout/alembic.ini`
с нужной командой Alembic.

## Переменные окружения

Полный список с комментариями — в [`.env.example`](../.env.example). Главные:

| Переменная | Смысл |
|---|---|
| `CHIMERA_MODE` | `server` или `desktop-local` |
| `DATABASE_URL` | Server требует URL PostgreSQL, Desktop Local принимает URL файла SQLite или пустое значение |
| `SECRET_KEY` | подпись сессий, ≥ 32 символов |
| `DATA_ENCRYPTION_KEY` | ключ Fernet для персональных данных; несколько через запятую — ротация |
| `CHIMERA_DATA_DIR` | Server требует отдельный каталог, Desktop Local использует каталог профиля или явный путь |
| `PG_DUMP_PATH` | путь к `pg_dump` для резервных копий PostgreSQL |
| `LOGIN_MAX_ATTEMPTS`, `LOGIN_WINDOW_SECONDS` | ограничение попыток входа |


Относительные значения `CHIMERA_DATA_DIR`, `CHIMERA_UPLOAD_DIR` и
`CHIMERA_BACKUP_DIR` разрешаются от корня checkout или от домашнего каталога
при установке wheel. Установленный Desktop Local на Linux по умолчанию
использует `$XDG_DATA_HOME/Chimera` или `~/.local/share/Chimera`.
Относительный путь в
SQLite URL разрешается от текущего рабочего каталога процесса. Для
однозначного запуска используйте абсолютные пути, например
`sqlite:///C:/Users/USER/AppData/Local/Chimera/chimera.db` на Windows и
`sqlite:////home/user/chimera/chimera.db` на Linux. Каталог и существующий
файл должны быть доступны текущему пользователю. Desktop Local отклоняет
каталоги сборки и временный каталог распаковки приложения.

Для диагностики проверьте режим, имя параметра в сообщении, права доступа и
`python -m chimera status`. При обращении за поддержкой передавайте сообщение
об ошибке без файла `.env`, ключей и строки подключения.

## Изолированные тесты

Из checkout с установленными зависимостями разработки запустите `python -m
pytest` для SQLite или `python -m pytest --chimera-postgres` для PostgreSQL
16. Добавьте каталог утилит PostgreSQL 16 в `PATH` и освободите loopback-порт
55439. Pytest создаёт временный кластер, проверяет его каталог и останавливает
после тестов. Для браузера используйте `python -m scripts.dev_test_env
postgres run` или `python -m scripts.dev_test_env sqlite run`. См.
[инструкцию стендов](planning/STAGE_02_TEST_ENVS.md).

Проверка поставки запускается через `python -m scripts.check_installed_migrations`
из checkout при установленном модуле `build`. Она собирает два wheel, проверяет
архивы и запускает команды из отдельных временных окружений. Флаг `--postgres`
добавляет проверку на выделенном PostgreSQL 16 (loopback-порт 55441).
