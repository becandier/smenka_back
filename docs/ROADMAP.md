# Roadmap — Smenka Backend

Этот файл — источник правды о том, что сделано и что предстоит. Каждый агент обновляет статусы после завершения работы.

Статусы: `[ ]` не начато, `[~]` в работе, `[x]` готово

---

## Фаза 0 — Скелет проекта `[x]`
- [x] Структура директорий
- [x] pyproject.toml, Dockerfile, docker-compose.yml
- [x] FastAPI app + health endpoint
- [x] Async SQLAlchemy + Alembic (async env.py)
- [x] Security-утилиты (JWT, bcrypt)
- [x] Базовый тестовый conftest

---

## Фаза 1 — Аутентификация `[ ]`
- [ ] Модель `User` (id, email, phone, password_hash, name, created_at, is_active)
- [ ] Модель `RefreshToken` (id, user_id, token, expires_at, revoked)
- [ ] Регистрация (email + пароль)
- [ ] Логин → access_token + refresh_token
- [ ] Refresh-эндпоинт
- [ ] GET /me — текущий пользователь
- [ ] Зависимость `get_current_user` в deps.py
- [ ] SQLAdmin: подключение, отображение User
- [ ] Alembic-миграция
- [ ] Тесты: регистрация, логин, refresh, /me, невалидный токен

---

## Фаза 2 — Персональный режим (смены) `[ ]`
- [ ] Модель `Shift` (id, user_id, started_at, finished_at, status: active/paused/finished)
- [ ] Модель `Pause` (id, shift_id, started_at, finished_at)
- [ ] POST /shifts/start — начать смену
- [ ] POST /shifts/{id}/pause — поставить на паузу
- [ ] POST /shifts/{id}/resume — возобновить
- [ ] POST /shifts/{id}/finish — завершить
- [ ] GET /shifts — история смен (пагинация, фильтр по дате)
- [ ] GET /shifts/stats — статистика (день / неделя / месяц)
- [ ] Бизнес-правила: нельзя начать вторую активную смену, нельзя паузить завершённую и т.д.
- [ ] Автозавершение по таймауту (16ч по умолчанию, пока синхронно при запросе)
- [ ] Миграция
- [ ] Тесты: весь lifecycle смены, edge cases

---

## Фаза 3 — Организации `[ ]`
- [ ] Модель `Organization` (id, name, owner_id, created_at, invite_code)
- [ ] Модель `OrganizationMember` (id, org_id, user_id, role: admin/employee, joined_at)
- [ ] Модель `WorkLocation` (id, org_id, name, latitude, longitude, radius_meters)
- [ ] CRUD организации (создание, обновление, удаление)
- [ ] Генерация и ротация инвайт-кода
- [ ] Присоединение по инвайт-коду
- [ ] Управление сотрудниками (список, удаление из организации)
- [ ] Миграция
- [ ] Тесты: создание орг, инвайт, роли, CRUD точек

---

## Фаза 4 — Правила организации `[ ]`
- [ ] Модель `OrganizationSettings` (org_id, geo_check_enabled, auto_finish_hours, max_pause_minutes, max_pauses_per_shift)
- [ ] Геопроверка при начале смены (Haversine, сравнение с WorkLocation)
- [ ] Применение правил организации к смене (если пользователь в организации)
- [ ] Автозавершение пауз при превышении лимита
- [ ] GET /organizations/{id}/shifts — смены сотрудников для админа
- [ ] GET /organizations/{id}/stats — статистика по организации
- [ ] Миграция
- [ ] Тесты: geo-расчёты, применение лимитов, админские эндпоинты

---

## Фаза 5 — Фоновые задачи `[ ]`
- [ ] Background-воркер для автозавершения зависших смен
- [ ] Периодическая проверка (cron-like)
- [ ] Логирование

---

## Фаза 6 — Продакшен `[ ]`
- [ ] Rate-limiting
- [ ] CORS-настройки
- [ ] CI/CD (lint → test → build)
- [ ] Финальная проверка OpenAPI-документации
