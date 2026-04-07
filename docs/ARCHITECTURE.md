# Архитектура — текущее состояние

Последнее обновление: 2026-04-07 (глобальные роли пользователей)

---

## Модели (SQLAlchemy)

| Модель | Таблица | Описание |
|--------|---------|----------|
| `User` | `users` | Пользователь (email, name, phone, password_hash, is_verified, role: super_admin/user) |
| `RefreshToken` | `refresh_tokens` | JWT refresh-токен (token, expires_at, revoked) |
| `VerificationCode` | `verification_codes` | Код верификации email (code, expires_at) |
| `Shift` | `shifts` | Рабочая смена (user_id, organization_id, started_at, finished_at, status) |
| `Pause` | `pauses` | Пауза внутри смены (shift_id, started_at, finished_at) |
| `Organization` | `organizations` | Организация (name, owner_id, invite_code, is_deleted) |
| `OrganizationMember` | `organization_members` | Участник организации (org_id, user_id, role) |
| `WorkLocation` | `work_locations` | Рабочая точка (org_id, name, lat, lng, radius) |
| `OrganizationSettings` | `organization_settings` | Настройки организации (geo, лимиты пауз, auto-finish) |

---

## Эндпоинты

| Метод | Путь | Описание | Авторизация |
|-------|------|----------|-------------|
| GET | `/health` | Проверка жизни | Нет |
| POST | `/api/v1/auth/register` | Регистрация | Нет |
| POST | `/api/v1/auth/verify` | Подтверждение email → auto-login | Нет |
| POST | `/api/v1/auth/resend-code` | Повторная отправка кода | Нет |
| POST | `/api/v1/auth/login` | Логин | Нет |
| POST | `/api/v1/auth/refresh` | Обновление пары токенов | Нет (refresh_token в body) |
| POST | `/api/v1/auth/logout` | Отзыв refresh-токена | Нет (refresh_token в body) |
| GET | `/api/v1/users/me` | Текущий пользователь | Bearer |
| PATCH | `/api/v1/users/me` | Обновление профиля (name, phone) | Bearer |
| GET | `/api/v1/shifts` | История смен (пагинация, фильтры) | Bearer |
| GET | `/api/v1/shifts/stats` | Статистика (день/неделя/месяц) | Bearer |
| POST | `/api/v1/shifts/start` | Начать с��ену | Bearer |
| POST | `/api/v1/shifts/{id}/pause` | Поставить на паузу | Bearer |
| POST | `/api/v1/shifts/{id}/resume` | Возобновить | Bearer |
| POST | `/api/v1/shifts/{id}/finish` | Завершить | Bearer |
| POST | `/api/v1/organizations` | Создать организацию | Bearer (super_admin) |
| GET | `/api/v1/organizations/all` | Все организации системы | Bearer (super_admin) |
| GET | `/api/v1/organizations` | Мои организации | Bearer |
| GET | `/api/v1/organizations/{id}` | Получить организацию | Bearer |
| PATCH | `/api/v1/organizations/{id}` | Обновить организацию | Bearer |
| DELETE | `/api/v1/organizations/{id}` | Удалить организацию (soft) | Bearer |
| POST | `/api/v1/organizations/{id}/rotate-invite` | Ротация инвайт-кода | Bearer |
| POST | `/api/v1/organizations/join/{code}` | Присоединиться по коду | Bearer |
| GET | `/api/v1/organizations/{id}/members` | Список участников | Bearer |
| DELETE | `/api/v1/organizations/{id}/members/{user_id}` | Удалить участника / выйти | Bearer |
| PATCH | `/api/v1/organizations/{id}/members/{user_id}/role` | Изменить роль участника | Bearer (owner/super_admin) |
| POST | `/api/v1/organizations/{id}/locations` | Создать точку | Bearer |
| GET | `/api/v1/organizations/{id}/locations` | Список точек | Bearer |
| PATCH | `/api/v1/organizations/{id}/locations/{loc_id}` | Обновить точку | Bearer |
| DELETE | `/api/v1/organizations/{id}/locations/{loc_id}` | Удалить точку | Bearer |
| GET | `/api/v1/organizations/{id}/settings` | Настройки организации | Bearer (owner) |
| PATCH | `/api/v1/organizations/{id}/settings` | Обновить настройки | Bearer (owner) |
| GET | `/api/v1/organizations/{id}/shifts` | Смены сотрудников | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/stats` | Статистика организации | Bearer (owner/admin) |

---

## Сервисы

| Файл | Описание |
|------|----------|
| `services/auth.py` | Регистрация, верификация, логи��, refresh, logout |
| `services/shift.py` | Lifecycle смен, статистика, автозавершение |
| `services/organization.py` | CRUD организаций, инвайты, участники |
| `services/work_location.py` | CRUD рабочих точек |
| `services/organization_settings.py` | CRUD настроек организации |
| `core/celery_app.py` | Конфигурация Celery (брокер, beat schedule) |
| `core/logging.py` | Конфигурация structlog |

---

## Зависимости (DI)

| Имя | Файл | Описание |
|-----|------|----------|
| `SessionDep` | `api/deps.py` | `AsyncSession` через `Depends` |
| `CurrentUserDep` | `api/deps.py` | Текущий пользователь из JWT (HTTPBearer) |
| `SuperAdminDep` | `api/deps.py` | Текущий пользователь + проверка role=super_admin (403) |

---

## Формат ответов

Все ответы обёрнуты в:

```json
{"data": <payload | null>, "error": <ApiError | null>}
```

`ApiError`: `{"code": "ERROR_CODE", "message": "...", "validation": [...]}`

---

## Утилиты

| Файл | Описание |
|------|----------|
| `utils/geo.py` | Haversine расчёт расстояния, проверка радиуса |

---

## Фоновые задачи (Celery + Redis)

| Файл | Задача | Расписание |
|------|--------|------------|
| `tasks/shifts.py` | `auto_finish_stale_shifts` — завершение зависших смен | Каждые 5 мин |
| `tasks/shifts.py` | `auto_finish_stale_pauses` — завершение просроченных пауз | Каждые 5 мин |
| `tasks/cleanup.py` | `cleanup_expired_tokens` — очистка протухших токенов/кодов | Ежедневно 03:00 UTC |

**Инфраструктура:**
- Redis 7 — брокер Celery + будущий кэш
- Celery worker с встроенным Beat (один контейнер)
- Синхронные DB-сессии для задач (`sync_session_factory` в `database.py`)

---

## Логирование

| Файл | Описание |
|------|----------|
| `core/logging.py` | Конфигурация structlog (JSON prod / pretty dev) |

- Все сервисы используют `structlog` через `get_logger()`
- HTTP middleware логирует каждый запрос (method, path, status_code, duration_ms)
- Celery-задачи логируют результаты выполнения

---

## Внешние сервисы

- **Redis** — брокер Celery, планируется для кэширования

---

## Ключевые решения

См. `docs/decisions/` для полных ADR.
