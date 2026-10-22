"""Synthetic market data, so the tests never touch an exchange."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pockettrader.config import Settings

from .helpers import make_frame


@pytest.fixture
def uptrend() -> pd.DataFrame:
    """A steady climb, a 12-bar dip, then a resumption on the last six bars.

    Shaped so the fast EMA crosses back above the slow one on the final bar —
    i.e. a textbook trend pullback, which is what the trend strategies look for.
    """
    rng = np.random.default_rng(7)
    trend = np.linspace(100, 140, 282) + rng.normal(0, 0.15, 282)
    dip = trend[-1] - np.linspace(0.0, 3.0, 12)
    resumption = dip[-1] + np.linspace(0.0, 3.0, 6)
    return make_frame(np.concatenate([trend, dip, resumption]))


@pytest.fixture
def downtrend(uptrend: pd.DataFrame) -> pd.DataFrame:
    """The uptrend mirrored around its first close."""
    pivot = float(uptrend["close"].iloc[0])
    flipped = 2 * pivot - uptrend["close"].to_numpy()
    return make_frame(flipped, start=str(uptrend.index[0]))


@pytest.fixture
def flat() -> pd.DataFrame:
    """A dead sideways market: nothing should fire here."""
    index = np.arange(300)
    return make_frame(100 + 0.02 * np.sin(index / 6.0))


@pytest.fixture
def spike_in_a_range() -> pd.DataFrame:
    """A quiet range, one bar that pierces the lower band, then a close back in."""
    rng = np.random.default_rng(5)
    closes = 100 + 0.3 * np.sin(np.arange(200) / 5.0) + rng.normal(0, 0.08, 200)
    closes[-2] -= 2.0
    closes[-1] = closes[-2] + 1.2
    return make_frame(closes)


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        watchlist=("BTC/USDT", "ETH/USDT"),
        subscribers_path=tmp_path / "subscribers.json",
        min_confidence=0.4,
        min_votes=1,
    )
