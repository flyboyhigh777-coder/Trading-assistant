"""Консоль: `python -m uzinvest <команда>`."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from .bonds import cashflows, effective_annual_rate, yield_to_maturity
from .config import Settings
from .formatting import format_bond_plain, format_date
from .market import CatalogError, MarketDataError, UzseClient, find, load_catalog
from .money import format_percent, format_sum


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uzinvest", description="Помощник по облигациям узбекского рынка"
    )
    parser.add_argument("--config", help="путь к YAML с настройками")
    parser.add_argument("--catalog", help="путь к каталогу выпусков")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный лог")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bonds", help="все выпуски каталога")

    bond = sub.add_parser("bond", help="разбор одного выпуска")
    bond.add_argument("query", help="ISIN или тикер")
    bond.add_argument("-n", "--quantity", type=int, default=1)
    bond.add_argument("--price", help="уплаченная цена, чтобы посчитать YTM")

    cal = sub.add_parser("calendar", help="календарь выплат по выпуску")
    cal.add_argument("query")
    cal.add_argument("-n", "--quantity", type=int, default=1)
    cal.add_argument("--limit", type=int, default=12)

    sub.add_parser("quotes", help="котировки акций с биржи")

    fetch = sub.add_parser("fetch", help="отладка парсера биржи")
    fetch.add_argument("--raw", action="store_true", help="вывести HTML как есть")
    fetch.add_argument("--tables", action="store_true", help="показать найденные таблицы")
    fetch.add_argument("--kind", choices=("stocks", "bonds"), default="stocks")

    sub.add_parser("config", help="показать итоговые настройки")
    sub.add_parser("bot", help="запустить Telegram-бота")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    try:
        settings = Settings.load(args.config)
    except ValueError as exc:
        print(f"ошибка настроек: {exc}", file=sys.stderr)
        return 2
    if args.catalog:
        settings.catalog_path = args.catalog

    if args.command == "config":
        return _cmd_config(settings)
    if args.command == "bot":
        from .bot import run_bot

        try:
            run_bot(settings)
        except RuntimeError as exc:
            print(f"ошибка: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.command in ("quotes", "fetch"):
        return _cmd_market(args, settings)

    try:
        catalog = load_catalog(settings.catalog_path)
    except CatalogError as exc:
        print(f"каталог: {exc}", file=sys.stderr)
        return 1

    if args.command == "bonds":
        if not catalog:
            print("каталог пуст")
            return 1
        for bond in sorted(catalog.values(), key=lambda b: -effective_annual_rate(b)):
            print(format_bond_plain(bond))
        return 0

    bond = find(catalog, args.query)
    if bond is None:
        print(f"выпуск «{args.query}» не найден", file=sys.stderr)
        return 1

    if args.command == "calendar":
        flows = cashflows(bond, args.quantity, since=date.today())
        if not flows:
            print("выплат не осталось — выпуск погашен")
            return 0
        for flow in flows[: args.limit]:
            label = "погашение" if flow.is_redemption else "купон"
            print(f"{format_date(flow.date):>22}  {format_sum(flow.amount):>18}  {label}")
        if len(flows) > args.limit:
            print(f"...и ещё {len(flows) - args.limit}")
        return 0

    return _cmd_bond(bond, args, settings)


def _cmd_bond(bond, args, settings: Settings) -> int:
    print(f"{bond.ticker}  {bond.isin}  {bond.issuer}")
    print(f"номинал          {format_sum(bond.face_value)}")
    print(f"купон            {format_percent(bond.coupon_rate, 0)}, {bond.frequency_label}")
    print(f"выплата          {format_sum(bond.coupon_amount)}")
    print(f"погашение        {format_date(bond.maturity_date)}")
    print(f"эффективная      {format_percent(effective_annual_rate(bond))}")
    if args.price:
        ytm = yield_to_maturity(bond, args.price, date.today(), args.quantity)
        print(f"YTM по цене      {format_percent(ytm) if ytm else 'не считается'}")
    return 0


def _cmd_market(args, settings: Settings) -> int:
    client = UzseClient(stocks_url=settings.stocks_url, bonds_url=settings.bonds_url)
    kind = getattr(args, "kind", "stocks")
    try:
        if args.command == "fetch" and args.raw:
            url = client.stocks_url if kind == "stocks" else client.bonds_url
            print(client.fetch_html(url))
            return 0
        if args.command == "fetch":
            tables = client.tables(kind)
            if not tables:
                print("таблиц на странице не нашлось", file=sys.stderr)
                return 1
            for index, table in enumerate(tables):
                print(f"\n--- таблица {index}: {len(table.body)} строк ---")
                print("заголовок:", table.header)
                for row in table.body[:3]:
                    print("   ", row)
            return 0
        quotes = client.quotes(kind)
    except MarketDataError as exc:
        print(f"биржа: {exc}", file=sys.stderr)
        return 1

    today = date.today()
    for quote in quotes:
        days = quote.days_since_trade(today)
        freshness = "нет сделок" if days is None else f"{days} дн. назад"
        price = format_sum(quote.price) if quote.price is not None else "—"
        print(f"{quote.ticker:<10}{price:>18}  {freshness:<16}{quote.name}")
    stale = sum(1 for q in quotes if q.is_stale(today, settings.stale_after_days))
    print(f"\n{stale} из {len(quotes)} не торговались {settings.stale_after_days}+ дней")
    return 0


def _cmd_config(settings: Settings) -> int:
    secrets_ = {"telegram_token", "payme_merchant_key", "click_secret_key"}
    for name, value in sorted(vars(settings).items()):
        shown = (
            (f"задан ({len(value)} симв.)" if value else "не задан")
            if name in secrets_
            else value
        )
        print(f"{name:<26} {shown}")

    urls = settings.callback_urls()
    if settings.billing_enabled and not urls:
        print("\nвнимание: кассы настроены, но PUBLIC_BASE_URL пуст —")
        print("          Payme и Click не смогут до вас достучаться.")
    elif urls:
        print("\nвпишите эти адреса в кабинеты касс:")
        for name, url in urls.items():
            print(f"  {name:<16} {url}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
