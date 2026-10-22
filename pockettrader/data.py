"""Market data access.

Only crypto (via ccxt) is implemented, but everything downstream talks to the
:class:`DataProvider` protocol, so a forex/OTC feed can be dropped in later
without touching the strategies or the bot.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Protocol, runtime_checkable

import pandas as pd

from .config import TIMEFRAME_MINUTES

log = logging.getLogger(__name__)

OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


class MarketDataError(RuntimeError):
    """Raised when candles could not be fetched or are unusable."""


@runtime_checkable
class DataProvider(Protocol):
    """Anything that can hand back closed OHLCV candles."""

    name: str

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
        """Return a UTC-indexed frame of *closed* candles, oldest first."""

    def symbols(self) -> list[str]:
        """Return the tradable symbols this provider knows about."""


class CcxtProvider:
    """Closed OHLCV candles from any ccxt spot exchange, with a short TTL cache.

    ccxt is synchronous; callers on an event loop should wrap these methods in
    ``asyncio.to_thread``.
    """

    def __init__(
        self,
        exchange: str = "binance",
        cache_seconds: int = 20,
        exchange_options: dict | None = None,
    ) -> None:
        self.name = exchange
        self.cache_seconds = cache_seconds
        self._exchange_options = exchange_options or {}
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str, int], tuple[float, pd.DataFrame]] = {}
        self._client = None
        self._symbols: list[str] | None = None

    # -- client -----------------------------------------------------------

    @property
    def client(self):
        if self._client is None:
            try:
                import ccxt
            except ImportError as exc:  # pragma: no cover - env issue
                raise MarketDataError(
                    "ccxt is not installed; run `pip install -r requirements.txt`"
                ) from exc
            if not hasattr(ccxt, self.name):
                raise MarketDataError(f"unknown ccxt exchange {self.name!r}")
            options = {"enableRateLimit": True, "timeout": 20_000}
            options.update(self._exchange_options)
            self._client = getattr(ccxt, self.name)(options)
        return self._client

    # -- public API -------------------------------------------------------

    def symbols(self) -> list[str]:
        if self._symbols is None:
            try:
                markets = self.client.load_markets()
            except Exception as exc:  # ccxt raises a wide variety of errors
                raise MarketDataError(f"could not load {self.name} markets: {exc}") from exc
            self._symbols = sorted(
                s for s, m in markets.items() if m.get("spot", True) and m.get("active", True)
            )
        return self._symbols

    def resolve(self, symbol: str) -> str:
        """Normalise user input such as ``btcusdt`` or ``btc-usdt`` to ``BTC/USDT``."""
        candidate = symbol.strip().upper().replace("-", "/").replace("_", "/")
        if "/" in candidate:
            return candidate
        for quote in ("USDT", "USDC", "USD", "BTC", "ETH", "EUR"):
            if candidate.endswith(quote) and len(candidate) > len(quote):
                return f"{candidate[: -len(quote)]}/{quote}"
        return candidate

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 400) -> pd.DataFrame:
        if timeframe not in TIMEFRAME_MINUTES:
            raise MarketDataError(f"unsupported timeframe {timeframe!r}")
        symbol = self.resolve(symbol)
        key = (symbol, timeframe, limit)
        now = time.monotonic()

        with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < self.cache_seconds:
                return cached[1].copy()

        # +1 because the newest candle is still open and gets dropped below.
        try:
            raw = self.client.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit + 1)
        except Exception as exc:
            raise MarketDataError(f"{self.name} rejected {symbol} {timeframe}: {exc}") from exc

        frame = to_frame(raw)
        frame = drop_unclosed(frame, timeframe)
        if frame.empty:
            raise MarketDataError(f"no candles returned for {symbol} {timeframe}")

        with self._lock:
            self._cache[key] = (now, frame)
        return frame.copy()


def to_frame(rows: list[list[float]]) -> pd.DataFrame:
    """Convert ccxt's ``[ts, o, h, l, c, v]`` rows into a UTC-indexed frame."""
    frame = pd.DataFrame(rows, columns=["timestamp", *OHLCV_COLUMNS])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    frame = frame.set_index("timestamp").astype("float64")
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    return frame


def drop_unclosed(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Drop the trailing candle if its period has not elapsed yet.

    Strategies read ``iloc[-1]`` as a settled bar, so a half-formed candle would
    make signals flicker within the period.
    """
    if frame.empty:
        return frame
    period = pd.Timedelta(minutes=TIMEFRAME_MINUTES[timeframe])
    now = pd.Timestamp.now(tz="UTC")
    if frame.index[-1] + period > now:
        return frame.iloc[:-1]
    return frame
