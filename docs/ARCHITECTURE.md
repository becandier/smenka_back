# Архитектура — текущее состояние

Последнее обновление: 2026-06-20 (checklist_photos — фото-подтверждения пунктов чек-листов: `photo_requirement`/`photo_source`, таблица `checklist_item_photos`, привязка/отвязка через файловое хранилище, единый пересчёт «satisfied»)

---

## Модели (SQLAlchemy)

| Модель | Таблица | Описание |
|--------|---------|----------|
| `User` | `users` | Пользователь (email, name, phone, password_hash, is_verified, role: super_admin/user) |
| `RefreshToken` | `refresh_tokens` | JWT refresh-токен (token, expires_at, revoked) |
| `VerificationCode` | `verification_codes` | Код верификации email (code, expires_at, **attempts** — счётчик неверных вводов; при `>= max_code_attempts` код «сжигается») |
| `Shift` | `shifts` | Рабочая смена (user_id, organization_id, **work_location_id nullable FK→work_locations ON DELETE SET NULL, indexed** — точка открытия смены, started_at, finished_at, status, has_incomplete_required_checklists) |
| `Pause` | `pauses` | Пауза внутри смены (shift_id, started_at, finished_at) |
| `Organization` | `organizations` | Организация (name, owner_id, invite_code, is_deleted) |
| `OrganizationMember` | `organization_members` | Участник (org_id, user_id, role, role_id → custom_role) |
| `OrganizationMemberRate` | `organization_member_rates` | История ставок участника (rate_amount_minor в копейках, rate_type hourly/per_shift, effective_from; UNIQUE (member_id, effective_from)) |
| `OrganizationRole` | `organization_roles` | Кастомная роль организации (org_id, name) |
| `WorkLocation` | `work_locations` | Рабочая точка (org_id, name, lat, lng, radius, address nullable VARCHAR512 — читаемый адрес, геокодинг в админке) |
| `OrganizationSettings` | `organization_settings` | Настройки организации (geo, **require_work_location bool NOT NULL default false** — требовать точку при старте, лимиты пауз, auto-finish) |
| `ChecklistTemplate` | `checklist_templates` | Шаблон чек-листа (org_id, name, type, is_required, is_archived) |
| `ChecklistTemplateItem` | `checklist_template_items` | Пункт шаблона (text, is_required, position, **photo_requirement** none/optional/required, **photo_source** camera/camera_or_gallery — VARCHAR32, дефолты none/camera) |
| `ChecklistRoleAssignment` | `checklist_role_assignments` | Привязка шаблона к роли |
| `ChecklistMemberOverride` | `checklist_member_overrides` | Личное переопределение (add/remove) |
| `ChecklistInstance` | `checklist_instances` | Экземпляр (снимок) чек-листа в смене (status: pending/completed/incomplete) |
| `ChecklistInstanceItem` | `checklist_instance_items` | Заполненный пункт (is_completed, comment, completed_at, change_count, **photo_requirement**/**photo_source** — снимок настроек фото на старте смены) |
| `ChecklistItemPhoto` | `checklist_item_photos` | Фото-подтверждение пункта-экземпляра (instance_item_id→CASCADE индекс, file_id→`files.id` CASCADE **UNIQUE**, captured_at, latitude/longitude double precision nullable — антифрод-метка, position, created_at). Один файл = одна привязка |
| `AuditLog` | `audit_logs` | Append-only журнал чувствительных действий (organization_id NULL→CASCADE, actor_user_id NULL→SET NULL, action, resource_type, resource_id, summary jsonb, ip_address, created_at). Системные авто-действия Celery — `actor_user_id = null`. Индексы `(organization_id, created_at DESC)`, `(actor_user_id)`, `(action)` |
| `File` | `files` | Чистый реестр блобов в S3 (storage_key UNIQUE, bucket, category enum-строка VARCHAR32, original_filename, content_type по реальному MIME, size_bytes, checksum_sha256 nullable, is_attached, organization_id NULL→CASCADE, owner_user_id→CASCADE, created/updated_at). Привязка — FK со стороны фичи-потребителя (НЕ полиморфизм). Индексы `category`, `organization_id`, `owner_user_id`, `(is_attached, created_at)` (очистка сирот). Enum `FileCategory`: checklist_photo/knowledge_base/avatar/other |

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
| GET | `/api/v1/shifts/stats` | Статистика: пресет `period` ЛИБО диапазон `date_from`/`date_to` | Bearer |
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
| GET | `/api/v1/organizations/{id}/shifts` | Смены сотрудников (обогащены identity сотрудника) | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/shifts/{shift_id}` | Деталь смены сотрудника (кликабельная карточка) | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/stats` | Статистика организации: пресет `period` ЛИБО диапазон `date_from`/`date_to` | Bearer (owner/admin) |
| POST | `/api/v1/organizations/{id}/roles` | Создать кастомную роль | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/roles` | Список ролей | Bearer (member) |
| PATCH | `/api/v1/organizations/{id}/roles/{role_id}` | Переименовать | Bearer (owner/admin) |
| DELETE | `/api/v1/organizations/{id}/roles/{role_id}` | Удалить (SET NULL у members) | Bearer (owner/admin) |
| PATCH | `/api/v1/organizations/{id}/members/{user_id}/custom-role` | Назначить/снять кастомную роль | Bearer (owner/admin) |
| POST | `/api/v1/organizations/{id}/checklist-templates` | Создать шаблон | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/checklist-templates` | Список шаблонов | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/checklist-templates/{tpl_id}` | Детали с пунктами | Bearer (owner/admin) |
| PATCH | `/api/v1/organizations/{id}/checklist-templates/{tpl_id}` | Обновить шаблон | Bearer (owner/admin) |
| DELETE | `/api/v1/organizations/{id}/checklist-templates/{tpl_id}` | Архивировать | Bearer (owner/admin) |
| POST | `/api/v1/organizations/{id}/checklist-templates/{tpl_id}/items` | Добавить пункт | Bearer (owner/admin) |
| PATCH | `/api/v1/organizations/{id}/checklist-templates/{tpl_id}/items/{item_id}` | Обновить пункт | Bearer (owner/admin) |
| DELETE | `/api/v1/organizations/{id}/checklist-templates/{tpl_id}/items/{item_id}` | Удалить пункт | Bearer (owner/admin) |
| PUT | `/api/v1/organizations/{id}/checklist-templates/{tpl_id}/items/reorder` | Изменить порядок | Bearer (owner/admin) |
| PUT | `/api/v1/organizations/{id}/checklist-templates/{tpl_id}/roles` | Назначить ролям (PUT) | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/checklist-templates/{tpl_id}/assignments` | Кому назначен | Bearer (owner/admin) |
| PUT | `/api/v1/organizations/{id}/members/{user_id}/checklist-overrides` | Личные overrides bulk (PUT) | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/members/{user_id}/checklist-overrides` | Список личных overrides сотрудника | Bearer (owner/admin/self) |
| PUT | `/api/v1/organizations/{id}/checklist-templates/{tpl_id}/personal/{user_id}` | Upsert override (шаблон, сотрудник) | Bearer (owner/admin) |
| DELETE | `/api/v1/organizations/{id}/checklist-templates/{tpl_id}/personal/{user_id}` | Снять override (идемпотентно) | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/members/{user_id}/checklists` | Эффективные чек-листы | Bearer (owner/admin/self) |
| GET | `/api/v1/shifts/{shift_id}/checklists` | Экземпляры чек-листов смены (items_summary: total, completed, satisfied_count, photos_required_missing — один GROUP BY) | Bearer (владелец смены / owner / admin) |
| GET | `/api/v1/shifts/{shift_id}/checklists/{instance_id}` | Детали экземпляра (+max_photos_per_item, по каждому пункту photos[] со свежими presigned URL; сбой storage → url=null без 502) | Bearer |
| PATCH | `/api/v1/shifts/{shift_id}/checklists/{instance_id}/items/{item_id}` | Отметить пункт | Bearer (владелец смены) |
| POST | `/api/v1/shifts/{shift_id}/checklists/{instance_id}/items/{item_id}/photos` | Привязать загруженный файл checklist_photo к пункту (FOR UPDATE на пункт; PHOTO_NOT_ALLOWED/PHOTO_LIMIT_EXCEEDED/PHOTO_FILE_INVALID) | Bearer (владелец активной смены) |
| DELETE | `/api/v1/shifts/{shift_id}/checklists/{instance_id}/items/{item_id}/photos/{photo_id}` | Отвязать и удалить фото (объект S3 + строка files); ответ `{data:null,error:null}` | Bearer (владелец активной смены) |
| POST | `/api/v1/organizations/{id}/members/{member_id}/rates` | Назначить ставку (новая запись истории) | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/members/{member_id}/rates` | История ставок участника (effective_from DESC) | Bearer (owner/admin) |
| PATCH | `/api/v1/organizations/{id}/members/{member_id}/rates/{rate_id}` | Исправить запись истории | Bearer (owner/admin) |
| DELETE | `/api/v1/organizations/{id}/members/{member_id}/rates/{rate_id}` | Удалить запись истории | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/payroll` | Отчёт по зарплате за период (`date_from`/`date_to` включительно); опц. `granularity`/`user_ids`/`location_ids`/`tz`/`only_missing_rate` | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/payroll/export` | Бинарный `.xlsx` (листы «Сводка»+«Детализация»); те же фильтры, `granularity` default `day` | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/my-earnings` | Личный заработок за период + current_rate | Bearer (member; owner → 403) |
| GET | `/api/v1/admin/users` | Список пользователей платформы (поиск, фильтры role/is_verified, sort, пагинация) | Bearer (super_admin) |
| GET | `/api/v1/admin/users/{user_id}` | Детали пользователя + агрегаты (owned/member orgs, shifts) | Bearer (super_admin) |
| PATCH | `/api/v1/admin/users/{user_id}/role` | Сменить глобальную роль (нельзя разжаловать себя) | Bearer (super_admin) |
| GET | `/api/v1/admin/organizations` | Обзор всех организаций (owner_email, member_count, фильтры) | Bearer (super_admin) |
| GET | `/api/v1/admin/stats` | Сводная статистика платформы (дашборд) | Bearer (super_admin) |
| GET | `/api/v1/organizations/{id}/audit-logs` | Лента аудита организации (фильтры action/actor/даты, пагинация, created_at DESC) | Bearer (owner/admin) |
| POST | `/api/v1/files` | Загрузка файла (multipart: file/category/organization_id); валидация размера и реального MIME, presigned GET в ответе | Bearer (право по category) |
| GET | `/api/v1/files/{file_id}` | Метаданные + свежий presigned GET URL (обновление протухшей ссылки) | Bearer (uploader/admin-owner/member по category) |
| DELETE | `/api/v1/files/{file_id}` | Удаление объекта + строки; привязанный → `FILE_IN_USE` (409), повтор → `FILE_NOT_FOUND` (404) | Bearer (uploader/admin-owner/super_admin) |

> **Файловое хранилище (file_storage).** Единый слой хранения поверх S3-совместимого storage (локально MinIO, в проде managed S3 — переезд = смена `S3_*` env, без правок кода). Загрузка идёт **через бэкенд** (multipart → API → storage), доступ к объектам **приватный** — клиент качает по **presigned GET URL** с коротким TTL (`S3_PRESIGN_EXPIRE_SECONDS`), который генерирует бэк. `files` — чистый реестр блобов (`File`); привязка к бизнес-сущности делается со стороны фичи-потребителя FK на `files.id` (НЕ полиморфизм) — целостность и `ON DELETE` каскады. Категория (`FileCategory`) задаёт префикс ключа, политику (лимит размера + разрешённые MIME, таблица `CATEGORY_POLICIES` в `services/file_storage.py`) и права: `checklist_photo` — любой member org; `knowledge_base` — admin/owner org; `avatar`/`other` — персональные. Реальный MIME определяется по сигнатуре содержимого (`filetype`), а не по заголовку multipart; имя в ключе — всегда UUID (anti path-traversal/коллизии), исходное имя хранится для `Content-Disposition`. **presigned + MinIO:** бэк ходит в storage по внутреннему `S3_ENDPOINT_URL`, но presigned-ссылка генерируется клиентом на публичном `S3_PUBLIC_ENDPOINT_URL` (подпись сразу от публичного хоста; в managed-S3 оба совпадают). Прямой стрим байтов через бэк не делаем. Жизненный цикл: строка создаётся с `is_attached=false`, потребитель ставит `true` при привязке; сироты (`is_attached=false`, старше `ORPHAN_FILE_TTL_HOURS`) подбирает Celery `cleanup_orphan_files`. Ошибки — `FileError` (`FILE_NOT_FOUND`/`FILE_TOO_LARGE`/`UNSUPPORTED_FILE_TYPE`/`INVALID_FILE_CATEGORY`/`FILE_IN_USE`/`STORAGE_UNAVAILABLE`).

> **Фото-подтверждения чек-листов (checklist_photos).** Пункт шаблона несёт `photo_requirement` (none/optional/required) и `photo_source` (camera/camera_or_gallery — подсказка UI, сервер источник **не** enforce-ит); при `requirement=none` source нормализуется к camera. На старте org-смены настройки копируются снимком в `checklist_instance_items` (правка шаблона на уже созданные экземпляры не влияет). Загрузка файла — через существующий `POST /api/v1/files` (категория `checklist_photo`); фича только **привязывает** загруженный `file_id` к пункту-экземпляру (`checklist_item_photos`, UNIQUE на `file_id`). Привязка/отвязка — только владелец активной смены, под `SELECT ... FOR UPDATE` на пункт (защита лимита `CHECKLIST_MAX_PHOTOS_PER_ITEM`); проблема с файлом-кандидатом (нет/чужой/другая org/не `checklist_photo`/уже привязан) → единый `PHOTO_FILE_INVALID`. Привязка ставит `files.is_attached=true`; отвязка снимает флаг и зовёт `delete_file` (объект S3 + строка `files`, связь уходит каскадом). Статус экземпляра считает **единая** функция `_recompute_instance_status` по критерию **satisfied** = `is_completed AND (photo_requirement != required OR photos_count >= 1)`; вызывается из PATCH пункта и привязки/отвязки фото, поэтому `finalize_shift_checklists` (и его sync-двойник) по-прежнему доверяют хранимому статусу. Режим **мягкий**: отметить пункт без фото и завершить смену можно — отсутствие обязательного фото лишь даёт `pending`→`incomplete` и `has_incomplete_required_checklists`. Деталь экземпляра отдаёт по каждому фото свежий presigned GET (батч-подпись одним клиентом, без N+1); при недоступности storage — `url=null` без 502 (клиент дотянет через `GET /files/{id}`). `items_summary` списка получил `satisfied_count` (честный прогресс) и `photos_required_missing` (бейдж «нужно фото») — одним GROUP BY. **Privacy-долг:** `cleanup_shift_photo_files` (в `services/checklist_instance.py`) удаляет файлы привязанных фото смены ДО каскадного сноса связей, НО прямого триггера нет — смены не hard-удаляются, org — soft-delete; `cleanup_orphan_files` не подбирает `is_attached=true`, поэтому при будущем hard-delete смены/org гео-привязанные фото останутся объектами S3 (массовая чистка бакета — отдельная DevOps-задача).

> **Сквозной доступ super_admin.** Все org-эндпоинты (`members`, `settings`, `locations`, `roles`, `checklist-*`, `shifts`, `stats`) пускают `super_admin`, даже если он не состоит в организации. Проверки прав вынесены в `services/common.py` (`ensure_owner` / `ensure_member` / `ensure_admin_or_owner`), и в каждой добавлена ветка super_admin. `GET /shifts` и `GET /organizations/{id}/shifts` дополнительно принимают `sort` (`started_at`/`finished_at`) и `order` (`asc`/`desc`).

> **Видимость владельца смены (orgrouted enrichment).** `ShiftResponse` несёт 4 nullable-поля `user_name` / `user_email` / `role` / `custom_role_name` (`default=None`). Они вычисляются на чтении (`services/shift.build_org_shift_identities`: имя/почта из `users`, роль/кастомная роль из `organization_members` — два batch-запроса без N+1, `custom_role` через `selectinload`) и наполняются ТОЛЬКО в орг-контексте: `GET /organizations/{id}/shifts` (список) и `GET /organizations/{id}/shifts/{shift_id}` (деталь). В персональном `GET /shifts` остаются `null` (сериализатор `_shift_to_response` без `identity`). Исключённый из org сотрудник: имя/почта сохраняются, `role`/`custom_role_name` = `null`. Деталь чужой org-смены строго проверяет `shift.organization_id == org_id` → иначе `404 SHIFT_NOT_FOUND` (персональные/чужие смены не раскрываются). Схема БД не меняется — денормализация в `shifts` отвергнута.

> **Привязка точки к смене (`shift_work_location`).** При старте орг-смены фиксируется `shifts.work_location_id` по матрице двух настроек org (`geo_check_enabled` × `require_work_location`): **гео вкл** — точку определяет сервер (ближайшая по Haversine из зон, в радиус которых попал сотрудник; присланный клиентом `work_location_id` игнорируется; вне зон — прежний `403 GEO_CHECK_FAILED`); **гео выкл + require** — клиент обязан прислать `work_location_id` (нет → `422 WORK_LOCATION_REQUIRED`); **гео выкл + не require** — точка опциональна. Любой переданный id, не принадлежащий org (или с невалидным форматом), → `404 WORK_LOCATION_NOT_FOUND`. Точка ставится один раз на старте (паузы/возобновления её не меняют); логика — `services/shift._resolve_org_shift_start`. Персональные смены (`org_id=null`) точку не привязывают. `ShiftResponse` отдаёт additive nullable `work_location_id` + денормализованный `work_location {id, name, address}` (текущее значение точки, eager `selectinload(Shift.work_location)` во всех читающих смену запросах). Настройку `require_work_location` нельзя включить без точек (`409 WORK_LOCATION_REQUIRED_NO_LOCATIONS`); удаление последней точки гасит и `geo_check_enabled`, и `require_work_location` (симметрично). Помимо `OrganizationSettings*` (RBAC owner/admin) обе настройки `geo_check_enabled` и `require_work_location` **флэттятся в `OrganizationResponse` и `JoinResponse`** (`_org_to_response` / join, из `org.settings.* if org.settings else False`) — чтобы employee (без доступа к `/settings`) активировал обязательный выбор точки на клиенте. Разблокирует фильтр по точке в `payroll_detailed_report`.

> **CORS.** Подключён `CORSMiddleware` (`main.py`), источники — из `Settings.cors_origins` (env `CORS_ORIGINS`, CSV; пусто = выключено). Нужен браузерным клиентам: админке `smenka_admin` и веб-версии мобилки (`flutter build web`). `allow_credentials=False` — клиенты шлют JWT в заголовке `Authorization`, не в cookie (переход на httpOnly-cookie + CSRF — отдельная задача); `allow_methods`/`allow_headers=["*"]`.

> **Ставки и зарплата (payroll).** История ставок — источник истины: строка `organization_member_rates` действует с `effective_from`, прошлые записи не перезаписываются (новая ставка «с даты» = POST новой строки; PATCH — только исправление опечаток). Для каждой завершённой смены берётся ставка с максимальным `effective_from <= shift.started_at`; смены без ставки → `unpaid_seconds`/`unpaid_shifts_count`/`has_missing_rate` и не входят в `gross_amount_minor`. Деньги — целые копейки: накопление точным Decimal, half-up до копейки ровно один раз на итог сотрудника; `totals.gross` = сумма округлённых итогов. Расчёты не кэшируются. `MemberResponse.current_rate` (additive nullable) — действующая ставка `max(effective_from) <= now`, заполняется **только** для owner/admin/super_admin (для employee всегда null — приватность зарплат). Owner != member (ADR-001): в payroll-отчёте не фигурирует, `my-earnings` отвечает ему 403. Ошибки: `MEMBER_NOT_FOUND`/`RATE_NOT_FOUND` (404), `RATE_EFFECTIVE_FROM_TAKEN` (409, дубль по `UNIQUE (member_id, effective_from)` — закрывает и гонку), `INVALID_DATE_RANGE` (400, согласовано с date_filters).

> **Детальный отчёт + экспорт (`payroll_detailed_report`).** Аддитивное расширение `GET …/payroll` (новых таблиц/полей нет). Опц. query: `granularity` (`none｜day｜week｜month`, default `none`), `user_ids`/`location_ids` (повтор или CSV uuid; `location_ids=none` — смены без точки, чужие точки → `422 VALIDATION_ERROR`), `tz` (IANA, нарезка корзин; битая → `422`), `only_missing_rate` (bool). При `granularity != none` к каждому `items[]` добавляется `breakdown[]` (корзины с ненулевым числом смен, `bucket_start` ASC) и top-level эхо `granularity`/`tz`; при `none` ответ **байт-в-байт** прежний (полей нет). **Округление — посуточное** только в детальном режиме/экспорте: атом = локальный день в `tz` (half-up один раз/день), корзины week/month и итог сотрудника = суммы округлённых дневных значений (день→корзина→сотрудник→totals — всё «бьётся»); режим `none` сохраняет старое единичное округление на сотрудника (ADR-002, расхождение ≤ нескольких копеек). Корзина по `started_at` в `tz`; фильтр периода — по UTC до нарезки. `GET …/payroll/export` отдаёт `.xlsx` (`openpyxl`, не конверт `{data,error}`; ошибки до файла — конвертом): лист «Сводка» (агрегат+ИТОГО), «Детализация» (сотрудник×корзина), часы/деньги числами для суммирования в Excel; `format` — enum (`xlsx`), default `granularity=day`. Логика — `services/payroll.get_org_payroll`/`export_org_payroll`/`_build_breakdown`/`_build_payroll_xlsx`.

> **Фильтры диапазона дат (date_filters).** Оба stats-эндпоинта принимают ровно один источник окна: пресет `period` (`day`/`week`/`month`, поведение не менялось) ЛИБО кастомный диапазон `date_from`/`date_to` (включительно по `Shift.started_at`, допускается открытый диапазон). Ошибки: `MISSING_STATS_RANGE` → `AMBIGUOUS_STATS_RANGE` → `INVALID_PERIOD` → `INVALID_DATE_RANGE` (этот порядок). В ответах stats добавлены `range_from`/`range_to` (фактически применённое окно), `period` стал nullable. Списочные эндпоинты смен получили валидацию `INVALID_DATE_RANGE`. Все границы нормализуются `services/shift.ensure_utc` (naive → UTC, aware → приведение к UTC) и в фильтрах, и в эхо-полях.

> **Усиление безопасности (security_hardening).** Три класса защит. **(1) Rate-limit** (slowapi, `core/rate_limit.py`) на `register`/`verify`/`resend-code`/`login` — ключ = IP клиента (`utils/request.get_client_ip`, первый из `X-Forwarded-For` за Caddy). Хранилище счётчиков — Redis (`rate_limit_storage_uri`||`redis_url`; в тестах `memory://`), пороги из ENV (`*_RATE_LIMIT`). Превышение → `429 RATE_LIMIT_EXCEEDED` в конверте `{data,error}` + `Retry-After`. Лимитер выключаем флагом `rate_limit_enabled`. **(2) Счётчик попыток кода**: `verification_codes.attempts` атомарно (`UPDATE attempts = attempts+1`, commit до отдачи ошибки) растёт на каждый неверный `verify`; при `>= max_code_attempts` (5) код «сожжён» → `429 TOO_MANY_CODE_ATTEMPTS`, нужен `resend-code`. **(3) Блокировка аккаунта** (`services/lockout.py`, Redis-ключ `login_fail:{email}` с TTL = окно): после `max_login_failures` (10) неудач — `423 ACCOUNT_LOCKED` на `account_lockout_minutes` (15) независимо от верности пароля; успех сбрасывает счётчик. Блокировка по email одинаково для существующего/несуществующего email (без enumeration-оракула); per-IP rate-limit ловит распределённый перебор. **Sentry** (`core/sentry.py`) включается только при `SENTRY_DSN` (иначе no-op; dev/CI/тесты без сети), `send_default_pii=False`, `max_request_body_size="never"`. Глобальный `@app.exception_handler(Exception)` ловит необработанные 500: лог через structlog (repr) + `capture_exception`, клиенту — тот же конверт `{data:null, error:{code:"ERROR"}}` (доменные ошибки и `RequestValidationError` идут своими хендлерами и в Sentry не шлются). **Аудит** пишется в той же транзакции, что и действие (см. `services/audit.py`, ниже).

> **Аудит действий (audit_logs).** Запись создаётся из endpoint-слоя ПОСЛЕ успешного сервисного вызова и ДО `session.commit()` — один commit, аудит не расходится с фактом. Покрытие: `org.update/delete/invite_rotate`, `member.join/remove/role_update`, `settings.update`, `location.create/update/delete`, `shift.finish` (actor = инициатор, IP из запроса), а также системные `shift.auto_finish`/`pause.auto_finish` из Celery (`record_sync`, `actor_user_id = null`). `summary` — ключевые поля без секретов (инвайт-код и токены не пишутся). Чтение — только `GET /organizations/{id}/audit-logs` (owner/admin, `created_at DESC`, фильтры `action`/`actor_user_id`/`date_from`/`date_to` с `date_to` включительно, пагинация limit≤200); `actor_name` подмешивается batch-запросом по `users` (или «Система» при null-акторе). Записи неизменяемы и не удаляются через API.

---

## Сервисы

| Файл | Описание |
|------|----------|
| `services/auth.py` | Регистрация, верификация, логи��, refresh, logout |
| `services/shift.py` | Lifecycle смен, статистика, автозавершение |
| `services/organization.py` | CRUD организаций, инвайты, участники |
| `services/work_location.py` | CRUD рабочих точек |
| `services/organization_settings.py` | CRUD настроек организации |
| `services/organization_role.py` | Кастомные роли организации и их назначение members (`RoleError`) |
| `services/checklist_template.py` | Шаблоны чек-листов, пункты (+`photo_requirement`/`photo_source`), reorder, архивация (`ChecklistError`) |
| `services/checklist_assignment.py` | Назначение шаблонов ролям, личные overrides (bulk PUT), вычисление эффективных шаблонов |
| `services/checklist_override.py` | Гранулярный upsert/delete/list личных overrides (ON CONFLICT DO UPDATE) |
| `services/checklist_instance.py` | Создание снимков в смене, заполнение пунктов, привязка/отвязка фото, единый пересчёт «satisfied» (`_recompute_instance_status`), finalize, `cleanup_shift_photo_files` |
| `services/common.py` | Общие guard-функции org-доступа (`ensure_owner/ensure_member/ensure_admin_or_owner`) со сквозной веткой super_admin (`AccessError`) |
| `services/payroll.py` | История ставок участников (CRUD, `PayrollError`), действующие ставки batch-запросом (DISTINCT ON), расчёт payroll/my-earnings «на лету», суточная разбивка (`_build_breakdown`, посуточное округление) + экспорт `.xlsx` (`export_org_payroll`/`_build_payroll_xlsx`) |
| `services/admin.py` | Платформенные операции super_admin: список/детали пользователей, смена роли, обзор организаций, статистика (`AdminError`) |
| `services/audit.py` | Запись аудита (`record` async / `record_sync` для Celery, в той же транзакции) и чтение ленты организации с именами инициаторов |
| `services/lockout.py` | Блокировка аккаунта по неудачным логинам (Redis-счётчик с TTL, по email) |
| `services/file_storage.py` | Файловое хранилище: политики категорий (`CATEGORY_POLICIES`), валидация размера/реального MIME, генерация ключа, реестр `files`, presigned URL, удаление, права по category (`FileError`) |
| `core/storage.py` | S3-обёртка над `aioboto3` (`upload_object`/`generate_presigned_get`/`delete_object`/`ensure_bucket`); внутренний vs публичный endpoint для presigned; ошибки S3 → `StorageError` |
| `core/celery_app.py` | Конфигурация Celery (брокер, beat schedule, task-события для мониторинга, `acks_late`, сигнал `task_failure` → structlog; Sentry в воркере) |
| `core/rate_limit.py` | slowapi-`Limiter` (ключ = IP, Redis-хранилище, пороги из ENV) |
| `core/redis.py` | Общий асинхронный Redis-клиент приложения (lockout); ленивый, подменяется fakeredis в тестах |
| `core/sentry.py` | Инициализация Sentry (только при `SENTRY_DSN`, без PII и тел запросов) |
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
| `utils/request.py` | `get_client_ip` — IP клиента из `X-Forwarded-For` (за Caddy) либо `request.client.host` (для rate-limit и аудита) |

---

## Фоновые задачи (Celery + Redis)

| Файл | Задача | Расписание |
|------|--------|------------|
| `tasks/shifts.py` | `auto_finish_stale_shifts` — завершение зависших смен (+ аудит `shift.auto_finish`, actor=null) | Каждые 5 мин |
| `tasks/shifts.py` | `auto_finish_stale_pauses` — завершение просроченных пауз (+ аудит `pause.auto_finish`, actor=null) | Каждые 5 мин |
| `tasks/cleanup.py` | `cleanup_expired_tokens` — очистка протухших токенов/кодов | Ежедневно 03:00 UTC |
| `tasks/cleanup.py` | `cleanup_orphan_files` — удаление файлов-сирот (`is_attached=false`, старше `ORPHAN_FILE_TTL_HOURS`): объект в S3 + строка | Ежечасно |

**Инфраструктура:**
- Redis 7 — брокер Celery + хранилище rate-limit (slowapi) и счётчиков lockout
- Celery worker с встроенным Beat (один контейнер); включены task-события (`task_track_started`/`*_send_*_event`) для мониторинга (Flower в проде), `acks_late`, сигнал `task_failure` → structlog (+ Sentry через CeleryIntegration)
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

- **Redis** — брокер Celery, хранилище rate-limit (slowapi) и счётчиков блокировки аккаунтов
- **Sentry** — error-tracking бэка (включается при `SENTRY_DSN`; провижининг — `DEPLOY_NOTES.md`)

---

## Ключевые решения

См. `docs/decisions/` для полных ADR.
