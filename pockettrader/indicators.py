"""Technical indicators implemented directly on pandas.

Deliberately dependency-free (pandas/numpy only) so the signal logic stays
testable and does not pin a TA C-extension.  Every function returns a Series or
DataFrame aligned to the input index, with NaN for the warm-up period.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = [
    "sma",
    "ema",
    "rma",
    "true_range",
    "atr",
    "rsi",
    "macd",
    "bollinger",
    "stochastic",
    "adx",
    "body_ratio",
]


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (used by RSI, ATR and ADX)."""
    return series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return rma(true_range(df), period)


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss == 0 means an unbroken run of gains -> RSI 100.
    out = out.where(avg_loss != 0.0, 100.0)
    return out.where(avg_gain.notna(), np.nan)


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    line = ema(series, fast) - ema(series, slow)
    signal_line = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame(
        {"macd": line, "signal": signal_line, "hist": line - signal_line}
    )


def bollinger(series: pd.Series, period: int = 20, std: float = 2.0) -> pd.DataFrame:
    mid = sma(series, period)
    dev = series.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + std * dev
    lower = mid - std * dev
    span = (upper - lower).replace(0.0, np.nan)
    return pd.DataFrame(
        {
            "mid": mid,
            "upper": upper,
            "lower": lower,
            "pctb": (series - lower) / span,
            "width": span / mid.replace(0.0, np.nan),
        }
    )


def stochastic(
    df: pd.DataFrame,
    k_period: int = 14,
    k_smooth: int = 3,
    d_period: int = 3,
) -> pd.DataFrame:
    lowest = df["low"].rolling(k_period, min_periods=k_period).min()
    highest = df["high"].rolling(k_period, min_periods=k_period).max()
    span = (highest - lowest).replace(0.0, np.nan)
    raw_k = 100.0 * (df["close"] - lowest) / span
    k = raw_k.rolling(k_smooth, min_periods=k_smooth).mean()
    d = k.rolling(d_period, min_periods=d_period).mean()
    return pd.DataFrame({"k": k, "d": d})


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(
        np.where((up > down) & (up > 0.0), up, 0.0), index=df.index, dtype="float64"
    )
    minus_dm = pd.Series(
        np.where((down > up) & (down > 0.0), down, 0.0), index=df.index, dtype="float64"
    )
    atr_ = rma(true_range(df), period).replace(0.0, np.nan)
    plus_di = 100.0 * rma(plus_dm, period) / atr_
    minus_di = 100.0 * rma(minus_dm, period) / atr_
    di_sum = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    return pd.DataFrame({"adx": rma(dx, period), "plus_di": plus_di, "minus_di": minus_di})


def body_ratio(df: pd.DataFrame) -> pd.Series:
    """Candle body size as a fraction of its full range (0 = doji, 1 = marubozu)."""
    span = (df["high"] - df["low"]).replace(0.0, np.nan)
    return (df["close"] - df["open"]).abs() / span
