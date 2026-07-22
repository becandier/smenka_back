# Архитектура — текущее состояние

Последнее обновление: 2026-07-21 (checklist_work_location — привязка шаблонов чек-листов к рабочим точкам: таблица `checklist_template_locations`, фильтр по точке в создании экземпляров на старте смены, `PUT .../checklist-templates/{id}/locations`, `GET/PUT .../locations/{id}/checklist-templates`, additive `location_ids` в `assignments`/`EffectiveTemplateResponse` + опциональный `work_location_id` у `GET .../members/{id}/checklists`)

---

## Модели (SQLAlchemy)

| Модель | Таблица | Описание |
|--------|---------|----------|
| `User` | `users` | Пользователь (email, name, phone, **password_hash nullable** — OAuth-only пользователь может не иметь пароля, is_verified, role: super_admin/user) |
| `RefreshToken` | `refresh_tokens` | JWT refresh-токен (token, expires_at, revoked) |
| `OAuthIdentity` | `oauth_identities` | Привязка user↔провайдер (user_id→CASCADE, provider google/apple, provider_user_id — `sub` из id-токена, email — справочно на момент привязки, created_at). `UNIQUE(provider, provider_user_id)`, `UNIQUE(user_id, provider)`, индекс `(user_id)` |
| `OAuthProviderSetting` | `oauth_provider_settings` | Платформенная настройка: какой Client ID бэк принимает как `aud` для пары provider×client_type (5 валидных комбинаций: google×{web,android,ios}, apple×{ios,web}); enabled — kill-switch, updated_by→users SET NULL, updated_at. `UNIQUE(provider, client_type)`. Редактируется только super_admin, не ENV |
| `VerificationCode` | `verification_codes` | Код верификации email (code, expires_at, **attempts** — счётчик неверных вводов; при `>= max_code_attempts` код «сжигается») |
| `Shift` | `shifts` | Рабочая смена (user_id, organization_id, **work_location_id nullable FK→work_locations ON DELETE SET NULL, indexed** — точка открытия смены, started_at, finished_at, status, has_incomplete_required_checklists, **is_deleted bool NOT NULL default false** — soft-delete смены; все читающие запросы по сменам фильтруют `is_deleted=false`; пишущего эндпоинта удаления пока нет) |
| `Pause` | `pauses` | Пауза внутри смены (shift_id, started_at, finished_at) |
| `Organization` | `organizations` | Организация (name, owner_id, invite_code, is_deleted) |
| `OrganizationMember` | `organization_members` | Участник (org_id, user_id, role, role_id → custom_role) |
| `OrganizationMemberRate` | `organization_member_rates` | История ставок участника (rate_amount_minor в копейках, rate_type hourly/per_shift, effective_from; UNIQUE (member_id, effective_from)) |
| `OrganizationPenaltyTemplate` | `organization_penalty_templates` | Шаблон штрафа организации (org_id→CASCADE, reason VARCHAR200, amount_minor int >0 копейки, currency RUB, is_deleted soft-delete, created/updated_at). Источник снимка при назначении штрафа; индекс `(organization_id, is_deleted)` |
| `Penalty` | `penalties` | Назначенный штраф (org_id→CASCADE, member_id→organization_members CASCADE, shift_id nullable→SET NULL, template_id nullable→SET NULL, reason/amount_minor/currency/occurred_at — **снимок**, comment, created_by_user_id, is_deleted/deleted_by_user_id/deleted_at — снятие=soft-delete, created/updated_at). Индексы `(org_id,is_deleted)`, `(member_id,is_deleted)`, `(shift_id)`, `(occurred_at)`. Owner != member ⇒ owner оштрафовать нельзя |
| `OrganizationRole` | `organization_roles` | Кастомная роль организации (org_id, name) |
| `WorkLocation` | `work_locations` | Рабочая точка (org_id, name, lat, lng, radius, address nullable VARCHAR512 — читаемый адрес, геокодинг в админке) |
| `OrganizationSettings` | `organization_settings` | Настройки организации (geo, **require_work_location bool NOT NULL default false** — требовать точку при старте, лимиты пауз, auto-finish) |
| `ChecklistTemplate` | `checklist_templates` | Шаблон чек-листа (org_id, name, type, is_required, is_archived) |
| `ChecklistTemplateItem` | `checklist_template_items` | Пункт шаблона (text, is_required, position, **photo_requirement** none/optional/required, **photo_source** camera/camera_or_gallery — VARCHAR32, дефолты none/camera) |
| `ChecklistRoleAssignment` | `checklist_role_assignments` | Привязка шаблона к роли |
| `ChecklistMemberOverride` | `checklist_member_overrides` | Личное переопределение (add/remove) |
| `ChecklistTemplateLocation` | `checklist_template_locations` | Привязка шаблона к рабочей точке many-to-many (`template_id`/`work_location_id` → CASCADE, `UNIQUE(template_id, work_location_id)`). Шаблон с привязками действует только на них; без привязок — на любой точке (`checklist_work_location`) |
| `ChecklistInstance` | `checklist_instances` | Экземпляр (снимок) чек-листа в смене (status: pending/completed/incomplete) |
| `ChecklistInstanceItem` | `checklist_instance_items` | Заполненный пункт (is_completed, comment, completed_at, change_count, **photo_requirement**/**photo_source** — снимок настроек фото на старте смены) |
| `ChecklistItemPhoto` | `checklist_item_photos` | Фото-подтверждение пункта-экземпляра (instance_item_id→CASCADE индекс, file_id→`files.id` CASCADE **UNIQUE**, captured_at, latitude/longitude double precision nullable — антифрод-метка, position, created_at). Один файл = одна привязка |
| `AuditLog` | `audit_logs` | Append-only журнал чувствительных действий (organization_id NULL→CASCADE, actor_user_id NULL→SET NULL, action, resource_type, resource_id, summary jsonb, ip_address, created_at). Системные авто-действия Celery — `actor_user_id = null`. Индексы `(organization_id, created_at DESC)`, `(actor_user_id)`, `(action)` |
| `File` | `files` | Чистый реестр блобов в S3 (storage_key UNIQUE, bucket, category enum-строка VARCHAR32, original_filename, content_type по реальному MIME, size_bytes, checksum_sha256 nullable, is_attached, organization_id NULL→CASCADE, owner_user_id→CASCADE, created/updated_at). Привязка — FK со стороны фичи-потребителя (НЕ полиморфизм). Индексы `category`, `organization_id`, `owner_user_id`, `(is_attached, created_at)` (очистка сирот). Enum `FileCategory`: checklist_photo/knowledge_base/avatar/other |
| `KnowledgeNode` | `knowledge_nodes` | Узел дерева базы знаний (org_id→CASCADE, parent_id self-ref nullable→CASCADE — бесконечная вложенность, kind section/page VARCHAR16, title, icon nullable, position int, all_members bool — тумблер «видно всем», content jsonb nullable — блоки только для page, schema_version smallint=1, created_by→users SET NULL, created/updated_at). Индексы `(organization_id)`, `(parent_id)`, `(organization_id, parent_id, position)`. Только организационный режим |
| `KnowledgeNodeAccess` | `knowledge_node_access` | ACL-правило на узле (node_id→CASCADE, subject_type role/member VARCHAR16, role_id nullable→organization_roles CASCADE, member_user_id nullable→users CASCADE, effect allow/deny VARCHAR8, created_at). CHECK целостности субъекта; частичные UNIQUE `(node_id, role_id) WHERE role` и `(node_id, member_user_id) WHERE member`; индекс `(node_id)` |
| `KnowledgeNodeFile` | `knowledge_node_files` | Реестр привязок файла к странице (node_id→CASCADE, file_id→`files.id` CASCADE **UNIQUE**, created_at, PK(node_id,file_id)). «FK от потребителя» как `checklist_item_photos`: пока строка есть — файл `is_attached=true`. `UNIQUE(file_id)` ⇒ один файл = одна страница |

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
| POST | `/api/v1/auth/oauth/google` | Вход/автолинк/регистрация через Google id_token | Нет (rate-limit `oauth_login_rate_limit`) |
| POST | `/api/v1/auth/oauth/apple` | Вход/автолинк/регистрация через Apple identity_token | Нет (rate-limit `oauth_login_rate_limit`) |
| GET | `/api/v1/auth/oauth/config` | Публичный конфиг для клиентов: какие провайдеры включены и их client_id (`?client_type=web\|ios\|android`) | Нет |
| GET | `/api/v1/admin/oauth-providers` | Список 5 комбинаций provider×client_type (заглушки для ненастроенных) | Bearer (super_admin) |
| PUT | `/api/v1/admin/oauth-providers/{provider}/{client_type}` | Upsert client_id/enabled одной комбинации | Bearer (super_admin) |
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
| DELETE | `/api/v1/organizations/{id}/locations/{loc_id}` | Удалить точку (каскадно снимает привязки чек-листов) | Bearer |
| GET | `/api/v1/organizations/{id}/locations/{loc_id}/checklist-templates` | Чек-листы точки (обратный срез; архивные включены с `is_archived=true`) | Bearer (owner/admin) |
| PUT | `/api/v1/organizations/{id}/locations/{loc_id}/checklist-templates` | Задать чек-листы точки (PUT, замена; та же таблица связей, что и `.../locations` выше) | Bearer (owner/admin) |
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
| PUT | `/api/v1/organizations/{id}/checklist-templates/{tpl_id}/locations` | Задать точки шаблона (PUT, замена); пустой список снимает все привязки | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/checklist-templates/{tpl_id}/assignments` | Кому назначен (+ `location_ids`) | Bearer (owner/admin) |
| PUT | `/api/v1/organizations/{id}/members/{user_id}/checklist-overrides` | Личные overrides bulk (PUT) | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/members/{user_id}/checklist-overrides` | Список личных overrides сотрудника | Bearer (owner/admin/self) |
| PUT | `/api/v1/organizations/{id}/checklist-templates/{tpl_id}/personal/{user_id}` | Upsert override (шаблон, сотрудник) | Bearer (owner/admin) |
| DELETE | `/api/v1/organizations/{id}/checklist-templates/{tpl_id}/personal/{user_id}` | Снять override (идемпотентно) | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/members/{user_id}/checklists` | Эффективные чек-листы (+ `location_ids`; опц. `work_location_id` — фильтр `matches_location`) | Bearer (owner/admin/self) |
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
| GET | `/api/v1/organizations/{id}/my-earnings` | Личный заработок за период + current_rate (+ penalty/net) | Bearer (member; owner → 403) |
| POST | `/api/v1/organizations/{id}/penalty-templates` | Создать шаблон штрафа | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/penalty-templates` | Список активных шаблонов | Bearer (owner/admin) |
| PATCH | `/api/v1/organizations/{id}/penalty-templates/{tid}` | Исправить шаблон (не меняет выданные штрафы) | Bearer (owner/admin) |
| DELETE | `/api/v1/organizations/{id}/penalty-templates/{tid}` | Удалить шаблон (soft-delete) | Bearer (owner/admin) |
| POST | `/api/v1/organizations/{id}/penalties` | Назначить штраф (снимок reason/amount из шаблона или кастом) | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/penalties` | Список штрафов (фильтры member_id/shift_id/период, пагинация) | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/penalties/{pid}` | Деталь штрафа (снятый → 404) | Bearer (owner/admin) |
| PATCH | `/api/v1/organizations/{id}/penalties/{pid}` | Исправить штраф (member_id неизменен) | Bearer (owner/admin) |
| DELETE | `/api/v1/organizations/{id}/penalties/{pid}` | Снять штраф (soft-delete; любой owner/admin) | Bearer (owner/admin) |
| GET | `/api/v1/organizations/{id}/my-penalties` | Мои штрафы за период | Bearer (member; owner → 403) |
| GET | `/api/v1/admin/users` | Список пользователей платформы (поиск, фильтры role/is_verified, sort, пагинация) | Bearer (super_admin) |
| GET | `/api/v1/admin/users/{user_id}` | Детали пользователя + агрегаты (owned/member orgs, shifts) | Bearer (super_admin) |
| PATCH | `/api/v1/admin/users/{user_id}/role` | Сменить глобальную роль (нельзя разжаловать себя) | Bearer (super_admin) |
| GET | `/api/v1/admin/organizations` | Обзор всех организаций (owner_email, member_count, фильтры) | Bearer (super_admin) |
| GET | `/api/v1/admin/stats` | Сводная статистика платформы (дашборд) | Bearer (super_admin) |
| GET | `/api/v1/organizations/{id}/audit-logs` | Лента аудита организации (фильтры action/actor/даты, пагинация, created_at DESC) | Bearer (owner/admin) |
| POST | `/api/v1/files` | Загрузка файла (multipart: file/category/organization_id); валидация размера и реального MIME, presigned GET в ответе | Bearer (право по category) |
| GET | `/api/v1/files/{file_id}` | Метаданные + свежий presigned GET URL (обновление протухшей ссылки) | Bearer (uploader/admin-owner/member по category) |
| DELETE | `/api/v1/files/{file_id}` | Удаление объекта + строки; привязанный → `FILE_IN_USE` (409), повтор → `FILE_NOT_FOUND` (404) | Bearer (uploader/admin-owner/super_admin) |
| POST | `/api/v1/organizations/{org_id}/knowledge/nodes` | Создать раздел/страницу (parent в org или корень) | Bearer (owner/admin/super_admin) |
| GET | `/api/v1/organizations/{org_id}/knowledge/nodes` | Дерево; owner/admin/super — всё, employee — по ACL (доступные узлы + разделы-предки) | Bearer (member/super_admin) |
| PUT | `/api/v1/organizations/{org_id}/knowledge/nodes/reorder` | Переупорядочить детей одного родителя (полный набор `ordered_ids`) | Bearer (owner/admin/super_admin) |
| GET | `/api/v1/organizations/{org_id}/knowledge/nodes/{node_id}` | Деталь + breadcrumbs; page — блоки с presigned; employee без allow → 404 | Bearer (member/super_admin) |
| PATCH | `/api/v1/organizations/{org_id}/knowledge/nodes/{node_id}` | Partial (title/icon/all_members/content/parent_id/position); пересчёт привязок файлов | Bearer (owner/admin/super_admin) |
| DELETE | `/api/v1/organizations/{org_id}/knowledge/nodes/{node_id}` | Удалить узел и поддерево; файлы поддерева — из S3/files до каскада | Bearer (owner/admin/super_admin) |
| GET | `/api/v1/organizations/{org_id}/knowledge/nodes/{node_id}/access` | Собственные ACL-правила узла + all_members | Bearer (owner/admin/super_admin) |
| PUT | `/api/v1/organizations/{org_id}/knowledge/nodes/{node_id}/access` | Bulk-замена набора правил + all_members (атомарно) | Bearer (owner/admin/super_admin) |

> **Файловое хранилище (file_storage).** Единый слой хранения поверх S3-совместимого storage (локально MinIO, в проде managed S3 — переезд = смена `S3_*` env, без правок кода). Загрузка идёт **через бэкенд** (multipart → API → storage), доступ к объектам **приватный** — клиент качает по **presigned GET URL** с коротким TTL (`S3_PRESIGN_EXPIRE_SECONDS`), который генерирует бэк. `files` — чистый реестр блобов (`File`); привязка к бизнес-сущности делается со стороны фичи-потребителя FK на `files.id` (НЕ полиморфизм) — целостность и `ON DELETE` каскады. Категория (`FileCategory`) задаёт префикс ключа, политику (лимит размера + разрешённые MIME, таблица `CATEGORY_POLICIES` в `services/file_storage.py`) и права: `checklist_photo` — любой member org; `knowledge_base` — admin/owner org; `avatar`/`other` — персональные. Реальный MIME определяется по сигнатуре содержимого (`filetype`), а не по заголовку multipart; имя в ключе — всегда UUID (anti path-traversal/коллизии), исходное имя хранится для `Content-Disposition`. **presigned + MinIO:** бэк ходит в storage по внутреннему `S3_ENDPOINT_URL`, но presigned-ссылка генерируется клиентом на публичном `S3_PUBLIC_ENDPOINT_URL` (подпись сразу от публичного хоста; в managed-S3 оба совпадают). Прямой стрим байтов через бэк не делаем. Жизненный цикл: строка создаётся с `is_attached=false`, потребитель ставит `true` при привязке; сироты (`is_attached=false`, старше `ORPHAN_FILE_TTL_HOURS`) подбирает Celery `cleanup_orphan_files`. Ошибки — `FileError` (`FILE_NOT_FOUND`/`FILE_TOO_LARGE`/`UNSUPPORTED_FILE_TYPE`/`INVALID_FILE_CATEGORY`/`FILE_IN_USE`/`STORAGE_UNAVAILABLE`).

> **Фото-подтверждения чек-листов (checklist_photos).** Пункт шаблона несёт `photo_requirement` (none/optional/required) и `photo_source` (camera/camera_or_gallery — подсказка UI, сервер источник **не** enforce-ит); при `requirement=none` source нормализуется к camera. На старте org-смены настройки копируются снимком в `checklist_instance_items` (правка шаблона на уже созданные экземпляры не влияет). Загрузка файла — через существующий `POST /api/v1/files` (категория `checklist_photo`); фича только **привязывает** загруженный `file_id` к пункту-экземпляру (`checklist_item_photos`, UNIQUE на `file_id`). Привязка/отвязка — только владелец активной смены, под `SELECT ... FOR UPDATE` на пункт (защита лимита `CHECKLIST_MAX_PHOTOS_PER_ITEM`); проблема с файлом-кандидатом (нет/чужой/другая org/не `checklist_photo`/уже привязан) → единый `PHOTO_FILE_INVALID`. Привязка ставит `files.is_attached=true`; отвязка снимает флаг и зовёт `delete_file` (объект S3 + строка `files`, связь уходит каскадом). Статус экземпляра считает **единая** функция `_recompute_instance_status` по критерию **satisfied** = `is_completed AND (photo_requirement != required OR photos_count >= 1)`; вызывается из PATCH пункта и привязки/отвязки фото, поэтому `finalize_shift_checklists` (и его sync-двойник) по-прежнему доверяют хранимому статусу. Режим **мягкий**: отметить пункт без фото и завершить смену можно — отсутствие обязательного фото лишь даёт `pending`→`incomplete` и `has_incomplete_required_checklists`. Деталь экземпляра отдаёт по каждому фото свежий presigned GET (батч-подпись одним клиентом, без N+1); при недоступности storage — `url=null` без 502 (клиент дотянет через `GET /files/{id}`). `items_summary` списка получил `satisfied_count` (честный прогресс) и `photos_required_missing` (бейдж «нужно фото») — одним GROUP BY. **Privacy-долг:** `cleanup_shift_photo_files` (в `services/checklist_instance.py`) удаляет файлы привязанных фото смены ДО каскадного сноса связей, НО прямого триггера нет — смены не hard-удаляются, org — soft-delete; `cleanup_orphan_files` не подбирает `is_attached=true`, поэтому при будущем hard-delete смены/org гео-привязанные фото останутся объектами S3 (массовая чистка бакета — отдельная DevOps-задача).

> **База знаний (knowledge_base).** Дерево разделов/страниц org с бесконечной вложенностью (`knowledge_nodes`, self-ref `parent_id`); контент страницы — массив блоков в JSONB (BLOCK SCHEMA `schema_version=1`: heading/paragraph/bulleted_list/numbered_list/quote/callout/divider/image/file/video(youtube)/table; `span` = inline rich-text). Только организационный режим. **ACL** (`knowledge_node_access`): правило `allow`/`deny` на роль или конкретного member; эффективный доступ employee к узлу считается обходом вверх по `parent_id` с приоритетом категорий — (1) персональное правило (member_user_id), (2) ролевое (role_id кастомной роли), (3) `all_members` на узле/предке, (4) deny по умолчанию; персональное всегда сильнее ролевого, внутри категории ближайший узел перебивает дальний (`services/knowledge._resolve_employee`, индекс ACL грузится одним проходом — без N+1). owner/admin/super_admin игнорируют ACL (полный доступ). Employee без эффективного allow видит узел как несуществующий (`404 KNOWLEDGE_NODE_NOT_FOUND`, не 403); дерево для employee отдаёт доступные узлы + разделы-предки как навигационные контейнеры (поле `all_members` опускается). **Файлы** грузятся через `POST /api/v1/files` (категория `knowledge_base`, admin/owner, 50 MB, image/*+pdf+OOXML docx/xlsx/pptx; OOXML — только без макросов: `vbaProject.bin`/`macroEnabled` в контейнере или битый архив → 415 `UNSUPPORTED_FILE_TYPE`; generic zip и легаси doc/xls/ppt запрещены) и привязываются к странице **через её `content`** (блоки image/file): на PATCH сервер диффит множества `file_id` в одной транзакции — новые валидирует (`knowledge_base`, та же org, не привязан к другой странице → иначе `400 KNOWLEDGE_FILE_INVALID`) и ставит `is_attached=true` + строку `knowledge_node_files` (`UNIQUE(file_id)` — один файл = одна страница, гонка двух страниц ловится конфликтом UNIQUE), исчезнувшие — `is_attached=false` + `delete_file` (объект S3 + строка `files`); добавления идут до удалений (необратимый S3-delete не теряется при откате). На чтении страницы блоки image/file обогащаются свежим presigned `url`/`url_expires_at` (батч-подпись, при сбое storage — `url=null` без 502; в хранимом `content` только `file_id`). Перемещение (`parent_id`) проверяет цикл подъёмом по предкам (`400 KNOWLEDGE_NODE_CYCLE`), чужой parent → `404`. **Privacy-критично:** `delete_node` (M5) собирает `file_id` всего поддерева и удаляет объекты S3 + строки `files` ДО каскадного `ON DELETE CASCADE` (иначе `cleanup_orphan_files` их не подберёт — они `is_attached=true`); при hard-delete org объекты S3 останутся — массовая чистка бакета вынесена в **общий DevOps-долг с `checklist_photos`**. Ошибки — `KnowledgeError` (`KNOWLEDGE_NODE_NOT_FOUND`/`KNOWLEDGE_NODE_CYCLE`/`KNOWLEDGE_FILE_INVALID`) + переиспользуемые `FORBIDDEN`/`ROLE_NOT_FOUND`/`MEMBER_NOT_FOUND`/`VALIDATION_ERROR`/`ORG_NOT_FOUND`.

> **Сквозной доступ super_admin.** Все org-эндпоинты (`members`, `settings`, `locations`, `roles`, `checklist-*`, `shifts`, `stats`) пускают `super_admin`, даже если он не состоит в организации. Проверки прав вынесены в `services/common.py` (`ensure_owner` / `ensure_member` / `ensure_admin_or_owner`), и в каждой добавлена ветка super_admin. `GET /shifts` и `GET /organizations/{id}/shifts` дополнительно принимают `sort` (`started_at`/`finished_at`) и `order` (`asc`/`desc`).

> **Видимость владельца смены (orgrouted enrichment).** `ShiftResponse` несёт 4 nullable-поля `user_name` / `user_email` / `role` / `custom_role_name` (`default=None`). Они вычисляются на чтении (`services/shift.build_org_shift_identities`: имя/почта из `users`, роль/кастомная роль из `organization_members` — два batch-запроса без N+1, `custom_role` через `selectinload`) и наполняются ТОЛЬКО в орг-контексте: `GET /organizations/{id}/shifts` (список) и `GET /organizations/{id}/shifts/{shift_id}` (деталь). В персональном `GET /shifts` остаются `null` (сериализатор `_shift_to_response` без `identity`). Исключённый из org сотрудник: имя/почта сохраняются, `role`/`custom_role_name` = `null`. Деталь чужой org-смены строго проверяет `shift.organization_id == org_id` → иначе `404 SHIFT_NOT_FOUND` (персональные/чужие смены не раскрываются). Схема БД не меняется — денормализация в `shifts` отвергнута.

> **Привязка точки к смене (`shift_work_location`).** При старте орг-смены фиксируется `shifts.work_location_id` по матрице двух настроек org (`geo_check_enabled` × `require_work_location`): **гео вкл** — точку определяет сервер (ближайшая по Haversine из зон, в радиус которых попал сотрудник; присланный клиентом `work_location_id` игнорируется; вне зон — прежний `403 GEO_CHECK_FAILED`); **гео выкл + require** — клиент обязан прислать `work_location_id` (нет → `422 WORK_LOCATION_REQUIRED`); **гео выкл + не require** — точка опциональна. Любой переданный id, не принадлежащий org (или с невалидным форматом), → `404 WORK_LOCATION_NOT_FOUND`. Точка ставится один раз на старте (паузы/возобновления её не меняют); логика — `services/shift._resolve_org_shift_start`. Персональные смены (`org_id=null`) точку не привязывают. `ShiftResponse` отдаёт additive nullable `work_location_id` + денормализованный `work_location {id, name, address}` (текущее значение точки, eager `selectinload(Shift.work_location)` во всех читающих смену запросах). Настройку `require_work_location` нельзя включить без точек (`409 WORK_LOCATION_REQUIRED_NO_LOCATIONS`); удаление последней точки гасит и `geo_check_enabled`, и `require_work_location` (симметрично). Помимо `OrganizationSettings*` (RBAC owner/admin) обе настройки `geo_check_enabled` и `require_work_location` **флэттятся в `OrganizationResponse` и `JoinResponse`** (`_org_to_response` / join, из `org.settings.* if org.settings else False`) — чтобы employee (без доступа к `/settings`) активировал обязательный выбор точки на клиенте. Разблокирует фильтр по точке в `payroll_detailed_report`.

> **Привязка чек-листов к рабочим точкам (`checklist_work_location`).** Шаблон
> чек-листа можно привязать к одной или нескольким точкам (`checklist_template_locations`,
> many-to-many, обе стороны `ON DELETE CASCADE`). Шаблон без привязок действует
> на любой точке (текущее поведение, не меняется); с привязками — только на них,
> причём фильтр `matches_location` применяется **единообразно ко всем каналам
> назначения**, включая `personal_add` (решение аналитика: точка сужает роль, а
> не дополняет её). Появился новый канал назначения: шаблон **без единой
> привязки к роли**, но с привязкой к точке, назначается **всем** сотрудникам
> организации (независимо от роли или её отсутствия) — реализовано как ещё
> один источник кандидатов внутри `_compute_effective` (`services/checklist_assignment.py`,
> `get_location_only_template_ids`), помечается тем же `source="role"` (новый
> enum-значение не вводилось). Фильтр по точке применяется **только** при
> создании экземпляров на старте смены (`services/checklist_instance.create_instances_for_shift`,
> по `shift.work_location_id`); смена без точки получает только шаблоны без
> привязок. Симметричные CRUD-эндпоинты пишут в одну таблицу с разных сторон:
> `PUT .../checklist-templates/{id}/locations` (со стороны шаблона) и
> `GET/PUT .../locations/{id}/checklist-templates` (со стороны точки, GET
> включает архивные шаблоны с `is_archived=true` — админ видит, что привязка
> существует). `GET .../members/{id}/checklists` получил additive `location_ids`
> на каждом элементе и опциональный query `work_location_id`: без параметра —
> весь `by_assignment` набор без фильтра (обратная совместимость), с параметром —
> ровно то, что сотрудник получил бы, открыв смену на этой точке (валидация
> принадлежности точки org → `404 WORK_LOCATION_NOT_FOUND`). Удаление точки
> каскадно снимает привязки; шаблон, оставшийся без привязок, снова действует
> везде (принято как есть, без запрета удаления). Миграция создаёт пустую
> таблицу без бэкфилла — нулевое изменение поведения на проде в момент выката.
> Ошибки — `ChecklistError` (`WORK_LOCATION_NOT_FOUND`/`INVALID_LOCATION`/`INVALID_TEMPLATE`,
> `TEMPLATE_NOT_FOUND` переиспользуется).

> **CORS.** Подключён `CORSMiddleware` (`main.py`), источники — из `Settings.cors_origins` (env `CORS_ORIGINS`, CSV; пусто = выключено). Нужен браузерным клиентам: админке `smenka_admin` и веб-версии мобилки (`flutter build web`). `allow_credentials=False` — клиенты шлют JWT в заголовке `Authorization`, не в cookie (переход на httpOnly-cookie + CSRF — отдельная задача); `allow_methods`/`allow_headers=["*"]`.

> **Ставки и зарплата (payroll).** История ставок — источник истины: строка `organization_member_rates` действует с `effective_from`, прошлые записи не перезаписываются (новая ставка «с даты» = POST новой строки; PATCH — только исправление опечаток). Для каждой завершённой смены берётся ставка с максимальным `effective_from <= shift.started_at`; смены без ставки → `unpaid_seconds`/`unpaid_shifts_count`/`has_missing_rate` и не входят в `gross_amount_minor`. Деньги — целые копейки: накопление точным Decimal, half-up до копейки ровно один раз на итог сотрудника; `totals.gross` = сумма округлённых итогов. Расчёты не кэшируются. `MemberResponse.current_rate` (additive nullable) — действующая ставка `max(effective_from) <= now`, заполняется **только** для owner/admin/super_admin (для employee всегда null — приватность зарплат). Owner != member (ADR-001): в payroll-отчёте не фигурирует, `my-earnings` отвечает ему 403. Ошибки: `MEMBER_NOT_FOUND`/`RATE_NOT_FOUND` (404), `RATE_EFFECTIVE_FROM_TAKEN` (409, дубль по `UNIQUE (member_id, effective_from)` — закрывает и гонку), `INVALID_DATE_RANGE` (400, согласовано с date_filters).

> **Детальный отчёт + экспорт (`payroll_detailed_report`).** Аддитивное расширение `GET …/payroll` (новых таблиц/полей нет). Опц. query: `granularity` (`none｜day｜week｜month`, default `none`), `user_ids`/`location_ids` (повтор или CSV uuid; `location_ids=none` — смены без точки, чужие точки → `422 VALIDATION_ERROR`), `tz` (IANA, нарезка корзин; битая → `422`), `only_missing_rate` (bool). При `granularity != none` к каждому `items[]` добавляется `breakdown[]` (корзины с ненулевым числом смен, `bucket_start` ASC) и top-level эхо `granularity`/`tz`; при `none` ответ **байт-в-байт** прежний (полей нет). **Округление — посуточное** только в детальном режиме/экспорте: атом = локальный день в `tz` (half-up один раз/день), корзины week/month и итог сотрудника = суммы округлённых дневных значений (день→корзина→сотрудник→totals — всё «бьётся»); режим `none` сохраняет старое единичное округление на сотрудника (ADR-002, расхождение ≤ нескольких копеек). Корзина по `started_at` в `tz`; фильтр периода — по UTC до нарезки. `GET …/payroll/export` отдаёт `.xlsx` (`openpyxl`, не конверт `{data,error}`; ошибки до файла — конвертом): лист «Сводка» (агрегат+ИТОГО), «Детализация» (сотрудник×корзина), часы/деньги числами для суммирования в Excel; `format` — enum (`xlsx`), default `granularity=day`. Логика — `services/payroll.get_org_payroll`/`export_org_payroll`/`_build_breakdown`/`_build_payroll_xlsx`.

> **Штрафы (`fines`).** Owner/admin назначает сотруднику штраф фиксированной суммой (копейки, `amount_minor > 0`, всегда RUB) — из **шаблона организации** (`organization_penalty_templates`) или кастомный. Паттерн «шаблон → снимок» как у checklist: `reason`/`amount_minor`/`currency` копируются в `penalties` на момент создания; частичное переопределение тела допускается (`reason` из шаблона, `amount` свой и наоборот); правка/soft-delete шаблона выданные штрафы не меняет (`template_id` → SET NULL при физ. удалении, снимок сохраняется). `occurred_at`: при `shift_id` по умолчанию `= shift.started_at`, иначе обязателен (`422 VALIDATION_ERROR`); по нему штраф попадает в период зарплаты (`[date_from, date_to]`, `date_to` включ., UTC). Снятие штрафа и удаление шаблона — **soft-delete** (`is_deleted=true`, у штрафа фиксируются `deleted_by_user_id`/`deleted_at`; снимает любой owner/admin, не только автор); все читающие запросы фильтруют `is_deleted=false`, повторная операция над снятым → `404 PENALTY_NOT_FOUND`/`PENALTY_TEMPLATE_NOT_FOUND`. Валидации при назначении: member принадлежит org (иначе `404 MEMBER_NOT_FOUND`; **owner != member** ⇒ оштрафовать owner нельзя), shift принадлежит этому member+org и `is_deleted=false` (иначе `404 SHIFT_NOT_FOUND`), шаблон активен и в org (иначе `404 PENALTY_TEMPLATE_NOT_FOUND`). **Интеграция в payroll/my-earnings/export — additive**: новый query `include_penalties` (bool, default `true`) у `GET …/payroll` и `…/payroll/export`; в каждый `items[]` и `totals` добавлены `penalty_amount_minor`/`penalties_count`/`net_amount_minor` (`net = gross − penalty`, **может быть < 0**, не обрезается). Штраф учитывается независимо от ставки (даже при `gross=0`); сотрудник только со штрафами (без завершённых смен) **всё равно попадает в `items`** (union по member→user), иначе штраф «потерялся бы»; при `include_penalties=false` penalty-поля = 0, `net=gross`, union не делается. `my-earnings` учитывает свои штрафы всегда (флага нет). В `.xlsx` добавлены колонки «Штраф, ₽»/«К выплате, ₽» («Сводка» — по сотруднику, «Детализация» — период-уровень, Штраф=0/К выплате=Начислено по корзине). Логика — `services/penalty.py` (+ агрегаты `aggregate_penalties_by_user`/`aggregate_member_penalties` для payroll). `shifts.is_deleted` — отдельное требование заказчика (soft-delete смен), фильтруется во всех читающих запросах по сменам; пишущего эндпоинта удаления смены в этой фиче нет (заготовка).

> **Фильтры диапазона дат (date_filters).** Оба stats-эндпоинта принимают ровно один источник окна: пресет `period` (`day`/`week`/`month`, поведение не менялось) ЛИБО кастомный диапазон `date_from`/`date_to` (включительно по `Shift.started_at`, допускается открытый диапазон). Ошибки: `MISSING_STATS_RANGE` → `AMBIGUOUS_STATS_RANGE` → `INVALID_PERIOD` → `INVALID_DATE_RANGE` (этот порядок). В ответах stats добавлены `range_from`/`range_to` (фактически применённое окно), `period` стал nullable. Списочные эндпоинты смен получили валидацию `INVALID_DATE_RANGE`. Все границы нормализуются `services/shift.ensure_utc` (naive → UTC, aware → приведение к UTC) и в фильтрах, и в эхо-полях.

> **Усиление безопасности (security_hardening).** Три класса защит. **(1) Rate-limit** (slowapi, `core/rate_limit.py`) на `register`/`verify`/`resend-code`/`login` — ключ = IP клиента (`utils/request.get_client_ip`, первый из `X-Forwarded-For` за Caddy). Хранилище счётчиков — Redis (`rate_limit_storage_uri`||`redis_url`; в тестах `memory://`), пороги из ENV (`*_RATE_LIMIT`). Превышение → `429 RATE_LIMIT_EXCEEDED` в конверте `{data,error}` + `Retry-After`. Лимитер выключаем флагом `rate_limit_enabled`. **(2) Счётчик попыток кода**: `verification_codes.attempts` атомарно (`UPDATE attempts = attempts+1`, commit до отдачи ошибки) растёт на каждый неверный `verify`; при `>= max_code_attempts` (5) код «сожжён» → `429 TOO_MANY_CODE_ATTEMPTS`, нужен `resend-code`. **(3) Блокировка аккаунта** (`services/lockout.py`, Redis-ключ `login_fail:{email}` с TTL = окно): после `max_login_failures` (10) неудач — `423 ACCOUNT_LOCKED` на `account_lockout_minutes` (15) независимо от верности пароля; успех сбрасывает счётчик. Блокировка по email одинаково для существующего/несуществующего email (без enumeration-оракула); per-IP rate-limit ловит распределённый перебор. **Sentry** (`core/sentry.py`) включается только при `SENTRY_DSN` (иначе no-op; dev/CI/тесты без сети), `send_default_pii=False`, `max_request_body_size="never"`. Глобальный `@app.exception_handler(Exception)` ловит необработанные 500: лог через structlog (repr) + `capture_exception`, клиенту — тот же конверт `{data:null, error:{code:"ERROR"}}` (доменные ошибки и `RequestValidationError` идут своими хендлерами и в Sentry не шлются). **Аудит** пишется в той же транзакции, что и действие (см. `services/audit.py`, ниже).

> **Аудит действий (audit_logs).** Запись создаётся из endpoint-слоя ПОСЛЕ успешного сервисного вызова и ДО `session.commit()` — один commit, аудит не расходится с фактом. Покрытие: `org.update/delete/invite_rotate`, `member.join/remove/role_update`, `settings.update`, `location.create/update/delete`, `shift.finish` (actor = инициатор, IP из запроса), а также системные `shift.auto_finish`/`pause.auto_finish` из Celery (`record_sync`, `actor_user_id = null`). `summary` — ключевые поля без секретов (инвайт-код и токены не пишутся). Чтение — только `GET /organizations/{id}/audit-logs` (owner/admin, `created_at DESC`, фильтры `action`/`actor_user_id`/`date_from`/`date_to` с `date_to` включительно, пагинация limit≤200); `actor_name` подмешивается batch-запросом по `users` (или «Система» при null-акторе). Записи неизменяемы и не удаляются через API.

> **Вход через Google/Apple (oauth_login).** Верификация id-токена — JWKS + RS256 (`services/oauth_tokens.py`, поверх `python-jose`+`httpx`, без новых зависимостей): подпись по `kid` из заголовка, `iss`/`aud`/`exp`; `aud` сверяется с `client_id`, настроенным в `oauth_provider_settings` для присланной пары provider×client_type (не ENV — редактирует только super_admin через `PUT /admin/oauth-providers/...`). JWKS кэшируется in-memory с TTL 1ч; при неизвестном `kid` кэш **форсированно рефетчится один раз** перед отказом (иначе легитимные токены, подписанные только что ротированным ключом провайдера, отклонялись бы до истечения TTL). Единая логика поиска/создания пользователя (`services/oauth.py._link_or_register_user`, три ветки): (1) есть `oauth_identities` по `(provider, sub)` → вход; (2) иначе поиск `users` по `lower(email) = lower(token_email)` **из проверенного id-токена** — один совпавший → автолинк без доп. подтверждения (`is_verified` выставляется `true`), больше одного → `500 OAUTH_LINK_AMBIGUOUS` (легаси case-дубли), ноль → регистрация нового `User(password_hash=null, is_verified=true)`. Токены выдаются тем же механизмом, что и `/auth/login` (`create_access_token` + `_create_refresh_token_db`). Для Apple, у которой email/name приходят от провайдера только при самой первой авторизации, тело запроса `email`/`name` используется **только** как fallback-имя нового аккаунта — identity-матчинг (поиск/линковка) всегда идёт по `claims.email` из подписанного токена, никогда по значению из тела запроса (в первой версии реализации была уязвимость: непроверенный body-email использовался для автолинка, что давало захват чужого аккаунта при повторном Apple-входе без email в токене — поймано и исправлено на ревью до мерджа). `GET /auth/oauth/config` отдаёт фронтам `{google, apple}` (`client_id`+`enabled` либо `null`) — фронты обязаны скрывать кнопку при `null`/`enabled=false`; для Apple на Android читается конфигурация `(apple, web)` (нативного Apple SDK на Android нет). Ошибки — переиспользуют `AuthError` (`INVALID_OAUTH_TOKEN`/`OAUTH_EMAIL_NOT_VERIFIED`/`OAUTH_PROVIDER_UNAVAILABLE`/`OAUTH_LINK_AMBIGUOUS`/`OAUTH_PROVIDER_NOT_CONFIGURED`) и `AdminError` (`VALIDATION_ERROR` — недопустимая комбинация provider/client_type). **Вне scope:** отвязка провайдера, server-side revoke через Apple/Google token endpoints, эндпоинт-редирект для Apple-входа на Android (`apple/android-callback`) — обнаружен мобильным треком как отдельная потребность, не описан в ТЗ, требует отдельного решения аналитика.

> **Отправка кодов подтверждения по email (smtp_email).** Код верификации доставляется письмом через SMTP (`services/email.py`, транспорт `aiosmtplib` — async, не блокирует event loop). Флаг включения — непустой `SMTP_HOST`: **выключен** (dev/CI/тесты) — письмо не шлётся, код как раньше возвращается в ответе `register`/`resend-code` и пишется в лог (`verification_code_generated`/`_resent`); **включён** (прод) — код уходит ТОЛЬКО письмом, в ответе `verification_code=null`, в логи код не пишется (`auth._log_code` опускает поле `code` при `smtp_enabled`). Поле `verification_code` в схемах остаётся **nullable** (обратная совместимость со старыми мобильными билдами — не удаляется). Порт 465 → implicit SSL (`use_tls`), 587 → STARTTLS (`start_tls`), выбор по `SMTP_USE_SSL`; `From == SMTP_USERNAME` (требование Яндекса). Доставка вызывается из endpoint-слоя **после `session.commit()`**: пользователь/код уже сохранены, поэтому сбой SMTP не теряет регистрацию — `email.deliver_verification_code` ловит `SMTPException`/`OSError`, логирует (без кода) и поднимает `AuthError("EMAIL_SEND_FAILED", 502)`; пользователь повторяет через `resend-code`. Env: `SMTP_HOST`/`SMTP_PORT`/`SMTP_USE_SSL`/`SMTP_USERNAME`/`SMTP_PASSWORD`/`SMTP_FROM`/`SMTP_FROM_NAME` (+ `SMTP_TIMEOUT_SECONDS`).

---

## Сервисы

| Файл | Описание |
|------|----------|
| `services/auth.py` | Регистрация, верификация, логин, refresh, logout |
| `services/oauth_tokens.py` | Верификация id-токенов Google/Apple: JWKS-fetch с TTL-кэшем (форс-рефетч при неизвестном kid), RS256-decode, проверка iss/aud/exp/email_verified → `OAuthClaims` |
| `services/oauth_provider_settings.py` | CRUD/чтение `oauth_provider_settings` (5 валидных комбинаций provider×client_type), `require_provider_setting` (kill-switch/не настроено → `OAUTH_PROVIDER_NOT_CONFIGURED`) |
| `services/oauth.py` | Бизнес-логика входа: поиск по `sub`/автолинк по email/регистрация, выдача токенов, `get_oauth_config` для публичного эндпоинта |
| `services/shift.py` | Lifecycle смен, статистика, автозавершение |
| `services/organization.py` | CRUD организаций, инвайты, участники |
| `services/work_location.py` | CRUD рабочих точек |
| `services/organization_settings.py` | CRUD настроек организации |
| `services/organization_role.py` | Кастомные роли организации и их назначение members (`RoleError`) |
| `services/checklist_template.py` | Шаблоны чек-листов, пункты (+`photo_requirement`/`photo_source`), reorder, архивация (`ChecklistError`) |
| `services/checklist_assignment.py` | Назначение шаблонов ролям, личные overrides (bulk PUT), вычисление эффективных шаблонов (`_compute_effective` — роль ∪ канал «нет ролей + есть точки» ∪ personal_add − personal_remove, опц. фильтр по `work_location_id`) |
| `services/checklist_location.py` | Привязка шаблонов к точкам (`checklist_template_locations`): симметричные `set_template_locations`/`set_location_templates`, `get_location_only_template_ids` (новый канал назначения), `matches_location`/`get_location_ids_for_templates` (переиспользуются assignment/instance) |
| `services/checklist_override.py` | Гранулярный upsert/delete/list личных overrides (ON CONFLICT DO UPDATE) |
| `services/checklist_instance.py` | Создание снимков в смене, заполнение пунктов, привязка/отвязка фото, единый пересчёт «satisfied» (`_recompute_instance_status`), finalize, `cleanup_shift_photo_files` |
| `services/common.py` | Общие guard-функции org-доступа (`ensure_owner/ensure_member/ensure_admin_or_owner`) со сквозной веткой super_admin (`AccessError`) |
| `services/payroll.py` | История ставок участников (CRUD, `PayrollError`), действующие ставки batch-запросом (DISTINCT ON), расчёт payroll/my-earnings «на лету» (+штрафы: `include_penalties`, `net`), суточная разбивка (`_build_breakdown`, посуточное округление) + экспорт `.xlsx` (`export_org_payroll`/`_build_payroll_xlsx`) |
| `services/penalty.py` | Штрафы и шаблоны (`PenaltyError`): CRUD шаблонов, назначение со снимком из шаблона/кастом, список/деталь/правка/снятие (soft-delete), `my-penalties`, агрегаты для payroll (`aggregate_penalties_by_user`/`aggregate_member_penalties`) |
| `services/knowledge.py` | База знаний (`KnowledgeError`): дерево узлов (M1–M6), ACL-резолюция employee с приоритетом категорий (`_resolve_employee`, индекс ACL без N+1), фильтрация дерева, привязка/отвязка файлов страницы через `content` (diff + `delete_file`), чистка файлов поддерева до каскада, замена ACL (A1/A2) |
| `services/admin.py` | Платформенные операции super_admin: список/детали пользователей, смена роли, обзор организаций, статистика (`AdminError`) |
| `services/audit.py` | Запись аудита (`record` async / `record_sync` для Celery, в той же транзакции) и чтение ленты организации с именами инициаторов |
| `services/lockout.py` | Блокировка аккаунта по неудачным логинам (Redis-счётчик с TTL, по email) |
| `services/email.py` | Отправка кодов подтверждения по SMTP (`aiosmtplib`): флаг по `SMTP_HOST`, выбор SSL/STARTTLS, ошибка → `AuthError("EMAIL_SEND_FAILED")` (`smtp_email`) |
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
- **SMTP (Яндекс)** — отправка кодов подтверждения по email (включается при `SMTP_HOST`; `services/email.py`, `aiosmtplib`)

---

## Ключевые решения

См. `docs/decisions/` для полных ADR.
