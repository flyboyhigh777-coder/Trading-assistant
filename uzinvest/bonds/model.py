"""Модель облигации узбекского рынка."""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from ..money import to_decimal

#: Базис для расчёта купона по периодам в днях. На рынке РУз принята
#: фактическая база, год считается за 365 дней.
DAYS_IN_YEAR = Decimal(365)


class BondError(ValueError):
    """Параметры выпуска противоречивы."""


def add_months(start: date, months: int) -> date:
    """Прибавить месяцы, прижимая число к концу месяца.

    31 января + 1 месяц = 28 (или 29) февраля, а не 3 марта.
    """
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    day = min(start.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


@dataclass(frozen=True)
class Bond:
    """Выпуск облигаций с фиксированным купоном.

    Частота задаётся одним из двух способов, как её показывает биржа:
    ``per_year=12`` для «Месячный» или ``period_days=30`` для «30 дней».
    Это не синонимы: 365/30 = 12,17 периода в году, и доходность отличается.
    """

    isin: str
    ticker: str
    issuer: str
    face_value: Decimal           # номинал, сум
    coupon_rate: Decimal          # годовая ставка купона, 0.26 = 26%
    issue_date: date
    maturity_date: date
    per_year: int | None = None       # выплат в год
    period_days: int | None = None    # либо длина периода в днях
    currency: str = "UZS"
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "face_value", to_decimal(self.face_value))
        object.__setattr__(self, "coupon_rate", to_decimal(self.coupon_rate))
        object.__setattr__(self, "isin", self.isin.strip().upper())
        object.__setattr__(self, "ticker", self.ticker.strip().upper())

        if (self.per_year is None) == (self.period_days is None):
            raise BondError(
                f"{self.ticker}: задайте ровно одно — per_year или period_days"
            )
        if self.per_year is not None and not 1 <= self.per_year <= 365:
            raise BondError(f"{self.ticker}: per_year={self.per_year} вне диапазона")
        if self.period_days is not None and not 1 <= self.period_days <= 365:
            raise BondError(f"{self.ticker}: period_days={self.period_days} вне диапазона")
        if self.maturity_date <= self.issue_date:
            raise BondError(f"{self.ticker}: погашение не позже выпуска")
        if self.face_value <= 0:
            raise BondError(f"{self.ticker}: номинал должен быть положительным")
        if self.coupon_rate < 0:
            raise BondError(f"{self.ticker}: отрицательный купон")

    # -- производные величины --------------------------------------------

    @property
    def periods_per_year(self) -> Decimal:
        """Сколько купонных периодов укладывается в год."""
        if self.per_year is not None:
            return Decimal(self.per_year)
        return DAYS_IN_YEAR / Decimal(self.period_days)

    @property
    def coupon_amount(self) -> Decimal:
        """Размер одной купонной выплаты на одну облигацию."""
        if self.per_year is not None:
            return self.face_value * self.coupon_rate / Decimal(self.per_year)
        return (
            self.face_value * self.coupon_rate * Decimal(self.period_days) / DAYS_IN_YEAR
        )

    @property
    def frequency_label(self) -> str:
        if self.per_year is None:
            return f"каждые {self.period_days} дн."
        return {1: "раз в год", 2: "раз в полгода", 4: "ежеквартально",
                12: "ежемесячно"}.get(self.per_year, f"{self.per_year} раз в год")

    @property
    def years_to_maturity(self) -> Decimal:
        return Decimal((self.maturity_date - self.issue_date).days) / DAYS_IN_YEAR

    def is_matured(self, on: date) -> bool:
        return on >= self.maturity_date


@dataclass(frozen=True)
class Position:
    """Сколько бумаг и почём держит пользователь."""

    isin: str
    quantity: int
    purchase_price: Decimal | None = None   # цена за облигацию, если известна
    purchase_date: date | None = None
    note: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "isin", self.isin.strip().upper())
        if self.quantity <= 0:
            raise BondError("количество должно быть положительным")
        if self.purchase_price is not None:
            object.__setattr__(self, "purchase_price", to_decimal(self.purchase_price))


@dataclass(frozen=True)
class Cashflow:
    """Одна будущая выплата по бумаге."""

    date: date
    amount: Decimal
    kind: str            # "coupon" или "redemption"
    isin: str = ""
    quantity: int = 1
    labels: tuple[str, ...] = field(default_factory=tuple)

    @property
    def is_redemption(self) -> bool:
        return self.kind == "redemption"
