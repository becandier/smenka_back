#!/usr/bin/env bash
# Бэкап БД: pg_dump в custom-формате + ротация 14 дней.
# Настраивается cron'ом при первом деплое (см. docs/DEPLOY_PLAYBOOK.md, §6).
#   crontab: 30 3 * * * /opt/smenka/scripts/backup-db.sh >> /opt/smenka/backups/backup.log 2>&1
set -euo pipefail

cd "$(dirname "$0")/.."                # корень репозитория (на сервере: /opt/smenka)
# Не source .env целиком: значения вида "5/minute;30/hour" (rate-limit) содержат
# ';' и парсятся bash'ом как отдельные команды. Тянем только нужные переменные.
POSTGRES_USER=$(grep -E '^POSTGRES_USER=' .env | cut -d= -f2-)
POSTGRES_DB=$(grep -E '^POSTGRES_DB=' .env | cut -d= -f2-)

mkdir -p backups
TS=$(date +%Y%m%d-%H%M)
OUT="backups/db-${TS}.dump"

docker compose -f docker-compose.prod.yml --env-file .env exec -T db \
  pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > "$OUT"

find backups -name 'db-*.dump' -mtime +14 -delete    # ротация 14 дней
echo "[$(date -Is)] OK $OUT ($(du -h "$OUT" | cut -f1))"

# Восстановление:
#   docker compose -f docker-compose.prod.yml --env-file .env exec -T db \
#     pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner \
#     < backups/db-YYYYMMDD-HHMM.dump
# TODO: отгружать дампы вовне сервера (S3 / rclone).
