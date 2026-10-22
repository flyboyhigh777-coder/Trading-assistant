import pandas as pd
import pytest

from pockettrader.data import (
    CcxtProvider,
    DataProvider,
    MarketDataError,
    drop_unclosed,
    to_frame,
)


def rows_ending_at(end: pd.Timestamp, count: int, minutes: int = 5):
    return [
        [
            int((end - pd.Timedelta(minutes=minutes * i)).timestamp() * 1000),
            100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i, 10.0,
        ]
        for i in range(count)
    ][::-1]


def test_to_frame_indexes_by_utc_timestamp():
    frame = to_frame(rows_ending_at(pd.Timestamp("2024-01-01 12:00", tz="UTC"), 3))
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert str(frame.index.tz) == "UTC"
    assert frame.index.is_monotonic_increasing
    assert frame.dtypes.unique().tolist() == ["float64"]


def test_to_frame_drops_duplicate_timestamps():
    rows = rows_ending_at(pd.Timestamp("2024-01-01 12:00", tz="UTC"), 2)
    frame = to_frame(rows + [rows[-1]])
    assert len(frame) == 2


def test_the_still_open_candle_is_dropped():
    now = pd.Timestamp.now(tz="UTC")
    frame = to_frame(rows_ending_at(now.floor("5min"), 4))
    trimmed = drop_unclosed(frame, "5m")
    assert len(trimmed) == len(frame) - 1
    assert trimmed.index[-1] + pd.Timedelta(minutes=5) <= pd.Timestamp.now(tz="UTC")


def test_settled_candles_are_kept():
    old = pd.Timestamp.now(tz="UTC").floor("5min") - pd.Timedelta(hours=2)
    frame = to_frame(rows_ending_at(old, 4))
    assert len(drop_unclosed(frame, "5m")) == len(frame)


def test_drop_unclosed_handles_an_empty_frame():
    empty = to_frame([])
    assert drop_unclosed(empty, "5m").empty


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("btcusdt", "BTC/USDT"),
        ("eth-usdt", "ETH/USDT"),
        ("sol_usdt", "SOL/USDT"),
        (" ada/usdt ", "ADA/USDT"),
        ("ethbtc", "ETH/BTC"),
        ("WEIRD", "WEIRD"),
    ],
)
def test_symbols_are_normalised(raw, expected):
    assert CcxtProvider().resolve(raw) == expected


def test_an_unknown_timeframe_is_refused_before_any_network_call():
    with pytest.raises(MarketDataError, match="unsupported timeframe"):
        CcxtProvider().fetch_ohlcv("BTC/USDT", "7m", 100)


def test_an_unknown_exchange_is_reported_clearly():
    with pytest.raises(MarketDataError, match="unknown ccxt exchange"):
        _ = CcxtProvider("not_a_real_exchange").client


class FakeExchange:
    """Stands in for a ccxt client and counts the calls it receives."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe, limit):
        self.calls += 1
        return self.rows


def test_results_are_cached_for_the_ttl():
    old = pd.Timestamp.now(tz="UTC").floor("5min") - pd.Timedelta(hours=3)
    provider = CcxtProvider(cache_seconds=60)
    provider._client = FakeExchange(rows_ending_at(old, 10))

    first = provider.fetch_ohlcv("BTC/USDT", "5m", 5)
    second = provider.fetch_ohlcv("BTC/USDT", "5m", 5)
    assert provider._client.calls == 1
    assert first.equals(second)

    provider.cache_seconds = 0
    provider.fetch_ohlcv("BTC/USDT", "5m", 5)
    assert provider._client.calls == 2


def test_callers_cannot_mutate_the_cached_frame():
    old = pd.Timestamp.now(tz="UTC").floor("5min") - pd.Timedelta(hours=3)
    provider = CcxtProvider(cache_seconds=60)
    provider._client = FakeExchange(rows_ending_at(old, 10))

    frame = provider.fetch_ohlcv("BTC/USDT", "5m", 5)
    frame.loc[frame.index[0], "close"] = -1.0
    assert provider.fetch_ohlcv("BTC/USDT", "5m", 5)["close"].iloc[0] != -1.0


def test_exchange_failures_become_market_data_errors():
    class Broken:
        def fetch_ohlcv(self, *args, **kwargs):
            raise TimeoutError("read timed out")

    provider = CcxtProvider()
    provider._client = Broken()
    with pytest.raises(MarketDataError, match="read timed out"):
        provider.fetch_ohlcv("BTC/USDT", "5m", 5)


def test_an_empty_response_is_an_error():
    provider = CcxtProvider()
    provider._client = FakeExchange([])
    with pytest.raises(MarketDataError, match="no candles"):
        provider.fetch_ohlcv("BTC/USDT", "5m", 5)


def test_ccxt_provider_satisfies_the_protocol():
    assert isinstance(CcxtProvider(), DataProvider)
