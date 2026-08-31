"""Чистые расчётные функции биллинга (`online_payments`, `services/billing_calc.py`):
скидка за период продления, доплата за апгрейд. Без БД/HTTP."""

from datetime import UTC, datetime, timedelta

from src.app.services import billing_calc


class TestComputeExtendAmount:
    """Скидки по backend.md: 1 мес → 0%, 3 мес → 5%, 6 мес → 10%."""

    def test_standard_1_month_no_discount(self):
        r = billing_calc.compute_extend_amount(500_000, 1, 0)
        assert r.base_amount_minor == 500_000
        assert r.discount_minor == 0
        assert r.amount_minor == 500_000
        assert r.monthly_minor == 500_000

    def test_standard_3_months_5_percent(self):
        r = billing_calc.compute_extend_amount(500_000, 3, 5)
        assert r.base_amount_minor == 1_500_000
        assert r.discount_minor == 75_000
        assert r.amount_minor == 1_425_000
        assert r.monthly_minor == 475_000

    def test_standard_6_months_10_percent(self):
        r = billing_calc.compute_extend_amount(500_000, 6, 10)
        assert r.base_amount_minor == 3_000_000
        assert r.discount_minor == 300_000
        assert r.amount_minor == 2_700_000
        assert r.monthly_minor == 450_000

    def test_premium_1_month_no_discount(self):
        r = billing_calc.compute_extend_amount(1_000_000, 1, 0)
        assert r.amount_minor == 1_000_000

    def test_premium_3_months_5_percent(self):
        r = billing_calc.compute_extend_amount(1_000_000, 3, 5)
        assert r.base_amount_minor == 3_000_000
        assert r.amount_minor == 2_850_000
        assert r.monthly_minor == 950_000

    def test_premium_6_months_10_percent(self):
        r = billing_calc.compute_extend_amount(1_000_000, 6, 10)
        assert r.base_amount_minor == 6_000_000
        assert r.amount_minor == 5_400_000
        assert r.monthly_minor == 900_000

    def test_discount_rounds_down_to_whole_ruble(self):
        """Не круглая цена: скидка сначала считается в копейках, затем ещё раз
        округляется вниз до ближайшего рубля — а не до копейки."""
        # base = 333333 * 3 = 999999; raw discount (5%) = 49999 коп = 499.99 руб
        # -> округление вниз до рубля = 49900 коп.
        r = billing_calc.compute_extend_amount(333_333, 3, 5)
        assert r.base_amount_minor == 999_999
        assert r.discount_minor == 49_900
        assert r.amount_minor == 950_099

    def test_zero_discount_percent_is_noop(self):
        r = billing_calc.compute_extend_amount(123_456, 6, 0)
        assert r.discount_minor == 0
        assert r.amount_minor == r.base_amount_minor


class TestComputeUpgradeMonthsRemaining:
    NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

    def test_far_future_end_rounds_up(self):
        end = self.NOW + timedelta(days=61)  # > 2 * 30 дней
        assert billing_calc.compute_upgrade_months_remaining(end, self.NOW) == 3

    def test_exact_30_days_is_one_month(self):
        end = self.NOW + timedelta(days=30)
        assert billing_calc.compute_upgrade_months_remaining(end, self.NOW) == 1

    def test_partial_month_rounds_up_to_two(self):
        end = self.NOW + timedelta(days=31)
        assert billing_calc.compute_upgrade_months_remaining(end, self.NOW) == 2

    def test_few_hours_left_rounds_up_to_one(self):
        end = self.NOW + timedelta(hours=3)
        assert billing_calc.compute_upgrade_months_remaining(end, self.NOW) == 1

    def test_already_past_end_clamps_to_minimum_one(self):
        """past_due: `now` уже позже `current_period_end` — минимум 1 месяц."""
        end = self.NOW - timedelta(days=2)
        assert billing_calc.compute_upgrade_months_remaining(end, self.NOW) == 1

    def test_end_equals_now_clamps_to_minimum_one(self):
        assert billing_calc.compute_upgrade_months_remaining(self.NOW, self.NOW) == 1


class TestComputeUpgradeAmount:
    def test_one_month_remaining(self):
        # (10000 - 5000) * 1 = 5000 руб = 500000 коп.
        assert billing_calc.compute_upgrade_amount(500_000, 1_000_000, 1) == 500_000

    def test_three_months_remaining(self):
        assert billing_calc.compute_upgrade_amount(500_000, 1_000_000, 3) == 1_500_000

    def test_no_discount_applied_to_upgrade_diff(self):
        """Скидка за период к доплате не применяется — просто линейная разница × месяцы."""
        amount = billing_calc.compute_upgrade_amount(500_000, 1_000_000, 6)
        assert amount == 500_000 * 6
