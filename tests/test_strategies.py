import numpy as np
import pytest

from pockettrader import strategies
from pockettrader.models import Direction
from pockettrader.strategies import (
    BollingerReversion,
    EmaPullback,
    MacdMomentum,
    StochasticCross,
)

from .helpers import make_frame

ALL = [EmaPullback(), BollingerReversion(), MacdMomentum(), StochasticCross()]


@pytest.mark.parametrize("strategy", ALL, ids=lambda s: s.name)
def test_abstains_without_enough_bars(strategy):
    short = make_frame(np.linspace(100, 110, 20))
    vote = strategy.evaluate(short)
    assert vote.direction is Direction.NONE
    assert vote.strength == 0.0
    assert "bars" in vote.reason


@pytest.mark.parametrize("strategy", ALL, ids=lambda s: s.name)
def test_votes_are_well_formed(strategy, uptrend):
    vote = strategy.evaluate(uptrend)
    assert vote.strategy == strategy.name
    assert 0.0 <= vote.strength <= 1.0
    assert vote.reason


@pytest.mark.parametrize("strategy", ALL, ids=lambda s: s.name)
def test_flat_market_produces_no_signal(strategy, flat):
    assert strategy.evaluate(flat).direction is Direction.NONE


@pytest.mark.parametrize("strategy", ALL, ids=lambda s: s.name)
def test_direction_mirrors_when_the_market_mirrors(strategy, uptrend, downtrend):
    up = strategy.evaluate(uptrend)
    down = strategy.evaluate(downtrend)
    if up.direction is Direction.NONE:
        pytest.skip(f"{strategy.name} did not fire on the uptrend fixture")
    assert down.direction is up.direction.opposite


def test_ema_pullback_fires_call_on_a_trend_resumption(uptrend):
    vote = EmaPullback().evaluate(uptrend)
    assert vote.direction is Direction.CALL
    assert vote.strength > 0.4


def test_bollinger_reversion_fades_a_spike(spike_in_a_range):
    vote = BollingerReversion().evaluate(spike_in_a_range)
    assert vote.direction is Direction.CALL
    assert "lower band" in vote.reason


def test_bollinger_reversion_stands_aside_in_a_strong_trend():
    # Same shape of spike, but inside a hard downtrend: fading it is the wrong
    # trade, so the ADX gate must keep the strategy out.
    closes = np.linspace(200.0, 120.0, 200)
    closes[-2] -= 6.0
    closes[-1] = closes[-2] + 2.0
    vote = BollingerReversion().evaluate(make_frame(closes))
    assert vote.direction is Direction.NONE
    assert "too trending" in vote.reason


def test_macd_momentum_refuses_a_flip_against_the_trend():
    # Long decline, then one sharp up bar: the histogram flips but price is
    # still far below the trend EMA, so the strategy must stay out.
    closes = np.concatenate([np.linspace(200, 100, 200), [104.0]])
    vote = MacdMomentum().evaluate(make_frame(closes))
    assert vote.direction is Direction.NONE


def test_registry_builds_defaults_and_rejects_unknown_names():
    built = strategies.build()
    assert {s.name for s in built} == set(strategies.REGISTRY)
    assert [s.name for s in strategies.build(["ema_pullback"])] == ["ema_pullback"]
    with pytest.raises(KeyError, match="unknown strategies"):
        strategies.build(["does_not_exist"])


def test_evaluate_survives_a_broken_frame():
    class Broken(EmaPullback):
        def _vote(self, df):
            raise ValueError("boom")

    vote = Broken().evaluate(make_frame(np.linspace(100, 120, 300)))
    assert vote.direction is Direction.NONE
    assert "boom" in vote.reason
