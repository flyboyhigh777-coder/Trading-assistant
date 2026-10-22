"""Поведение бота, не требующее соединения с Telegram."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from uzinvest.bonds import Position
from uzinvest.bot import UzInvestBot
from uzinvest.config import Settings
from uzinvest.storage import Store


@dataclass
class FakeBot:
    sent: list[tuple[int, str]] = field(default_factory=list)

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


@dataclass
class FakeContext:
    bot: FakeBot = field(default_factory=FakeBot)


def make_bot(tmp_path, catalog, **overrides) -> UzInvestBot:
    settings = Settings(
        subscribers_path=tmp_path / "s.json",
        telegram_token="тест",
        coupon_reminder_days=3,
        **overrides,
    )
    return UzInvestBot(settings, store=Store(settings.subscribers_path), catalog=catalog)


@pytest.fixture
def bot(tmp_path, catalog):
    return make_bot(tmp_path, catalog)


# -- разбор ввода ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("22", Decimal("0.22")), ("22%", Decimal("0.22")), ("0.22", Decimal("0.22")),
     ("0,22", Decimal("0.22")), ("9.5", Decimal("0.095"))],
)
def test_rates_are_accepted_in_any_usual_spelling(bot, raw, expected):
    assert bot._parse_rate(raw) == expected


@pytest.mark.parametrize("raw", ["", "много", "-5", "99999"])
def test_nonsense_rates_are_rejected(bot, raw):
    assert bot._parse_rate(raw) is None


# -- настройки подписчика -------------------------------------------------


def test_subscriber_settings_fall_back_to_defaults(bot):
    sub = bot.store.upsert(1)
    assert bot._deposit_rate(sub) == bot.settings.default_deposit_rate
    assert bot._inflation(sub) == bot.settings.default_inflation


def test_subscriber_settings_override_defaults(bot):
    sub = bot.store.upsert(1, deposit_rate=Decimal("0.3"), inflation=Decimal("0.05"))
    assert bot._deposit_rate(sub) == Decimal("0.3")
    assert bot._inflation(sub) == Decimal("0.05")


def test_access_is_open_when_billing_is_off(bot):
    assert not bot.settings.billing_enabled
    assert bot._has_access(bot.store.upsert(1))


def test_access_is_gated_when_a_till_is_configured(tmp_path, catalog):
    bot = make_bot(tmp_path, catalog, payme_merchant_id="m", payme_merchant_key="k")
    assert not bot._has_access(bot.store.upsert(1))
    bot.store.grant_access(1, 7)
    assert bot._has_access(bot.store.get(1))


def test_a_broken_catalog_does_not_stop_the_bot(tmp_path):
    settings = Settings(
        subscribers_path=tmp_path / "s.json",
        catalog_path=tmp_path / "нет.yaml",
        telegram_token="тест",
    )
    bot = UzInvestBot(settings, store=Store(settings.subscribers_path))
    assert bot.catalog == {}


# -- напоминания о купонах ------------------------------------------------


async def test_reminder_lists_upcoming_payments(bot, hmmt3b, monkeypatch):
    bot.store.upsert(1, alerts_enabled=True)
    bot.store.set_position(1, Position(hmmt3b.isin, 3))
    # Выплата 15-го числа; «сегодня» — 13-е, горизонт — 3 дня.
    monkeypatch.setattr(bot, "_has_access", lambda sub: True)
    context = FakeContext()
    await bot.job_coupon_reminders(context, today=dt.date(2026, 11, 13))
    assert len(context.bot.sent) == 1
    chat_id, text = context.bot.sent[0]
    assert chat_id == 1
    assert "HMMT3B" in text
    assert "65 001" in text      # 21 667 × 3, как в карточке


async def test_reminder_is_not_repeated(bot, hmmt3b, monkeypatch):
    bot.store.upsert(1, alerts_enabled=True)
    bot.store.set_position(1, Position(hmmt3b.isin, 1))
    monkeypatch.setattr(bot, "_has_access", lambda sub: True)
    context = FakeContext()
    await bot.job_coupon_reminders(context, today=dt.date(2026, 11, 13))
    await bot.job_coupon_reminders(context, today=dt.date(2026, 11, 13))
    assert len(context.bot.sent) == 1


async def test_nothing_is_sent_without_upcoming_payments(bot, hmmt3b, monkeypatch):
    bot.store.upsert(1, alerts_enabled=True)
    bot.store.set_position(1, Position(hmmt3b.isin, 1))
    monkeypatch.setattr(bot, "_has_access", lambda sub: True)
    context = FakeContext()
    # До 15-го ещё далеко.
    await bot.job_coupon_reminders(context, today=dt.date(2026, 11, 2))
    assert context.bot.sent == []


async def test_nothing_is_sent_with_alerts_off(bot, hmmt3b):
    bot.store.upsert(1, alerts_enabled=False)
    bot.store.set_position(1, Position(hmmt3b.isin, 1))

    context = FakeContext()
    await bot.job_coupon_reminders(context, today=dt.date(2026, 11, 13))
    assert context.bot.sent == []


async def test_unpaid_subscribers_get_no_reminders(tmp_path, catalog, hmmt3b):
    bot = make_bot(tmp_path, catalog, payme_merchant_id="m", payme_merchant_key="k")
    bot.store.upsert(1, alerts_enabled=True)
    bot.store.set_position(1, Position(hmmt3b.isin, 1))
    bot.store.upsert(2, alerts_enabled=True)
    bot.store.set_position(2, Position(hmmt3b.isin, 1))
    bot.store.grant_access(2, 30)

    context = FakeContext()
    await bot.job_coupon_reminders(context, today=dt.date(2026, 11, 13))
    assert [chat for chat, _ in context.bot.sent] == [2]


async def test_a_failed_delivery_is_retried_next_time(bot, hmmt3b, monkeypatch):
    bot.store.upsert(1, alerts_enabled=True)
    bot.store.set_position(1, Position(hmmt3b.isin, 1))
    monkeypatch.setattr(bot, "_has_access", lambda sub: True)
    class FailingBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            raise RuntimeError("чат недоступен")

    today = dt.date(2026, 11, 13)
    await bot.job_coupon_reminders(FakeContext(bot=FailingBot()), today=today)
    context = FakeContext()
    await bot.job_coupon_reminders(context, today=today)
    assert len(context.bot.sent) == 1


# -- сборка ---------------------------------------------------------------


def test_building_without_a_token_fails(tmp_path, catalog):
    settings = Settings(subscribers_path=tmp_path / "s.json", telegram_token="")
    bot = UzInvestBot(settings, store=Store(settings.subscribers_path), catalog=catalog)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        bot.build_application()


def test_tills_are_wired_only_when_configured(tmp_path, catalog):
    plain = make_bot(tmp_path, catalog)
    assert plain.payme is None and plain.click is None and plain.routes is None

    paid = make_bot(
        tmp_path, catalog, payme_merchant_id="m", payme_merchant_key="k",
        click_service_id="1", click_merchant_id="2", click_secret_key="3",
    )
    assert paid.payme.is_configured() and paid.click.is_configured()
    assert paid.routes is not None
