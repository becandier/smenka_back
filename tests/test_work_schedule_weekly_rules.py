from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from src.app.models.work_schedule import WorkSchedule, WorkScheduleWeeklyRule
from src.app.services.work_schedule import compute_scheduled_window_with_weekly_rules

TZ = ZoneInfo("Europe/Moscow")


def _schedule() -> WorkSchedule:
    return WorkSchedule(
        name="Основной", start_time=time(7), end_time=time(18), organization_id=None
    )


def test_enabled_saturday_override_changes_window() -> None:
    schedule = _schedule()
    rules = {
        6: WorkScheduleWeeklyRule(
            weekday=6, is_enabled=True, start_time=time(9), end_time=time(19)
        )
    }
    start, end = compute_scheduled_window_with_weekly_rules(
        datetime(2026, 9, 5, 10, tzinfo=UTC), TZ, schedule, rules
    )
    assert start.astimezone(TZ).time() == time(9)
    assert end.astimezone(TZ).time() == time(19)


def test_disabled_day_has_no_window() -> None:
    schedule = _schedule()
    rules = {7: WorkScheduleWeeklyRule(weekday=7, is_enabled=False)}
    assert (
        compute_scheduled_window_with_weekly_rules(
            datetime(2026, 9, 6, 10, tzinfo=UTC), TZ, schedule, rules
        )
        is None
    )


def test_overnight_uses_rule_for_start_date_after_midnight() -> None:
    schedule = WorkSchedule(
        name="Ночной", start_time=time(22), end_time=time(6), organization_id=None
    )
    rules = {
        6: WorkScheduleWeeklyRule(
            weekday=6, is_enabled=True, start_time=time(23), end_time=time(7)
        ),
        7: WorkScheduleWeeklyRule(weekday=7, is_enabled=False),
    }
    start, end = compute_scheduled_window_with_weekly_rules(
        datetime(2026, 9, 6, 2, tzinfo=UTC), TZ, schedule, rules
    )
    assert start.astimezone(TZ).date().isoweekday() == 6
    assert start.astimezone(TZ).time() == time(23)
    assert end.astimezone(TZ).time() == time(7)


def test_dst_window_keeps_local_end_time() -> None:
    tz = ZoneInfo("Europe/Berlin")
    schedule = WorkSchedule(
        name="DST", start_time=time(22), end_time=time(6), organization_id=None
    )
    rules = {
        7: WorkScheduleWeeklyRule(
            weekday=7, is_enabled=True, start_time=time(22), end_time=time(6)
        )
    }
    start, end = compute_scheduled_window_with_weekly_rules(
        datetime(2026, 3, 29, 1, 30, tzinfo=UTC), tz, schedule, rules
    )
    assert start.astimezone(tz).time() == time(22)
    assert end.astimezone(tz).time() == time(6)
