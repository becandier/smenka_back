# Roadmap — Smenka Backend

Этот файл — источник правды о том, что сделано и что предстоит. Каждый агент обновляет статусы после завершения работы.

Статусы: `[ ]` не начато, `[~]` в работе, `[x]` готово

---

## Фича — Окно дозаполнения чек-листа после закрытия смены (`checklist_grace_period`) `[~]`
ТЗ: `../../docs/tasks/checklist_grace_period/backend.md`  STATUS: `../../docs/tasks/checklist_grace_period/STATUS.md`  ADR: `docs/decisions/004-checklist-grace-period-deferred-finalization.md`
- [x] `OrganizationSettings.checklist_grace_minutes` (int, 0–240, default/server_default `30`) — сколько минут после `finished_at` разрешено дозаполнять чек-листы завершённой смены; `0` = прежнее поведение (`SHIFT_FINISHED` сразу). Миграция `5856058d23ab` (колонка), обратима. Частичный индекс `ix_checklist_instances_pending_required` вынесен в отдельную non-transactional миграцию `d43e672a012a` (`CREATE INDEX CONCURRENTLY` — таблица растёт на каждый старт org-смены, блокирующий `CREATE INDEX` внутри общей транзакции на живом проде неприемлем)
- [x] `services/checklist_instance.py`: `compute_fill_window`/`get_checklist_grace_minutes` — чистая логика окна; `_assert_fill_window_open` (гейт) и `_reassert_fill_window_open` (повторная проверка прямо перед мутацией — аналог `_reassert_shift_active`, но по границе окна) заменяют прежний терминальный `_assert_shift_active` в `update_instance_item`/`attach_photo`/`detach_photo`. Окно закрыто по ИЛИ времени, ИЛИ уже случившейся терминальной финализации (`_shift_checklists_finalized`, `already_finalized` в `compute_fill_window`) — иначе задним числом раскатанный/увеличенный `checklist_grace_minutes` «воскрешал» бы уже зафиксированный `incomplete` (правка после финального ревью, см. ADR-004 «Инвариант»)
- [x] Отложенная финализация: `close_shift_checklists`/`_close_shift_checklists_sync` на финише решают — немедленный терминальный `finalize_shift_checklists` (`grace=0`) или живой снимок `_has_live_incomplete_required` (`grace>0`, экземпляры остаются `pending`); `_refresh_live_incomplete_flag` пересчитывает `Shift.has_incomplete_required_checklists` при каждой правке пункта/фото завершённой смены в течение окна
- [x] Celery Beat `finalize_expired_checklist_grace_periods` (60 сек, как `auto_finish_stale_shifts`) — терминальная фиксация `pending → incomplete` по истечении окна; кандидаты через новый частичный индекс `ix_checklist_instances_pending_required`
- [x] Окно распространяется на авто-завершённые смены (`auto_finish_stale_shifts`, inline auto-finish по графику) — не только на ручной `finish_shift`
- [x] `GET .../checklists` и `.../checklists/{instance_id}` — аддитивные `fill_allowed`/`fill_deadline_at` (null для активной смены и для закрытого окна); `GET`/`PATCH /organizations/{id}/settings` читают/пишут `checklist_grace_minutes`
- [x] Формулировка `SHIFT_FINISHED` уточнена: «время на дозаполнение чек-листа истекло» вместо «нельзя редактировать завершённую смену»; новых кодов ошибок нет
- [x] Дополнение к ТЗ («Длительность окна — рядовому сотруднику»): `checklist_grace_minutes` дополнительно денормализован (nullable, additive) в `OrganizationResponse` (`GET /organizations`/`{id}`/`all`, `_org_to_response`) — тот же паттерн, что и `overtime_request_days`, чтобы employee (без доступа к `/settings`) знал длительность окна для диалога завершения смены
- [x] `docs/openapi.json` обновлён: затронутые фичей компоненты (`ChecklistInstanceListResponse`/`ChecklistInstanceDetailResponse`/`OrganizationSettingsUpdate`) — точечно; заодно закрыт накопившийся до этой фичи долг синхронизации спеки (`require_work_location`, `auto_finish_by_schedule`, `require_schedule`, `late_tolerance_minutes`, `overtime_request_days`, `early_start_minutes` — поля добавлены задолго до `checklist_grace_period`, но не попадали в спеку; диф это исправил попутно). `OrganizationResponse`/`OrganizationSettingsResponse` в спеке не участвуют вовсе (эндпоинты возвращают их без типизированного `response_model`) — патчить нечего
- [x] 26 новых/переработанных теста (`test_checklist_instances.py`, `test_checklist_photos.py`, `test_organization_settings.py`, `test_tasks.py`): окно открыто/закрыто/отключено (`grace=0`), авто-финиш (inline + Celery) открывает окно, `has_incomplete_required_checklists` сходится к `false` после дозаполнения, финализация по истечении окна (Celery-задача, 4 сценария) + идемпотентность повторного прогона, граница окна перед мутацией (реассерт ловит гонку), инвариант «`incomplete` не воскресает» — увеличение `checklist_grace_minutes` после финализации не открывает окно заново, гонка мутации с Celery-задачей на уровне `_recompute_instance_status`, `fill_allowed`/`fill_deadline_at` в трёх состояниях, привязка/отвязка фото в окне, границы `checklist_grace_minutes` (0/240/вне диапазона/нецелое), `checklist_grace_minutes` в `OrganizationResponse` (default/обновление/read-only/без строки настроек)
- [ ] Мердж в `main` — за оркестратором (см. `STATUS.md`)

---

## Фича — Таймзона организации в контракте смены (`shift_timezone_display`) `[x]`
ТЗ: `../../docs/tasks/shift_timezone_display/backend.md`  STATUS: `../../docs/tasks/shift_timezone_display/STATUS.md`
- [x] `ShiftResponse.organization_timezone` — nullable IANA-контекст: организация получает текущую зону, персональная смена — `null`
- [x] Все ответные пути смены: mixed list/self detail/lifecycle, org list/detail, manual mutations и смена графика
- [x] Mixed list загружает зоны организаций одним batch-запросом; UTC timestamps и схема БД не менялись
- [x] Контрактные тесты: орг-контекст, персональный `null`, lifecycle и сохранение UTC-момента
- [ ] Мердж в `main` — за оркестратором (см. `STATUS.md`)

---

## Фича — Старт без графика при отключённом требовании (`optional_schedule_start`) `[x]`
ТЗ: `../../docs/tasks/optional_schedule_start/backend.md`  STATUS: `../../docs/tasks/optional_schedule_start/STATUS.md`
- [x] Явный доступный, но закрытый по времени график при `require_schedule=false` отбрасывается; смена создаётся без снимка графика
- [x] Строгий режим `require_schedule=true`, проверки существования/доступности и автоподбор без явного id не ослаблены
- [x] Структурированное событие `optional_schedule_fallback` с причиной `window_closed_optional_schedule`
- [x] Регрессионные сервисный и API-тесты; миграции и новые коды ошибок не требуются
- [ ] Мердж в `main` — за оркестратором (см. `STATUS.md`)

---

## Фича — Старт смены по фото при недоступной геолокации (`shift_geo_photo_fallback`) `[~]`
ТЗ: `../../docs/tasks/shift_geo_photo_fallback/backend.md`  STATUS: `../../docs/tasks/shift_geo_photo_fallback/STATUS.md`
- [x] Категория файла `FileCategory.shift_geo_photo` + политика (`shift-geo-photos/`, 10 МБ, image-MIME, org-scoped): загрузка — любой участник org, чтение — владелец/admin/owner org/super_admin. Схема категорий не мигрируется — `files.category` не native PG enum, а VARCHAR(32) без CHECK
- [x] `shifts` +2 nullable-колонки: `geo_fallback_photo_file_id` (FK→`files.id` ON DELETE SET NULL) и `geo_fallback_reason` (VARCHAR(40), enum `GeoFallbackReason` — 6 машинных кодов клиента). Инвариант «стартовала без геопроверки» ⇔ `geo_fallback_reason IS NOT NULL`
- [x] `POST /shifts/start` +пара `geo_fallback_photo_id`/`geo_fallback_reason` (только вместе; несовместимы с координатами; только org с `geo_check_enabled`; `work_location_id` обязателен и валидируется на org); персональная смена и org без геопроверки → `422 VALIDATION_ERROR`, ни координат ни фото → прежний `400 COORDS_REQUIRED`
- [x] Новый код `422 GEO_FALLBACK_PHOTO_INVALID` (нет файла/битый id/чужой/другая org/не та категория/уже привязан) — в `../../docs/ERROR_FORMAT.md`
- [x] Захват фото (`is_attached=true`) в одной транзакции со вставкой смены под `SELECT ... FOR UPDATE` — два параллельных старта с одним снимком не дадут две смены; любой отказ старта оставляет файл непривязанным (подберёт чистка сирот)
- [x] `ShiftResponse` +3 additive-поля (`geo_fallback`/`geo_fallback_reason`/`geo_fallback_photo_file_id`) во всех местах выдачи смены; `GET /organizations/{id}/shifts` +query `geo_fallback` (true/false/не передан)
- [x] Настроек организации фича не добавляет (фолбэк включён всегда); аудита старта смены в проекте нет — дополнять нечего, факт фолбэка попал в structlog `shift_started`
- [x] Миграция `ac0ce20ba98d`, обратима — проверено `upgrade`/`downgrade` локально
- [x] 23 теста (`tests/test_shift_geo_photo_fallback.py`): happy path + `is_attached`, права на категорию (участник грузит, коллега не читает, owner читает), пара полей (одно без другого), фото+координаты, невалидный reason, точка (обязательна/чужая), org без гео, персональная смена, невалидное фото (нет/битый id/чужой владелец/другая org/чужая категория/уже привязан/повторное использование), фильтр `geo_fallback` в реестре, обратная совместимость обычной и персональной смены
- [ ] Мердж в `main` — за оркестратором (см. `STATUS.md`)

---

## Фича — Ручной учёт времени и начисления (`manual_time_entry`) `[~]`
ТЗ: `../../docs/tasks/manual_time_entry/backend.md`  STATUS: `../../docs/tasks/manual_time_entry/STATUS.md`
- [x] `shifts` — 6 nullable-колонок ручного ввода (`created_by_user_id`/`edited_by_user_id`/`edited_at`/`manual_note`/`deleted_by_user_id`/`deleted_at`, FK→`users.id` ON DELETE SET NULL); задействована существующая заготовка `is_deleted` (`fines`)
- [x] Новая таблица `payroll_adjustments` (знаковое ручное начисление/удержание, `amount_minor != 0` — CHECK), без шаблонов, симметрично `penalties`
- [x] Ручные смены (`services/manual_shift.py`, `api/v1/manual_shifts.py`): `POST/PATCH/DELETE .../shifts[/{id}]` + `POST .../shifts/{id}/restore` — создание задним числом, правка (в т.ч. завершение зависшей active/paused через `finished_at`), soft-delete, восстановление (идемпотентно для уже неудалённой); валидации интервала (≤48ч, не в будущем)/пересечений (R2, `SHIFT_OVERLAP`)/пауз (R3)
- [x] Ручные начисления (`services/adjustment.py`, `api/v1/adjustments.py`): `POST/GET/PATCH/DELETE .../adjustments[/{id}]` + `GET .../my-adjustments`
- [x] Права: только `org_owner`/`org_admin`, **super_admin намеренно исключён** (`services/common.ensure_admin_or_owner(..., allow_super_admin=False)` — единственная фича с явным исключением сквозного платформенного доступа)
- [x] Прозрачность для сотрудника (R7): каждая операция пишет `audit_logs` (7 новых `AuditAction` + `AuditResource.adjustment`) и уведомление (`NotificationType.shift_manual_changed`/`payroll_adjustment_changed`) в той же транзакции
- [x] `ShiftResponse` +7 additive-полей (`is_manual`/`is_edited`/`manual_note`/`edited_at`/`is_deleted` — везде, включая персональный `GET /shifts`; `edited_by_name`/`created_by_name` — только орг-контекст, `build_manual_actor_names` без N+1); `GET .../shifts` +`include_deleted`/`only_manual` (A5)
- [x] Payroll — additive `adjustment_amount_minor`/`adjustments_count`, `net = gross − penalty + adjustment`, query `include_adjustments` (default true), колонка в XLSX
- [x] Побочный фикс моделей: `Shift.user`/`User.shifts` relationship потребовал явный `foreign_keys` (3 новых FK shifts→users сделали связь неоднозначной для SQLAlchemy)
- [x] Миграция `c6d7e8f90013`, обратима — проверено `upgrade`/`downgrade` локально
- [x] 69 тестов (`tests/test_manual_shifts.py`, `tests/test_adjustments.py`): пересечения (вкл. касание границами и активного соседа), завершение через PATCH, валидации пауз, создание на owner (404), employee/super_admin (403), payroll с ручной сменой + знаковыми начислениями (плюс/минус, вместе со штрафом), сотрудник только с начислением в items, soft-delete → исчезновение из payroll/списков/реестра чек-листов, restore с конфликтом
- [ ] Мердж в `main` — за оркестратором (см. `STATUS.md`)

---

## Фича — Тестирование сотрудников (`employee_tests`) `[~]`
ТЗ: `../../docs/tasks/employee_tests/backend.md`  STATUS: `../../docs/tasks/employee_tests/STATUS.md`
- [x] 7 таблиц (`test_templates`→`test_questions`→`test_question_options`, `test_assignments`, `test_attempts`+`test_attempt_questions`+`test_attempt_options`) — паттерн «шаблон → снимок», как у чек-листов
- [x] Инварианты шаблона (`services/employee_test.validate_template_payload`, единый код `422 TEST_TEMPLATE_INVALID`): ≥1 вопрос, у каждого ≥2 варианта, `single_choice` — ровно один верный, `multiple_choice` — минимум один, `points≥1`, `0≤pass_threshold_percent≤100`, `max_attempts≥1`
- [x] CRUD шаблонов + `POST .../test-templates/validate` (сухая проверка тела — тот же формат, что и импорт из `import-format.md`), архивация
- [x] Назначение — upsert по `UNIQUE(template_id, member_id)` (повторное обновляет только `due_at`, не сбрасывает результаты) + уведомление `test_assigned` в той же транзакции (зависимость от фичи `notifications`)
- [x] Прохождение сотрудником: старт попытки (guard'ы `TEST_TEMPLATE_ARCHIVED`/`TEST_ALREADY_PASSED`/`TEST_ATTEMPTS_EXHAUSTED`, идемпотентный возврат уже открытой попытки, `SELECT ... FOR UPDATE` от гонки параллельного старта), сдача (`TEST_ATTEMPT_ALREADY_SUBMITTED` на повтор)
- [x] Снимок порога зачёта в попытке (`test_attempts.pass_threshold_percent`, ADR-003) — правка порога в шаблоне после сдачи не двигает `passed` уже сданной попытки
- [x] Грейдинг all-or-nothing (`compute_awarded`) + денормализация `test_assignments` (`attempts_used`/`best_percent`/`passed` sticky/`status`) после каждой сдачи
- [x] Реестр для админки (`GET .../test-templates/{id}/assignments`, `GET .../test-assignments`) + `TestAssignmentOut.last_attempt_id` — контракт-фикс аналитика: без него admin-трек не мог открыть разбор попытки из реестра (пробел зафиксирован в `STATUS.md` на ревью admin-трека)
- [x] `GET .../test-attempts/{id}` — разбор попытки для админа (вопросы-снимки, выбор сотрудника, верные ответы, баллы)
- [x] Миграция `9632b96364fd` (зависит от `6686fd74797c` — notifications), обратима — проверено `upgrade`/`downgrade` локально
- [x] 51 тест (`tests/test_employee_tests.py`): инварианты шаблона, импорт/валидация, назначение+уведомление+upsert, старт/лимит попыток/`already_passed`/`in_progress`, грейдинг single/multiple all-or-nothing+порог, денорм статусов, снимок не зависит от правки шаблона, изоляция org/member/user, `last_attempt_id` (null до сдачи, указывает на последнюю сданную попытку при нескольких)
- [ ] Мердж в `main` — за оркестратором (см. `STATUS.md`)

---

## Фича — Центр уведомлений (`notifications`) `[~]`
ТЗ: `../../docs/tasks/notifications/backend.md`  STATUS: `../../docs/tasks/notifications/STATUS.md`
- [x] Таблица `notifications` (получатель — `User`, не `OrganizationMember`; `organization_id` nullable — контекст события; `type` VARCHAR(48) без CHECK — новый тип будущей фичи не требует миграции схемы), индексы `(user_id, created_at DESC)`/`(user_id, is_read)`
- [x] Сервисный слой без публичного эндпоинта создания — `create_notification`/`bulk_create_notifications` (без `commit`, вызывается производителем события в его транзакции)
- [x] `GET /notifications` (лента, `unread=true`), `GET /notifications/unread-count`, `POST /notifications/{id}/read` (идемпотентно), `POST /notifications/read-all`
- [x] Изоляция по `user_id`: чужое уведомление → `404 NOTIFICATION_NOT_FOUND` (существование не раскрывается)
- [x] Миграция `6686fd74797c`, обратима — проверено `upgrade`/`downgrade` локально
- [x] 15 тестов (`tests/test_notifications.py`): лента (сортировка/пагинация/unread-фильтр/изоляция/payload roundtrip), счётчик, read (идемпотентность/404/чужое), read-all (только свои непрочитанные)
- [ ] Мердж в `main` — за оркестратором (см. `STATUS.md`)

---

## Фича — Имя участника в организации (`member_display_name`) `[~]`
ТЗ: `../../docs/tasks/member_display_name/backend.md`  STATUS: `../../docs/tasks/member_display_name/STATUS.md`
- [x] Колонка `organization_members.display_name` (VARCHAR(100) NULL, без уникальности/индекса) — имя участника внутри ЭТОЙ организации, не трогает `users.name`; owner не member (ADR-001) ⇒ поля у владельца нет
- [x] Нормализация на сервере (`services/organization.normalize_display_name`): trim + `\n`/`\r`/`\t`→пробел, пусто/пробелы→`NULL` (сброс), 1–100 символов иначе `400 INVALID_DISPLAY_NAME`, управляющие символы отклоняются
- [x] `PATCH /organizations/{org_id}/members/{member_user_id}` (`{"display_name": "..." | null}`) — owner/admin/super_admin (`ensure_admin_or_owner`); employee, включая переименование самого себя — `403`; чужой участник — `404 MEMBER_NOT_FOUND`; аудит `member.display_name_update` (старое/новое значение)
- [x] Additive nullable `display_name` рядом с `user_name` (не переименован, не удалён — обратная совместимость мобильных билдов) в `MemberResponse`, `ShiftResponse` (орг-контекст), `OrgChecklistInstanceResponse`, `PenaltyResponse`, `PayrollItemResponse` (обе проекции), `EmployeeStatsResponse` — везде тем же запросом/join, без N+1
- [x] Миграция `352f7e3148af` (голова `a4b5c6d7e803` → `352f7e3148af`): только `ADD COLUMN`, без бэкфилла, обратима
- [x] 21 тест (`tests/test_member_display_name.py`): установка/сброс (null/пустая строка/пробелы), обрезка и схлопывание пробелов, границы 100/101 символ, права (owner/admin/super_admin — да; employee на себе и на других — 403; чужая org — 403; несуществующий участник — 404), два одинаковых `display_name`, аудит-запись, регресс поля (и что `user_name` не подменяется) во всех перечисленных схемах
- [ ] Мердж в `main` — за оркестратором (см. `STATUS.md`)

---

## Фича — Управленческие отчёты по чек-листам (`checklist_reports`) `[x]`
ТЗ: `../../docs/tasks/checklist_reports/backend.md`  STATUS: `../../docs/tasks/checklist_reports/STATUS.md`
- [x] Query-параметр `checklists` (`none`/`all_completed`/`has_incomplete`/`required_incomplete`) в `GET /organizations/{id}/shifts` — считается на лету по `checklist_instances` (флаг `has_incomplete_required_checklists` для этого не годится — проставляется только на завершении смены); `400 INVALID_CHECKLIST_FILTER` на неизвестное значение; комбинируется с `user_id`/`status`/`date_from`/`date_to`, `total`/пагинация учитывают фильтр
- [x] Additive nullable `checklists_summary` (`total`/`completed`/`required_total`/`required_incomplete`) в `ShiftResponse` — заполняется ТОЛЬКО в орг-эндпоинтах списка/детали смены одним GROUP BY на страницу (`get_checklists_summary_for_shifts`, без N+1); в персональных эндпоинтах остаётся `null`
- [x] Новый реестр `GET /organizations/{id}/checklist-instances`: фильтры `user_id`/`template_id`/`type`/`status`/`state`/`is_required`/`work_location_id`/`date_from`/`date_to`, сортировка (`shift_started_at`/`completed_at`/`created_at`), пагинация; только owner/admin; персональные смены и `is_deleted=true` исключены структурно; `template_id=null` (удалённый шаблон) не ломает выдачу
- [x] Миграция `a4b5c6d7e803`: индексы `ix_checklist_instances_template_id` и составной `ix_checklist_instances_shift_id_status` (только индексы, без бэкфилла), обратима
- [x] 29 тестов (`tests/test_checklist_reports.py`): фильтр `checklists` (4 значения + комбинации + total), регресс на активную смену с необязательным флагом, `checklists_summary` в орг/персональном контексте, реестр (пагинация, все фильтры по отдельности, сортировки, 403, изоляция чужой org, персональные смены исключены, `template_id=null`)

---

## Фича — Привязка чек-листов к рабочим точкам (`checklist_work_location`) `[x]`
ТЗ: `../../docs/tasks/checklist_work_location/backend.md`  STATUS: `../../docs/tasks/checklist_work_location/STATUS.md`
- [x] Модель `ChecklistTemplateLocation` (`checklist_template_locations`): many-to-many шаблон↔точка, обе стороны `ON DELETE CASCADE`, `UNIQUE(template_id, work_location_id)`
- [x] `services/checklist_location.py`: симметричные `set_template_locations`/`set_location_templates` со стороны шаблона и точки, `get_location_only_template_ids` (новый канал «нет ролей + есть точки»), `matches_location`/`get_location_ids_for_templates`
- [x] `_compute_effective` (checklist_assignment.py) расширен третьим источником кандидатов (location-only), фильтр по точке применяется единообразно ко всем каналам, включая `personal_add`
- [x] `create_instances_for_shift` (checklist_instance.py) фильтрует эффективный набор по `shift.work_location_id` перед созданием снимков
- [x] `PUT /organizations/{id}/checklist-templates/{tpl_id}/locations` + additive `location_ids` в `GET .../assignments`
- [x] `GET/PUT /organizations/{id}/locations/{loc_id}/checklist-templates` (обратный срез, архивные включены с `is_archived=true`)
- [x] `GET /organizations/{id}/members/{user_id}/checklists` — additive `location_ids` + опц. `work_location_id` (обратная совместимость: без параметра — без фильтра)
- [x] Миграция `82e9e9625926` (create table, без бэкфилла — нулевое изменение поведения на проде), обратима
- [x] 21 тест (`tests/test_checklist_locations.py`): полная матрица создания экземпляров (9), API-права/валидация/идемпотентность/каскад (7), фильтр эффективных чек-листов (2) + доп. проверки симметрии эндпоинтов и additive-полей

---

## Фича — Файловое хранилище (`file_storage`) `[x]`
ТЗ: `../docs/tasks/file_storage/backend.md`
- [x] Модель `File` (`files`): реестр блобов (storage_key UNIQUE, bucket, category, original_filename, content_type, size_bytes, checksum_sha256, is_attached, organization_id NULL→CASCADE, owner_user_id→CASCADE), enum `FileCategory`; индексы `category`/`organization_id`/`owner_user_id`/`(is_attached, created_at)`
- [x] S3-слой `core/storage.py` (aioboto3): `upload_object`/`generate_presigned_get`/`delete_object`/`ensure_bucket`, внутренний vs публичный endpoint для presigned, `StorageError`; конфиг `S3_*`/`MAX_UPLOAD_SIZE_MB`/`ORPHAN_FILE_TTL_HOURS` в `config.py`
- [x] `services/file_storage.py`: политики категорий (`CATEGORY_POLICIES` — лимит + MIME), валидация размера (стрим) и реального MIME (`filetype`), генерация ключа `{prefix}{scope}/{yyyy}/{mm}/{uuid}{ext}`, права по category, `FileError`
- [x] `POST/GET/DELETE /api/v1/files` (multipart upload, presigned-выдача, удаление; `FILE_IN_USE` для привязанного, идемпотентный `FILE_NOT_FOUND`); обработчик `FileError` в `main.py`, коды в `../docs/ERROR_FORMAT.md`
- [x] Celery `cleanup_orphan_files` (ежечасно): удаление сирот `is_attached=false` старше `ORPHAN_FILE_TTL_HOURS` (объект + строка)
- [x] Миграция `d4f5a6b80004` (create `files`) — обратима, `alembic check` без дрейфа
- [x] 20 тестов: success-upload (avatar/checklist_photo/knowledge_base/other), 413/415/400/422, RBAC (чужая org, employee→KB), presigned-выдача, удаление привязанного/идемпотентность, RBAC чтения/удаления, очистка сирот (`test_files.py`, `test_tasks.py`)
- DevOps-часть (MinIO в dev-compose, S3-env, прод managed-S3) — дорожка `devops`

---

## Фича — Усиление безопасности (`security_hardening`) `[x]`
ТЗ: `../docs/tasks/security_hardening/backend.md`
- [x] Rate-limit (slowapi + Redis) на `register`/`verify`/`resend-code`/`login` — ключ = IP (`X-Forwarded-For` за Caddy), пороги из ENV, `429 RATE_LIMIT_EXCEEDED` + `Retry-After` в конверте `{data,error}` (`core/rate_limit.py`, `core/redis.py`, `utils/request.py`)
- [x] Счётчик попыток кода: `verification_codes.attempts` (атомарный инкремент + commit), `429 TOO_MANY_CODE_ATTEMPTS` при `>= max_code_attempts`; `resend-code` выдаёт свежий код
- [x] Блокировка аккаунта по неудачным логинам (`services/lockout.py`, Redis TTL по email): `423 ACCOUNT_LOCKED` после `max_login_failures`, сброс при успехе, без enumeration-оракула
- [x] Sentry (`core/sentry.py`, включается при `SENTRY_DSN`, без PII/тел) + глобальный `@app.exception_handler(Exception)` → 500 в конверте `{data,error}` (code `ERROR`) + `capture_exception`
- [x] Аудит-лог `audit_logs` (`services/audit.py`): запись в той же транзакции из всех чувствительных endpoint'ов + Celery (`actor_user_id = null`); `GET /organizations/{id}/audit-logs` (owner/admin, фильтры, пагинация)
- [x] Мониторинг Celery: task-события, `acks_late`, сигнал `task_failure` → structlog; Sentry в воркере (CeleryIntegration)
- [x] Миграции `a1c2e3f50001` (attempts) и `b2d3f4a60002` (audit_logs) — обратимы, `alembic check` без дрейфа
- [x] 14 тестов (`tests/test_security_hardening.py`): rate-limit 429, сожжённый код, lockout 423 + сброс + no-enumeration, аудит (запись/чтение/фильтр/403/404 + системная запись Celery), глобальный 500
- Инфра-часть (Docker non-root, CI-сканеры, ресурсные лимиты, Caddy-заголовки, Flower) — дорожка `devops`; провижининг `SENTRY_DSN` — `DEPLOY_NOTES.md`

---

## Фича — Ставки и расчёт зарплаты (`payroll`) `[x]`
ТЗ: `../docs/tasks/payroll/backend.md`
- [x] Модель `OrganizationMemberRate` (история ставок: member_id FK CASCADE, rate_amount_minor > 0 в копейках, enum `ratetype` hourly/per_shift, currency RUB, effective_from, note)
- [x] Миграция `f7a8b9c0d1e2`: таблица + `UNIQUE (member_id, effective_from)` + индекс `(member_id, effective_from DESC)`; обратима (downgrade проверен)
- [x] CRUD ставок: POST/GET/PATCH/DELETE `/organizations/{org_id}/members/{member_id}/rates[/{rate_id}]` (org_admin; 404 `MEMBER_NOT_FOUND`/`RATE_NOT_FOUND`, 409 `RATE_EFFECTIVE_FROM_TAKEN` + защита гонки через UNIQUE)
- [x] `GET /organizations/{org_id}/payroll` — отчёт за период: ставка на момент `started_at` каждой завершённой смены, Decimal-накопление, half-up один раз на итог сотрудника, `unpaid_*`/`has_missing_rate`
- [x] `GET /organizations/{org_id}/my-earnings` — личный заработок участника + `current_rate` (owner/посторонние → 403)
- [x] `MemberResponse.current_rate` (additive nullable) — заполняется только для owner/admin/super_admin; для employee всегда null
- [x] 41 тест (`tests/test_payroll.py`): CRUD, права, смена ставки по истории, mixed rate_type, unpaid, единое округление, паузы, границы периода, каскад при исключении участника

---

## Фича — Фильтры по диапазону дат (`date_filters`) `[x]`
ТЗ: `../docs/tasks/date_filters/backend.md`
- [x] `validate_date_range` — 400 `INVALID_DATE_RANGE` на всех 4 эндпоинтах (оба конца заданы и `date_from > date_to`); открытый диапазон допустим
- [x] `resolve_stats_window` — ровно один источник окна stats: `period` ЛИБО `date_from`/`date_to` (`MISSING_STATS_RANGE` / `AMBIGUOUS_STATS_RANGE`, порядок валидации по ТЗ)
- [x] `GET /shifts/stats` и `GET /organizations/{id}/stats`: `period` стал опциональным; кастомное окно `[date_from, date_to]` включительно по `started_at`; `period = null` в ответе при кастоме
- [x] Новые поля ответа stats `range_from`/`range_to` (пресет → вычисленное начало + «сейчас»; кастом → переданные границы)
- [x] `ensure_utc` — naive datetime трактуется как UTC, aware приводится к UTC (единая семантика списков и stats, фильтры и эхо-поля)
- [x] Пресеты `day/week/month` не изменены; обратная совместимость со старыми билдами (period-ветка идентична)
- [x] Без миграций (схема БД не менялась); 34 теста (`tests/test_date_filters.py`)

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
- [x] Rate-limiting — закрыто в `security_hardening` (slowapi + Redis, per-IP на auth-эндпоинтах)
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
