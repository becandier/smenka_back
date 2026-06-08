#!/usr/bin/env bash
# Деплой прод-стека на сервере. Запускается вручную (ssh deploy@host '...')
# или из CI по SSH. Идемпотентен.
set -euo pipefail

cd "$(dirname "$0")/.."                # корень репозитория (на сервере: /opt/smenka)
COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env"

echo "▶ git pull (обновить compose / Caddyfile / скрипты)"
git pull --ff-only || echo "  ⚠ git pull пропущен (не git-checkout?)"

# опциональный логин в ghcr, если пакеты приватные (GHCR_USER/GHCR_TOKEN в окружении)
if [[ -n "${GHCR_TOKEN:-}" && -n "${GHCR_USER:-}" ]]; then
  echo "▶ docker login ghcr.io"
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin
fi

echo "▶ pull свежих образов"
$COMPOSE pull

echo "▶ up -d (migrate прогонит alembic upgrade head до старта api/worker)"
$COMPOSE up -d --remove-orphans

echo "▶ статус"
$COMPOSE ps
