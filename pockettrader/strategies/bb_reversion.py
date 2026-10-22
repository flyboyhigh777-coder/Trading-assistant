"""Mean reversion: fade a stretched move that pokes outside a Bollinger band."""

from __future__ import annotations

import pandas as pd

from .. import indicators as ind
from ..models import Direction, Vote
from .base import Strategy


class BollingerReversion(Strategy):
    name = "bb_reversion"
    min_bars = 80
    weight = 1.0

    def __init__(
        self,
        period: int = 20,
        std: float = 2.0,
        rsi_period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
        adx_ceiling: float = 32.0,
    ) -> None:
        self.period = period
        self.std = std
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.overbought = overbought
        self.adx_ceiling = adx_ceiling

    def _vote(self, df: pd.DataFrame) -> Vote:
        close = df["close"]
        bands = ind.bollinger(close, self.period, self.std)
        rsi = ind.rsi(close, self.rsi_period)
        adx = ind.adx(df)["adx"]

        last = -1
        prev = -2
        if pd.isna(bands["pctb"].iloc[last]) or pd.isna(adx.iloc[prev - 1]):
            return self.abstain("indicator warm-up incomplete")

        # Mean reversion is a bad bet inside a strong trend.  Read ADX from
        # before the spike: the spike itself inflates directional movement, and
        # the question here is what regime it happened in.
        regime_adx = adx.iloc[prev - 1]
        if regime_adx > self.adx_ceiling:
            return self.abstain(f"ADX {regime_adx:.1f} too trending to fade")

        pctb = bands["pctb"]
        pierced_low = df["low"].iloc[prev] < bands["lower"].iloc[prev]
        pierced_high = df["high"].iloc[prev] > bands["upper"].iloc[prev]
        closed_back_up = pctb.iloc[last] > 0.0 and close.iloc[last] > close.iloc[prev]
        closed_back_down = pctb.iloc[last] < 1.0 and close.iloc[last] < close.iloc[prev]

        # Wider bands mean a more meaningful stretch; cap the bonus.
        width_score = min(bands["width"].iloc[last] / 0.05, 1.0)

        if pierced_low and closed_back_up and rsi.iloc[prev] < self.oversold:
            depth = min((self.oversold - rsi.iloc[prev]) / 20.0, 1.0)
            return self.vote(
                Direction.CALL,
                0.40 + 0.30 * depth + 0.20 * width_score,
                f"rejected lower band, RSI {rsi.iloc[prev]:.0f}, %B {pctb.iloc[last]:.2f}",
            )

        if pierced_high and closed_back_down and rsi.iloc[prev] > self.overbought:
            depth = min((rsi.iloc[prev] - self.overbought) / 20.0, 1.0)
            return self.vote(
                Direction.PUT,
                0.40 + 0.30 * depth + 0.20 * width_score,
                f"rejected upper band, RSI {rsi.iloc[prev]:.0f}, %B {pctb.iloc[last]:.2f}",
            )

        return self.abstain("price not rejecting a band")
