import numpy as np
import pytest

from pockettrader.data import MarketDataError
from pockettrader.engine import SignalEngine
from pockettrader.models import Direction, Vote
from pockettrader.strategies.base import Strategy

from .helpers import make_frame


class Fixed(Strategy):
    """A strategy that always votes the same way, for testing the tally."""

    min_bars = 1

    def __init__(self, name, direction, strength, weight=1.0):
        self.name = name
        self.weight = weight
        self._direction = direction
        self._strength = strength

    def _vote(self, df):
        return Vote(self.name, self._direction, self._strength, "fixed")


@pytest.fixture
def frame():
    return make_frame(100 + np.linspace(0, 5, 300))


def engine_with(settings, *strategies, **overrides):
    for key, value in overrides.items():
        setattr(settings, key, value)
    return SignalEngine(settings, list(strategies))


def test_unanimous_votes_produce_a_confident_signal(settings, frame):
    engine = engine_with(
        settings,
        Fixed("a", Direction.CALL, 1.0),
        Fixed("b", Direction.CALL, 1.0),
        min_votes=2,
    )
    signal = engine.evaluate("BTC/USDT", "5m", frame)
    assert signal.direction is Direction.CALL
    assert signal.confidence == pytest.approx(1.0)
    assert len(signal.agreeing) == 2


def test_opposing_votes_cancel_out(settings, frame):
    engine = engine_with(
        settings,
        Fixed("a", Direction.CALL, 0.8),
        Fixed("b", Direction.PUT, 0.8),
    )
    signal = engine.evaluate("BTC/USDT", "5m", frame)
    assert signal.direction is Direction.NONE
    assert signal.confidence == pytest.approx(0.0)


def test_weights_tip_a_split_decision(settings, frame):
    engine = engine_with(
        settings,
        Fixed("heavy", Direction.PUT, 0.9, weight=2.0),
        Fixed("light", Direction.CALL, 0.9, weight=1.0),
        min_votes=1,
        min_confidence=0.2,
    )
    signal = engine.evaluate("BTC/USDT", "5m", frame)
    assert signal.direction is Direction.PUT
    assert signal.opposing[0].strategy == "light"


def test_min_votes_suppresses_a_lone_shout(settings, frame):
    engine = engine_with(
        settings,
        Fixed("a", Direction.CALL, 1.0),
        Fixed("quiet", Direction.NONE, 0.0),
        min_votes=2,
        min_confidence=0.1,
    )
    signal = engine.evaluate("BTC/USDT", "5m", frame)
    assert signal.direction is Direction.NONE
    assert any("required strategies agreed" in n for n in signal.notes)


def test_confidence_threshold_blocks_a_weak_signal(settings, frame):
    engine = engine_with(
        settings,
        Fixed("a", Direction.CALL, 0.2),
        Fixed("b", Direction.CALL, 0.2),
        min_votes=2,
        min_confidence=0.6,
    )
    signal = engine.evaluate("BTC/USDT", "5m", frame)
    assert signal.direction is Direction.NONE
    assert any("below the" in n for n in signal.notes)


def test_wild_volatility_vetoes_the_signal(settings):
    # A market doubling and halving every bar is unpriceable, not a setup.
    closes = 100 * (1 + 0.2 * np.tile([1.0, -1.0], 150)).cumprod()
    engine = engine_with(
        settings, Fixed("a", Direction.CALL, 1.0), min_votes=1, max_atr_pct=0.03
    )
    signal = engine.evaluate("BTC/USDT", "5m", make_frame(closes))
    assert signal.direction is Direction.NONE
    assert any("ceiling" in n for n in signal.notes)


def test_dead_market_vetoes_the_signal(settings):
    engine = engine_with(
        settings,
        Fixed("a", Direction.CALL, 1.0),
        min_votes=1,
        min_atr_pct=0.01,
    )
    signal = engine.evaluate("BTC/USDT", "5m", make_frame(np.full(300, 100.0)))
    assert signal.direction is Direction.NONE
    assert any("floor" in n for n in signal.notes)


def test_expiry_follows_the_timeframe(settings, frame):
    settings.expiry_bars = 3
    engine = engine_with(settings, Fixed("a", Direction.CALL, 1.0), min_votes=1)
    assert engine.evaluate("BTC/USDT", "15m", frame).expiry_minutes == 45
    assert engine.evaluate("BTC/USDT", "1h", frame).expiry_minutes == 180


def test_scan_sorts_actionable_first_and_collects_errors(settings, frame):
    class Provider:
        name = "fake"

        def fetch_ohlcv(self, symbol, timeframe, limit):
            if symbol == "BAD/USDT":
                raise MarketDataError("symbol not listed")
            return frame

        def symbols(self):
            return ["BTC/USDT", "ETH/USDT"]

    engine = engine_with(settings, Fixed("a", Direction.CALL, 1.0), min_votes=1)
    signals, errors = engine.scan(Provider(), ["BTC/USDT", "BAD/USDT", "ETH/USDT"], "5m")
    assert [s.symbol for s in signals] == ["BTC/USDT", "ETH/USDT"]
    assert errors == {"BAD/USDT": "symbol not listed"}


def test_engine_needs_at_least_one_strategy(settings):
    with pytest.raises(ValueError, match="at least one strategy"):
        SignalEngine(settings, [])


def test_scan_symbol_rejects_an_unknown_timeframe(settings, frame):
    class Provider:
        name = "fake"

        def fetch_ohlcv(self, symbol, timeframe, limit):
            return frame

        def symbols(self):
            return []

    engine = engine_with(settings, Fixed("a", Direction.CALL, 1.0))
    with pytest.raises(MarketDataError, match="unsupported timeframe"):
        engine.scan_symbol(Provider(), "BTC/USDT", "7m")


def test_real_strategies_run_end_to_end(settings, uptrend):
    engine = SignalEngine(settings)
    signal = engine.evaluate("BTC/USDT", "5m", uptrend)
    assert len(signal.votes) == len(engine.strategies)
    assert 0.0 <= signal.confidence <= 1.0
