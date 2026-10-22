"""Каталог выпусков и разбор страницы биржи."""

from datetime import date
from decimal import Decimal

import pytest

from uzinvest.market import (
    CatalogError,
    MarketDataError,
    Quote,
    extract_tables,
    find,
    load_catalog,
    parse_date,
    parse_price,
    parse_quotes,
    pick_quote_table,
)

# Страница биржи, как её видно по колонкам на uzse.uz. Классы и обёртки
# намеренно «шумные»: парсер не должен на них опираться.
PAGE = """
<html><body><div class="layout"><table class="nav"><tr><td>меню</td></tr></table>
<section><table id="t-7" class="table table-striped">
<thead><tr>
  <th>Тикер</th><th>Наименование</th><th>Цена открытия</th>
  <th>Цена&nbsp;котировки</th><th>Изменение</th><th>Дата последней сделки</th>
</tr></thead><tbody>
  <tr><td>HMKB</td><td>HAMKORBANK ATB</td><td>107.00</td><td>107.87</td>
      <td>0.00</td><td>21.10.2026</td></tr>
  <tr><td>UZTL</td><td>O'ZBEKTELEKOM</td><td>21 000.00</td><td>21 000.00</td>
      <td>0.00</td><td>13.10.2026</td></tr>
  <tr><td>UZMK</td><td>O'ZMETKOMBINAT</td><td>—</td><td>7 000.00</td>
      <td>0.00</td><td></td></tr>
</tbody></table></section></div></body></html>
"""


# -- разбор ячеек ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("21 000.00 UZS", Decimal("21000.00")),
        ("3 398,99", Decimal("3398.99")),
        ("1,234.56", Decimal("1234.56")),
        ("107.87", Decimal("107.87")),
    ],
)
def test_prices_parse(raw, expected):
    assert parse_price(raw) == expected


@pytest.mark.parametrize("raw", ["", "—", "-", "н/д", None])
def test_missing_prices_are_none(raw):
    assert parse_price(raw) is None


@pytest.mark.parametrize(
    "raw,expected",
    [("21.10.2026", date(2026, 10, 21)), ("2026-10-21", date(2026, 10, 21))],
)
def test_dates_parse(raw, expected):
    assert parse_date(raw) == expected


@pytest.mark.parametrize("raw", ["", "никогда", "32.13.2026"])
def test_bad_dates_are_none(raw):
    assert parse_date(raw) is None


# -- таблицы --------------------------------------------------------------


def test_the_quote_table_is_picked_out_of_the_page():
    table = pick_quote_table(extract_tables(PAGE))
    assert table is not None
    assert len(table.body) == 3          # навигация не прошла отбор
    assert table.column_index("тикер") == 0


def test_columns_are_found_by_header_text_not_position():
    # Биржа может переставить колонки местами — парсер это переживает.
    shuffled = PAGE.replace(
        "<th>Тикер</th><th>Наименование</th>", "<th>Наименование</th><th>Тикер</th>"
    ).replace(
        "<td>HMKB</td><td>HAMKORBANK ATB</td>", "<td>HAMKORBANK ATB</td><td>HMKB</td>"
    )
    quotes = parse_quotes(shuffled)
    assert quotes[0].ticker == "HMKB"
    assert quotes[0].name == "HAMKORBANK ATB"


def test_quotes_are_parsed_from_the_page():
    quotes = parse_quotes(PAGE)
    assert [q.ticker for q in quotes] == ["HMKB", "UZTL", "UZMK"]
    assert quotes[1].price == Decimal("21000.00")
    assert quotes[0].last_trade == date(2026, 10, 21)
    assert quotes[2].last_trade is None


def test_a_page_without_a_quote_table_says_what_to_do():
    with pytest.raises(MarketDataError, match="COLUMN_HINTS"):
        parse_quotes("<html><body><p>ничего</p></body></html>")


def test_nested_and_unclosed_tables_do_not_break_the_parser():
    broken = "<table><tr><th>Тикер</th><th>Цена</th></tr><tr><td>X</td><td>1</td></tr>"
    quotes = parse_quotes(broken)
    assert [q.ticker for q in quotes] == ["X"]


# -- давность сделки ------------------------------------------------------


def test_days_since_trade_is_the_metric_that_matters():
    quote = Quote("UZTL", price=Decimal("21000"), last_trade=date(2026, 10, 13))
    assert quote.days_since_trade(date(2026, 10, 22)) == 9
    assert quote.is_stale(date(2026, 10, 22))


def test_a_traded_today_quote_is_fresh():
    quote = Quote("HMKB", price=Decimal("107.87"), last_trade=date(2026, 10, 22))
    assert quote.days_since_trade(date(2026, 10, 22)) == 0
    assert not quote.is_stale(date(2026, 10, 22))


def test_a_quote_that_never_traded_counts_as_stale():
    quote = Quote("UZMK", price=Decimal("7000"))
    assert quote.days_since_trade(date(2026, 10, 22)) is None
    assert quote.is_stale(date(2026, 10, 22))


# -- каталог --------------------------------------------------------------


CATALOG_YAML = """
bonds:
  - isin: UZ6058537AA0
    ticker: HMMT3B
    issuer: Hamkorbank
    face_value: 1000000
    coupon_rate: 0.26
    per_year: 12
    issue_date: 2025-01-15
    maturity_date: 2028-01-15
  - isin: UZ6057687AF3
    ticker: BFMT3B6
    issuer: Ipoteka-bank
    face_value: 100000
    coupon_rate: 0.24
    period_days: 30
    issue_date: 01.03.2025
    maturity_date: 01.03.2028
"""


def test_catalog_loads_both_date_formats(tmp_path):
    path = tmp_path / "bonds.yaml"
    path.write_text(CATALOG_YAML, encoding="utf-8")
    catalog = load_catalog(path)
    assert set(catalog) == {"UZ6058537AA0", "UZ6057687AF3"}
    assert catalog["UZ6057687AF3"].issue_date == date(2025, 3, 1)


def test_one_broken_entry_does_not_lose_the_rest(tmp_path, caplog):
    path = tmp_path / "bonds.yaml"
    path.write_text(
        CATALOG_YAML + """
  - ticker: СЛОМАН
    face_value: 1000
""",
        encoding="utf-8",
    )
    catalog = load_catalog(path)
    assert len(catalog) == 2
    assert "СЛОМАН" in caplog.text


def test_a_missing_catalog_is_reported(tmp_path):
    with pytest.raises(CatalogError, match="не найден"):
        load_catalog(tmp_path / "нет.yaml")


def test_broken_yaml_is_reported(tmp_path):
    path = tmp_path / "bonds.yaml"
    path.write_text("bonds: [ не закрыт", encoding="utf-8")
    with pytest.raises(CatalogError, match="YAML"):
        load_catalog(path)


def test_lookup_works_by_isin_and_ticker(catalog, hmmt3b):
    assert find(catalog, "hmmt3b") is hmmt3b
    assert find(catalog, hmmt3b.isin.lower()) is hmmt3b
    assert find(catalog, "  HMMT3B  ") is hmmt3b
    assert find(catalog, "НЕТ") is None
