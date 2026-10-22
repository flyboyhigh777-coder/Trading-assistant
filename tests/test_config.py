import pytest

from pockettrader.config import TIMEFRAME_MINUTES, Settings


def test_defaults_are_sane():
    settings = Settings()
    assert settings.exchange == "binance"
    assert settings.timeframe in TIMEFRAME_MINUTES
    assert settings.expiry_minutes() == 15
    assert not settings.billing_enabled


def test_watchlist_is_upper_cased_and_deduplicated():
    assert Settings(watchlist=("btc/usdt", "BTC/USDT", "eth/usdt")).watchlist == (
        "BTC/USDT",
        "ETH/USDT",
    )


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"timeframe": "7m"}, "unsupported timeframe"),
        ({"min_confidence": 0.0}, "min_confidence"),
        ({"min_confidence": 1.5}, "min_confidence"),
        ({"min_votes": 0}, "min_votes"),
        ({"candles": 50}, "candles"),
    ],
)
def test_invalid_settings_are_rejected(kwargs, message):
    with pytest.raises(ValueError, match=message):
        Settings(**kwargs)


def test_env_overrides_are_coerced(monkeypatch):
    monkeypatch.setenv("POCKETTRADER_WATCHLIST", "btc/usdt, sol/usdt")
    monkeypatch.setenv("POCKETTRADER_MIN_CONFIDENCE", "0.8")
    monkeypatch.setenv("POCKETTRADER_MIN_VOTES", "3")
    monkeypatch.setenv("POCKETTRADER_ADMIN_CHAT_IDS", "1,2")
    settings = Settings.load()
    assert settings.watchlist == ("BTC/USDT", "SOL/USDT")
    assert settings.min_confidence == 0.8
    assert settings.min_votes == 3
    assert settings.admin_chat_ids == (1, 2)
    assert settings.is_admin(2) and not settings.is_admin(3)


@pytest.mark.parametrize(
    "raw,expected",
    [("true", True), ("1", True), ("yes", True), ("false", False), ("0", False), ("off", False)],
)
def test_testnet_flag_parses_common_spellings(monkeypatch, raw, expected):
    monkeypatch.setenv("POCKETTRADER_CRYPTOBOT_TESTNET", raw)
    assert Settings.load().cryptobot_testnet is expected


def test_an_empty_env_value_does_not_count_as_false(monkeypatch):
    # A blank line in an env file must not silently switch on live payments.
    monkeypatch.setenv("POCKETTRADER_CRYPTOBOT_TESTNET", "")
    monkeypatch.setenv("POCKETTRADER_EXCHANGE", "  ")
    settings = Settings.load()
    assert settings.cryptobot_testnet is True
    assert settings.exchange == "binance"


def test_bare_env_names_are_honoured(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-token")
    monkeypatch.setenv("CRYPTOBOT_TOKEN", "pay-token")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://local.example.com/")
    settings = Settings.load()
    assert settings.telegram_token == "tg-token"
    assert settings.billing_enabled
    assert settings.webhook_url == "https://local.example.com/api/billing/webhook"


def test_yaml_file_is_read_and_env_wins(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("exchange: bybit\ntimeframe: 15m\nmin_votes: 3\n")
    monkeypatch.setenv("POCKETTRADER_TIMEFRAME", "1h")
    settings = Settings.load(config)
    assert settings.exchange == "bybit"
    assert settings.timeframe == "1h"      # the env overrides the file
    assert settings.min_votes == 3


def test_unknown_yaml_keys_are_kept_but_not_fatal(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("exchange: kraken\nsomething_new: 42\n")
    settings = Settings.load(config)
    assert settings.extra == {"something_new": 42}


def test_allowed_chats_gate_access():
    settings = Settings(allowed_chat_ids=(10,))
    assert settings.is_chat_allowed(10)
    assert not settings.is_chat_allowed(11)
    assert Settings().is_chat_allowed(99)   # empty list means everyone


def test_webhook_path_is_normalised():
    assert Settings(webhook_path="api/billing").webhook_path == "/api/billing"
    assert Settings(public_base_url="https://x.io").webhook_url.startswith("https://x.io/")
