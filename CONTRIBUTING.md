# Участие в разработке

Сначала прочитайте [правила разработки](docs/DEVELOPMENT.md): стек, структура
пакета, требования к данным, безопасности и терминологии.

## Окружение

Проект поддерживает только CPython 3.11.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\pre-commit.exe install
```

На Linux используйте `python3.11` и `.venv/bin/...`.

Для тестов на PostgreSQL поднимите базу из `deploy/docker-compose.dev.yml`:

```bash
docker compose -f deploy/docker-compose.dev.yml up -d
export CHIMERA_TEST_DATABASE_URL=postgresql+psycopg://chimera:chimera-dev@localhost:5433/chimera_test
```

## Данные

В тестах, документации и fixtures допустимы только синтетические данные
(Faker с локалью `ru_RU`). Папка `_samples/` с образцами пользователя не
коммитится и не копируется в `tests/`. Не коммитьте `.env`, базы, выгрузки,
логи, бэкапы, ключи и пароли.

## Миграции

Изменение моделей сопровождается новой миграцией:

```bash
alembic revision --autogenerate -m "кратко о изменении"
```

Проверьте сгенерированный файл вручную, затем `alembic upgrade head`,
`alembic downgrade -1`, `alembic upgrade head` и `alembic check`. Не меняйте
уже выпущенные миграции.

## Проверки перед pull request

```bash
pytest
ruff check .
ruff format --check .
mypy
alembic check
python sync_app_metadata.py --check
pre-commit run --all-files
```

## Коммиты и pull request

- Conventional Commits на русском: `feat(auth): …`, `fix(audit): …`.
- Один логический набор изменений на ветку; тесты сервисного слоя и хотя бы
  один интеграционный тест роутов для новой функциональности.
- Для нового модуля обязателен тест изоляции организаций.
- Обновите `CHANGELOG.md` и пользовательскую документацию.
- Контакты разработчика меняются в `chimera/core/metadata.py`, затем
  `python sync_app_metadata.py`; сгенерированный блок README вручную не правится.
