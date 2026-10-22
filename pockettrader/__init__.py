"""PocketTrader — a signal engine and Telegram assistant for short-expiry trades.

The engine produces CALL/PUT opinions from indicator strategies; it does not
place orders anywhere.  See README.md for the risk note that belongs with that.
"""

from .config import Settings
from .engine import SignalEngine
from .models import Direction, Signal, Vote

__version__ = "0.1.0"

__all__ = ["Settings", "SignalEngine", "Direction", "Signal", "Vote", "__version__"]
