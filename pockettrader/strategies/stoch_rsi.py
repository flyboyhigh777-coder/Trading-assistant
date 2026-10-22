"""Stochastic cross out of an extreme zone, confirmed by the trend EMA."""

from __future__ import annotations

import pandas as pd

from .. import indicators as ind
from ..models import Direction, Vote
from .base import Strategy


class StochasticCross(Strategy):
    name = "stoch_cross"
    min_bars = 80
    weight = 0.9

    def __init__(
        self,
        k_period: int = 14,
        k_smooth: int = 3,
        d_period: int = 3,
        trend: int = 50,
        oversold: float = 25.0,
        overbought: float = 75.0,
    ) -> None:
        self.k_period = k_period
        self.k_smooth = k_smooth
        self.d_period = d_period
        self.trend = trend
        self.oversold = oversold
        self.overbought = overbought

    def _vote(self, df: pd.DataFrame) -> Vote:
        stoch = ind.stochastic(df, self.k_period, self.k_smooth, self.d_period)
        trend = ind.ema(df["close"], self.trend)
        body = ind.body_ratio(df)

        last = -1
        prev = -2
        k, d = stoch["k"], stoch["d"]
        if pd.isna(k.iloc[prev]) or pd.isna(d.iloc[prev]) or pd.isna(trend.iloc[last]):
            return self.abstain("indicator warm-up incomplete")

        crossed_up = k.iloc[prev] <= d.iloc[prev] and k.iloc[last] > d.iloc[last]
        crossed_down = k.iloc[prev] >= d.iloc[prev] and k.iloc[last] < d.iloc[last]
        # A decisive candle makes the cross more trustworthy than a doji does.
        conviction = 0.0 if pd.isna(body.iloc[last]) else min(body.iloc[last], 1.0)
        above_trend = df["close"].iloc[last] > trend.iloc[last]

        if crossed_up and k.iloc[prev] < self.oversold and above_trend:
            depth = min((self.oversold - k.iloc[prev]) / self.oversold, 1.0)
            return self.vote(
                Direction.CALL,
                0.35 + 0.35 * depth + 0.20 * conviction,
                f"%K crossed up from {k.iloc[prev]:.0f} above EMA{self.trend}",
            )

        if crossed_down and k.iloc[prev] > self.overbought and not above_trend:
            depth = min((k.iloc[prev] - self.overbought) / (100.0 - self.overbought), 1.0)
            return self.vote(
                Direction.PUT,
                0.35 + 0.35 * depth + 0.20 * conviction,
                f"%K crossed down from {k.iloc[prev]:.0f} below EMA{self.trend}",
            )

        return self.abstain("no stochastic cross out of an extreme")
