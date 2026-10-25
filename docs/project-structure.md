# Структура проекта

Весь код приложения — в пакете `chimera/`. В корне репозитория нет модулей
приложения, чтобы при объединении с Manticore в общий лаунчер имена не
конфликтовали.

```text
chimera/
  __init__.py           версия пакета
  __main__.py           CLI: python -m chimera migrate|status|bootstrap|run
  wsgi.py               точка входа gunicorn (chimera.wsgi:app)
  core/                 будущий общий с Manticore код
    config.py           типизированные настройки из окружения и .env
    db.py               движок, сессии, базовый класс моделей, UTC-время, PRAGMA SQLite
    tenancy.py          изоляция организаций: ORM-фильтр и проверка при сохранении
    context.py          ServiceContext: пользователь, организация, роль; ошибки сервисов
    permissions.py      роли и матрица разрешений
    audit.py            таблица audit_log, снимки и diff было/стало
    crypto.py           Fernet-шифрование полей, EncryptedString, слепой индекс
    security.py         пароли, CSRF, заголовки безопасности и CSP
    terms.py            терминология по типу организации
    features.py         фича-флаги «В разработке» и бейджи
    migrations.py       Alembic из кода, проверка версии схемы
    metadata.py         название, версия, контакты разработчика
    validation.py       нормализация и проверка полей форм
  models/
    organization.py     Organization, Division
    user.py             User, Membership, AccessRequest, LoginAttempt
  services/             бизнес-логика без Flask
    auth.py             вход, пароли, регистрация, начальный администратор
    organizations.py    организации и подразделения
    users.py            пользователи, роли, заявки
    impersonation.py    режим «работать как»
    audit_log.py        поиск по журналу аудита
    backup.py           резервные копии SQLite / pg_dump
    dashboard.py        карточки главной страницы
  web/                  Flask: фабрика и Blueprints
    __init__.py         create_app, CSRF, заголовки, обработчики ошибок
    context.py          контекст запроса, декораторы прав
    auth.py, main.py    вход/профиль, главная/переключатель организации
    admin/              администрирование
  templates/            Jinja-шаблоны (base.html, _ui_macros.html, auth/, admin/)
  static/
    css/                tokens.css, legacy.css, modern.css (из Manticore), chimera.css
    js/                 theme.js, chimera-ui.js, user-menu.js
    images/             логотип
migrations/             Alembic (env.py, versions/)
deploy/                 Dockerfile, docker-compose, nginx, systemd, шрифты
tests/                  pytest; фикстуры с Faker ru_RU
scripts/                служебные проверки (check_forbidden_files.py)
docs/                   документация
```

## Слои

1. **Роут** (`web/`) читает форму, вызывает сервис, фиксирует транзакцию и
   рендерит шаблон. Логики в роутах нет; роут длиннее ~40 строк — повод
   вынести код в сервис.
2. **Сервис** (`services/`) получает `ServiceContext`, проверяет права
   (`ctx.require(...)`), работает с моделями и пишет аудит. Сервисы не
   импортируют Flask и тестируются без HTTP.
3. **Модели** (`models/`) — типизированные `Mapped[...]`; предметные модели
   наследуют `OrgScoped`.

## Зависимости между пакетами

`core` не импортирует `models`, `services` и `web`. `services` не импортирует
`web`. Шаблоны получают терминологию через переменную `t` и не содержат
слов вида «факультет»/«отделение» напрямую.
