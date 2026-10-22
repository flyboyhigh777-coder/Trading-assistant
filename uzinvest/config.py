"""Настройки: YAML-файл, поверх него переменные окружения."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from decimal import Decimal
from pathlib import Path
from typing import Any

from .money import to_decimal

ENV_PREFIX = "UZINVEST_"


@dataclass
class Settings:
    """Всё, что нужно расчётному слою и боту."""

    # --- данные ----------------------------------------------------------
    catalog_path: Path = Path("bonds.yaml")
    stocks_url: str = "https://www.uzse.uz/isu_infos/STK"
    bonds_url: str = "https://www.uzse.uz/isu_infos/BND"
    quotes_cache_seconds: int = 900
    stale_after_days: int = 5

    # --- ориентиры для сравнения -----------------------------------------
    # Значения по умолчанию, пока пользователь не задал свои. Их стоит
    # обновлять: ставка ЦБ и инфляция в РУз меняются.
    default_deposit_rate: Decimal = Decimal("0.22")
    default_inflation: Decimal = Decimal("0.10")

    # --- telegram --------------------------------------------------------
    telegram_token: str = ""
    subscribers_path: Path = Path("data/subscribers.json")
    rate_limit_per_minute: int = 20
    free_trial_days: int = 7
    admin_chat_ids: tuple[int, ...] = ()
    allowed_chat_ids: tuple[int, ...] = ()

    # --- напоминания -----------------------------------------------------
    reminder_hour_utc: int = 4          # 09:00 по Ташкенту (UTC+5)
    coupon_reminder_days: int = 3       # за сколько дней напоминать о купоне
    alert_cooldown_seconds: int = 72_000

    # --- оплата ----------------------------------------------------------
    payme_merchant_id: str = ""
    payme_merchant_key: str = ""
    payme_account_field: str = "order_id"
    payme_test_mode: bool = True

    click_service_id: str = ""
    click_merchant_id: str = ""
    click_secret_key: str = ""
    click_merchant_user_id: str = ""

    public_base_url: str = ""
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080
    webhook_path: str = "/api/billing"

    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.catalog_path = Path(self.catalog_path)
        self.subscribers_path = Path(self.subscribers_path)
        self.admin_chat_ids = tuple(int(c) for c in self.admin_chat_ids)
        self.allowed_chat_ids = tuple(int(c) for c in self.allowed_chat_ids)
        self.default_deposit_rate = to_decimal(self.default_deposit_rate)
        self.default_inflation = to_decimal(self.default_inflation)
        self.public_base_url = self.public_base_url.rstrip("/")
        if not self.webhook_path.startswith("/"):
            self.webhook_path = "/" + self.webhook_path
        self.webhook_path = self.webhook_path.rstrip("/")
        if not 0 <= self.reminder_hour_utc <= 23:
            raise ValueError("reminder_hour_utc должен быть от 0 до 23")
        if self.coupon_reminder_days < 0:
            raise ValueError("coupon_reminder_days не может быть отрицательным")
        if self.rate_limit_per_minute < 1:
            raise ValueError("rate_limit_per_minute должен быть не меньше 1")

    # -- производное ------------------------------------------------------

    def is_chat_allowed(self, chat_id: int) -> bool:
        return not self.allowed_chat_ids or chat_id in self.allowed_chat_ids

    def is_admin(self, chat_id: int) -> bool:
        return chat_id in self.admin_chat_ids

    @property
    def payme_enabled(self) -> bool:
        return bool(self.payme_merchant_id and self.payme_merchant_key)

    @property
    def click_enabled(self) -> bool:
        return bool(self.click_service_id and self.click_merchant_id and self.click_secret_key)

    @property
    def billing_enabled(self) -> bool:
        """Платный режим включается появлением ключей хотя бы одной кассы.

        Без ключей бот открыт всем — это удобно при разработке и недопустимо
        в продакшене.
        """
        return self.payme_enabled or self.click_enabled

    def callback_urls(self) -> dict[str, str]:
        """Адреса, которые вписываются в кабинеты Payme и Click."""
        if not self.public_base_url:
            return {}
        base = f"{self.public_base_url}{self.webhook_path}"
        return {
            "payme": f"{base}/payme",
            "click_prepare": f"{base}/click/prepare",
            "click_complete": f"{base}/click/complete",
        }

    # -- загрузка ---------------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Settings":
        data: dict[str, Any] = {}
        config_path = Path(path) if path else Path(os.getenv(f"{ENV_PREFIX}CONFIG", ""))
        if config_path and config_path.is_file():
            import yaml

            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"{config_path}: ожидался словарь настроек")
            data.update(loaded)
        data.update(_from_env())

        known = {f.name for f in fields(cls)} - {"extra"}
        extra = {k: v for k, v in data.items() if k not in known}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(extra=extra, **_coerce(kwargs))


#: Привычные короткие имена, чтобы env-файл читался как документация касс.
_BARE_NAMES = {
    "TELEGRAM_BOT_TOKEN": "telegram_token",
    "PAYME_MERCHANT_ID": "payme_merchant_id",
    "PAYME_MERCHANT_KEY": "payme_merchant_key",
    "PAYME_TEST_MODE": "payme_test_mode",
    "CLICK_SERVICE_ID": "click_service_id",
    "CLICK_MERCHANT_ID": "click_merchant_id",
    "CLICK_SECRET_KEY": "click_secret_key",
    "CLICK_MERCHANT_USER_ID": "click_merchant_user_id",
    "PUBLIC_BASE_URL": "public_base_url",
}


def _from_env() -> dict[str, Any]:
    """Прочитать UZINVEST_* и короткие имена.

    Пустое значение считается незаданным: случайная пустая строка в env-файле
    не должна, например, выключать тестовый режим Payme и отправлять боевые
    платежи.
    """
    out: dict[str, Any] = {}
    for key, value in os.environ.items():
        if key.startswith(ENV_PREFIX) and key != f"{ENV_PREFIX}CONFIG" and value.strip():
            out[key[len(ENV_PREFIX):].lower()] = value
    for env_name, field_name in _BARE_NAMES.items():
        value = os.getenv(env_name)
        if value and value.strip() and field_name not in out:
            out[field_name] = value
    return out


_LIST_FIELDS = {"admin_chat_ids", "allowed_chat_ids"}
_INT_FIELDS = {
    "quotes_cache_seconds",
    "stale_after_days",
    "rate_limit_per_minute",
    "free_trial_days",
    "reminder_hour_utc",
    "coupon_reminder_days",
    "alert_cooldown_seconds",
    "webhook_port",
}
_DECIMAL_FIELDS = {"default_deposit_rate", "default_inflation"}
_BOOL_FIELDS = {"payme_test_mode"}
_TRUTHY = {"1", "true", "yes", "on", "да"}


def _coerce(kwargs: dict[str, Any]) -> dict[str, Any]:
    out = dict(kwargs)
    for name in _LIST_FIELDS & out.keys():
        value = out[name]
        out[name] = (
            tuple(p.strip() for p in value.split(",") if p.strip())
            if isinstance(value, str)
            else tuple(value)
        )
    for name in _INT_FIELDS & out.keys():
        out[name] = int(out[name])
    for name in _DECIMAL_FIELDS & out.keys():
        out[name] = to_decimal(out[name])
    for name in _BOOL_FIELDS & out.keys():
        value = out[name]
        out[name] = (
            value.strip().lower() in _TRUTHY if isinstance(value, str) else bool(value)
        )
    return out
