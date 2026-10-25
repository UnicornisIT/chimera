#!/bin/sh
# Перед запуском: обновить схему базы и создать начального администратора
# (если задан ADMIN_DEFAULT_PASSWORD и пользователей ещё нет).
set -eu
python -m chimera migrate
python -m chimera bootstrap --no-input
exec "$@"
