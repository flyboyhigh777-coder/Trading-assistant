"""Bot behaviour that does not need a Telegram connection."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pytest

from pockettrader.bot import PocketTraderBot
from pockettrader.config import Settings
from pockettrader.models import Direction, Signal, Vote
from pockettrader.storage import Store

from .helpers import make_frame


class FakeProvider:
    """Serves the same frame for every symbol."""

    name = "fake"

    def __init__(self, frame):
        self.frame = frame
        self.requested: list[tuple[str, str]] = []

    def fetch_ohlcv(self, symbol, timeframe, limit):
        self.requested.append((symbol, timeframe))
        return self.frame

    def symbols(self):
        return ["BTC/USDT", "ETH/USDT"]

    def resolve(self, symbol):
        return symbol.strip().upper()


@dataclass
class FakeBot:
    sent: list[tuple[int, str]] = field(default_factory=list)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


@dataclass
class FakeContext:
    bot: FakeBot = field(default_factory=FakeBot)


def signal_for(symbol: str, timeframe: str = "5m", confidence: float = 0.9) -> Signal:
    return Signal(
        symbol=symbol,
        timeframe=timeframe,
        direction=Direction.CALL,
        confidence=confidence,
        price=100.0,
        bar_time=datetime(2024, 5, 1, tzinfo=timezone.utc),
        expiry_minutes=15,
        votes=(Vote("ema_pullback", Direction.CALL, 0.9, "cross"),),
    )


@pytest.fixture
def bot(tmp_path):
    settings = Settings(
        watchlist=("BTC/USDT", "ETH/USDT"),
        subscribers_path=tmp_path / "subs.json",
        telegram_token="test-token",
        alert_cooldown_seconds=1800,
    )
    frame = make_frame(100 + np.linspace(0, 5, 300))
    return PocketTraderBot(
        settings,
        provider=FakeProvider(frame),
        store=Store(settings.subscribers_path),
    )


# -- timeframe parsing ----------------------------------------------------


@pytest.mark.parametrize(
    "args,expected",
    [([], "5m"), (["15m"], "15m"), (["BTC/USDT", "1h"], "1h"), (["BTC/USDT"], "5m")],
)
def test_timeframe_argument_is_optional(bot, args, expected):
    timeframe, error = bot._parse_timeframe(args, "5m")
    assert (timeframe, error) == (expected, None)


def test_a_bogus_timeframe_is_reported(bot):
    timeframe, error = bot._parse_timeframe(["7m"], "5m")
    assert timeframe is None
    assert "Unknown timeframe" in error


# -- per-subscriber settings ---------------------------------------------


def test_subscriber_settings_fall_back_to_the_defaults(bot):
    sub = bot.store.upsert(1)
    assert bot._timeframe_for(sub) == bot.settings.timeframe
    assert bot._watchlist_for(sub) == bot.settings.watchlist
    assert bot._min_confidence_for(sub) == bot.settings.min_confidence


def test_subscriber_settings_override_the_defaults(bot):
    sub = bot.store.upsert(1, timeframe="15m", watchlist=("SOL/USDT",), min_confidence=0.9)
    assert bot._timeframe_for(sub) == "15m"
    assert bot._watchlist_for(sub) == ("SOL/USDT",)
    assert bot._min_confidence_for(sub) == 0.9


def test_access_is_open_when_billing_is_off(bot):
    assert not bot.settings.billing_enabled
    assert bot._has_access(bot.store.upsert(1))


def test_access_is_gated_when_billing_is_on(tmp_path):
    settings = Settings(
        subscribers_path=tmp_path / "subs.json",
        telegram_token="t",
        cryptobot_token="12345:AA",
    )
    bot = PocketTraderBot(settings, provider=FakeProvider(None),
                          store=Store(settings.subscribers_path))
    assert not bot._has_access(bot.store.upsert(1))
    bot.store.grant_access(1, days=7)
    assert bot._has_access(bot.store.get(1))


# -- the scan job ---------------------------------------------------------


async def test_scan_job_pushes_a_signal_to_a_subscriber(bot, monkeypatch):
    bot.store.upsert(1, alerts_enabled=True, watchlist=("BTC/USDT",))

    async def fake_scan(symbols, timeframe):
        return [signal_for(s, timeframe) for s in symbols], {}

    monkeypatch.setattr(bot, "_analyse_many", fake_scan)
    context = FakeContext()
    await bot.job_scan(context)

    assert len(context.bot.sent) == 1
    chat_id, text = context.bot.sent[0]
    assert chat_id == 1
    assert "BTC/USDT" in text


async def test_the_same_setup_is_not_pushed_twice(bot, monkeypatch):
    bot.store.upsert(1, alerts_enabled=True, watchlist=("BTC/USDT",))

    async def fake_scan(symbols, timeframe):
        return [signal_for(s, timeframe) for s in symbols], {}

    monkeypatch.setattr(bot, "_analyse_many", fake_scan)
    context = FakeContext()
    await bot.job_scan(context)
    await bot.job_scan(context)
    assert len(context.bot.sent) == 1


async def test_a_subscriber_threshold_filters_the_push(bot, monkeypatch):
    bot.store.upsert(1, alerts_enabled=True, watchlist=("BTC/USDT",), min_confidence=0.95)

    async def fake_scan(symbols, timeframe):
        return [signal_for(s, timeframe, confidence=0.6) for s in symbols], {}

    monkeypatch.setattr(bot, "_analyse_many", fake_scan)
    context = FakeContext()
    await bot.job_scan(context)
    assert context.bot.sent == []


async def test_nothing_is_pushed_to_someone_with_alerts_off(bot, monkeypatch):
    bot.store.upsert(1, alerts_enabled=False, watchlist=("BTC/USDT",))

    async def fake_scan(symbols, timeframe):
        return [signal_for(s, timeframe) for s in symbols], {}

    monkeypatch.setattr(bot, "_analyse_many", fake_scan)
    context = FakeContext()
    await bot.job_scan(context)
    assert context.bot.sent == []


async def test_each_symbol_is_fetched_once_for_all_subscribers(bot, monkeypatch):
    bot.store.upsert(1, alerts_enabled=True, watchlist=("BTC/USDT",))
    bot.store.upsert(2, alerts_enabled=True, watchlist=("BTC/USDT", "ETH/USDT"))
    scanned: list[tuple[tuple[str, ...], str]] = []

    async def fake_scan(symbols, timeframe):
        scanned.append((tuple(symbols), timeframe))
        return [signal_for(s, timeframe) for s in symbols], {}

    monkeypatch.setattr(bot, "_analyse_many", fake_scan)
    context = FakeContext()
    await bot.job_scan(context)

    assert scanned == [(("BTC/USDT", "ETH/USDT"), "5m")]
    assert sorted(chat for chat, _ in context.bot.sent) == [1, 2, 2]


async def test_subscribers_on_different_timeframes_each_get_a_scan(bot, monkeypatch):
    bot.store.upsert(1, alerts_enabled=True, watchlist=("BTC/USDT",), timeframe="5m")
    bot.store.upsert(2, alerts_enabled=True, watchlist=("BTC/USDT",), timeframe="1h")
    timeframes = []

    async def fake_scan(symbols, timeframe):
        timeframes.append(timeframe)
        return [signal_for(s, timeframe) for s in symbols], {}

    monkeypatch.setattr(bot, "_analyse_many", fake_scan)
    await bot.job_scan(FakeContext())
    assert sorted(timeframes) == ["1h", "5m"]


async def test_a_failed_delivery_is_retried_next_round(bot, monkeypatch):
    bot.store.upsert(1, alerts_enabled=True, watchlist=("BTC/USDT",))

    async def fake_scan(symbols, timeframe):
        return [signal_for(s, timeframe) for s in symbols], {}

    monkeypatch.setattr(bot, "_analyse_many", fake_scan)

    class FailingBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            raise RuntimeError("chat not found")

    await bot.job_scan(FakeContext(bot=FailingBot()))
    # The send failed, so the signal must not be marked as delivered.
    assert bot.store.should_alert(1, "BTC/USDT|5m|CALL", cooldown_seconds=1800)

    context = FakeContext()
    await bot.job_scan(context)
    assert len(context.bot.sent) == 1


async def test_the_scan_job_is_a_no_op_without_subscribers(bot, monkeypatch):
    async def fail(*args, **kwargs):
        raise AssertionError("no scan should run")

    monkeypatch.setattr(bot, "_analyse_many", fail)
    await bot.job_scan(FakeContext())


def test_building_the_application_without_a_token_fails(tmp_path):
    settings = Settings(subscribers_path=tmp_path / "s.json", telegram_token="")
    bot = PocketTraderBot(settings, provider=FakeProvider(None),
                          store=Store(settings.subscribers_path))
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        bot.build_application()


async def test_an_unpaid_subscriber_gets_no_alerts_when_billing_is_on(tmp_path, monkeypatch):
    settings = Settings(
        subscribers_path=tmp_path / "subs.json",
        telegram_token="t",
        cryptobot_token="12345:AA",
        watchlist=("BTC/USDT",),
    )
    bot = PocketTraderBot(
        settings,
        provider=FakeProvider(make_frame(100 + np.linspace(0, 5, 300))),
        store=Store(settings.subscribers_path),
    )
    bot.store.upsert(1, alerts_enabled=True)              # opted in, never paid
    bot.store.upsert(2, alerts_enabled=True)
    bot.store.grant_access(2, days=7)

    async def fake_scan(symbols, timeframe):
        return [signal_for(s, timeframe) for s in symbols], {}

    monkeypatch.setattr(bot, "_analyse_many", fake_scan)
    context = FakeContext()
    await bot.job_scan(context)
    assert [chat for chat, _ in context.bot.sent] == [2]
