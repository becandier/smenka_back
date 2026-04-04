# Smenka Backend

## Что это

REST API для мобильного приложения учёта рабочего времени. Два режима: персональный (трекер для себя) и организационный (контроль сотрудников с геопроверкой).

## Стек

- Python 3.12, FastAPI, async SQLAlchemy 2.0, asyncpg
- Alembic (async миграции), PostgreSQL 16
- SQLAdmin (админка), Pydantic v2
- Docker Compose для локальной разработки
- pytest + httpx для тестов

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
