"""Signal strategies.

Add a new strategy by subclassing :class:`Strategy` and registering it in
``REGISTRY``; it then becomes selectable from the config by name.
"""

from __future__ import annotations

from .base import Strategy
from .bb_reversion import BollingerReversion
from .ema_pullback import EmaPullback
from .macd_momentum import MacdMomentum
from .stoch_rsi import StochasticCross

REGISTRY: dict[str, type[Strategy]] = {
    EmaPullback.name: EmaPullback,
    BollingerReversion.name: BollingerReversion,
    MacdMomentum.name: MacdMomentum,
    StochasticCross.name: StochasticCross,
}

DEFAULT_STRATEGIES: tuple[str, ...] = tuple(REGISTRY)


def build(names: list[str] | tuple[str, ...] | None = None) -> list[Strategy]:
    """Instantiate strategies by name, defaulting to all of them."""
    selected = tuple(names) if names else DEFAULT_STRATEGIES
    unknown = [n for n in selected if n not in REGISTRY]
    if unknown:
        raise KeyError(
            f"unknown strategies: {', '.join(unknown)}; "
            f"available: {', '.join(REGISTRY)}"
        )
    return [REGISTRY[n]() for n in selected]


__all__ = [
    "Strategy",
    "EmaPullback",
    "BollingerReversion",
    "MacdMomentum",
    "StochasticCross",
    "REGISTRY",
    "DEFAULT_STRATEGIES",
    "build",
]
