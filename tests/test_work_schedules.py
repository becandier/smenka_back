"""Фича work_schedules: графики работы, резолв R1/R2, старт смены, R7, таймзона."""

import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.core.security import hash_password
from src.app.models.organization import MemberRole, Organization, OrganizationMember
from src.app.models.organization_role import OrganizationRole
from src.app.models.shift import Shift
from src.app.models.user import User
from src.app.models.work_location import WorkLocation
from src.app.models.work_schedule import (
    ScheduleOverrideType,
    WorkSchedule,
    WorkScheduleLocation,
    WorkScheduleMemberOverride,
    WorkScheduleRole,
)
from src.app.services.work_location import resolve_nearest_work_location
from src.app.services.work_schedule import (
    _compute_effective_schedules,
    compute_scheduled_window,
    get_effective_schedules,
)

# --- Хелперы -----------------------------------------------------------------


async def _create_user(db_session: AsyncSession, email: str, name: str = "User") -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password("Test1234"),
        name=name,
        is_verified=True,
    )
    db_session.add(user)
    await db_session.commit()
    return user


async def _login_as(client: AsyncClient, email: str) -> dict[str, str]:
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "Test1234"},
    )
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


async def _create_org(client: AsyncClient, super_admin_headers: dict[str, str]) -> str:
    resp = await client.post(
        "/api/v1/organizations",
        headers=super_admin_headers,
        json={"name": "Org WS"},
    )
    return resp.json()["data"]["id"]


async def _create_schedule(
    client: AsyncClient,
    headers: dict[str, str],
    org_id: str,
    *,
    name: str = "Дневная",
    start_time: str = "09:00",
    end_time: str = "18:00",
) -> dict:
    resp = await client.post(
        f"/api/v1/organizations/{org_id}/work-schedules",
        headers=headers,
        json={"name": name, "start_time": start_time, "end_time": end_time},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


# --- R2: расчёт планового окна (unit, без БД) --------------------------------


class TestComputeScheduledWindow:
    """5 примеров из backend.md + переход на летнее время."""

    TZ_MSK = ZoneInfo("Europe/Moscow")

    def test_day_schedule_arrive_early_no_late(self):
        # график 09:00–18:00, старт 08:52 -> окно сегодня 09:00–18:00
        started = datetime(2026, 7, 20, 5, 52, tzinfo=UTC)  # 08:52 MSK (UTC+3)
        start, end = compute_scheduled_window(started, self.TZ_MSK, time(9, 0), time(18, 0))
        assert start == datetime(2026, 7, 20, 6, 0, tzinfo=UTC)  # 09:00 MSK
        assert end == datetime(2026, 7, 20, 15, 0, tzinfo=UTC)  # 18:00 MSK
        assert started < start  # пришёл раньше -> опоздания нет

    def test_day_schedule_late_14_minutes(self):
        # график 09:00–18:00, старт 09:14 -> опоздание 14 мин
        started = datetime(2026, 7, 20, 6, 14, tzinfo=UTC)  # 09:14 MSK
        start, end = compute_scheduled_window(started, self.TZ_MSK, time(9, 0), time(18, 0))
        assert start == datetime(2026, 7, 20, 6, 0, tzinfo=UTC)
        assert (started - start) == timedelta(minutes=14)
        assert end == datetime(2026, 7, 20, 15, 0, tzinfo=UTC)

    def test_night_schedule_started_after_midnight(self):
        # график 22:00–06:00, старт 01:30 -> окно вчера 22:00 - сегодня 06:00
        started = datetime(2026, 7, 20, 22, 30, tzinfo=UTC)  # 01:30 MSK (21-го)
        start, end = compute_scheduled_window(started, self.TZ_MSK, time(22, 0), time(6, 0))
        assert start == datetime(2026, 7, 20, 19, 0, tzinfo=UTC)  # вчера 22:00 MSK
        assert end == datetime(2026, 7, 21, 3, 0, tzinfo=UTC)  # сегодня 06:00 MSK
        assert (started - start) == timedelta(hours=3, minutes=30)

    def test_night_schedule_started_before_midnight(self):
        # график 22:00–06:00, старт 21:40 -> окно сегодня 22:00 - завтра 06:00
        started = datetime(2026, 7, 20, 18, 40, tzinfo=UTC)  # 21:40 MSK
        start, end = compute_scheduled_window(started, self.TZ_MSK, time(22, 0), time(6, 0))
        assert start == datetime(2026, 7, 20, 19, 0, tzinfo=UTC)  # сегодня 22:00 MSK
        assert end == datetime(2026, 7, 21, 3, 0, tzinfo=UTC)  # завтра 06:00 MSK
        assert start > started  # график ещё не начался -> опоздания нет

    def test_day_schedule_window_closed_rolls_to_tomorrow(self):
        # график 09:00–18:00, старт 19:00 (окно дня уже закрыто) -> окно завтра
        started = datetime(2026, 7, 20, 16, 0, tzinfo=UTC)  # 19:00 MSK
        start, end = compute_scheduled_window(started, self.TZ_MSK, time(9, 0), time(18, 0))
        assert start == datetime(2026, 7, 21, 6, 0, tzinfo=UTC)  # завтра 09:00 MSK
        assert end == datetime(2026, 7, 21, 15, 0, tzinfo=UTC)

    def test_night_schedule_across_spring_forward_dst(self):
        """Europe/Berlin, переход на летнее время 2026-03-29 02:00->03:00.

        Ночной график 22:00-06:00, начатый вечером 28-го -> реальная UTC-длительность
        окна на 1 час короче номинальных 8 часов (клиенты "теряют" этот час).
        """
        tz = ZoneInfo("Europe/Berlin")
        # Наивное локальное время нужно намеренно — tzinfo прикрепляется ниже
        # через .replace(tzinfo=tz), как и рекомендует backend.md для R2.
        started_local = datetime(2026, 3, 28, 22, 5)  # noqa: DTZ001
        started = started_local.replace(tzinfo=tz).astimezone(UTC)

        start, end = compute_scheduled_window(started, tz, time(22, 0), time(6, 0))

        # Начало окна — 2026-03-28 22:00 CET (UTC+1) = 21:00 UTC
        assert start == datetime(2026, 3, 28, 21, 0, tzinfo=UTC)
        # Конец окна — 2026-03-29 06:00 CEST (UTC+2, после перевода стрелок) = 04:00 UTC
        assert end == datetime(2026, 3, 29, 4, 0, tzinfo=UTC)
        # Номинально 8ч, реально — 7ч (час "потерян" переводом стрелок).
        assert (end - start) == timedelta(hours=7)


# --- R1: резолв эффективного набора графиков (unit на сервисе) ----------------


class TestEffectiveSchedulesResolution:
    async def _make_org_member(
        self,
        db_session: AsyncSession,
        *,
        role_id: uuid.UUID | None = None,
    ) -> tuple[Organization, OrganizationMember]:
        owner = await _create_user(db_session, f"owner-{uuid.uuid4().hex[:8]}@example.com")
        org = Organization(name="Resolve Org", owner_id=owner.id)
        db_session.add(org)
        await db_session.flush()

        user = await _create_user(db_session, f"member-{uuid.uuid4().hex[:8]}@example.com")
        member = OrganizationMember(
            organization_id=org.id, user_id=user.id, role=MemberRole.employee, role_id=role_id
        )
        db_session.add(member)
        await db_session.commit()
        return org, member

    async def test_global_schedule_no_bindings_applies_to_everyone(self, db_session: AsyncSession):
        org, member = await self._make_org_member(db_session)
        schedule = WorkSchedule(
            organization_id=org.id, name="Общий", start_time=time(9, 0), end_time=time(18, 0)
        )
        db_session.add(schedule)
        await db_session.commit()

        pairs = await _compute_effective_schedules(db_session, org.id, member)
        assert [s.id for s, _ in pairs] == [schedule.id]
        assert pairs[0][1] == "global"

    async def test_location_only_schedule_applies_to_all_at_that_location(
        self, db_session: AsyncSession
    ):
        org, member = await self._make_org_member(db_session)
        location = WorkLocation(
            organization_id=org.id, name="Точка", latitude=55.0, longitude=37.0
        )
        db_session.add(location)
        await db_session.flush()

        schedule = WorkSchedule(
            organization_id=org.id, name="По точке", start_time=time(9, 0), end_time=time(18, 0)
        )
        db_session.add(schedule)
        await db_session.flush()
        db_session.add(WorkScheduleLocation(schedule_id=schedule.id, work_location_id=location.id))
        await db_session.commit()

        pairs = await _compute_effective_schedules(db_session, org.id, member)
        assert [s.id for s, _ in pairs] == [schedule.id]
        assert pairs[0][1] == "location"

        # Фильтр по точке: подходит на этой точке, не подходит без точки/на другой.
        matching = await get_effective_schedules(db_session, org.id, member, location.id)
        assert [s.id for s, _ in matching] == [schedule.id]
        no_location = await get_effective_schedules(db_session, org.id, member, None)
        assert no_location == []

    async def test_role_schedule_applies_only_to_role_holders(self, db_session: AsyncSession):
        owner = await _create_user(db_session, "owner-role@example.com")
        org = Organization(name="Role Org", owner_id=owner.id)
        db_session.add(org)
        await db_session.flush()
        role = OrganizationRole(organization_id=org.id, name="Бариста")
        db_session.add(role)
        await db_session.flush()

        schedule = WorkSchedule(
            organization_id=org.id, name="Для баристы", start_time=time(9, 0), end_time=time(18, 0)
        )
        db_session.add(schedule)
        await db_session.flush()
        db_session.add(WorkScheduleRole(schedule_id=schedule.id, role_id=role.id))
        await db_session.commit()

        user_with_role = await _create_user(db_session, "with-role@example.com")
        member_with_role = OrganizationMember(
            organization_id=org.id, user_id=user_with_role.id, role_id=role.id
        )
        db_session.add(member_with_role)

        user_without_role = await _create_user(db_session, "without-role@example.com")
        member_without_role = OrganizationMember(
            organization_id=org.id, user_id=user_without_role.id, role_id=None
        )
        db_session.add(member_without_role)
        await db_session.commit()

        with_role_pairs = await _compute_effective_schedules(db_session, org.id, member_with_role)
        assert [s.id for s, _ in with_role_pairs] == [schedule.id]
        assert with_role_pairs[0][1] == "role"

        without_role_pairs = await _compute_effective_schedules(
            db_session, org.id, member_without_role
        )
        assert without_role_pairs == []

    async def test_personal_add_grants_schedule_not_otherwise_assigned(
        self, db_session: AsyncSession
    ):
        org, member = await self._make_org_member(db_session)
        role = OrganizationRole(organization_id=org.id, name="Другая роль")
        db_session.add(role)
        await db_session.flush()

        schedule = WorkSchedule(
            organization_id=org.id,
            name="Только по add",
            start_time=time(9, 0),
            end_time=time(18, 0),
        )
        db_session.add(schedule)
        await db_session.flush()
        db_session.add(WorkScheduleRole(schedule_id=schedule.id, role_id=role.id))
        db_session.add(
            WorkScheduleMemberOverride(
                schedule_id=schedule.id,
                member_id=member.id,
                override_type=ScheduleOverrideType.add,
            )
        )
        await db_session.commit()

        pairs = await _compute_effective_schedules(db_session, org.id, member)
        assert [s.id for s, _ in pairs] == [schedule.id]
        assert pairs[0][1] == "personal_add"

    async def test_personal_remove_excludes_global_schedule(self, db_session: AsyncSession):
        org, member = await self._make_org_member(db_session)
        schedule = WorkSchedule(
            organization_id=org.id, name="Общий", start_time=time(9, 0), end_time=time(18, 0)
        )
        db_session.add(schedule)
        await db_session.flush()
        db_session.add(
            WorkScheduleMemberOverride(
                schedule_id=schedule.id,
                member_id=member.id,
                override_type=ScheduleOverrideType.remove,
            )
        )
        await db_session.commit()

        pairs = await _compute_effective_schedules(db_session, org.id, member)
        assert pairs == []

    async def test_remove_takes_priority_over_add_same_schedule(self, db_session: AsyncSession):
        """remove и add на одном graphике одновременно для одного member невозможны
        (UNIQUE(schedule_id, member_id)) — проверяем приоритет remove над глобальным
        назначением, а add — восстанавливает то, что remove не трогало."""
        org, member = await self._make_org_member(db_session)
        global_schedule = WorkSchedule(
            organization_id=org.id, name="Общий", start_time=time(9, 0), end_time=time(18, 0)
        )
        add_only_schedule = WorkSchedule(
            organization_id=org.id,
            name="Персонально добавленный",
            start_time=time(20, 0),
            end_time=time(23, 0),
        )
        db_session.add_all([global_schedule, add_only_schedule])
        await db_session.flush()

        role = OrganizationRole(organization_id=org.id, name="Роль X")
        db_session.add(role)
        await db_session.flush()
        db_session.add(WorkScheduleRole(schedule_id=add_only_schedule.id, role_id=role.id))

        db_session.add(
            WorkScheduleMemberOverride(
                schedule_id=global_schedule.id,
                member_id=member.id,
                override_type=ScheduleOverrideType.remove,
            )
        )
        db_session.add(
            WorkScheduleMemberOverride(
                schedule_id=add_only_schedule.id,
                member_id=member.id,
                override_type=ScheduleOverrideType.add,
            )
        )
        await db_session.commit()

        pairs = await _compute_effective_schedules(db_session, org.id, member)
        result_ids = {s.id for s, _ in pairs}
        assert global_schedule.id not in result_ids  # remove побеждает
        assert add_only_schedule.id in result_ids  # add работает независимо
        source_by_id = {s.id: src for s, src in pairs}
        assert source_by_id[add_only_schedule.id] == "personal_add"

    async def test_paused_schedule_excluded(self, db_session: AsyncSession):
        org, member = await self._make_org_member(db_session)
        schedule = WorkSchedule(
            organization_id=org.id,
            name="Приостановленный",
            start_time=time(9, 0),
            end_time=time(18, 0),
            is_paused=True,
        )
        db_session.add(schedule)
        await db_session.commit()

        pairs = await _compute_effective_schedules(db_session, org.id, member)
        assert pairs == []


# --- CRUD графиков (API) ------------------------------------------------------


class TestScheduleCrud:
    async def test_create_and_get(self, client: AsyncClient, super_admin_headers):
        org_id = await _create_org(client, super_admin_headers)
        schedule = await _create_schedule(client, super_admin_headers, org_id)
        assert schedule["name"] == "Дневная"
        assert schedule["start_time"] == "09:00"
        assert schedule["end_time"] == "18:00"
        assert schedule["duration_minutes"] == 540
        assert schedule["crosses_midnight"] is False
        assert schedule["is_paused"] is False

        get_resp = await client.get(
            f"/api/v1/organizations/{org_id}/work-schedules/{schedule['id']}",
            headers=super_admin_headers,
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["data"]["id"] == schedule["id"]

    async def test_night_schedule_crosses_midnight(self, client: AsyncClient, super_admin_headers):
        org_id = await _create_org(client, super_admin_headers)
        schedule = await _create_schedule(
            client,
            super_admin_headers,
            org_id,
            name="Ночная",
            start_time="22:00",
            end_time="06:00",
        )
        assert schedule["crosses_midnight"] is True
        assert schedule["duration_minutes"] == 8 * 60

    async def test_equal_start_end_time_rejected(self, client: AsyncClient, super_admin_headers):
        org_id = await _create_org(client, super_admin_headers)
        resp = await client.post(
            f"/api/v1/organizations/{org_id}/work-schedules",
            headers=super_admin_headers,
            json={"name": "Плохой", "start_time": "09:00", "end_time": "09:00"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "SCHEDULE_INVALID_TIME"

    async def test_update_time_does_not_affect_started_shifts(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        org_id = await _create_org(client, super_admin_headers)
        schedule = await _create_schedule(client, super_admin_headers, org_id)
        me = await client.get("/api/v1/users/me", headers=super_admin_headers)

        shift = Shift(
            user_id=uuid.UUID(me.json()["data"]["id"]),
            organization_id=uuid.UUID(org_id),
            work_schedule_id=uuid.UUID(schedule["id"]),
            schedule_name=schedule["name"],
            scheduled_start_at=datetime(2026, 7, 20, 6, 0, tzinfo=UTC),
            scheduled_end_at=datetime(2026, 7, 20, 15, 0, tzinfo=UTC),
        )
        db_session.add(shift)
        await db_session.commit()

        await client.patch(
            f"/api/v1/organizations/{org_id}/work-schedules/{schedule['id']}",
            headers=super_admin_headers,
            json={"start_time": "10:00", "end_time": "19:00"},
        )

        await db_session.refresh(shift)
        assert shift.scheduled_start_at == datetime(2026, 7, 20, 6, 0, tzinfo=UTC)
        assert shift.scheduled_end_at == datetime(2026, 7, 20, 15, 0, tzinfo=UTC)

    async def test_list_include_paused(self, client: AsyncClient, super_admin_headers):
        org_id = await _create_org(client, super_admin_headers)
        schedule = await _create_schedule(client, super_admin_headers, org_id)
        patch_resp = await client.patch(
            f"/api/v1/organizations/{org_id}/work-schedules/{schedule['id']}",
            headers=super_admin_headers,
            json={"is_paused": True},
        )
        assert patch_resp.json()["data"]["is_paused"] is True

        default_resp = await client.get(
            f"/api/v1/organizations/{org_id}/work-schedules", headers=super_admin_headers
        )
        assert default_resp.json()["data"]["total"] == 0

        with_paused_resp = await client.get(
            f"/api/v1/organizations/{org_id}/work-schedules",
            headers=super_admin_headers,
            params={"include_paused": "true"},
        )
        assert with_paused_resp.json()["data"]["total"] == 1
        assert with_paused_resp.json()["data"]["items"][0]["is_paused"] is True

        unpaused_resp = await client.patch(
            f"/api/v1/organizations/{org_id}/work-schedules/{schedule['id']}",
            headers=super_admin_headers,
            json={"is_paused": False},
        )
        assert unpaused_resp.json()["data"]["is_paused"] is False
        default_resp_again = await client.get(
            f"/api/v1/organizations/{org_id}/work-schedules", headers=super_admin_headers
        )
        assert default_resp_again.json()["data"]["total"] == 1

    async def test_delete_schedule_shift_keeps_snapshot(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        org_id = await _create_org(client, super_admin_headers)
        schedule = await _create_schedule(client, super_admin_headers, org_id)

        me = await client.get("/api/v1/users/me", headers=super_admin_headers)
        shift = Shift(
            user_id=uuid.UUID(me.json()["data"]["id"]),
            organization_id=uuid.UUID(org_id),
            work_schedule_id=uuid.UUID(schedule["id"]),
            schedule_name=schedule["name"],
            scheduled_start_at=datetime(2026, 7, 20, 6, 0, tzinfo=UTC),
            scheduled_end_at=datetime(2026, 7, 20, 15, 0, tzinfo=UTC),
        )
        db_session.add(shift)
        await db_session.commit()
        shift_id = shift.id

        del_resp = await client.delete(
            f"/api/v1/organizations/{org_id}/work-schedules/{schedule['id']}",
            headers=super_admin_headers,
        )
        assert del_resp.status_code == 200

        db_session.expire_all()
        updated_shift = (
            await db_session.execute(select(Shift).where(Shift.id == shift_id))
        ).scalar_one()
        assert updated_shift.work_schedule_id is None
        assert updated_shift.schedule_name == "Дневная"
        assert updated_shift.scheduled_start_at == datetime(2026, 7, 20, 6, 0, tzinfo=UTC)


# --- Назначения (API) ----------------------------------------------------------


class TestScheduleAssignments:
    async def test_assign_roles_and_locations(self, client: AsyncClient, super_admin_headers):
        org_id = await _create_org(client, super_admin_headers)
        schedule = await _create_schedule(client, super_admin_headers, org_id)

        role_resp = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Бариста"},
        )
        role_id = role_resp.json()["data"]["id"]

        loc_resp = await client.post(
            f"/api/v1/organizations/{org_id}/locations",
            headers=super_admin_headers,
            json={"name": "Точка", "latitude": 55.0, "longitude": 37.0, "radius_meters": 100},
        )
        location_id = loc_resp.json()["data"]["id"]

        roles_put = await client.put(
            f"/api/v1/organizations/{org_id}/work-schedules/{schedule['id']}/roles",
            headers=super_admin_headers,
            json={"role_ids": [role_id]},
        )
        assert roles_put.status_code == 200
        assert roles_put.json()["data"]["role_ids"] == [role_id]

        locations_put = await client.put(
            f"/api/v1/organizations/{org_id}/work-schedules/{schedule['id']}/locations",
            headers=super_admin_headers,
            json={"work_location_ids": [location_id]},
        )
        assert locations_put.status_code == 200
        assert locations_put.json()["data"]["work_location_ids"] == [location_id]

        assignments = await client.get(
            f"/api/v1/organizations/{org_id}/work-schedules/{schedule['id']}/assignments",
            headers=super_admin_headers,
        )
        assert assignments.json()["data"]["role_ids"] == [role_id]
        assert assignments.json()["data"]["work_location_ids"] == [location_id]

    async def test_role_from_other_org_rejected(self, client: AsyncClient, super_admin_headers):
        org_id = await _create_org(client, super_admin_headers)
        schedule = await _create_schedule(client, super_admin_headers, org_id)

        other_org_id = await _create_org(client, super_admin_headers)
        role_resp = await client.post(
            f"/api/v1/organizations/{other_org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Чужая роль"},
        )
        other_role_id = role_resp.json()["data"]["id"]

        resp = await client.put(
            f"/api/v1/organizations/{org_id}/work-schedules/{schedule['id']}/roles",
            headers=super_admin_headers,
            json={"role_ids": [other_role_id]},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "ROLE_NOT_FOUND"

    async def test_member_overrides_put(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        org_id = await _create_org(client, super_admin_headers)
        # Схема привязана к роли, которой у сотрудника не будет — иначе она была бы
        # "global" сама по себе, и override "add" стал бы неотличим по source.
        schedule = await _create_schedule(client, super_admin_headers, org_id)
        role_resp = await client.post(
            f"/api/v1/organizations/{org_id}/roles",
            headers=super_admin_headers,
            json={"name": "Другая роль"},
        )
        await client.put(
            f"/api/v1/organizations/{org_id}/work-schedules/{schedule['id']}/roles",
            headers=super_admin_headers,
            json={"role_ids": [role_resp.json()["data"]["id"]]},
        )

        member_user = await _create_user(db_session, "override-member@example.com")
        org_resp = await client.get(f"/api/v1/organizations/{org_id}", headers=super_admin_headers)
        invite_code = org_resp.json()["data"]["invite_code"]
        member_headers = await _login_as(client, "override-member@example.com")
        await client.post(f"/api/v1/organizations/join/{invite_code}", headers=member_headers)

        resp = await client.put(
            f"/api/v1/organizations/{org_id}/members/{member_user.id}/schedule-overrides",
            headers=super_admin_headers,
            json={"overrides": [{"schedule_id": schedule["id"], "override_type": "add"}]},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["overrides"] == [
            {"schedule_id": schedule["id"], "override_type": "add"}
        ]

        effective = await client.get(
            f"/api/v1/organizations/{org_id}/members/{member_user.id}/schedules",
            headers=super_admin_headers,
        )
        items = effective.json()["data"]["items"]
        assert any(
            item["id"] == schedule["id"] and item["source"] == "personal_add" for item in items
        )


# --- Старт смены с графиком (API) ---------------------------------------------


async def _setup_member_org(
    client: AsyncClient,
    db_session: AsyncSession,
    super_admin_headers: dict[str, str],
    *,
    email: str = "ws-employee@example.com",
) -> dict:
    org_id = await _create_org(client, super_admin_headers)
    member_user = await _create_user(db_session, email)
    org_resp = await client.get(f"/api/v1/organizations/{org_id}", headers=super_admin_headers)
    invite_code = org_resp.json()["data"]["invite_code"]
    member_headers = await _login_as(client, email)
    await client.post(f"/api/v1/organizations/join/{invite_code}", headers=member_headers)
    return {"org_id": org_id, "member_headers": member_headers, "member_user": member_user}


class TestStartShiftWithSchedule:
    async def test_zero_schedules_require_schedule_false_starts_without_schedule(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_member_org(client, db_session, super_admin_headers)
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={"organization_id": ctx["org_id"]},
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["work_schedule_id"] is None
        assert data["scheduled_start_at"] is None
        assert data["late_seconds"] is None

    async def test_zero_schedules_require_schedule_true_blocks_start(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_member_org(client, db_session, super_admin_headers)
        # require_schedule нельзя включить без НЕАРХИВНЫХ графиков организации —
        # заводим один, но привязываем к роли, которой у сотрудника нет, чтобы
        # у него самого эффективный набор остался пустым (0 доступных).
        role_resp = await client.post(
            f"/api/v1/organizations/{ctx['org_id']}/roles",
            headers=super_admin_headers,
            json={"name": "Не для сотрудника"},
        )
        schedule = await _create_schedule(client, super_admin_headers, ctx["org_id"])
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/work-schedules/{schedule['id']}/roles",
            headers=super_admin_headers,
            json={"role_ids": [role_resp.json()["data"]["id"]]},
        )

        settings_resp = await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/settings",
            headers=super_admin_headers,
            json={"require_schedule": True},
        )
        assert settings_resp.status_code == 200

        resp = await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={"organization_id": ctx["org_id"]},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "SCHEDULE_REQUIRED"

    async def test_one_schedule_auto_assigned(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_member_org(client, db_session, super_admin_headers)
        schedule = await _create_schedule(client, super_admin_headers, ctx["org_id"])

        resp = await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={"organization_id": ctx["org_id"]},
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["work_schedule_id"] == schedule["id"]
        assert data["schedule_name"] == "Дневная"
        assert data["scheduled_start_at"] is not None
        assert data["scheduled_end_at"] is not None
        assert data["late_seconds"] is not None

    async def test_multiple_schedules_require_false_starts_without_schedule(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_member_org(client, db_session, super_admin_headers)
        await _create_schedule(client, super_admin_headers, ctx["org_id"], name="A")
        await _create_schedule(client, super_admin_headers, ctx["org_id"], name="B")

        resp = await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={"organization_id": ctx["org_id"]},
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["work_schedule_id"] is None

    async def test_multiple_schedules_require_true_blocks_start(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_member_org(client, db_session, super_admin_headers)
        await _create_schedule(client, super_admin_headers, ctx["org_id"], name="A")
        await _create_schedule(client, super_admin_headers, ctx["org_id"], name="B")
        await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/settings",
            headers=super_admin_headers,
            json={"require_schedule": True},
        )

        resp = await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={"organization_id": ctx["org_id"]},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "SCHEDULE_REQUIRED"

    async def test_explicit_schedule_id_used_when_available(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_member_org(client, db_session, super_admin_headers)
        schedule_a = await _create_schedule(client, super_admin_headers, ctx["org_id"], name="A")
        await _create_schedule(client, super_admin_headers, ctx["org_id"], name="B")

        resp = await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={"organization_id": ctx["org_id"], "work_schedule_id": schedule_a["id"]},
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["work_schedule_id"] == schedule_a["id"]

    async def test_explicit_schedule_not_in_org_returns_not_found(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_member_org(client, db_session, super_admin_headers)
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={
                "organization_id": ctx["org_id"],
                "work_schedule_id": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "SCHEDULE_NOT_FOUND"

    async def test_explicit_schedule_from_other_org_returns_not_found(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_member_org(client, db_session, super_admin_headers)
        other_org_id = await _create_org(client, super_admin_headers)
        other_schedule = await _create_schedule(client, super_admin_headers, other_org_id)
        await _create_schedule(client, super_admin_headers, ctx["org_id"])

        # Смежная организация: график существует в СИСТЕМЕ, но не в org сотрудника ->
        # трактуется как SCHEDULE_NOT_FOUND (не принадлежит организации).
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={"organization_id": ctx["org_id"], "work_schedule_id": other_schedule["id"]},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "SCHEDULE_NOT_FOUND"

    async def test_explicit_schedule_in_org_but_not_assigned_returns_not_available(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """График принадлежит org, но привязан к роли, которой у сотрудника нет ->
        403 SCHEDULE_NOT_AVAILABLE (не 404 — график существует)."""
        ctx = await _setup_member_org(client, db_session, super_admin_headers)
        role_resp = await client.post(
            f"/api/v1/organizations/{ctx['org_id']}/roles",
            headers=super_admin_headers,
            json={"name": "Только для бариста"},
        )
        role_id = role_resp.json()["data"]["id"]
        restricted_schedule = await _create_schedule(
            client, super_admin_headers, ctx["org_id"], name="Для бариста"
        )
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/work-schedules/{restricted_schedule['id']}/roles",
            headers=super_admin_headers,
            json={"role_ids": [role_id]},
        )

        resp = await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={
                "organization_id": ctx["org_id"],
                "work_schedule_id": restricted_schedule["id"],
            },
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "SCHEDULE_NOT_AVAILABLE"

    async def test_personal_shift_ignores_work_schedule_id(
        self, client: AsyncClient, auth_headers
    ):
        resp = await client.post(
            "/api/v1/shifts/start",
            headers=auth_headers,
            json={"work_schedule_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["organization_id"] is None
        assert data["work_schedule_id"] is None
        assert data["scheduled_start_at"] is None


# --- R7: смена графика администратором ----------------------------------------


class TestChangeShiftSchedule:
    async def test_admin_changes_schedule_recomputes_window(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_member_org(client, db_session, super_admin_headers)
        schedule_a = await _create_schedule(client, super_admin_headers, ctx["org_id"], name="A")
        schedule_b = await _create_schedule(
            client,
            super_admin_headers,
            ctx["org_id"],
            name="B",
            start_time="14:00",
            end_time="22:00",
        )

        start_resp = await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={"organization_id": ctx["org_id"], "work_schedule_id": schedule_a["id"]},
        )
        shift_id = start_resp.json()["data"]["id"]
        old_scheduled_start = start_resp.json()["data"]["scheduled_start_at"]

        change_resp = await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/shifts/{shift_id}/schedule",
            headers=super_admin_headers,
            json={"work_schedule_id": schedule_b["id"]},
        )
        assert change_resp.status_code == 200
        data = change_resp.json()["data"]
        assert data["work_schedule_id"] == schedule_b["id"]
        assert data["schedule_name"] == "B"
        assert data["scheduled_start_at"] != old_scheduled_start

    async def test_admin_removes_schedule_from_shift(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_member_org(client, db_session, super_admin_headers)
        schedule = await _create_schedule(client, super_admin_headers, ctx["org_id"])

        start_resp = await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={"organization_id": ctx["org_id"], "work_schedule_id": schedule["id"]},
        )
        shift_id = start_resp.json()["data"]["id"]

        change_resp = await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/shifts/{shift_id}/schedule",
            headers=super_admin_headers,
            json={"work_schedule_id": None},
        )
        assert change_resp.status_code == 200
        data = change_resp.json()["data"]
        assert data["work_schedule_id"] is None
        assert data["schedule_name"] is None
        assert data["scheduled_start_at"] is None
        assert data["scheduled_end_at"] is None

    async def test_change_schedule_does_not_affect_finished_at(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_member_org(client, db_session, super_admin_headers)
        schedule_a = await _create_schedule(client, super_admin_headers, ctx["org_id"], name="A")
        schedule_b = await _create_schedule(
            client,
            super_admin_headers,
            ctx["org_id"],
            name="B",
            start_time="14:00",
            end_time="22:00",
        )

        start_resp = await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={"organization_id": ctx["org_id"], "work_schedule_id": schedule_a["id"]},
        )
        shift_id = start_resp.json()["data"]["id"]
        finish_resp = await client.post(
            f"/api/v1/shifts/{shift_id}/finish", headers=ctx["member_headers"]
        )
        finished_at = finish_resp.json()["data"]["finished_at"]

        change_resp = await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/shifts/{shift_id}/schedule",
            headers=super_admin_headers,
            json={"work_schedule_id": schedule_b["id"]},
        )
        assert change_resp.json()["data"]["finished_at"] == finished_at

    async def test_schedule_not_found_returns_404(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_member_org(client, db_session, super_admin_headers)
        start_resp = await client.post(
            "/api/v1/shifts/start",
            headers=ctx["member_headers"],
            json={"organization_id": ctx["org_id"]},
        )
        shift_id = start_resp.json()["data"]["id"]

        resp = await client.patch(
            f"/api/v1/organizations/{ctx['org_id']}/shifts/{shift_id}/schedule",
            headers=super_admin_headers,
            json={"work_schedule_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "SCHEDULE_NOT_FOUND"


# --- Таймзона организации ------------------------------------------------------


class TestOrganizationTimezone:
    async def test_default_timezone_is_moscow(self, client: AsyncClient, super_admin_headers):
        org_id = await _create_org(client, super_admin_headers)
        resp = await client.get(f"/api/v1/organizations/{org_id}", headers=super_admin_headers)
        assert resp.json()["data"]["timezone"] == "Europe/Moscow"

    async def test_update_timezone(self, client: AsyncClient, super_admin_headers):
        org_id = await _create_org(client, super_admin_headers)
        resp = await client.patch(
            f"/api/v1/organizations/{org_id}",
            headers=super_admin_headers,
            json={"timezone": "Asia/Yekaterinburg"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["timezone"] == "Asia/Yekaterinburg"

    async def test_invalid_timezone_rejected(self, client: AsyncClient, super_admin_headers):
        org_id = await _create_org(client, super_admin_headers)
        resp = await client.patch(
            f"/api/v1/organizations/{org_id}",
            headers=super_admin_headers,
            json={"timezone": "Not/AZone"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "INVALID_TIMEZONE"

    async def test_update_timezone_only_does_not_require_name(
        self, client: AsyncClient, super_admin_headers
    ):
        org_id = await _create_org(client, super_admin_headers)
        resp = await client.patch(
            f"/api/v1/organizations/{org_id}",
            headers=super_admin_headers,
            json={"timezone": "Europe/London"},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["timezone"] == "Europe/London"
        assert data["name"] == "Org WS"  # unchanged


# --- my-schedules: owner получает пустой список -------------------------------


class TestMySchedules:
    async def test_owner_gets_empty_list(self, client: AsyncClient, super_admin_headers):
        org_id = await _create_org(client, super_admin_headers)
        await _create_schedule(client, super_admin_headers, org_id)

        resp = await client.get(
            f"/api/v1/organizations/{org_id}/my-schedules", headers=super_admin_headers
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["items"] == []

    async def test_member_sees_current_schedule(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        ctx = await _setup_member_org(client, db_session, super_admin_headers)
        await _create_schedule(client, super_admin_headers, ctx["org_id"])

        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/my-schedules",
            headers=ctx["member_headers"],
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["require_schedule"] is False
        item = data["items"][0]
        assert "next_start_at" in item
        assert "starts_in_minutes" in item
        assert data["resolved_work_location"] is None


# --- resolve_nearest_work_location: общий хелпер (work_schedules_geo_resolve) --


class TestResolveNearestWorkLocation:
    async def test_picks_nearest_among_matched_zones(self, db_session: AsyncSession):
        owner = await _create_user(db_session, f"owner-{uuid.uuid4().hex[:8]}@example.com")
        org = Organization(name="Geo Resolve Org", owner_id=owner.id)
        db_session.add(org)
        await db_session.flush()

        loc_a = WorkLocation(
            organization_id=org.id,
            name="A",
            latitude=55.7558,
            longitude=37.6173,
            radius_meters=500,
        )
        loc_b = WorkLocation(
            organization_id=org.id,
            name="B",
            latitude=55.7600,
            longitude=37.6173,
            radius_meters=500,
        )
        db_session.add_all([loc_a, loc_b])
        await db_session.commit()

        nearest = await resolve_nearest_work_location(db_session, org.id, 55.7565, 37.6173)
        assert nearest is not None
        assert nearest.id == loc_a.id

    async def test_none_when_no_zone_matches(self, db_session: AsyncSession):
        owner = await _create_user(db_session, f"owner-{uuid.uuid4().hex[:8]}@example.com")
        org = Organization(name="Geo Resolve Org 2", owner_id=owner.id)
        db_session.add(org)
        await db_session.flush()
        loc = WorkLocation(
            organization_id=org.id,
            name="A",
            latitude=55.7558,
            longitude=37.6173,
            radius_meters=200,
        )
        db_session.add(loc)
        await db_session.commit()

        nearest = await resolve_nearest_work_location(db_session, org.id, 10.0, 10.0)
        assert nearest is None


# --- my-schedules: резолв точки по lat/lng (work_schedules_geo_resolve) -------


class TestMySchedulesGeoResolve:
    """Баг с прода: график, привязанный только к точке, никогда не показывался в
    my-schedules при geo_check_enabled=true, потому что мобилка запрашивала этот
    эндпоинт без work_location_id (точку знает только сервер, только на старте
    смены). Фикс — резолв точки по lat/lng тем же Haversine-подбором."""

    async def _setup_geo_org(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        super_admin_headers: dict[str, str],
        *,
        geo_check_enabled: bool = True,
    ) -> dict:
        ctx = await _setup_member_org(
            client, db_session, super_admin_headers, email="geo-employee@example.com"
        )
        loc_resp = await client.post(
            f"/api/v1/organizations/{ctx['org_id']}/locations",
            headers=super_admin_headers,
            json={
                "name": "Точка",
                "latitude": 55.7558,
                "longitude": 37.6173,
                "radius_meters": 200,
            },
        )
        assert loc_resp.status_code == 201, loc_resp.text
        location = loc_resp.json()["data"]

        schedule = await _create_schedule(
            client, super_admin_headers, ctx["org_id"], name="По точке"
        )
        await client.put(
            f"/api/v1/organizations/{ctx['org_id']}/work-schedules/{schedule['id']}/locations",
            headers=super_admin_headers,
            json={"work_location_ids": [location["id"]]},
        )

        if geo_check_enabled:
            settings_resp = await client.patch(
                f"/api/v1/organizations/{ctx['org_id']}/settings",
                headers=super_admin_headers,
                json={"geo_check_enabled": True},
            )
            assert settings_resp.status_code == 200, settings_resp.text

        ctx["location"] = location
        ctx["schedule"] = schedule
        return ctx

    async def test_lat_lng_inside_zone_reveals_location_only_schedule(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """Приёмка п.1: координаты внутри радиуса точки -> location-only график
        появляется в items, точка резолвится в ответе."""
        ctx = await self._setup_geo_org(client, db_session, super_admin_headers)

        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/my-schedules",
            headers=ctx["member_headers"],
            params={"lat": 55.7558, "lng": 37.6173},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == ctx["schedule"]["id"]
        assert data["resolved_work_location"] == {
            "id": ctx["location"]["id"],
            "name": "Точка",
        }

    async def test_coords_outside_all_zones_is_not_error(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """Приёмка п.2: координаты вне всех гео-зон -> 200, список без учёта точки
        (НЕ GEO_CHECK_FAILED, в отличие от /shifts/start)."""
        ctx = await self._setup_geo_org(client, db_session, super_admin_headers)

        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/my-schedules",
            headers=ctx["member_headers"],
            params={"lat": 10.0, "lng": 10.0},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["items"] == []
        assert data["resolved_work_location"] is None

    async def test_explicit_work_location_id_has_priority_over_lat_lng(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """Приёмка п.3: явный work_location_id побеждает lat/lng, даже если
        координаты указывают на другую/никакую зону."""
        ctx = await self._setup_geo_org(client, db_session, super_admin_headers)

        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/my-schedules",
            headers=ctx["member_headers"],
            params={
                "work_location_id": ctx["location"]["id"],
                "lat": 10.0,
                "lng": 10.0,
            },
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == ctx["schedule"]["id"]
        assert data["resolved_work_location"]["id"] == ctx["location"]["id"]

    async def test_geo_check_disabled_ignores_lat_lng(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """Приёмка п.4: geo_check_enabled=false -> lat/lng игнорируются, поведение
        как до фикса (location-only график не появляется без явного work_location_id)."""
        ctx = await self._setup_geo_org(
            client, db_session, super_admin_headers, geo_check_enabled=False
        )

        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/my-schedules",
            headers=ctx["member_headers"],
            params={"lat": 55.7558, "lng": 37.6173},
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["items"] == []
        assert data["resolved_work_location"] is None

    async def test_no_coords_no_work_location_id_behaves_as_before(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """Приёмка п.5: существующий вызов без lat/lng/work_location_id не ломается."""
        ctx = await self._setup_geo_org(client, db_session, super_admin_headers)

        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/my-schedules",
            headers=ctx["member_headers"],
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["items"] == []
        assert data["resolved_work_location"] is None

    async def test_out_of_range_coords_rejected_with_422(
        self, client: AsyncClient, super_admin_headers, db_session: AsyncSession
    ):
        """`lat`/`lng` валидируются тем же диапазоном, что и в POST /shifts/start
        (`ShiftStartRequest`: lat ge=-90/le=90, lng ge=-180/le=180) — координаты вне
        диапазона не должны тихо уходить в Haversine-подбор."""
        ctx = await self._setup_geo_org(client, db_session, super_admin_headers)

        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/my-schedules",
            headers=ctx["member_headers"],
            params={"lat": 999.0, "lng": 37.6173},
        )
        assert resp.status_code == 422

        resp = await client.get(
            f"/api/v1/organizations/{ctx['org_id']}/my-schedules",
            headers=ctx["member_headers"],
            params={"lat": 55.7558, "lng": -4000.0},
        )
        assert resp.status_code == 422
