"""Адаптер к сайту Республиканской фондовой биржи «Тошкент» (uzse.uz).

ВАЖНО. Официального API у биржи нет, поэтому это парсер HTML-страницы, и его
НУЖНО откалибровать по живой странице перед продакшеном:

    python -m uzinvest fetch --raw > page.html     # что реально отдаёт сайт
    python -m uzinvest fetch --tables              # какие таблицы и колонки видны

Колонки ищутся по подстроке заголовка (см. ``COLUMN_HINTS``), а не по
CSS-селекторам, поэтому переверстка страницы парсер обычно не ломает. Если
биржа переименует колонки — достаточно дописать слово в подсказки ниже, трогать
остальной код не придётся.

Перед регулярным использованием сверьтесь с условиями использования сайта.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date

from .quotes import Quote, parse_date, parse_price
from .tables import Table, extract_tables

log = logging.getLogger(__name__)

STOCKS_URL = "https://www.uzse.uz/isu_infos/STK"
BONDS_URL = "https://www.uzse.uz/isu_infos/BND"

USER_AGENT = "uzinvest/0.2 (+https://github.com/flyboyhigh777-coder/Trading-assistant)"

#: Подстроки заголовков, по которым ищется колонка. Русские и английские
#: варианты, потому что страница отдаётся на обоих языках.
COLUMN_HINTS: dict[str, tuple[str, ...]] = {
    "ticker": ("тикер", "ticker", "код", "symbol", "isu"),
    "name": ("наименование", "эмитент", "name", "issuer", "компан"),
    "price": ("цена котировки", "котировк", "quoted", "цена", "price", "last"),
    "open": ("цена открытия", "открыт", "open"),
    "last_trade": ("последней сделки", "дата сделки", "last transaction", "дата"),
    "change": ("изменение", "change", "%"),
}


class MarketDataError(RuntimeError):
    """Страница биржи недоступна или не разобралась."""


@dataclass
class UzseClient:
    """Тонкий клиент к uzse.uz: скачать страницу и разобрать таблицу."""

    stocks_url: str = STOCKS_URL
    bonds_url: str = BONDS_URL
    timeout: float = 20.0
    locale: str = "ru"
    _cache: dict[str, tuple[float, str]] = field(default_factory=dict, repr=False)

    # -- сеть -------------------------------------------------------------

    def fetch_html(self, url: str) -> str:
        """Скачать страницу как текст."""
        target = f"{url}?locale={self.locale}" if "locale=" not in url else url
        request = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            raise MarketDataError(f"биржа ответила {exc.code} на {target}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MarketDataError(f"не достучался до {target}: {exc}") from exc

    # -- разбор -----------------------------------------------------------

    def quotes(self, kind: str = "stocks") -> list[Quote]:
        """Котировки акций (``stocks``) или облигаций (``bonds``)."""
        url = self.stocks_url if kind == "stocks" else self.bonds_url
        return parse_quotes(self.fetch_html(url))

    def tables(self, kind: str = "stocks") -> list[Table]:
        """Сырые таблицы страницы — для калибровки парсера."""
        url = self.stocks_url if kind == "stocks" else self.bonds_url
        return extract_tables(self.fetch_html(url))


def pick_quote_table(tables: list[Table]) -> Table | None:
    """Выбрать из таблиц страницы ту, что похожа на котировки.

    Берётся таблица, в заголовке которой есть и тикер, и цена, и у которой
    больше всего строк — на странице обычно есть ещё навигация и подвал,
    свёрстанные таблицами.
    """
    candidates = [
        t
        for t in tables
        if t.column_index(*COLUMN_HINTS["ticker"]) is not None
        and t.column_index(*COLUMN_HINTS["price"]) is not None
    ]
    return max(candidates, key=lambda t: len(t.body), default=None)


def parse_quotes(html: str, today: date | None = None) -> list[Quote]:
    """Разобрать страницу котировок в список ``Quote``."""
    table = pick_quote_table(extract_tables(html))
    if table is None:
        raise MarketDataError(
            "на странице не нашлось таблицы с колонками тикера и цены; "
            "запустите `python -m uzinvest fetch --tables` и поправьте COLUMN_HINTS"
        )

    index = {
        key: table.column_index(*hints) for key, hints in COLUMN_HINTS.items()
    }

    def cell(row: list[str], key: str) -> str:
        position = index.get(key)
        if position is None or position >= len(row):
            return ""
        return row[position]

    quotes: list[Quote] = []
    for row in table.body:
        ticker = cell(row, "ticker").strip().upper()
        if not ticker or ticker.isdigit():
            continue
        quotes.append(
            Quote(
                ticker=ticker,
                name=cell(row, "name"),
                price=parse_price(cell(row, "price")),
                last_trade=parse_date(cell(row, "last_trade")),
                change=parse_price(cell(row, "change")),
            )
        )
    return quotes
