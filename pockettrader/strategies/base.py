"""Strategy contract.

A strategy looks at a closed-bar OHLCV frame and returns a single :class:`Vote`
about the *next* bar(s).  Strategies never place orders and never touch the
network; they are pure functions of the frame, which keeps them unit-testable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from ..models import Direction, Vote

NO_VOTE_REASON = "no setup"


class Strategy(ABC):
    """Base class for every signal strategy."""

    #: short identifier used in configs and in the Telegram output
    name: str = "strategy"
    #: how many bars the indicators need before the vote is meaningful
    min_bars: int = 60
    #: relative importance when the engine blends votes
    weight: float = 1.0

    @abstractmethod
    def _vote(self, df: pd.DataFrame) -> Vote:
        """Evaluate the last closed bar of ``df``."""

    def evaluate(self, df: pd.DataFrame) -> Vote:
        if len(df) < self.min_bars:
            return self.abstain(f"needs {self.min_bars} bars, got {len(df)}")
        try:
            vote = self._vote(df)
        except (KeyError, ValueError, ZeroDivisionError) as exc:  # pragma: no cover
            return self.abstain(f"evaluation failed: {exc}")
        if vote.direction is not Direction.NONE and pd.isna(vote.strength):
            return self.abstain("indicator warm-up incomplete")
        return vote

    def abstain(self, reason: str = NO_VOTE_REASON) -> Vote:
        return Vote(self.name, Direction.NONE, 0.0, reason)

    def vote(self, direction: Direction, strength: float, reason: str) -> Vote:
        """Build a vote, clamping ``strength`` into the valid range."""
        return Vote(self.name, direction, float(min(max(strength, 0.0), 1.0)), reason)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} name={self.name!r} weight={self.weight}>"
