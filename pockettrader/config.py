"""Configuration: YAML file overlaid with environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

DEFAULT_WATCHLIST: tuple[str, ...] = (
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "XRP/USDT",
    "BNB/USDT",
)

#: minutes per candle, for the timeframes the bot accepts
TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "2h": 120,
    "4h": 240,
    "1d": 1440,
}

ENV_PREFIX = "POCKETTRADER_"


@dataclass
class Settings:
    """Everything the engine and the bot need to run."""

    # --- market data -----------------------------------------------------
    exchange: str = "binance"
    timeframe: str = "5m"
    watchlist: tuple[str, ...] = DEFAULT_WATCHLIST
    candles: int = 400
    cache_seconds: int = 20

    # --- signal engine ---------------------------------------------------
    strategies: tuple[str, ...] = ()
    min_confidence: float = 0.55
    min_votes: int = 2
    expiry_bars: int = 3
    max_atr_pct: float = 0.035   # skip bars whose ATR exceeds this share of price
    min_atr_pct: float = 0.0005  # ...or that are too dead to move

    # --- telegram --------------------------------------------------------
    telegram_token: str = ""
    scan_interval_seconds: int = 300
    alert_cooldown_seconds: int = 1800
    allowed_chat_ids: tuple[int, ...] = ()
    admin_chat_ids: tuple[int, ...] = ()
    subscribers_path: Path = Path("subscribers.json")
    rate_limit_per_minute: int = 20
    free_trial_days: int = 3

    # --- billing (Crypto Pay) --------------------------------------------
    # One app per environment: @CryptoTestnetBot locally, @CryptoBot in prod.
    cryptobot_token: str = ""
    cryptobot_testnet: bool = True
    # Public origin the webhook is reachable on (a tunnel locally, the real
    # domain in production).  Crypto Pay only delivers to a public HTTPS URL.
    public_base_url: str = ""
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080
    webhook_path: str = "/api/billing/webhook"
    invoice_poll_seconds: int = 120

    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.watchlist = tuple(dict.fromkeys(s.upper() for s in self.watchlist))
        self.strategies = tuple(self.strategies)
        self.allowed_chat_ids = tuple(int(c) for c in self.allowed_chat_ids)
        self.admin_chat_ids = tuple(int(c) for c in self.admin_chat_ids)
        self.subscribers_path = Path(self.subscribers_path)
        self.public_base_url = self.public_base_url.rstrip("/")
        if not self.webhook_path.startswith("/"):
            self.webhook_path = "/" + self.webhook_path
        if self.timeframe not in TIMEFRAME_MINUTES:
            raise ValueError(
                f"unsupported timeframe {self.timeframe!r}; "
                f"choose one of {', '.join(TIMEFRAME_MINUTES)}"
            )
        if not 0.0 < self.min_confidence <= 1.0:
            raise ValueError("min_confidence must be in (0, 1]")
        if self.min_votes < 1:
            raise ValueError("min_votes must be at least 1")
        if self.candles < 150:
            raise ValueError("candles must be >= 150 so indicators can warm up")

    # -- helpers ----------------------------------------------------------

    def expiry_minutes(self, timeframe: str | None = None) -> int:
        tf = timeframe or self.timeframe
        return TIMEFRAME_MINUTES[tf] * self.expiry_bars

    def is_chat_allowed(self, chat_id: int) -> bool:
        return not self.allowed_chat_ids or chat_id in self.allowed_chat_ids

    def is_admin(self, chat_id: int) -> bool:
        return chat_id in self.admin_chat_ids

    @property
    def billing_enabled(self) -> bool:
        """Billing is on only when an app token is configured.

        Without one the bot serves everyone, which is what you want while
        developing and what you must not ship.
        """
        return bool(self.cryptobot_token)

    @property
    def webhook_url(self) -> str:
        """The URL to paste into the Crypto Pay app's Webhooks setting."""
        if not self.public_base_url:
            return ""
        return f"{self.public_base_url}{self.webhook_path}"

    # -- loading ----------------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Settings":
        """Build settings from an optional YAML file, then the environment."""
        data: dict[str, Any] = {}
        config_path = Path(path) if path else Path(os.getenv(f"{ENV_PREFIX}CONFIG", ""))
        if config_path and config_path.is_file():
            import yaml  # imported lazily so the core stays dependency-light

            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            if not isinstance(loaded, dict):
                raise ValueError(f"{config_path} must contain a YAML mapping")
            data.update(loaded)
        data.update(_from_env())

        known = {f.name for f in fields(cls)} - {"extra"}
        extra = {k: v for k, v in data.items() if k not in known}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(extra=extra, **_coerce(kwargs))


def _from_env() -> dict[str, Any]:
    """Read POCKETTRADER_* variables, plus a few conventional bare names.

    An empty value counts as unset rather than as a value.  A stray
    ``CRYPTOBOT_TESTNET=`` line in an env file would otherwise read as false and
    quietly point the bot at live payments.
    """
    out: dict[str, Any] = {}
    for key, value in os.environ.items():
        if key.startswith(ENV_PREFIX) and key != f"{ENV_PREFIX}CONFIG" and value.strip():
            out[key[len(ENV_PREFIX):].lower()] = value
    # Conventional bare names, so the env file reads like the upstream docs.
    for env_name, field_name in (
        ("TELEGRAM_BOT_TOKEN", "telegram_token"),
        ("CRYPTOBOT_TOKEN", "cryptobot_token"),
        ("CRYPTOBOT_TESTNET", "cryptobot_testnet"),
        ("PUBLIC_BASE_URL", "public_base_url"),
    ):
        value = os.getenv(env_name)
        if value and field_name not in out:
            out[field_name] = value
    return out


_LIST_FIELDS = {"watchlist", "strategies", "allowed_chat_ids", "admin_chat_ids"}
_BOOL_FIELDS = {"cryptobot_testnet"}
_TRUTHY = {"1", "true", "yes", "on"}
_INT_FIELDS = {
    "candles",
    "cache_seconds",
    "min_votes",
    "expiry_bars",
    "scan_interval_seconds",
    "alert_cooldown_seconds",
    "rate_limit_per_minute",
    "free_trial_days",
    "webhook_port",
    "invoice_poll_seconds",
}
_FLOAT_FIELDS = {"min_confidence", "max_atr_pct", "min_atr_pct"}


def _coerce(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Turn environment strings into the types the dataclass expects."""
    out = dict(kwargs)
    for name in _LIST_FIELDS & out.keys():
        value = out[name]
        if isinstance(value, str):
            out[name] = tuple(p.strip() for p in value.split(",") if p.strip())
        else:
            out[name] = tuple(value)
    for name in _INT_FIELDS & out.keys():
        out[name] = int(out[name])
    for name in _FLOAT_FIELDS & out.keys():
        out[name] = float(out[name])
    for name in _BOOL_FIELDS & out.keys():
        value = out[name]
        out[name] = value.strip().lower() in _TRUTHY if isinstance(value, str) else bool(value)
    return out
