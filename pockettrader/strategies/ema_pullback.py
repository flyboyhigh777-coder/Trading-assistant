"""Trend-following pullback: trade with the EMA trend after a shallow dip."""

from __future__ import annotations

import pandas as pd

from .. import indicators as ind
from ..models import Direction, Vote
from .base import Strategy


class EmaPullback(Strategy):
    name = "ema_pullback"
    min_bars = 120
    weight = 1.2

    def __init__(
        self,
        fast: int = 9,
        slow: int = 21,
        trend: int = 50,
        rsi_period: int = 14,
        adx_floor: float = 18.0,
    ) -> None:
        self.fast = fast
        self.slow = slow
        self.trend = trend
        self.rsi_period = rsi_period
        self.adx_floor = adx_floor
        self.min_bars = max(self.min_bars, trend * 2)

    def _vote(self, df: pd.DataFrame) -> Vote:
        close = df["close"]
        fast = ind.ema(close, self.fast)
        slow = ind.ema(close, self.slow)
        trend = ind.ema(close, self.trend)
        rsi = ind.rsi(close, self.rsi_period)
        adx = ind.adx(df)["adx"]

        last = -1
        prev = -2
        if pd.isna(trend.iloc[last]) or pd.isna(adx.iloc[last]):
            return self.abstain("indicator warm-up incomplete")

        trend_strength = adx.iloc[last]
        if trend_strength < self.adx_floor:
            return self.abstain(f"ADX {trend_strength:.1f} below {self.adx_floor:.0f}")

        up_trend = close.iloc[last] > trend.iloc[last] and slow.iloc[last] > trend.iloc[last]
        down_trend = close.iloc[last] < trend.iloc[last] and slow.iloc[last] < trend.iloc[last]

        crossed_up = fast.iloc[prev] <= slow.iloc[prev] and fast.iloc[last] > slow.iloc[last]
        crossed_down = fast.iloc[prev] >= slow.iloc[prev] and fast.iloc[last] < slow.iloc[last]

        # Strength blends how decisive the trend is with how much room RSI leaves.
        adx_score = min((trend_strength - self.adx_floor) / 22.0, 1.0)

        if up_trend and crossed_up and rsi.iloc[last] < 70.0:
            room = (70.0 - rsi.iloc[last]) / 35.0
            return self.vote(
                Direction.CALL,
                0.45 + 0.35 * adx_score + 0.20 * min(room, 1.0),
                f"EMA{self.fast}>EMA{self.slow} above EMA{self.trend}, "
                f"ADX {trend_strength:.0f}, RSI {rsi.iloc[last]:.0f}",
            )

        if down_trend and crossed_down and rsi.iloc[last] > 30.0:
            room = (rsi.iloc[last] - 30.0) / 35.0
            return self.vote(
                Direction.PUT,
                0.45 + 0.35 * adx_score + 0.20 * min(room, 1.0),
                f"EMA{self.fast}<EMA{self.slow} below EMA{self.trend}, "
                f"ADX {trend_strength:.0f}, RSI {rsi.iloc[last]:.0f}",
            )

        return self.abstain("no EMA cross in the direction of the trend")
