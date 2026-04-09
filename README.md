# Smenka Backend

REST API для мобильного приложения учёта рабочего времени.

## Стек

- Python 3.12, FastAPI, async SQLAlchemy 2.0, asyncpg
- PostgreSQL 16, Redis 7, Celery
- Alembic (async миграции), Pydantic v2
- Docker Compose

## Запуск

```bash
cp .env.example .env
make setup        # поднимает контейнеры + миграции
```

API доступен на `http://localhost:8000/docs`

## Команды

```bash
make up           # запустить сервисы
make down         # остановить
make logs-api     # логи API
make test         # тесты
make lint         # линтер (ruff)
make migrate      # применить миграции
make migration msg="название"  # создать миграцию
```

## Связанные репозитории

- [Мобильное приложение](https://github.com/becandier/smenka_mobile)
