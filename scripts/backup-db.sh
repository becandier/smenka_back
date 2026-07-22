#!/usr/bin/env bash
# Бэкап БД: pg_dump в custom-формате + локальная ротация + отгрузка в S3 с ротацией.
# Ставится в cron (см. docs/DEPLOY_PLAYBOOK.md, §7):
#   30 3 * * * /opt/smenka/scripts/backup-db.sh >> /opt/smenka/backups/backup.log 2>&1
#
# Локальная копия — быстрый откат. Копия в S3 — страховка на случай смерти VPS
# целиком: бэкап, лежащий на том же диске, что и база, бэкапом не является.
set -euo pipefail

cd "$(dirname "$0")/.."                # корень бандла (на сервере: /opt/smenka)

# Не source .env целиком: значения вида "5/minute;30/hour" (rate-limit) содержат
# ';' и парсятся bash'ом как отдельные команды. Тянем только нужные переменные.
env_get() { grep -E "^$1=" .env 2>/dev/null | cut -d= -f2- || true; }

POSTGRES_USER=$(env_get POSTGRES_USER)
POSTGRES_DB=$(env_get POSTGRES_DB)

LOCAL_KEEP_DAYS=14                     # сколько держать дампы на диске сервера
S3_KEEP_DAYS=30                        # сколько держать дампы в S3
S3_PREFIX=db-backups                   # префикс внутри бакета
AWS_BIN=/usr/local/bin/aws             # cron идёт с урезанным PATH — только абсолютный путь

log() { echo "[$(date -Is)] $*"; }

mkdir -p backups
TS=$(date +%Y%m%d-%H%M)
OUT="backups/db-${TS}.dump"

docker compose -f docker-compose.prod.yml --env-file .env exec -T db \
  pg_dump -U "$POSTGRES_USER" -Fc "$POSTGRES_DB" > "$OUT"

if [ ! -s "$OUT" ]; then
  log "FAIL пустой дамп $OUT — прерываюсь, старые копии не трогаю"
  rm -f "$OUT"
  exit 1
fi

log "OK локально $OUT ($(du -h "$OUT" | cut -f1))"

# ── Отгрузка в S3 ───────────────────────────────────────────────────────────
# Креды берём из тех же S3_*, что использует приложение. Если S3 не настроен
# или клиент не установлен — бэкап не валим: локальная копия уже снята.
S3_BUCKET=$(env_get S3_BUCKET)
S3_ENDPOINT_URL=$(env_get S3_ENDPOINT_URL)
AWS_ACCESS_KEY_ID=$(env_get S3_ACCESS_KEY)
AWS_SECRET_ACCESS_KEY=$(env_get S3_SECRET_KEY)
AWS_DEFAULT_REGION=$(env_get S3_REGION)
export AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_DEFAULT_REGION

s3_ready=1
if [ ! -x "$AWS_BIN" ]; then
  log "WARN $AWS_BIN не найден — отгрузка в S3 пропущена"
  s3_ready=0
elif [ -z "$S3_BUCKET" ] || [ -z "$AWS_ACCESS_KEY_ID" ]; then
  log "WARN S3 не сконфигурирован в .env — отгрузка пропущена"
  s3_ready=0
fi

if [ "$s3_ready" = "1" ]; then
  S3_URI="s3://${S3_BUCKET}/${S3_PREFIX}/$(basename "$OUT")"
  if "$AWS_BIN" --endpoint-url="$S3_ENDPOINT_URL" s3 cp "$OUT" "$S3_URI" --only-show-errors; then
    log "OK в S3 $S3_URI"
  else
    log "FAIL отгрузка в S3 не удалась (локальная копия на месте)"
  fi

  # Ротация в S3: Timeweb не даёт настроить lifecycle через API (CreateBucket и
  # PutBucketLifecycle недоступны), поэтому чистим сами — по дате из имени файла
  # db-YYYYMMDD-HHMM.dump.
  cutoff=$(date -d "-${S3_KEEP_DAYS} days" +%Y%m%d 2>/dev/null || true)
  if [ -n "$cutoff" ]; then
    "$AWS_BIN" --endpoint-url="$S3_ENDPOINT_URL" s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}/" 2>/dev/null \
      | awk '{print $4}' \
      | grep -E '^db-[0-9]{8}-[0-9]{4}\.dump$' \
      | while read -r name; do
          file_date=${name:3:8}
          if [ "$file_date" -lt "$cutoff" ]; then
            if "$AWS_BIN" --endpoint-url="$S3_ENDPOINT_URL" s3 rm \
                 "s3://${S3_BUCKET}/${S3_PREFIX}/${name}" --only-show-errors; then
              log "ротация S3: удалён $name"
            fi
          fi
        done || true
  fi
fi

# ── Локальная ротация ───────────────────────────────────────────────────────
# Делаем ПОСЛЕ отгрузки: если S3 недоступен, свежая копия всё равно остаётся на диске.
find backups -name 'db-*.dump' -mtime +${LOCAL_KEEP_DAYS} -delete
log "готово (локально ${LOCAL_KEEP_DAYS}д, S3 ${S3_KEEP_DAYS}д)"

# Восстановление:
#   docker compose -f docker-compose.prod.yml --env-file .env exec -T db \
#     pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists --no-owner \
#     < backups/db-YYYYMMDD-HHMM.dump
# Скачать копию из S3:
#   aws --endpoint-url=<S3_ENDPOINT_URL> s3 cp \
#     s3://<S3_BUCKET>/db-backups/db-YYYYMMDD-HHMM.dump .
