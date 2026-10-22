"""Доходность: НКД, эффективная ставка, YTM, реальная доходность.

Всё считается точно и не требует истории цен — поэтому работает на бумагах,
по которым сделок не было неделями.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ..money import round_sum, to_decimal
from .model import DAYS_IN_YEAR, Bond
from .schedule import cashflows, period_bounds


def coupon_payment(bond: Bond, quantity: int = 1) -> Decimal:
    """Размер одной выплаты, округлённый до сума.

    Округление на одну бумагу, затем умножение — ровно так же, как в
    :func:`~uzinvest.bonds.schedule.cashflows`. Иначе карточка выпуска и
    календарь показали бы на трёх бумагах 65 000 и 65 001.
    """
    return round_sum(bond.coupon_amount) * quantity


def accrued_interest(bond: Bond, on: date, quantity: int = 1) -> Decimal:
    """НКД — накопленный купонный доход на дату.

    Это то, что покупатель доплачивает продавцу сверх цены: купон за дни,
    которые бумага пролежала у продавца в текущем периоде.
    """
    if on <= bond.issue_date:
        return Decimal(0)
    if bond.is_matured(on):
        return Decimal(0)
    start, end = period_bounds(bond, on)
    period_days = (end - start).days
    if period_days <= 0:
        return Decimal(0)
    elapsed = (on - start).days
    accrued = bond.coupon_amount * Decimal(elapsed) / Decimal(period_days)
    return round_sum(accrued) * quantity


def simple_annual_rate(bond: Bond) -> Decimal:
    """Купонная ставка как есть — то число, что написано на карточке."""
    return bond.coupon_rate


def effective_annual_rate(bond: Bond) -> Decimal:
    """Эффективная годовая ставка при реинвестировании купонов под ту же ставку.

    Это то, чего нет на карточке выпуска: 26% с ежемесячным купоном — это
    29,33% годовых, если купоны вкладывать обратно.
    """
    n = bond.periods_per_year
    per_period = bond.coupon_rate / n
    return (1 + per_period) ** n - 1


def total_with_reinvestment(bond: Bond, years: Decimal | float | int) -> Decimal:
    """Сколько нарастёт процентов на номинал за N лет с реинвестом."""
    n = bond.periods_per_year
    periods = n * to_decimal(years)
    grown = bond.face_value * ((1 + bond.coupon_rate / n) ** periods - 1)
    return round_sum(grown)


def real_rate(nominal: Decimal | float | str, inflation: Decimal | float | str) -> Decimal:
    """Доходность за вычетом инфляции, по Фишеру.

    Вычитать инфляцию из ставки — распространённая, но неверная привычка:
    при 29% и 10% реальная ставка 17,6%, а не 19%.
    """
    nominal_d = to_decimal(nominal)
    inflation_d = to_decimal(inflation)
    if inflation_d <= -1:
        raise ValueError("инфляция не может быть −100% и ниже")
    return (1 + nominal_d) / (1 + inflation_d) - 1


def price_from_yield(
    bond: Bond, annual_yield: Decimal, settle: date, quantity: int = 1
) -> Decimal:
    """Приведённая стоимость будущих выплат при заданной годовой доходности."""
    total = Decimal(0)
    for flow in cashflows(bond, quantity, since=settle):
        years = Decimal((flow.date - settle).days) / DAYS_IN_YEAR
        total += flow.amount / (1 + annual_yield) ** years
    return total


def yield_to_maturity(
    bond: Bond,
    price: Decimal | float | str,
    settle: date,
    quantity: int = 1,
    tolerance: Decimal = Decimal("0.0000001"),
    max_iterations: int = 200,
) -> Decimal | None:
    """YTM: ставка, при которой приведённые выплаты равны уплаченной цене.

    Цена — «грязная», то есть с НКД, как её реально платят. Решается делением
    пополам: функция монотонно убывает по ставке, так что сходится всегда, а
    производную считать не нужно.
    """
    paid = to_decimal(price)
    if paid <= 0:
        raise ValueError("цена должна быть положительной")
    if bond.is_matured(settle):
        return None

    low, high = Decimal("-0.9"), Decimal("10")
    if price_from_yield(bond, high, settle, quantity) > paid:
        return None   # даже 1000% годовых не опускает цену так низко

    for _ in range(max_iterations):
        middle = (low + high) / 2
        value = price_from_yield(bond, middle, settle, quantity)
        if abs(value - paid) < tolerance * paid:
            return middle
        if value > paid:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def compare_with_deposit(
    bond: Bond, deposit_rate: Decimal | float | str, deposit_per_year: int = 12
) -> dict[str, Decimal]:
    """Сопоставить выпуск с банковским вкладом на равных основаниях.

    Обе ставки приводятся к эффективным годовым, иначе сравниваются числа,
    посчитанные по разным правилам.
    """
    rate = to_decimal(deposit_rate)
    n = Decimal(deposit_per_year)
    deposit_effective = (1 + rate / n) ** n - 1
    bond_effective = effective_annual_rate(bond)
    return {
        "bond_effective": bond_effective,
        "deposit_effective": deposit_effective,
        "advantage": bond_effective - deposit_effective,
    }
