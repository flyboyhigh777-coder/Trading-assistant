from datetime import date
from decimal import Decimal

import pytest

from uzinvest.bonds import (
    Bond,
    BondError,
    Position,
    accrued_interest,
    add_months,
    cashflows,
    compare_with_deposit,
    coupon_dates,
    coupon_payment,
    effective_annual_rate,
    monthly_totals,
    next_coupon,
    portfolio_cashflows,
    price_from_yield,
    real_rate,
    total_with_reinvestment,
    yield_to_maturity,
)


def approx(value, expected, tol="0.0001"):
    return abs(Decimal(value) - Decimal(expected)) < Decimal(tol)


# -- модель ---------------------------------------------------------------


def test_coupon_matches_the_card(hmmt3b):
    # 1 000 000 × 26% ÷ 12 = 21 666,67
    assert approx(hmmt3b.coupon_amount, "21666.6666", tol="0.01")
    assert coupon_payment(hmmt3b) == Decimal("21667")


def test_thirty_days_is_not_a_month(bfmt3b6):
    # 365/30 = 12,1667 периода в году, а не 12 — и купон считается от дней.
    assert approx(bfmt3b6.periods_per_year, "12.1667", tol="0.001")
    assert approx(bfmt3b6.coupon_amount, "1972.6027", tol="0.01")
    assert bfmt3b6.frequency_label == "каждые 30 дн."


def test_frequency_must_be_given_exactly_one_way():
    base = dict(
        isin="UZ1", ticker="X", issuer="И", face_value=1000, coupon_rate="0.2",
        issue_date=date(2025, 1, 1), maturity_date=date(2026, 1, 1),
    )
    with pytest.raises(BondError, match="ровно одно"):
        Bond(**base)
    with pytest.raises(BondError, match="ровно одно"):
        Bond(**base, per_year=12, period_days=30)


@pytest.mark.parametrize(
    "bad,message",
    [
        (dict(per_year=12, maturity_date=date(2024, 1, 1)), "не позже выпуска"),
        (dict(per_year=12, face_value=0), "номинал"),
        (dict(per_year=12, coupon_rate="-0.1"), "отрицательный"),
        (dict(per_year=400), "вне диапазона"),
    ],
)
def test_nonsense_issues_are_rejected(bad, message):
    base = dict(
        isin="UZ1", ticker="X", issuer="И", face_value=1000, coupon_rate="0.2",
        issue_date=date(2025, 1, 1), maturity_date=date(2026, 1, 1),
    )
    with pytest.raises(BondError, match=message):
        Bond(**{**base, **bad})


@pytest.mark.parametrize(
    "start,months,expected",
    [
        (date(2025, 1, 31), 1, date(2025, 2, 28)),   # прижимаем к концу месяца
        (date(2024, 1, 31), 1, date(2024, 2, 29)),   # високосный
        (date(2025, 1, 15), 12, date(2026, 1, 15)),
        (date(2025, 12, 15), 1, date(2026, 1, 15)),  # через год
    ],
)
def test_month_arithmetic_clamps_to_month_end(start, months, expected):
    assert add_months(start, months) == expected


# -- календарь ------------------------------------------------------------


def test_monthly_bond_pays_once_a_month_until_maturity(hmmt3b):
    dates = coupon_dates(hmmt3b)
    assert len(dates) == 36               # 3 года × 12
    assert dates[0] == date(2025, 2, 15)
    assert dates[-1] == hmmt3b.maturity_date


def test_thirty_day_bond_pays_more_often(bfmt3b6):
    # 3 года по 30 дней — это 36 полных периодов плюс остаток.
    assert len(coupon_dates(bfmt3b6)) == 37


def test_redemption_pays_coupon_and_face_separately(hmmt3b):
    flows = cashflows(hmmt3b, quantity=1)
    final = [f for f in flows if f.date == hmmt3b.maturity_date]
    assert len(final) == 2
    assert {f.kind for f in final} == {"coupon", "redemption"}
    assert sum(f.amount for f in final) == Decimal("1021667")


def test_next_coupon_bundles_the_face_value_at_maturity(hmmt3b):
    moment, amount = next_coupon(hmmt3b, date(2027, 12, 20))
    assert moment == hmmt3b.maturity_date
    assert amount == Decimal("1021667")


def test_next_coupon_is_none_once_matured(hmmt3b):
    assert next_coupon(hmmt3b, date(2030, 1, 1)) is None


def test_cashflows_scale_exactly_with_quantity(hmmt3b):
    # Округление на бумагу, затем умножение: пользователь должен уметь
    # проверить сумму в уме, умножив свой купон на количество.
    one = cashflows(hmmt3b, 1)
    ten = cashflows(hmmt3b, 10)
    assert len(one) == len(ten)
    assert ten[0].amount == one[0].amount * 10
    assert ten[0].amount == coupon_payment(hmmt3b) * 10


def test_cashflows_can_be_windowed(hmmt3b):
    window = cashflows(hmmt3b, since=date(2025, 6, 1), until=date(2025, 12, 31))
    assert [f.date.month for f in window] == [6, 7, 8, 9, 10, 11, 12]


# -- НКД ------------------------------------------------------------------


def test_accrued_interest_grows_through_the_period(hmmt3b):
    # 17 дней из 31: 21 666,67 × 17/31 = 11 881,7
    assert accrued_interest(hmmt3b, date(2025, 2, 1)) == Decimal("11882")


def test_accrued_interest_is_zero_at_the_start_and_after_maturity(hmmt3b):
    assert accrued_interest(hmmt3b, hmmt3b.issue_date) == 0
    assert accrued_interest(hmmt3b, date(2030, 1, 1)) == 0


@pytest.mark.parametrize("quantity", [1, 3, 7, 100])
def test_card_and_calendar_never_disagree(hmmt3b, quantity):
    # Пользователь видит одну и ту же выплату в /bond и в /calendar.
    # Если округлять то на бумагу, то на итог — числа разойдутся на сум,
    # и доверие к расчёту закончится.
    from_card = coupon_payment(hmmt3b, quantity)
    from_calendar = next(
        f.amount for f in cashflows(hmmt3b, quantity) if f.kind == "coupon"
    )
    assert from_card == from_calendar
    assert from_card == coupon_payment(hmmt3b) * quantity


@pytest.mark.parametrize("quantity", [1, 3, 7])
def test_accrued_interest_scales_per_bond_too(hmmt3b, quantity):
    one = accrued_interest(hmmt3b, date(2025, 2, 1))
    assert accrued_interest(hmmt3b, date(2025, 2, 1), quantity) == one * quantity


def test_accrued_interest_never_exceeds_one_coupon(hmmt3b):
    day = hmmt3b.issue_date
    while day < hmmt3b.maturity_date:
        assert accrued_interest(hmmt3b, day) <= coupon_payment(hmmt3b)
        day = add_months(day, 1)


# -- доходность -----------------------------------------------------------


def test_effective_rate_beats_the_number_on_the_card(hmmt3b):
    # Карточка говорит 26%. С ежемесячным купоном и реинвестом — 29,33%.
    assert approx(effective_annual_rate(hmmt3b), "0.29333398", tol="0.00000001")
    assert effective_annual_rate(hmmt3b) > hmmt3b.coupon_rate


def test_annual_coupon_has_no_compounding_bonus():
    yearly = Bond("UZ2", "Y", "И", 1000, "0.26", date(2025, 1, 1),
                  date(2030, 1, 1), per_year=1)
    assert effective_annual_rate(yearly) == Decimal("0.26")


def test_reinvested_total_matches_the_compound_formula(hmmt3b):
    assert total_with_reinvestment(hmmt3b, 3) == Decimal("1163376")


def test_real_rate_is_fisher_not_subtraction():
    # Вычитание дало бы 19,33%, что завышает результат почти на два пункта.
    result = real_rate("0.29333398", "0.10")
    assert approx(result, "0.17575816", tol="0.00000001")
    assert result < Decimal("0.29333398") - Decimal("0.10")


def test_real_rate_rejects_impossible_inflation():
    with pytest.raises(ValueError):
        real_rate("0.2", "-1")


def test_ytm_at_par_equals_the_effective_rate(hmmt3b):
    ytm = yield_to_maturity(hmmt3b, 1_000_000, hmmt3b.issue_date)
    assert approx(ytm, effective_annual_rate(hmmt3b), tol="0.001")


def test_ytm_rises_when_you_pay_less(hmmt3b):
    cheap = yield_to_maturity(hmmt3b, 900_000, hmmt3b.issue_date)
    par = yield_to_maturity(hmmt3b, 1_000_000, hmmt3b.issue_date)
    dear = yield_to_maturity(hmmt3b, 1_100_000, hmmt3b.issue_date)
    assert cheap > par > dear


def test_ytm_round_trips_through_price(hmmt3b):
    settle = date(2025, 4, 1)
    target = Decimal("0.31")
    price = price_from_yield(hmmt3b, target, settle)
    assert approx(yield_to_maturity(hmmt3b, price, settle), target, tol="0.0001")


def test_ytm_is_none_for_a_matured_bond(hmmt3b):
    assert yield_to_maturity(hmmt3b, 1_000_000, date(2030, 1, 1)) is None


def test_ytm_rejects_a_nonsense_price(hmmt3b):
    with pytest.raises(ValueError):
        yield_to_maturity(hmmt3b, 0, hmmt3b.issue_date)


def test_deposit_comparison_puts_both_on_effective_terms(hmmt3b):
    result = compare_with_deposit(hmmt3b, "0.22", deposit_per_year=12)
    assert approx(result["deposit_effective"], "0.243599", tol="0.00001")
    assert result["advantage"] == result["bond_effective"] - result["deposit_effective"]
    assert result["advantage"] > 0


def test_a_better_deposit_wins(hmmt3b):
    assert compare_with_deposit(hmmt3b, "0.40")["advantage"] < 0


# -- портфель -------------------------------------------------------------


def test_portfolio_merges_and_sorts_flows(catalog, hmmt3b, dmmt2b3):
    positions = [Position(hmmt3b.isin, 2), Position(dmmt2b3.isin, 1)]
    flows = portfolio_cashflows(positions, catalog, until=date(2025, 12, 31))
    assert flows == sorted(flows, key=lambda f: (f.date, f.isin, f.kind))
    assert {f.isin for f in flows} == {hmmt3b.isin, dmmt2b3.isin}


def test_portfolio_skips_unknown_isins(catalog, hmmt3b):
    flows = portfolio_cashflows(
        [Position(hmmt3b.isin, 1), Position("UZ-НЕТ-ТАКОЙ", 5)], catalog
    )
    assert {f.isin for f in flows} == {hmmt3b.isin}


def test_monthly_totals_roll_up_the_calendar(catalog, hmmt3b):
    flows = portfolio_cashflows(
        [Position(hmmt3b.isin, 3)], catalog, until=date(2025, 12, 31)
    )
    totals = monthly_totals(flows)
    assert list(totals) == sorted(totals)
    assert totals[(2025, 6)] == coupon_payment(hmmt3b) * 3


def test_quantity_must_be_positive():
    with pytest.raises(BondError):
        Position("UZ1", 0)
