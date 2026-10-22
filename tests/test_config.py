from decimal import Decimal
from pathlib import Path

import pytest

from uzinvest.config import Settings


def test_defaults_are_sane():
    settings = Settings()
    assert not settings.billing_enabled
    assert settings.default_deposit_rate == Decimal("0.22")
    assert settings.callback_urls() == {}


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"reminder_hour_utc": 25}, "reminder_hour_utc"),
        ({"coupon_reminder_days": -1}, "coupon_reminder_days"),
        ({"rate_limit_per_minute": 0}, "rate_limit_per_minute"),
    ],
)
def test_invalid_settings_are_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        Settings(**kwargs)


def test_billing_turns_on_with_either_till():
    assert Settings(payme_merchant_id="a", payme_merchant_key="b").billing_enabled
    assert Settings(
        click_service_id="1", click_merchant_id="2", click_secret_key="3"
    ).billing_enabled
    # Половина ключей — это не настроенная касса.
    assert not Settings(payme_merchant_id="a").billing_enabled
    assert not Settings(click_service_id="1", click_merchant_id="2").billing_enabled


def test_callback_urls_are_spelled_out_for_both_tills():
    settings = Settings(public_base_url="https://uzinvest.uz/", payme_merchant_id="a",
                        payme_merchant_key="b")
    urls = settings.callback_urls()
    assert urls["payme"] == "https://uzinvest.uz/api/billing/payme"
    assert urls["click_prepare"].endswith("/click/prepare")
    assert urls["click_complete"].endswith("/click/complete")


def test_env_overrides_are_coerced(monkeypatch):
    monkeypatch.setenv("UZINVEST_DEFAULT_INFLATION", "0.125")
    monkeypatch.setenv("UZINVEST_ADMIN_CHAT_IDS", "1, 2")
    monkeypatch.setenv("UZINVEST_FREE_TRIAL_DAYS", "14")
    settings = Settings.load()
    assert settings.default_inflation == Decimal("0.125")
    assert settings.admin_chat_ids == (1, 2)
    assert settings.free_trial_days == 14
    assert settings.is_admin(2) and not settings.is_admin(3)


def test_bare_env_names_are_honoured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg")
    monkeypatch.setenv("PAYME_MERCHANT_ID", "m")
    monkeypatch.setenv("PAYME_MERCHANT_KEY", "k")
    monkeypatch.setenv("CLICK_SECRET_KEY", "s")
    settings = Settings.load()
    assert settings.telegram_token == "tg"
    assert settings.payme_enabled


@pytest.mark.parametrize(
    "raw,expected",
    [("true", True), ("1", True), ("да", True), ("false", False), ("off", False)],
)
def test_test_mode_parses_common_spellings(monkeypatch, raw, expected):
    monkeypatch.setenv("PAYME_TEST_MODE", raw)
    assert Settings.load().payme_test_mode is expected


def test_an_empty_env_value_does_not_switch_to_live_payments(monkeypatch):
    # Пустая строка в env-файле не должна выключить тестовый режим и
    # отправить настоящие платежи.
    monkeypatch.setenv("PAYME_TEST_MODE", "")
    monkeypatch.setenv("UZINVEST_PAYME_TEST_MODE", "   ")
    assert Settings.load().payme_test_mode is True


def test_yaml_is_read_and_env_wins(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        "default_inflation: 0.09\nfree_trial_days: 3\nwebhook_port: 9000\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("UZINVEST_FREE_TRIAL_DAYS", "30")
    settings = Settings.load(config)
    assert settings.default_inflation == Decimal("0.09")
    assert settings.free_trial_days == 30
    assert settings.webhook_port == 9000


def test_unknown_yaml_keys_are_kept_aside(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("что_то_новое: 42\n", encoding="utf-8")
    assert Settings.load(config).extra == {"что_то_новое": 42}


def test_webhook_path_is_normalised():
    assert Settings(webhook_path="api/billing/").webhook_path == "/api/billing"


def test_allowed_chats_gate_access():
    assert Settings(allowed_chat_ids=(10,)).is_chat_allowed(10)
    assert not Settings(allowed_chat_ids=(10,)).is_chat_allowed(11)
    assert Settings().is_chat_allowed(99)


def test_paths_become_path_objects():
    settings = Settings(catalog_path="a/b.yaml", subscribers_path="c/d.json")
    assert isinstance(settings.catalog_path, Path)
    assert isinstance(settings.subscribers_path, Path)
