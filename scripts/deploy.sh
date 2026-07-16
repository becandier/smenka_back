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

# Образы приложения (ghcr.io) тянем строго: их обновление — суть деплоя.
# Базовые образы (docker.io: caddy/postgres/redis/flower) — best-effort:
# Docker Hub отдаёт 429 на анонимные pull'ы, и это не должно валить деплой —
# при отказе продолжаем на локально закэшированных образах. На свежем сервере
# (без локального кэша) повторить деплой позже или залогиниться в Docker Hub.
echo "▶ pull образов приложения (ghcr)"
$COMPOSE pull migrate api worker admin web

echo "▶ pull базовых образов (docker.io, best-effort)"
$COMPOSE pull --ignore-pull-failures caddy db redis flower \
  || echo "  ⚠ базовые образы не обновлены (rate limit?) — используем локальные"

echo "▶ up -d (migrate прогонит alembic upgrade head до старта api/worker)"
$COMPOSE up -d --remove-orphans

echo "▶ статус"
$COMPOSE ps
