"""Деньги в сумах.

Всё хранится в Decimal и считается точно: купон в 21 666.67 сум, умноженный на
36 выплат, не должен разъезжаться с реальностью из-за float. Тийины нужны
только для Payme, который принимает суммы в них.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

#: В одном суме 100 тийинов. Payme оперирует тийинами, Click — сумами.
TIYIN_IN_SUM = 100

SUM = Decimal("1")
TIYIN = Decimal("0.01")


class MoneyError(ValueError):
    """Сумма не распознана или не имеет смысла."""


def to_decimal(value: object) -> Decimal:
    """Привести что угодно разумное к Decimal, не теряя точность."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # Через str, иначе 0.1 превратится в 0.1000000000000000055511151231257827
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = value.replace(" ", "").replace(" ", "").replace(",", ".")
        try:
            return Decimal(cleaned)
        except InvalidOperation as exc:
            raise MoneyError(f"не похоже на сумму: {value!r}") from exc
    raise MoneyError(f"не похоже на сумму: {value!r}")


def round_sum(value: object) -> Decimal:
    """Округлить до целого сума — копеек в обороте нет."""
    return to_decimal(value).quantize(SUM, rounding=ROUND_HALF_UP)


def to_tiyin(value: object) -> int:
    """Сумы → тийины, для Payme."""
    return int(to_decimal(value).quantize(TIYIN, rounding=ROUND_HALF_UP) * TIYIN_IN_SUM)


def from_tiyin(value: int) -> Decimal:
    """Тийины → сумы."""
    return (Decimal(int(value)) / TIYIN_IN_SUM).quantize(TIYIN)


def format_sum(value: object, decimals: int = 0, unit: str = "сум") -> str:
    """Отформатировать по-местному: 1 163 376 сум, разделитель — узкий пробел."""
    amount = to_decimal(value)
    quantum = Decimal(1).scaleb(-decimals) if decimals else SUM
    amount = amount.quantize(quantum, rounding=ROUND_HALF_UP)
    whole, _, fraction = f"{abs(amount):f}".partition(".")
    grouped = f"{int(whole):,}".replace(",", " ")
    sign = "−" if amount < 0 else ""
    body = f"{sign}{grouped}" + (f",{fraction}" if decimals and fraction else "")
    return f"{body} {unit}" if unit else body


def format_percent(value: object, decimals: int = 2) -> str:
    """0.2933 → «29,33%». Запятая как десятичный разделитель."""
    percent = to_decimal(value) * 100
    return f"{percent:.{decimals}f}".replace(".", ",") + "%"
