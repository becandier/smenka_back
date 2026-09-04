.PHONY: help up down restart build rebuild setup sync hooks logs logs-api logs-worker logs-db ps \
       shell dbshell redis-cli migrate rollback migration migration-check db-current \
       test-db test test-fast test-cov lint lint-fix typecheck check clean

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
# conftest.py коннектится в "<POSTGRES_DB>_test" (см. tests/conftest.py), а образ
# postgres на первом init создаёт только основную POSTGRES_DB — "smenka_test"
# сама не появляется. Создаём её идемпотентно перед каждым прогоном тестов.
test-db:  ## Создать тестовую БД (идемпотентно, требуется для make test)
	@$(COMPOSE) exec db psql -U $(DB_USER) -d $(DB_NAME) -tAc \
		"SELECT 1 FROM pg_database WHERE datname = '$(DB_NAME)_test'" | grep -q 1 || \
		$(COMPOSE) exec db psql -U $(DB_USER) -d $(DB_NAME) -c "CREATE DATABASE $(DB_NAME)_test"

test: test-db  ## Запустить тесты (последовательно, один процесс)
	$(API) python -m pytest tests/ -v

# Параллельный прогон (pytest-xdist). Своя БД на процесс создаётся сама
# (tests/conftest.py::_ensure_test_database_exists, суффикс — PYTEST_XDIST_WORKER,
# который выставляет xdist), поэтому test-db как пререквизит не нужен. Число
# процессов — одна переменная: `make test-fast TEST_WORKERS=4`, по умолчанию auto
# (по числу ядер).
TEST_WORKERS ?= auto
test-fast:  ## Запустить тесты параллельно (pytest-xdist, своя БД на процесс)
	$(API) python -m pytest tests/ -v -n $(TEST_WORKERS)

test-cov: test-db  ## Тесты с покрытием
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
