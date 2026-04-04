# Архитектура — текущее состояние

Последнее обновление: 2026-04-04 (фаза 3)

---

## Модели (SQLAlchemy)

| Модель | Таблица | Описание |
|--------|---------|----------|
| `User` | `users` | Пользователь (email, name, phone, password_hash, is_verified) |
| `RefreshToken` | `refresh_tokens` | JWT refresh-токен (token, expires_at, revoked) |
| `VerificationCode` | `verification_codes` | Код верификации email (code, expires_at) |
| `Shift` | `shifts` | Рабочая смена (user_id, started_at, finished_at, status) |
| `Pause` | `pauses` | Пауза внутри смены (shift_id, started_at, finished_at) |
| `Organization` | `organizations` | Организация (name, owner_id, invite_code, is_deleted) |
| `OrganizationMember` | `organization_members` | Участник организации (org_id, user_id, role) |
| `WorkLocation` | `work_locations` | Рабочая точка (org_id, name, lat, lng, radius) |

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
| POST | `/api/v1/organizations` | Создать организацию | Bearer |
| GET | `/api/v1/organizations` | Мои организации | Bearer |
| GET | `/api/v1/organizations/{id}` | Получить организацию | Bearer |
| PATCH | `/api/v1/organizations/{id}` | Обновить организацию | Bearer |
| DELETE | `/api/v1/organizations/{id}` | Удалить организацию (soft) | Bearer |
| POST | `/api/v1/organizations/{id}/rotate-invite` | Ротация инвайт-кода | Bearer |
| POST | `/api/v1/organizations/join/{code}` | Присоединиться по коду | Bearer |
| GET | `/api/v1/organizations/{id}/members` | Список участников | Bearer |
| DELETE | `/api/v1/organizations/{id}/members/{user_id}` | Удалить участника / выйти | Bearer |
| POST | `/api/v1/organizations/{id}/locations` | Создать точку | Bearer |
| GET | `/api/v1/organizations/{id}/locations` | Список точек | Bearer |
| PATCH | `/api/v1/organizations/{id}/locations/{loc_id}` | Обновить точку | Bearer |
| DELETE | `/api/v1/organizations/{id}/locations/{loc_id}` | Удалить точку | Bearer |

---

## Сервисы

| Файл | Описание |
|------|----------|
| `services/auth.py` | Регистрация, верификация, логи��, refresh, logout |
| `services/shift.py` | Lifecycle смен, статистика, автозавершение |
| `services/organization.py` | CRUD организаций, инвайты, участники |
| `services/work_location.py` | CRUD рабочих точек |

---

## Зависимости (DI)

| Имя | Файл | Описание |
|-----|------|----------|
| `SessionDep` | `api/deps.py` | `AsyncSession` через `Depends` |
| `CurrentUserDep` | `api/deps.py` | Текущий пользователь из JWT (HTTPBearer) |

---

## Формат ответов

Все ответы обёрнуты в:

```json
{"data": <payload | null>, "error": <ApiError | null>}
```

`ApiError`: `{"code": "ERROR_CODE", "message": "...", "validation": [...]}`

---

## Внешние сервисы

Нет. Проект полностью автономный (PostgreSQL + API).

---

## Ключевые решения

См. `docs/decisions/` для полных ADR.
