#!/usr/bin/env bash
# Общая логика авто-синка dev-окружения. Аргументы: <from_ref> <to_ref>.
# Причина: api-контейнер применяет миграции и ставит зависимости только ПРИ СТАРТЕ.
# uvicorn --reload перечитывает код, но не пересоздаёт контейнер — новые миграции/пакеты
# после pull/checkout сами не подхватываются → 500. Хук делает минимально необходимое.
set -euo pipefail

from_ref="${1:-}"
to_ref="${2:-HEAD}"

cd "$(git rev-parse --show-toplevel)"

command -v docker >/dev/null 2>&1 || exit 0
[ -n "$from_ref" ] || exit 0

changed="$(git diff --name-only "$from_ref" "$to_ref" 2>/dev/null || true)"
[ -n "$changed" ] || exit 0

deps_changed="$(echo "$changed" | grep -E 'pyproject\.toml|uv\.lock' || true)"
mig_changed="$(echo "$changed" | grep -E 'src/migrations/versions/' || true)"

# api не запущен — синкать нечего (поднимется через make up/setup).
if ! docker compose ps --status running api 2>/dev/null | grep -q api; then
  [ -n "$deps_changed$mig_changed" ] && echo "ℹ️  [smenka] api не запущен — после make up зависимости/миграции применятся сами"
  exit 0
fi

if [ -n "$deps_changed" ]; then
  echo "🔧 [smenka] изменились зависимости → make sync (пересборка образа + пересоздание + миграции)"
  make sync
elif [ -n "$mig_changed" ]; then
  echo "🔧 [smenka] новые миграции → пересоздаю api (авто-миграция на старте) + make migrate"
  docker compose up -d api
  make migrate
fi
