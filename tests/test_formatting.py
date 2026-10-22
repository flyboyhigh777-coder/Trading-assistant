from datetime import date
from decimal import Decimal

import pytest

from uzinvest.bonds import Position, cashflows, portfolio_cashflows
from uzinvest.formatting import (
    format_bond,
    format_bond_list,
    format_calendar,
    format_date,
    format_portfolio,
    format_quotes,
    plural_days,
)
from uzinvest.market import Quote

TODAY = date(2026, 10, 22)


def test_bond_card_leads_with_the_effective_rate(hmmt3b):
    text = format_bond(hmmt3b, today=TODAY)
    assert "29,33%" in text
    assert "26%" in text
    # Объяснение разницы — главная ценность карточки.
    assert "можно вложить обратно" in text


def test_bond_card_shows_the_next_payment_and_accrued(hmmt3b):
    text = format_bond(hmmt3b, today=TODAY)
    assert "ближайшая выплата" in text
    assert "НКД" in text


def test_bond_card_compares_with_a_deposit(hmmt3b):
    better = format_bond(hmmt3b, today=TODAY, deposit_rate=Decimal("0.22"))
    worse = format_bond(hmmt3b, today=TODAY, deposit_rate=Decimal("0.45"))
    assert "выгоднее" in better
    assert "хуже" in worse


def test_bond_card_shows_real_yield(hmmt3b):
    text = format_bond(hmmt3b, today=TODAY, inflation=Decimal("0.10"))
    assert "реальная доходность" in text
    assert "17,58%" in text


def test_matured_bond_says_so(hmmt3b):
    text = format_bond(hmmt3b, today=date(2030, 1, 1))
    assert "погашен" in text
    assert "ближайшая выплата" not in text


@pytest.mark.parametrize("quantity", [1, 3, 10])
def test_card_and_calendar_show_the_same_number(hmmt3b, quantity):
    card = format_bond(hmmt3b, today=TODAY, quantity=quantity)
    calendar = format_calendar(cashflows(hmmt3b, quantity, since=TODAY))
    coupon_line = next(line for line in card.splitlines() if line.startswith("выплата"))
    amount = coupon_line.split("<b>")[1].split("</b>")[0]
    assert amount in calendar


def test_issuer_and_ticker_are_escaped():
    from uzinvest.bonds import Bond

    nasty = Bond(
        isin="UZ1", ticker="<script>", issuer="A & B <b>", face_value=1000,
        coupon_rate="0.2", issue_date=date(2025, 1, 1),
        maturity_date=date(2026, 1, 1), per_year=12,
    )
    text = format_bond(nasty, today=TODAY)
    assert "<script>" not in text
    assert "&lt;SCRIPT&gt;" in text      # модель приводит тикер к верхнему регистру
    assert "A &amp; B" in text
    assert "&lt;b&gt;" in text


def test_empty_calendar_tells_you_what_to_do():
    assert "/add" in format_calendar([])


def test_calendar_marks_redemption_apart(hmmt3b):
    flows = cashflows(hmmt3b, since=date(2027, 12, 1))
    text = format_calendar(flows)
    assert "погашение" in text
    assert "купон" in text


def test_empty_portfolio_explains_how_to_fill_it(catalog):
    text = format_portfolio([], catalog, [])
    assert "/add" in text
    assert "/bonds" in text


def test_portfolio_totals_the_nominal_and_monthly_income(catalog, hmmt3b):
    positions = [Position(hmmt3b.isin, 3)]
    flows = portfolio_cashflows(positions, catalog, since=TODAY)
    text = format_portfolio(positions, catalog, flows, TODAY)
    assert "3\u202f000\u202f000" in text   # номинал 3 × 1 млн, узкий пробел
    assert "в среднем в месяц" in text


def test_portfolio_flags_a_paper_missing_from_the_catalog(catalog):
    text = format_portfolio([Position("UZ-ПРИЗРАК", 1)], catalog, [], TODAY)
    assert "нет в каталоге" in text


def test_bond_list_is_sorted_by_effective_rate(catalog):
    text = format_bond_list(catalog)
    assert text.index("DMMT2B3") < text.index("BFMT3B6")


def test_empty_catalog_points_at_the_example():
    assert "bonds.example.yaml" in format_bond_list({})


def test_quotes_lead_with_how_stale_they_are():
    quotes = [
        Quote("HMKB", price=Decimal("107.87"), last_trade=date(2026, 10, 22)),
        Quote("UZTL", price=Decimal("21000"), last_trade=date(2026, 10, 13)),
        Quote("UZMK", price=Decimal("7000")),
    ]
    text = format_quotes(quotes, TODAY)
    assert "сегодня" in text
    assert "9 дней назад" in text
    assert "сделок не было" in text
    assert "2 из 3" in text


def test_quotes_handle_an_empty_response():
    assert "не загрузились" in format_quotes([])


@pytest.mark.parametrize(
    "count,expected",
    [(1, "1 день"), (2, "2 дня"), (5, "5 дней"), (11, "11 дней"),
     (21, "21 день"), (22, "22 дня"), (14, "14 дней"), (101, "101 день")],
)
def test_days_are_declined_correctly(count, expected):
    assert plural_days(count) == expected


def test_dates_are_russian():
    assert format_date(date(2026, 1, 15)) == "15 января 2026"
    assert format_date(date(2026, 11, 2)) == "2 ноября 2026"
