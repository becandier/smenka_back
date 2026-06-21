.PHONY: help up down restart build rebuild setup sync hooks logs logs-api logs-worker logs-db ps \
       shell dbshell redis-cli migrate rollback migration migration-check db-current \
       test test-cov lint lint-fix typecheck check clean

COMPOSE = docker compose
API     = $(COMPOSE) exec api
# alembic.ini лежит в /app/src — запускаем alembic именно оттуда, иначе "No script_location".
ALEMBIC = $(COMPOSE) exec -w /app/src api alembic
DB_USER = smenka
DB_NAME = smenka

# ─────────────────────────────────────────────────────────────
help:  ## Список команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ─── Сервисы ─────────────────────────────────────────────────
up:  ## Запустить все сервисы
	$(COMPOSE) up -d

down:  ## Остановить все сервисы
	$(COMPOSE) down

restart:  ## Перезапустить все сервисы
	$(COMPOSE) restart

build:  ## Пересобрать образы
	$(COMPOSE) build

rebuild:  ## Пересобрать без кэша
	$(COMPOSE) build --no-cache

setup: up migrate  ## Первый запуск: поднять + миграции

sync:  ## После git pull бэка (новые зависимости/миграции): пересобрать образы, пересоздать контейнеры, накатить миграции
	$(COMPOSE) build
	$(COMPOSE) up -d
	$(MAKE) migrate

hooks:  ## Установить git-хуки (авто-sync dev-окружения после pull/checkout) — выполнить один раз
	@chmod +x scripts/git-hooks/* 2>/dev/null || true
	git config core.hooksPath scripts/git-hooks
	@echo "✅ git-хуки включены: core.hooksPath=scripts/git-hooks (post-merge/post-checkout → авто make sync)"

# ─── Логи ────────────────────────────────────────────────────
logs:  ## Все логи (follow)
	$(COMPOSE) logs -f

logs-api:  ## Логи API
	$(COMPOSE) logs -f api

logs-worker:  ## Логи Celery worker
	$(COMPOSE) logs -f celery-worker

logs-db:  ## Логи PostgreSQL
	$(COMPOSE) logs -f db

# ─── Статус ──────────────────────────────────────────────────
ps:  ## Статус контейнеров
	$(COMPOSE) ps

# ─── Шеллы ───────────────────────────────────────────────────
shell:  ## bash внутри API контейнера
	$(API) bash

dbshell:  ## psql внутри postgres
	$(COMPOSE) exec db psql -U $(DB_USER) $(DB_NAME)

redis-cli:  ## Redis CLI
	$(COMPOSE) exec redis redis-cli

# ─── Миграции ────────────────────────────────────────────────
migrate:  ## Применить все миграции
	$(ALEMBIC) upgrade head

rollback:  ## Откатить последнюю миграцию
	$(ALEMBIC) downgrade -1

migration:  ## Создать миграцию: make migration msg="add_users_table"
ifndef msg
	$(error Укажи сообщение: make migration msg="add_users_table")
endif
	$(ALEMBIC) revision --autogenerate -m "$(msg)"

migration-check:  ## Проверить, что нет незафиксированных изменений в моделях
	$(ALEMBIC) check

db-current:  ## Показать текущую ревизию БД
	$(ALEMBIC) current

# ─── Тесты ───────────────────────────────────────────────────
test:  ## Запустить тесты
	$(API) python -m pytest tests/ -v

test-cov:  ## Тесты с покрытием
	$(API) python -m pytest tests/ -v --cov=app --cov-report=term-missing

# ─── Качество кода ───────────────────────────────────────────
lint:  ## Проверить линтером (ruff)
	$(API) python -m ruff check src/ tests/ scripts/

lint-fix:  ## Автоисправление (ruff)
	$(API) python -m ruff check src/ tests/ scripts/ --fix

format:  ## Отформатировать код (ruff format)
	$(API) python -m ruff format src/ tests/ scripts/

format-check:  ## Проверить форматирование без изменений
	$(API) python -m ruff format --check src/ tests/ scripts/

typecheck:  ## Проверка типов (mypy)
	$(API) python -m mypy src/

check: lint format-check typecheck  ## Линтер + формат + типы

# ─── Очистка ─────────────────────────────────────────────────
clean:  ## Остановить сервисы и удалить volumes
	$(COMPOSE) down -v
