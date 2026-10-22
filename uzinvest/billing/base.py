"""Общий слой оплаты: тариф, заказ, интерфейс провайдера.

Payme и Click устроены противоположно привычным вебхукам: не мы зовём их и
ждём уведомления, а ОНИ зовут наш эндпоинт и ждут ответа в своём формате.
Поэтому провайдер здесь — это не клиент к API, а обработчик входящих вызовов.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import IntEnum
from typing import Protocol, runtime_checkable

from ..money import format_sum, to_decimal


class OrderState(IntEnum):
    """Состояние заказа. Значения совпадают с состояниями транзакции Payme."""

    CREATED = 1        # создан, денег ещё нет
    PAID = 2           # оплачен
    CANCELLED = -1     # отменён до оплаты
    REFUNDED = -2      # отменён после оплаты


@dataclass(frozen=True)
class Plan:
    """Тариф: сколько дней доступа и почём, в сумах."""

    code: str
    title: str
    days: int
    price: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "price", to_decimal(self.price))
        if self.days <= 0:
            raise ValueError(f"тариф {self.code}: дней должно быть больше нуля")
        if self.price <= 0:
            raise ValueError(f"тариф {self.code}: цена должна быть положительной")

    def describe(self) -> str:
        return f"{self.title} — {format_sum(self.price)}"

    @property
    def price_per_day(self) -> Decimal:
        return self.price / self.days


#: Цены в сумах. Ориентир: месяц ≈ цена двух чашек кофе в Ташкенте.
DEFAULT_PLANS: tuple[Plan, ...] = (
    Plan("week", "Неделя", 7, 15_000),
    Plan("month", "Месяц", 30, 49_000),
    Plan("quarter", "Три месяца", 90, 120_000),
)


def plans_by_code(plans: tuple[Plan, ...] = DEFAULT_PLANS) -> dict[str, Plan]:
    return {p.code: p for p in plans}


class BillingError(RuntimeError):
    """Оплата не прошла по нашей вине или по вине провайдера."""


@dataclass(frozen=True)
class PaymentLink:
    """Ссылка, по которой пользователь платит."""

    provider: str
    url: str
    order_id: str
    amount: Decimal
    title: str = ""


@runtime_checkable
class PaymentProvider(Protocol):
    """Что должен уметь платёжный провайдер."""

    name: str
    label: str

    def payment_link(self, order_id: str, plan: Plan) -> PaymentLink:
        """Ссылка на оплату заказа."""

    def is_configured(self) -> bool:
        """Заданы ли ключи, можно ли показывать этот способ пользователю."""
