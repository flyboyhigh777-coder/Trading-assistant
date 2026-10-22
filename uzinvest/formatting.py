"""Рендер для Telegram (HTML) и для консоли."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from html import escape

from .bonds import (
    Bond,
    Cashflow,
    Position,
    accrued_interest,
    compare_with_deposit,
    coupon_payment,
    effective_annual_rate,
    monthly_totals,
    next_coupon,
    real_rate,
    total_with_reinvestment,
)
from .market.quotes import Quote
from .money import format_percent, format_sum

MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

DISCLAIMER = (
    "<i>Расчёт справочный. Параметры выпуска сверяйте с проспектом эмиссии: "
    "ошибка в дате или ставке меняет весь результат.</i>"
)


def format_date(moment: date) -> str:
    return f"{moment.day} {MONTHS[moment.month - 1]} {moment.year}"


def plural_days(count: int) -> str:
    if 11 <= count % 100 <= 14:
        return f"{count} дней"
    return f"{count} " + {1: "день", 2: "дня", 3: "дня", 4: "дня"}.get(count % 10, "дней")


def format_bond(
    bond: Bond,
    today: date | None = None,
    deposit_rate: Decimal | None = None,
    inflation: Decimal | None = None,
    quantity: int = 1,
) -> str:
    """Карточка выпуска — с тем, чего нет в приложении брокера."""
    today = today or date.today()
    simple = bond.coupon_rate
    effective = effective_annual_rate(bond)

    lines = [
        f"<b>{escape(bond.ticker)}</b> · {escape(bond.issuer)}",
        f"<code>{escape(bond.isin)}</code>",
        "",
        f"номинал <b>{format_sum(bond.face_value)}</b>",
        f"купон <b>{format_percent(simple, 0)}</b> годовых, {bond.frequency_label}",
        f"выплата <b>{format_sum(coupon_payment(bond, quantity))}</b>"
        + (f" за {quantity} шт." if quantity > 1 else ""),
        f"погашение {format_date(bond.maturity_date)}",
        "",
        f"<b>Эффективная ставка {format_percent(effective)}</b>",
        f"<i>на карточке написано {format_percent(simple, 0)} — разница от того, "
        f"что купон приходит {bond.frequency_label} и его можно вложить обратно</i>",
    ]

    if not bond.is_matured(today):
        years = bond.years_to_maturity
        lines.append(
            f"за весь срок с реинвестом: "
            f"<b>{format_sum(total_with_reinvestment(bond, years) * quantity)}</b> "
            f"процентами"
        )

    if inflation is not None:
        lines += [
            "",
            f"реальная доходность при инфляции {format_percent(inflation, 0)}: "
            f"<b>{format_percent(real_rate(effective, inflation))}</b>",
        ]
    if deposit_rate is not None:
        comparison = compare_with_deposit(bond, deposit_rate)
        verdict = "выгоднее" if comparison["advantage"] > 0 else "хуже"
        lines.append(
            f"против вклада под {format_percent(deposit_rate, 0)}: "
            f"<b>{verdict} на {format_percent(abs(comparison['advantage']))}</b>"
        )

    upcoming = next_coupon(bond, today)
    if upcoming:
        moment, amount = upcoming
        days = (moment - today).days
        lines += [
            "",
            f"ближайшая выплата {format_date(moment)} "
            f"(через {plural_days(days)}): <b>{format_sum(amount * quantity)}</b>",
            f"НКД на сегодня: {format_sum(accrued_interest(bond, today, quantity))}",
        ]
    else:
        lines += ["", "<i>выпуск погашен</i>"]

    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def format_calendar(flows: Sequence[Cashflow], limit: int = 12) -> str:
    """Ближайшие выплаты списком."""
    if not flows:
        return "Выплат не нашлось. Добавьте бумаги: /add ISIN КОЛИЧЕСТВО"

    lines = ["<b>Ближайшие выплаты</b>", ""]
    total = Decimal(0)
    for flow in flows[:limit]:
        mark = "🏁" if flow.is_redemption else "💵"
        label = "погашение" if flow.is_redemption else "купон"
        lines.append(
            f"{mark} {format_date(flow.date)} — <b>{format_sum(flow.amount)}</b> "
            f"<i>{label}, {escape(flow.isin)}</i>"
        )
        total += flow.amount
    if len(flows) > limit:
        lines.append(f"<i>…и ещё {len(flows) - limit}</i>")
    lines += ["", f"итого показано: <b>{format_sum(total)}</b>"]
    return "\n".join(lines)


def format_portfolio(
    positions: Sequence[Position],
    catalog: dict[str, Bond],
    flows: Sequence[Cashflow],
    today: date | None = None,
) -> str:
    """Портфель: что лежит, сколько приносит, когда."""
    today = today or date.today()
    if not positions:
        return (
            "Портфель пуст.\n\n"
            "Добавить бумагу: <code>/add UZ6058537AA0 3</code>\n"
            "Список выпусков: /bonds"
        )

    lines = ["<b>Портфель</b>", ""]
    face_total = Decimal(0)
    monthly_income = Decimal(0)

    for position in positions:
        bond = catalog.get(position.isin)
        if bond is None:
            lines.append(
                f"⚠️ <code>{escape(position.isin)}</code> × {position.quantity} — "
                f"нет в каталоге"
            )
            continue
        face = bond.face_value * position.quantity
        face_total += face
        coupon = coupon_payment(bond, position.quantity)
        # Приводим к месяцу, чтобы «30 дней» и «ежемесячно» можно было сложить.
        monthly_income += coupon * bond.periods_per_year / 12
        lines.append(
            f"<b>{escape(bond.ticker)}</b> × {position.quantity} — "
            f"{format_sum(face)} номиналом"
        )
        lines.append(
            f"    {format_sum(coupon)} {bond.frequency_label}, "
            f"{format_percent(effective_annual_rate(bond))} эффективных"
        )

    lines += [
        "",
        f"номинал всего: <b>{format_sum(face_total)}</b>",
        f"в среднем в месяц: <b>{format_sum(monthly_income)}</b>",
    ]

    totals = monthly_totals(list(flows))
    if totals:
        lines += ["", "<b>По месяцам</b>"]
        for (year, month), amount in list(totals.items())[:6]:
            lines.append(f"{MONTHS[month - 1]} {year}: <b>{format_sum(amount)}</b>")

    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def format_bond_list(catalog: dict[str, Bond], limit: int = 20) -> str:
    """Все выпуски каталога, по убыванию эффективной ставки."""
    if not catalog:
        return "Каталог пуст. Заполните bonds.yaml — см. bonds.example.yaml."

    ranked = sorted(catalog.values(), key=lambda b: -effective_annual_rate(b))
    lines = ["<b>Выпуски</b>", "<i>отсортированы по эффективной ставке</i>", ""]
    for bond in ranked[:limit]:
        lines.append(
            f"<b>{escape(bond.ticker)}</b> — {format_percent(effective_annual_rate(bond))} "
            f"<i>(купон {format_percent(bond.coupon_rate, 0)}, {bond.frequency_label})</i>"
        )
        lines.append(f"    <code>{escape(bond.isin)}</code> · {escape(bond.issuer)}")
    if len(ranked) > limit:
        lines.append(f"<i>…и ещё {len(ranked) - limit}</i>")
    lines += ["", "Подробнее: <code>/bond HMMT3B</code>"]
    return "\n".join(lines)


def format_quotes(quotes: Sequence[Quote], today: date | None = None, limit: int = 20) -> str:
    """Котировки акций с давностью сделки — главным, а не мелким шрифтом."""
    today = today or date.today()
    if not quotes:
        return "Котировки не загрузились."

    lines = ["<b>Акции</b>", ""]
    for quote in quotes[:limit]:
        days = quote.days_since_trade(today)
        if days is None:
            freshness = "сделок не было"
        elif days == 0:
            freshness = "сегодня"
        else:
            freshness = f"{plural_days(days)} назад"
        mark = "🟡" if quote.is_stale(today) else "🟢"
        price = format_sum(quote.price) if quote.price is not None else "—"
        lines.append(
            f"{mark} <b>{escape(quote.ticker)}</b> {price} "
            f"<i>· {freshness}</i>"
        )
    stale = sum(1 for q in quotes if q.is_stale(today))
    lines += [
        "",
        f"<i>{stale} из {len(quotes)} не торговались пять дней и дольше. "
        f"Цена без сделок — не цена, а последнее, о чём когда-то договорились.</i>",
    ]
    return "\n".join(lines)


def format_bond_plain(bond: Bond) -> str:
    """Одна строка для консоли."""
    return (
        f"{bond.ticker:<10} {bond.isin:<14} "
        f"{format_percent(bond.coupon_rate, 0):>5} → "
        f"{format_percent(effective_annual_rate(bond)):>7}  "
        f"{format_sum(coupon_payment(bond)):>14}  {bond.frequency_label}"
    )
