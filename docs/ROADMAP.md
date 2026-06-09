# Roadmap — Smenka Backend

Этот файл — источник правды о том, что сделано и что предстоит. Каждый агент обновляет статусы после завершения работы.

Статусы: `[ ]` не начато, `[~]` в работе, `[x]` готово

---

## Фича — Видимость владельца смены (`shift_owner_visibility`) `[x]`
ТЗ: `../docs/tasks/shift_owner_visibility/backend.md`
- [x] `ShiftResponse` +4 nullable-поля `user_name` / `user_email` / `role` / `custom_role_name` (`default=None`, схема БД не меняется)
- [x] `build_org_shift_identities` — обогащение орг-смен без N+1 (два batch-запроса, `custom_role` через `selectinload`)
- [x] `_shift_to_response(shift, identity=None)` — персональный контекст → поля `null`
- [x] `GET /organizations/{id}/shifts` — наполняет identity сотрудника в каждом item
- [x] `GET /organizations/{id}/shifts/{shift_id}` — деталь чужой смены для owner/admin (строгая проверка `organization_id`, иначе 404)
- [x] Edge: исключённый из org сотрудник — имя/почта остаются, роль/кастомная роль = `null`
- [x] 16 тестов (обогащение, кастомная роль, super_admin, фильтры/пагинация, 403/404-семантика, персональный контекст null)

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

## Фаза 3 — Организации `[x]`
- [x] Модель `Organization` (id, name, owner_id, created_at, invite_code)
- [x] Модель `OrganizationMember` (id, org_id, user_id, role: admin/employee, joined_at)
- [x] Модель `WorkLocation` (id, org_id, name, latitude, longitude, radius_meters)
- [x] CRUD организации (создание, обновление, удаление)
- [x] Генерация и ротация инвайт-кода
- [x] Присоединение по инвайт-коду
- [x] Управление сотрудниками (список, удаление из организации)
- [x] Миграция
- [x] Тесты: создание орг, инвайт, роли, CRUD точек

---

## Фаза 4 — Правила организации `[x]`
- [x] Модель `OrganizationSettings` (org_id, geo_check_enabled, auto_finish_hours, max_pause_minutes, max_pauses_per_shift)
- [x] Геопроверка при начале смены (Haversine, сравнение с WorkLocation)
- [x] Применение правил организации к смене (если пользователь в организации)
- [x] Автозавершение пауз при превышении лимита
- [x] GET /organizations/{id}/shifts — смены сотрудников для админа
- [x] GET /organizations/{id}/stats — статистика по организации
- [x] Миграция
- [x] Тесты: geo-расчёты, применение лимитов, админские эндпоинты

---

## Фаза 5 — Фоновые задачи и логирование `[x]`
- [x] Celery + Redis инфраструктура (брокер, воркер с Beat)
- [x] Задача `auto_finish_stale_shifts` — автозавершение зависших смен (каждые 5 мин)
- [x] Задача `auto_finish_stale_pauses` — автозавершение просроченных пауз (каждые 5 мин)
- [x] Задача `cleanup_expired_tokens` — очистка протухших токенов и кодов (ежедневно 03:00 UTC)
- [x] Использование `OrganizationSettings.auto_finish_hours` вместо глобального дефолта для орг-смен
- [x] Структурированное логирование (structlog) — JSON в проде, pretty в dev
- [x] Request logging middleware (method, path, status, duration)
- [x] Логирование во всех сервисах (auth, shifts, organizations, locations, settings)
- [x] Тесты фоновых задач

---

## Глобальные роли пользователей `[x]`
- [x] Enum `UserRole` (super_admin, user), default user
- [x] Поле `role` в модели `User` + Alembic-миграция
- [x] Зависимость `SuperAdminDep` в deps.py
- [x] Защита POST /organizations — только super_admin
- [x] GET /organizations/all — все организации для super_admin
- [x] PATCH /organizations/{id}/members/{user_id}/role — смена роли участника (owner/super_admin)
- [x] /users/me возвращает role
- [x] super_admin задаётся только вручную в БД

---

## Фаза 6 — Продакшен `[~]`
- [ ] Rate-limiting
- [x] CORS-настройки (`CORSMiddleware` + `Settings.cors_origins`, env `CORS_ORIGINS`) — закрыто в admin_panel
- [x] CI/CD: `.github/workflows/ci.yml` (ruff + mypy + pytest на PR/ветках) + `release.yml` (build → ghcr). Весь бэк: ruff+mypy zero-errors.
- [ ] Финальная проверка OpenAPI-документации

---

## admin_panel — веб-админка (backend-трек) `[x]`
**ТЗ:** `smenka/docs/tasks/admin_panel/backend.md`  **STATUS:** `smenka/docs/tasks/admin_panel/STATUS.md`

- [x] Блок A — CORS (`CORSMiddleware`, `cors_origins`, `NoDecode`-парсинг CSV из env)
- [x] Блок B — `sort`/`order` для `GET /shifts` и `GET /organizations/{id}/shifts` (обратно совместимо)
- [x] Блок C2 — сквозной доступ super_admin: guard-проверки вынесены в `services/common.py` (`ensure_owner/ensure_member/ensure_admin_or_owner`, `AccessError`), добавлена ветка super_admin; `_check_*` в 6 сервисах стали тонкими делегаторами
- [x] Блок C1 — `GET /admin/users`, `GET /admin/users/{id}`, `PATCH /admin/users/{id}/role` (`CANNOT_DEMOTE_SELF`)
- [x] Блок C2 — `GET /admin/organizations` (`AdminOrganizationResponse`: owner_email, member_count)
- [x] Блок C3 — `GET /admin/stats` (сводка платформы для дашборда)
- [x] Блок D — `scripts/seed_superadmin.py <email>` (идемпотентно, верифицированный пользователь)
- [x] Без миграций (схема не менялась); 28 тестов (`tests/test_admin.py`): права, пагинация, self-demote, сквозной super_admin, сортировка смен

---

## Фаза 7 — Чек-листы и кастомные роли `[x]`
**Спека:** `smenka/docs/CHECKLISTS_SPEC.md`  **ADR:** `smenka/docs/decisions/002-custom-roles-single-per-member.md`

### Этап 1 — Кастомные роли `[x]`
- [x] Модель `OrganizationRole` (id, org_id, name, created_at, UNIQUE(org_id, name))
- [x] `OrganizationMember.role_id` (FK → organization_roles, nullable, SET NULL)
- [x] CRUD ролей (owner/admin) + list (all members)
- [x] PATCH /members/{user_id}/custom-role — назначение/снятие
- [x] `MemberResponse.custom_role: RoleResponse | null`
- [x] Миграция, `RoleError`, 19 тестов

### Этап 2 — Шаблоны чек-листов `[x]`
- [x] Модели `ChecklistTemplate`, `ChecklistTemplateItem`, enum `ChecklistType`
- [x] CRUD шаблонов + пунктов, PUT reorder
- [x] Мягкое удаление (is_archived), фильтр архивных в list
- [x] Миграция, `ChecklistError`, 18 тестов

### Этап 3 — Назначение `[x]`
- [x] Модели `ChecklistRoleAssignment`, `ChecklistMemberOverride` (enum `OverrideType`)
- [x] PUT-семантика для assign-to-roles и member-overrides
- [x] GET /assignments — роли + personal_add / personal_remove
- [x] GET /members/{user_id}/checklists — effective (роль − remove) + add, фильтр архивных
- [x] Миграция, 13 тестов

### Этап 4 — Экземпляры и заполнение `[x]`
- [x] Модели `ChecklistInstance`, `ChecklistInstanceItem`, enum `ChecklistInstanceStatus`
- [x] `Shift.has_incomplete_required_checklists`
- [x] При старте org-смены — создание снимков (templates + items)
- [x] PATCH пункта: владелец смены, смена не finished, change_count, авто-пересчёт статуса
- [x] `finalize_shift_checklists` вызывается в finish_shift, inline auto-finish и Celery
- [x] Миграция, 12 тестов

### Этап 7.2 — `my_role` / `my_custom_role` в OrganizationResponse `[x]`
- [x] `OrganizationResponse.my_role` (owner/admin/employee | null) и `my_custom_role` (RoleResponse | null)
- [x] `batch_get_my_roles(session, orgs, user_id)` — один membership-запрос на любой набор
- [x] `GET /organizations`, `GET /organizations/{id}`, `GET /organizations/all` возвращают поля
- [x] Убирает N+1 `getMembers` на мобильном экране Profile
- [x] 10 тестов (включая guard на N+1)

### Этап 7.1 — Гранулярные overrides `[x]`
- [x] `GET /members/{user_id}/checklist-overrides` — все overrides сотрудника (с архивными)
- [x] `PUT /checklist-templates/{tpl_id}/personal/{user_id}` — upsert через ON CONFLICT DO UPDATE
- [x] `DELETE /checklist-templates/{tpl_id}/personal/{user_id}` — идемпотентное удаление
- [x] Запрет PUT на архивный шаблон (TEMPLATE_ARCHIVED)
- [x] Bulk PUT остаётся для «очистить всё» сценариев
- [x] 21 тест (CRUD, права, архивность, cascade, обратная совместимость)
