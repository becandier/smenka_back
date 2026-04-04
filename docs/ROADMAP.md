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

## Фаза 1 — Аутентификация `[x]`
- [x] Модель `User` (id, email, phone, password_hash, name, is_verified, created_at)
- [x] Модель `RefreshToken` (id, user_id, token, expires_at, revoked)
- [x] Модель `VerificationCode` (id, user_id, code, expires_at, created_at)
- [x] Регистрация (email + пароль + name)
- [x] Верификация email (4-значный код, 15 мин TTL, cooldown 30 сек)
- [x] Логин → access_token + refresh_token
- [x] Refresh-эндпоинт (ротация токенов)
- [x] Logout (отзыв refresh-токена)
- [x] GET /me — текущий пользователь
- [x] PATCH /me — обновление профиля (name, phone)
- [x] Зависимость `get_current_user` в deps.py
- [ ] SQLAdmin: отложено до стабилизации бека
- [x] Alembic-миграция
- [x] Тесты: регистрация, верификация, логин, refresh, logout, /me, невалидный токен
- [x] Обёртка ответов: `{"data": ..., "error": ...}`

---

## Фаза 2 — Персональный режим (смены) `[x]`
- [x] Модель `Shift` (id, user_id, started_at, finished_at, status: active/paused/finished)
- [x] Модель `Pause` (id, shift_id, started_at, finished_at)
- [x] POST /shifts/start — начать смену
- [x] POST /shifts/{id}/pause — поставить на паузу
- [x] POST /shifts/{id}/resume — возобновить
- [x] POST /shifts/{id}/finish — завершить
- [x] GET /shifts — история смен (пагинация, фильтр по дате и статусу)
- [x] GET /shifts/stats — статистика (день / неделя / месяц)
- [x] Бизнес-правила: нельзя начать вторую активную смену, нельзя паузить завершённую и т.д.
- [x] Автозавершение по таймауту (16ч по умолчанию, пока синхронно при запросе)
- [x] Миграция
- [x] Тесты: весь lifecycle смены, edge cases

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
