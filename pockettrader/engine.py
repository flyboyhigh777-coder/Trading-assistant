"""Blend strategy votes into one signal per symbol."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

import pandas as pd

from . import indicators as ind
from .config import TIMEFRAME_MINUTES, Settings
from .data import DataProvider, MarketDataError
from .models import Direction, Signal, Vote
from .strategies import Strategy
from .strategies import build as build_strategies

log = logging.getLogger(__name__)


class SignalEngine:
    """Run every strategy over a frame and reduce their votes to one verdict.

    Confidence is the net weighted agreement: agreeing strength minus opposing
    strength, divided by the total weight on the panel.  A lone strategy
    shouting into four silent ones therefore scores low, which is the point.
    """

    def __init__(self, settings: Settings, strategies: Sequence[Strategy] | None = None):
        self.settings = settings
        self.strategies: list[Strategy] = list(
            strategies if strategies is not None else build_strategies(settings.strategies)
        )
        if not self.strategies:
            raise ValueError("the engine needs at least one strategy")

    @property
    def total_weight(self) -> float:
        return sum(s.weight for s in self.strategies) or 1.0

    @property
    def min_bars(self) -> int:
        return max(s.min_bars for s in self.strategies)

    # -- evaluation -------------------------------------------------------

    def evaluate(self, symbol: str, timeframe: str, df: pd.DataFrame) -> Signal:
        """Produce a signal for the last closed bar of ``df``."""
        votes = tuple(s.evaluate(df) for s in self.strategies)
        notes: list[str] = []

        price = float(df["close"].iloc[-1])
        bar_time = df.index[-1].to_pydatetime()

        direction, confidence = self._tally(votes)

        if len(df) < self.min_bars:
            notes.append(f"only {len(df)} bars available, wanted {self.min_bars}")

        gate = self._volatility_note(df, price)
        if gate:
            notes.append(gate)
            direction, confidence = Direction.NONE, 0.0

        if direction is not Direction.NONE:
            agreeing = [v for v in votes if v.direction is direction]
            if len(agreeing) < self.settings.min_votes:
                notes.append(
                    f"{len(agreeing)} of {self.settings.min_votes} required strategies agreed"
                )
                # Keep the confidence figure: it is useful for "how close was
                # this?", even though we are not calling a trade.
                direction = Direction.NONE
            elif confidence < self.settings.min_confidence:
                notes.append(
                    f"confidence {confidence:.0%} below the "
                    f"{self.settings.min_confidence:.0%} threshold"
                )
                direction = Direction.NONE

        return Signal(
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            confidence=round(confidence, 4),
            price=price,
            bar_time=bar_time,
            expiry_minutes=self.settings.expiry_minutes(timeframe),
            votes=votes,
            notes=tuple(notes),
        )

    def _tally(self, votes: Iterable[Vote]) -> tuple[Direction, float]:
        score = 0.0
        for vote in votes:
            weight = self._weight_of(vote.strategy)
            score += vote.direction.sign * vote.strength * weight
        direction = Direction.from_sign(score)
        confidence = min(abs(score) / self.total_weight, 1.0)
        return direction, confidence

    def _weight_of(self, strategy_name: str) -> float:
        for strategy in self.strategies:
            if strategy.name == strategy_name:
                return strategy.weight
        return 1.0

    def _volatility_note(self, df: pd.DataFrame, price: float) -> str | None:
        """Reject bars that are too wild to price or too dead to move."""
        atr = ind.atr(df).iloc[-1]
        if pd.isna(atr) or price <= 0:
            return None
        atr_pct = float(atr) / price
        if atr_pct > self.settings.max_atr_pct:
            return f"ATR {atr_pct:.2%} above the {self.settings.max_atr_pct:.2%} ceiling"
        if atr_pct < self.settings.min_atr_pct:
            return f"ATR {atr_pct:.3%} below the {self.settings.min_atr_pct:.3%} floor"
        return None

    # -- orchestration ----------------------------------------------------

    def scan_symbol(
        self,
        provider: DataProvider,
        symbol: str,
        timeframe: str | None = None,
    ) -> Signal:
        tf = timeframe or self.settings.timeframe
        if tf not in TIMEFRAME_MINUTES:
            raise MarketDataError(f"unsupported timeframe {tf!r}")
        df = provider.fetch_ohlcv(symbol, tf, self.settings.candles)
        return self.evaluate(symbol, tf, df)

    def scan(
        self,
        provider: DataProvider,
        symbols: Sequence[str] | None = None,
        timeframe: str | None = None,
    ) -> tuple[list[Signal], dict[str, str]]:
        """Scan a watchlist, returning signals and per-symbol failures."""
        signals: list[Signal] = []
        errors: dict[str, str] = {}
        for symbol in symbols or self.settings.watchlist:
            try:
                signals.append(self.scan_symbol(provider, symbol, timeframe))
            except MarketDataError as exc:
                log.warning("skipping %s: %s", symbol, exc)
                errors[symbol] = str(exc)
        signals.sort(key=lambda s: (not s.actionable, -s.confidence, s.symbol))
        return signals, errors
