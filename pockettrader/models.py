"""Core value objects shared by the data layer, strategies and the bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Direction(str, Enum):
    """Trade direction, in binary-option terms."""

    CALL = "CALL"   # expect price up at expiry
    PUT = "PUT"     # expect price down at expiry
    NONE = "NONE"   # no edge

    @property
    def sign(self) -> int:
        if self is Direction.CALL:
            return 1
        if self is Direction.PUT:
            return -1
        return 0

    @property
    def opposite(self) -> "Direction":
        if self is Direction.CALL:
            return Direction.PUT
        if self is Direction.PUT:
            return Direction.CALL
        return Direction.NONE

    @classmethod
    def from_sign(cls, value: float) -> "Direction":
        if value > 0:
            return cls.CALL
        if value < 0:
            return cls.PUT
        return cls.NONE


@dataclass(frozen=True)
class Vote:
    """One strategy's opinion about the most recent closed bar."""

    strategy: str
    direction: Direction
    strength: float  # 0.0 .. 1.0, how strongly the setup fired
    reason: str

    def __post_init__(self) -> None:
        if not 0.0 <= self.strength <= 1.0:
            raise ValueError(f"strength must be in [0, 1], got {self.strength}")
        if self.direction is Direction.NONE and self.strength != 0.0:
            raise ValueError("a NONE vote must have zero strength")


@dataclass(frozen=True)
class Signal:
    """The engine's verdict for one symbol/timeframe."""

    symbol: str
    timeframe: str
    direction: Direction
    confidence: float          # 0.0 .. 1.0
    price: float
    bar_time: datetime         # close time of the bar the signal is based on
    expiry_minutes: int        # suggested option expiry
    votes: tuple[Vote, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)
    created_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )

    @property
    def actionable(self) -> bool:
        return self.direction is not Direction.NONE

    @property
    def agreeing(self) -> tuple[Vote, ...]:
        return tuple(v for v in self.votes if v.direction is self.direction)

    @property
    def opposing(self) -> tuple[Vote, ...]:
        return tuple(v for v in self.votes if v.direction is self.direction.opposite)

    def key(self) -> tuple[str, str, str]:
        """Identity used to suppress duplicate alerts."""
        return (self.symbol, self.timeframe, self.direction.value)
