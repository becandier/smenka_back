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
make test         # тесты (pytest, в контейнере api)
make lint         # линтер (ruff)
make typecheck    # типы (mypy)
make check        # lint + format-check + typecheck разом
make migrate      # применить миграции
make migration msg="название"  # создать миграцию
```

## Проверки качества кода (lint / typecheck / test)

`make lint`, `make typecheck`, `make test` выполняются **внутри контейнера `api`**
(`docker compose exec api ...`), а не в локальном venv на хосте:

- Сервис `api` в `docker-compose.yml` собирается из стадии `dev` (`Dockerfile`,
  `target: dev`) — она расширяет прод-стадию `app`, доустанавливая dev-группу
  зависимостей (`ruff`, `mypy`, `pytest`, `fakeredis`, `pytest-cov` — см.
  `[project.optional-dependencies].dev` в `pyproject.toml`). Прод-образ
  (`docker build --target app`, его же собирают CI/`release.yml` и
  `docker-compose.prod.yml`) стадию `dev` не затрагивает и dev-зависимостей не
  содержит.
- `tests/` и `scripts/` в образ не копируются (в него копируется только `src/`) —
  они примонтированы в контейнер `api` как bind-mounts, поэтому правки в тестах
  видны без пересборки образа.
- Тестовая БД (`smenka_test`) создаётся автоматически и идемпотентно целью
  `make test-db` (зависимость целей `test`/`test-cov`) — Postgres при первой
  инициализации тома создаёт только основную БД (`smenka`), suffix `_test`
  добавляет `tests/conftest.py` на лету.

Порядок: `cp .env.example .env` → `make setup` (или `make up`) → `make lint` /
`make typecheck` / `make test` — всё из коробки, ничего доустанавливать в хостовый
venv не нужно.

### Порт Postgres: 5432 везде, кроме публикации на хост

Внутри docker-сети `db` всегда слушает `5432` (`POSTGRES_HOST=db` в `environment:`
сервиса `api`, порт из `.env`/`.env.example` — `POSTGRES_PORT=5432` — не
переопределяется и совпадает с внутренним портом). `make lint/typecheck/test`
идут через `docker compose exec`, то есть всегда внутри сети — от того, какой
порт Postgres опубликован наружу на хосте, они не зависят.

Наружу (`ports:` в `docker-compose.yml`) Postgres по умолчанию тоже публикуется
на `5432:5432` — совпадает с `.env.example`, поэтому host-инструменты (psql,
локальный venv-pytest) из коробки тоже подключаются на `5432`.

Если `5432` на хосте занят другим проектом — создайте **локальный**
`docker-compose.override.yml` (в `.gitignore`, не версионируется) и сдвиньте
публикуемый порт, например:

```yaml
services:
  db:
    ports: !override
      - "5433:5432"
```

В этом случае для host-инструментов (не `make`-целей) синхронно поменяйте
`POSTGRES_PORT` в своём `.env` на `5433` — иначе venv-pytest/psql будут стучаться
не туда. `make lint/typecheck/test` эта настройка не касается — они всё равно
идут через `db:5432` внутри контейнера.

## Связанные репозитории

- [Мобильное приложение](https://github.com/becandier/smenka_mobile)
