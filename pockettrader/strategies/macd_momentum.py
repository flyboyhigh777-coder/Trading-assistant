"""Momentum: MACD histogram flipping sign while price agrees with the trend EMA."""

from __future__ import annotations

import pandas as pd

from .. import indicators as ind
from ..models import Direction, Vote
from .base import Strategy


class MacdMomentum(Strategy):
    name = "macd_momentum"
    min_bars = 120
    weight = 1.0

    def __init__(
        self,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        trend: int = 100,
    ) -> None:
        self.fast = fast
        self.slow = slow
        self.signal = signal
        self.trend = trend
        self.min_bars = max(self.min_bars, trend + slow)

    def _vote(self, df: pd.DataFrame) -> Vote:
        close = df["close"]
        macd = ind.macd(close, self.fast, self.slow, self.signal)
        trend = ind.ema(close, self.trend)
        atr_pct = ind.atr(df).iloc[-1] / close.iloc[-1]

        last = -1
        prev = -2
        hist = macd["hist"]
        if pd.isna(hist.iloc[prev]) or pd.isna(trend.iloc[last]):
            return self.abstain("indicator warm-up incomplete")

        flipped_up = hist.iloc[prev] <= 0.0 < hist.iloc[last]
        flipped_down = hist.iloc[prev] >= 0.0 > hist.iloc[last]
        if not (flipped_up or flipped_down):
            return self.abstain("MACD histogram did not flip")

        # Normalise the flip against recent volatility so the score is comparable
        # across symbols and timeframes.
        impulse = abs(hist.iloc[last]) / close.iloc[last] / max(atr_pct, 1e-9)
        impulse_score = min(impulse / 0.35, 1.0)

        if flipped_up and close.iloc[last] > trend.iloc[last]:
            return self.vote(
                Direction.CALL,
                0.40 + 0.40 * impulse_score,
                f"MACD hist turned positive above EMA{self.trend}",
            )

        if flipped_down and close.iloc[last] < trend.iloc[last]:
            return self.vote(
                Direction.PUT,
                0.40 + 0.40 * impulse_score,
                f"MACD hist turned negative below EMA{self.trend}",
            )

        return self.abstain("MACD flip fights the trend EMA")
