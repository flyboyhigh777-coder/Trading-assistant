"""Shared synthetic-data builders for the test suite."""

from __future__ import annotations

import numpy as np
import pandas as pd


def make_frame(closes, start: str = "2024-01-01", freq: str = "5min") -> pd.DataFrame:
    """Build an OHLCV frame around a close series with plausible wicks."""
    closes = np.asarray(closes, dtype="float64")
    index = pd.date_range(start, periods=len(closes), freq=freq, tz="UTC")
    opens = np.concatenate([[closes[0]], closes[:-1]])
    spread = np.maximum(np.abs(closes - opens), closes * 0.0005)
    highs = np.maximum(opens, closes) + spread * 0.4
    lows = np.minimum(opens, closes) - spread * 0.4
    return pd.DataFrame(
        {
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": np.full(len(closes), 1000.0),
        },
        index=index,
    )
