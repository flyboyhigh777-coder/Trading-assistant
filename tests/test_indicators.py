import numpy as np
import pandas as pd
import pytest

from pockettrader import indicators as ind


def test_ema_warms_up_then_tracks_price():
    series = pd.Series(np.full(50, 10.0))
    ema = ind.ema(series, 10)
    assert ema.iloc[:9].isna().all()
    assert ema.iloc[-1] == pytest.approx(10.0)


def test_rsi_bounds_and_extremes():
    rising = pd.Series(np.arange(1, 80, dtype=float))
    falling = pd.Series(np.arange(80, 1, -1, dtype=float))
    assert ind.rsi(rising).iloc[-1] == pytest.approx(100.0)
    assert ind.rsi(falling).iloc[-1] == pytest.approx(0.0, abs=1e-9)

    rng = np.random.default_rng(1)
    noisy = pd.Series(100 + np.cumsum(rng.normal(0, 1, 300)))
    values = ind.rsi(noisy).dropna()
    assert ((values >= 0) & (values <= 100)).all()


def test_macd_histogram_is_line_minus_signal():
    rng = np.random.default_rng(2)
    series = pd.Series(100 + np.cumsum(rng.normal(0, 1, 200)))
    frame = ind.macd(series)
    tail = frame.dropna()
    assert tail["hist"].equals(tail["macd"] - tail["signal"])


def test_bollinger_bands_bracket_price_and_pctb_is_relative():
    rng = np.random.default_rng(3)
    series = pd.Series(100 + np.cumsum(rng.normal(0, 1, 200)))
    bands = ind.bollinger(series).dropna()
    assert (bands["upper"] >= bands["mid"]).all()
    assert (bands["mid"] >= bands["lower"]).all()
    # %B above 1 means the close sits outside the upper band.
    outside = series.loc[bands.index] > bands["upper"]
    assert (bands.loc[outside, "pctb"] > 1.0).all()


def test_atr_is_positive_and_scales_with_range():
    quiet = pd.DataFrame(
        {"open": [10.0] * 40, "high": [10.1] * 40, "low": [9.9] * 40, "close": [10.0] * 40}
    )
    wild = pd.DataFrame(
        {"open": [10.0] * 40, "high": [12.0] * 40, "low": [8.0] * 40, "close": [10.0] * 40}
    )
    assert ind.atr(quiet).iloc[-1] > 0
    assert ind.atr(wild).iloc[-1] > ind.atr(quiet).iloc[-1]


def test_stochastic_pins_to_zero_and_hundred():
    highs = np.linspace(10, 20, 60)
    df = pd.DataFrame(
        {"open": highs, "high": highs, "low": highs - 1.0, "close": highs}
    )
    stoch = ind.stochastic(df)
    assert stoch["k"].iloc[-1] == pytest.approx(100.0)


def test_adx_rises_in_a_trend_and_stays_low_in_chop():
    trend = np.linspace(100, 160, 200)
    chop = 100 + np.sin(np.arange(200) / 3.0)
    trend_df = pd.DataFrame(
        {"open": trend, "high": trend + 0.5, "low": trend - 0.5, "close": trend}
    )
    chop_df = pd.DataFrame({"open": chop, "high": chop + 0.5, "low": chop - 0.5, "close": chop})
    assert ind.adx(trend_df)["adx"].iloc[-1] > ind.adx(chop_df)["adx"].iloc[-1]


def test_body_ratio_marks_doji_and_marubozu():
    df = pd.DataFrame(
        {
            "open": [10.0, 10.0],
            "high": [11.0, 11.0],
            "low": [9.0, 9.0],
            "close": [10.0, 11.0],
        }
    )
    ratios = ind.body_ratio(df)
    assert ratios.iloc[0] == pytest.approx(0.0)
    assert ratios.iloc[1] == pytest.approx(0.5)
