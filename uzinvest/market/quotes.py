"""Котировка бумаги и главная метрика этого рынка — давность сделки."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from ..money import MoneyError, to_decimal

#: Сколько дней без сделок считать «бумага стоит». На УзРФБ неделя без
#: сделок — обычное дело, поэтому порог высокий.
STALE_AFTER_DAYS = 5

_DATE_FORMATS = ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m.%y")


def parse_price(raw: str) -> Decimal | None:
    """Вытащить число из ячейки: «21 000.00 UZS», «3 398,99», «—»."""
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.,\-]", "", raw.replace(" ", "").replace(" ", ""))
    if not cleaned or cleaned in {"-", "—", "."}:
        return None
    # «1,234.56» — разделитель тысяч запятая; «1234,56» — запятая десятичная.
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    try:
        return to_decimal(cleaned)
    except MoneyError:
        return None


def parse_date(raw: str) -> date | None:
    """Разобрать дату в любом из форматов, которые встречаются на бирже."""
    text = (raw or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class Quote:
    """Одна строка из таблицы котировок."""

    ticker: str
    name: str = ""
    price: Decimal | None = None
    last_trade: date | None = None
    change: Decimal | None = None
    currency: str = "UZS"

    def days_since_trade(self, today: date | None = None) -> int | None:
        """Сколько дней бумага не торговалась.

        На этом рынке — важнее самой цены: цена без сделок это не цена, а
        последнее, о чём кто-то когда-то договорился.
        """
        if self.last_trade is None:
            return None
        return ((today or date.today()) - self.last_trade).days

    def is_stale(self, today: date | None = None, threshold: int = STALE_AFTER_DAYS) -> bool:
        days = self.days_since_trade(today)
        return days is None or days >= threshold
