"""Command line entry point: `python -m pockettrader <command>`."""

from __future__ import annotations

import argparse
import logging
import sys

from .config import TIMEFRAME_MINUTES, Settings
from .data import CcxtProvider, MarketDataError
from .engine import SignalEngine
from .formatting import format_signal_plain
from .models import Direction


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pockettrader", description="PocketTrader signal assistant"
    )
    parser.add_argument("--config", help="path to a YAML config file")
    parser.add_argument("--exchange", help="override the ccxt exchange id")
    parser.add_argument(
        "-t", "--timeframe", choices=sorted(TIMEFRAME_MINUTES), help="candle timeframe"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan the watchlist once and print the result")
    scan.add_argument("symbols", nargs="*", help="pairs to scan (default: the watchlist)")
    scan.add_argument(
        "--all", action="store_true", help="also print symbols with no signal"
    )

    signal = sub.add_parser("signal", help="analyse a single pair in detail")
    signal.add_argument("symbol")

    sub.add_parser("bot", help="run the Telegram bot")
    sub.add_parser("config", help="print the resolved configuration")
    return parser


def _settings_from(args: argparse.Namespace) -> Settings:
    settings = Settings.load(args.config)
    if args.exchange:
        settings.exchange = args.exchange
    if args.timeframe:
        settings.timeframe = args.timeframe
    return settings


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        settings = _settings_from(args)
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.command == "config":
        return _cmd_config(settings)
    if args.command == "bot":
        from .bot import run_bot

        try:
            run_bot(settings)
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0

    provider = CcxtProvider(settings.exchange, cache_seconds=settings.cache_seconds)
    engine = SignalEngine(settings)

    if args.command == "signal":
        try:
            result = engine.scan_symbol(provider, args.symbol, settings.timeframe)
        except MarketDataError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        _print_detail(result)
        return 0

    symbols = [provider.resolve(s) for s in args.symbols] or list(settings.watchlist)
    signals, errors = engine.scan(provider, symbols, settings.timeframe)
    shown = signals if args.all else [s for s in signals if s.actionable]
    for signal in shown:
        print(format_signal_plain(signal))
    if not shown:
        print("no actionable signals")
    for symbol, message in errors.items():
        print(f"warning: {symbol}: {message}", file=sys.stderr)
    return 1 if errors and not signals else 0


def _cmd_config(settings: Settings) -> int:
    secrets = {"telegram_token", "cryptobot_token"}
    for name, value in sorted(vars(settings).items()):
        shown = (f"set ({len(value)} chars)" if value else "not set") if name in secrets else value
        print(f"{name:<24} {shown}")
    if settings.billing_enabled and not settings.webhook_url:
        print("\nwarning: billing is on but PUBLIC_BASE_URL is unset, so Crypto Pay")
        print("         has nowhere to deliver webhooks. Payments will only be")
        print("         picked up by the slower invoice poll.")
    elif settings.webhook_url:
        print(f"\npaste into the Crypto Pay app's Webhooks setting:\n  {settings.webhook_url}")
    return 0


def _print_detail(signal) -> None:
    print(f"{signal.symbol} {signal.timeframe}  bar {signal.bar_time:%Y-%m-%d %H:%M UTC}")
    print(f"price      {signal.price}")
    print(f"direction  {signal.direction.value}")
    print(f"confidence {signal.confidence:.1%}")
    if signal.actionable:
        print(f"expiry     {signal.expiry_minutes} min")
    print("votes:")
    for vote in signal.votes:
        marker = " " if vote.direction is Direction.NONE else "*"
        print(f"  {marker} {vote.strategy:<16} {vote.direction.value:<5} "
              f"{vote.strength:>5.0%}  {vote.reason}")
    for note in signal.notes:
        print(f"note: {note}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
