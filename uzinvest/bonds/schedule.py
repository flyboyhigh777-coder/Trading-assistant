"""Купонный календарь: когда и сколько придёт."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from ..money import round_sum
from .model import Bond, Cashflow, Position, add_months


def coupon_dates(bond: Bond) -> list[date]:
    """Все даты выплат купона, от первой до погашения включительно.

    Последний период подрезается датой погашения: если выпуск гасится не ровно
    в купонную дату, последний купон приходит вместе с номиналом.
    """
    dates: list[date] = []
    if bond.per_year is not None:
        step_months = 12 // bond.per_year if 12 % bond.per_year == 0 else None
        index = 1
        while True:
            if step_months is not None:
                moment = add_months(bond.issue_date, step_months * index)
            else:
                days = int(Decimal(365) / Decimal(bond.per_year) * index)
                moment = bond.issue_date + timedelta(days=days)
            if moment >= bond.maturity_date:
                break
            dates.append(moment)
            index += 1
    else:
        moment = bond.issue_date + timedelta(days=bond.period_days)
        while moment < bond.maturity_date:
            dates.append(moment)
            moment = moment + timedelta(days=bond.period_days)

    dates.append(bond.maturity_date)
    return dates


def period_bounds(bond: Bond, on: date) -> tuple[date, date]:
    """Начало и конец купонного периода, в который попадает дата."""
    start = bond.issue_date
    for moment in coupon_dates(bond):
        if on < moment:
            return start, moment
        start = moment
    return start, bond.maturity_date


def next_coupon(bond: Bond, on: date) -> tuple[date, Decimal] | None:
    """Ближайшая выплата после указанной даты, или None если бумага погашена."""
    for moment in coupon_dates(bond):
        if moment > on:
            amount = (
                bond.coupon_amount + bond.face_value
                if moment == bond.maturity_date
                else bond.coupon_amount
            )
            return moment, round_sum(amount)
    return None


def cashflows(
    bond: Bond,
    quantity: int = 1,
    since: date | None = None,
    until: date | None = None,
) -> list[Cashflow]:
    """Поток выплат по бумаге, начиная со дня после ``since``.

    Округление идёт на одну бумагу, а потом умножается на количество — так же,
    как это считает реестродержатель, и так же, как пользователь проверит
    руками: «купон 21 667, у меня три штуки». Округление итога дало бы на трёх
    бумагах 65 000 вместо 65 001, и цифры в /bond и /portfolio разошлись бы.
    """
    flows: list[Cashflow] = []
    for moment in coupon_dates(bond):
        if since is not None and moment <= since:
            continue
        if until is not None and moment > until:
            break
        coupon = round_sum(bond.coupon_amount) * quantity
        if moment == bond.maturity_date:
            # В дату погашения приходит и последний купон, и номинал —
            # это две разные строки, чтобы в календаре было видно почему сумма
            # резко больше остальных.
            flows.append(Cashflow(moment, coupon, "coupon", bond.isin, quantity))
            flows.append(
                Cashflow(
                    moment,
                    round_sum(bond.face_value) * quantity,
                    "redemption",
                    bond.isin,
                    quantity,
                )
            )
        else:
            flows.append(Cashflow(moment, coupon, "coupon", bond.isin, quantity))
    return flows


def portfolio_cashflows(
    positions: list[Position],
    catalog: dict[str, Bond],
    since: date | None = None,
    until: date | None = None,
) -> list[Cashflow]:
    """Собрать поток по всем позициям, отсортированный по дате."""
    flows: list[Cashflow] = []
    for position in positions:
        bond = catalog.get(position.isin)
        if bond is None:
            continue
        flows.extend(cashflows(bond, position.quantity, since, until))
    flows.sort(key=lambda f: (f.date, f.isin, f.kind))
    return flows


def monthly_totals(flows: list[Cashflow]) -> dict[tuple[int, int], Decimal]:
    """Свернуть поток по месяцам: {(год, месяц): сумма}."""
    totals: dict[tuple[int, int], Decimal] = {}
    for flow in flows:
        key = (flow.date.year, flow.date.month)
        totals[key] = totals.get(key, Decimal(0)) + flow.amount
    return dict(sorted(totals.items()))
