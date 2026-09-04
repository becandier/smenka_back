# Smenka Backend

## Что это

REST API для мобильного приложения учёта рабочего времени. Два режима: персональный (трекер для себя) и организационный (контроль сотрудников с геопроверкой).

## Стек

- Python 3.12, FastAPI, async SQLAlchemy 2.0, asyncpg
- Alembic (async миграции), PostgreSQL 16
- Pydantic v2
- Docker Compose для локальной разработки
- pytest + httpx для тестов, pytest-xdist для параллельного прогона (`make test-fast`)

## Структура проекта

```
src/
├── app/
│   ├── api/
│   │   ├── deps.py          # DI: сессия, текущий юзер
│   │   └── v1/              # Версионированные эндпоинты
│   ├── core/
│   │   ├── config.py        # Pydantic Settings (.env)
│   │   ├── database.py      # Async engine, session, Base
│   │   └── security.py      # JWT (HS256) + bcrypt
│   ├── models/              # SQLAlchemy ORM-модели
│   ├── schemas/             # Pydantic-схемы (request/response)
│   ├── services/            # Бизнес-логика (по одному файлу на домен)
│   └── utils/               # Утилиты (geo, время)
│   └── main.py
├── alembic.ini
└── migrations/
tests/
```

## Конвенции кода

- Все timestamps в UTC (`datetime.now(UTC)`)
- Async everywhere: engine, session, эндпоинты
- Модели наследуют `Base` из `src.app.core.database`
- Сервисный слой принимает `AsyncSession` первым аргументом
- Схемы: `*Create`, `*Update`, `*Response` (суффиксы)
- Эндпоинты возвращают Pydantic-схемы, не ORM-объекты
- Каждый роутер — отдельный файл в `src/app/api/v1/`
- Зависимости (current_user и т.д.) — в `src/app/api/deps.py`
- Тесты рядом по структуре: `tests/test_auth.py`, `tests/test_shifts.py`

## Правила для агентов

1. **Перед началом работы** прочитай:
   - Этот файл
   - `docs/ROADMAP.md` — фазы и текущий статус
   - `docs/ARCHITECTURE.md` — текущее состояние архитектуры
   - `docs/decisions/` — все ADR (архитектурные решения)

2. **После завершения работы** обнови:
   - `docs/ARCHITECTURE.md` — добавь новые модели, эндпоинты, сервисы
   - `docs/ROADMAP.md` — отметь фазу/подзадачу как выполненную
   - Если принял решение, которое отклоняется от плана или влияет на другие фазы — создай ADR в `docs/decisions/`

3. **ADR формат** (`docs/decisions/NNN-название.md`):
   ```
   # NNN — Краткое название решения
   Статус: принято
   Фаза: N
   Влияет на: фаза X, Y (что именно)
   
   ## Контекст
   Почему возник вопрос.
   
   ## Решение
   Что решили и почему.
   
   ## Последствия
   Что нужно учесть в будущих фазах.
   ```

4. **Не ломай существующие контракты** — если меняешь схему ответа или сигнатуру сервиса, проверь что нет зависимого кода.

5. Не добавляй `Co-Authored-By` с упоминанием ИИ в коммиты.

6. **Ветвление**: перед началом каждой фазы или большой задачи создавай отдельную ветку от `main` (например `phase-5-background-tasks`). Работай в ней, по завершении — мержи в `main`. Если при старте работы ты уже находишься на рабочей ветке (не `main`) — **спроси у пользователя** что делать: возможно, там идёт незавершённая работа или нужно продолжить.

## Трекинг задач (кросс-сервисные фичи)

Кроме `docs/ROADMAP.md` (фазовый журнал этого репо), есть единая система ТЗ в корне-оркестраторе: `../docs/tasks/<feature>/`. Если работаешь над фичей оттуда:

1. **При старте** — переведи дорожку `backend` в `../docs/tasks/<feature>/STATUS.md` из `todo` в `in_progress`, обнови дату и добавь запись в changelog (формат: `YYYY-MM-DD HH:MM | backend | <old> → <new> | <author> | <commit> | <заметка>`).
2. **ТЗ берёшь** из `backend.md` этой фичи; контракт ошибок — `../docs/ERROR_FORMAT.md`.
3. **При завершении** — `in_progress → review/done`, вставь SHA коммита, опиши что сделано.

## Ревью-гейт (перед `review`/`done`)

Прежде чем ставить статус `review`/`done`:
- `make lint` (ruff) и `make typecheck` (mypy) — зелёные.
- `make test` (pytest, последовательно) или `make test-fast` (pytest-xdist, параллельно — своя БД на процесс, см. `docs/ARCHITECTURE.md` → «Тестовое окружение») — зелёный, покрыты edge cases.
- Прогнать `/code-review` (для крупной фичи — уровень `high`) и `/simplify`.
- Миграция (если есть) прогнана локально и лежит в `migrations/versions/`.
- Контракт `{data,error}` не сломан для старых мобильных билдов.

## DevOps / деплой (управляется в корне)

- Деплой-бандл (`docker-compose.prod.yml`, `infra/caddy/Caddyfile`, `.env.prod.example`, `scripts/deploy.sh`, `scripts/backup-db.sh`) и CI (`.github/workflows/{ci,release}.yml`) живут здесь, но их ведёт DevOps-роль из корня-оркестратора (`../CLAUDE.md`). Подробности — `../docs/INFRASTRUCTURE.md`, `../docs/DEPLOY_PLAYBOOK.md`.
- Новые env-переменные → добавляй в `.env.prod.example`.
- Прод живёт на VPS `smenka.space`; автодеплой на push в `main` включён (`DEPLOY_ENABLED=true`) с 2026-07-02.
