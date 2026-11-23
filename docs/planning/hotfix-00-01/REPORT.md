# Hotfix 00-01. Миграции Alembic в sdist и wheel

Ветка `fix/alembic-wheel-packaging`, база `b360eb99abf95f9a45dc6a8989db94ae8f12da7d`.

## Первопричина и воспроизведение

Ревизии находились в корневом каталоге `migrations/` вне Python-пакета.
Setuptools поставлял `chimera` и его шаблоны, а код искал миграции рядом
с исходным проектом. До правок оба архива собраны заново. Ревизии
`0001` в них не было. Wheel установлен во временный каталог, импорт
проверен вне checkout. Команда `migrate` вернула код 2, файл SQLite
не появился.

## Устройство поставки

В sdist и wheel используется один каталог.

```text
chimera/
  migrations/
    __init__.py
    env.py
    script.py.mako
    versions/
      20261025_0001_initial_schema.py
```

Python-файлы включаются в пакет setuptools, шаблон и файлы ревизий
задаются шаблонами package-data. Runtime берёт физический путь
от установленного Python-модуля. Alembic открывает файлы по этому пути,
поэтому каталог извлечённой установки не зависит от cwd. При неполной
поставке выводится ошибка установки. Проектный `alembic.ini` указывает
на тот же каталог, при этом настраивает путь импорта checkout относительно
собственного расположения.

Blob ревизии `0001` до и после переноса идентичен,
`e03f7f0ed9c490ebae59e036f7f1169a323a0fe9`. Схема, SQL-операции и граф
ревизий сохранены. Установленный режим Desktop Local хранит профиль
вне site-packages. Linux использует XDG-каталог, существующий checkout
сохраняет прежние правила путей.

## Изменённые файлы

- `migrations/{env.py,script.py.mako,versions/20261025_0001_initial_schema.py}`
  перенесены в `chimera/migrations/`. Добавлен `__init__.py`.
  В `env.py` уточнён возвращаемый тип callback для mypy.
- `pyproject.toml`, `alembic.ini`, `chimera/core/migrations.py`
  задают упаковку, каталог ресурсов и диагностику неполной установки.
- `chimera/core/config.py`, `tests/test_config.py` уточняют размещение
  данных установленного пакета. `tests/test_platform.py` проверяет отказ
  при отсутствии начальной ревизии.
- `scripts/check_installed_migrations.py` сравнивает состав архивов и
  устанавливает оба wheel в отдельные чистые venv. Изолированный Python
  проверяет путь импорта и отсутствие checkout в `sys.path`. CLI выполняется
  в отдельных процессах. Снимок site-packages проверяется до и после вызовов.
- `deploy/Dockerfile` запускает entrypoint и Gunicorn из домашнего каталога
  пользователя контейнера. `.github/workflows/ci.yml` добавляет проверку
  установленных wheel на Windows и Ubuntu и обращение к установленному
  пакету из `/tmp` внутри Docker.
- `README.md`, `docs/installation.md`, `docs/DEVELOPMENT.md`,
  `docs/project-structure.md`, `.github/pull_request_template.md`
  отражают расположение единственного дерева миграций.
- В `docs/planning/MANTICORE_FEATURE_PARITY.md` отмечен результат hotfix
  после проверки установленных пакетов.

## Результаты

| Проверка | Результат |
|---|---|
| Исходные sdist и wheel | **FAIL**, в обоих отсутствовала `0001`. |
| Исходный wheel вне checkout | **FAIL**, `migrate` вернул 2. |
| Состав исправленных артефактов | **PASS**. sdist, прямой wheel и wheel из sdist содержат четыре одинаковых ресурса Alembic, включая `0001`. |
| Прямой wheel на Windows | **PASS**. Установка в чистый venv и запуск из каталога вне checkout. Путь окружения и cwd содержали кириллицу и пробелы. |
| SQLite из прямого wheel | **PASS**. Применена `0001`, найдены ожидаемые таблицы, выполнены `status`, bootstrap и штатный запуск на loopback с HTTP 200. Повторная миграция сохранила схему, строки и файлы ключей. Неизвестная ревизия отклонена. |
| PostgreSQL 16.16 из прямого wheel | **PASS**. Подтверждены каталог кластера, база и роль. На чистой схеме выполнены `migrate`, bootstrap, повторный `migrate` и `status`. Штатный WSGI вернул HTTP 200 с Secure cookie. |
| sdist → wheel → установка на Windows | **PASS**. Второй wheel собран из sdist и установлен в отдельный чистый venv. Подтверждены `0001`, чистая SQLite, повторная миграция без изменений, bootstrap, HTTP 200 и отказ для неизвестной ревизии. |
| Запись в site-packages | **PASS**. Снимок после выполнения команд совпал с исходным у обоих venv. |
| Pytest SQLite на Windows | **PASS**, 288 прошли, 2 пропуска для PostgreSQL. |
| Pytest PostgreSQL 16 на Windows | **PASS**, 290 прошли. |
| Alembic CLI разработки из внешнего cwd | **PASS**. С явным `alembic.ini` выполнены `upgrade head`, `check` и `downgrade base` на одноразовой SQLite. |
| Ruff check, Ruff format --check, mypy | **PASS**. |
| Linux и Docker | **NOT TESTED** локально. WSL без дистрибутива, Docker CLI недоступен. CI-изменения проверяются в GitHub Actions после публикации ветки. |
| PyInstaller и Windows EXE | **NOT TESTED**. Для будущей сборки Alembic понадобятся физические файлы всего `chimera/migrations/`, включая Python-скрипты. |

Установленные артефакты проверяются отдельно от pytest. Изолированный
интерпретатор запускается с `-I -B`, чтобы исключить путь checkout и
записи Python bytecode из измерения. Снимок site-packages включает файлы
и каталоги.

Итоговый статус 00-01 — **FIXED**. Локальная приёмка пройдена, ветка
готова к публикации для проверки GitHub Actions. Новых P1, P2 и P3
по проверенным сценариям не выявлено. Linux, Docker и PyInstaller
остаются отдельными непроверенными сценариями.

## Ручная приёмка

1. В checkout с CPython 3.11 выполните `python -m pip install build`, затем
   `python -m scripts.check_installed_migrations`. Для PostgreSQL добавьте
   `--postgres`, утилиты 16 в `PATH` и освободите порт 55441.
2. Для ручной проверки установите wheel в чистое окружение. Перейдите в
   каталог вне исходников. Задайте `CHIMERA_MODE=desktop-local` и временный
   `CHIMERA_DATA_DIR`, затем выполните `python -m chimera migrate`,
   `python -m chimera status` и `python -m chimera bootstrap --no-input`
   с синтетическими начальными учётными данными. Для Server используйте
   выделенный PostgreSQL 16 с явными URL, каталогом и секретами.
3. Повторите `migrate` на синтетической копии с `alembic_version=0001`.
   Убедитесь, что прежние строки и локальные ключи сохранились.

## Откат

Остановите экземпляр и сохраните существующую БД, профиль и ключи.
Верните предыдущую проверенную сборку или запуск из checkout и укажите
прежние пути к данным. Исходный wheel до hotfix имеет дефект 00-01.
Операции отката БД не требуются, поскольку ревизия `0001` идентична.
