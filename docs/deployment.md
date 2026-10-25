# Развёртывание и эксплуатация

## Перед началом

- Chimera хранит персональные данные студентов. Разместите сервер во
  внутренней сети или за VPN, доступ из интернета — только через HTTPS.
- Одна установка обслуживает несколько организаций; изоляция обеспечивается
  приложением. Если организации требуют физического разделения, ставьте
  отдельные установки.
- Ключ `DATA_ENCRYPTION_KEY` храните отдельно от резервных копий (например, в
  сейфе паролей администратора).

## Docker Compose

`deploy/docker-compose.yml` поднимает три контейнера:

| Сервис | Образ | Назначение |
|---|---|---|
| `db` | `postgres:16` | база данных (том `pgdata`) |
| `app` | сборка `deploy/Dockerfile` | gunicorn + Chimera, LibreOffice и шрифты (том `appdata`) |
| `nginx` | `nginx:1.27-alpine` | обратный прокси на порт `HTTP_PORT` (по умолчанию 80) |

Образ приложения основан на `python:3.11-slim-bookworm` и включает
`postgresql-client-16` (из репозитория PGDG, чтобы версия `pg_dump` совпадала
с сервером), LibreOffice Writer без GUI для будущей конвертации DOCX → PDF и
шрифты DejaVu, Liberation, Carlito/Caladea, PT Serif/Sans. PT Astra Serif можно
добавить в `deploy/fonts/` (см. README в этой папке).

Обновление:

```bash
git pull
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build
```

Миграции выполняются при старте контейнера. Перед обновлением сделайте
резервную копию (см. ниже).

## Установка на хост с systemd

1. Пользователь и каталоги:

   ```bash
   sudo useradd --system --home /srv/chimera chimera
   sudo install -d -o chimera -g chimera -m 0700 /var/lib/chimera /var/lib/chimera/backups
   ```

2. Код в `/srv/chimera`, окружение по [инструкции](installation.md#вариант-2-ручная-установка),
   `.env` с `CHIMERA_DATA_DIR=/var/lib/chimera` и правами `0600`.
3. `deploy/systemd/chimera.service` → `/etc/systemd/system/`, затем
   `sudo systemctl enable --now chimera`.
4. nginx: пример с HTTPS — `deploy/nginx-https.example.conf`. При работе только
   по HTTPS включите `ENABLE_HSTS=true`.

## Резервные копии

- **Из интерфейса** (системный администратор → «Резервные копии»): для SQLite
  создаётся копия файла с проверкой целостности, для PostgreSQL вызывается
  `pg_dump --format=custom`. Если `pg_dump` не найден, страница покажет, что
  установить. Копии старше `BACKUP_RETENTION_DAYS` удаляются, три последние
  сохраняются всегда.
- **По расписанию** (systemd): `deploy/systemd/chimera-backup.service` и
  `chimera-backup.timer` — ежедневный `pg_dump` в 02:30 UTC.
- **Docker**: `docker compose -f deploy/docker-compose.yml exec db pg_dump -U chimera -Fc chimera > chimera.dump`.

Восстановление PostgreSQL:

```bash
pg_restore --clean --if-exists --no-owner -d chimera chimera-YYYYMMDD-HHMMSS.dump
python -m chimera migrate
```

Восстановление SQLite: остановите приложение и замените файл базы копией.

Регулярно проверяйте восстановление на отдельном сервере — копия, которую ни
разу не восстанавливали, не считается копией.

## Версия схемы

`python -m chimera status` показывает ревизию схемы. Приложение не запустится,
если база создана более новой версией Chimera (защита от отката кода без отката
данных), или если схема не обновлена (в режиме `server` миграции выполняются
явно командой `migrate`).

## Проверка перед вводом в эксплуатацию

- [ ] HTTPS, `SESSION_COOKIE_SECURE=true`
- [ ] Пароль начального администратора сменён, `ADMIN_DEFAULT_PASSWORD` удалён из `.env`
- [ ] Копия `DATA_ENCRYPTION_KEY` хранится отдельно
- [ ] Резервная копия создана и восстановлена на тестовом сервере
- [ ] Права на `.env` и каталог данных — только пользователь `chimera`
