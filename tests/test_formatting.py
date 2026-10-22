from datetime import datetime, timezone

import pytest

from pockettrader.formatting import (
    confidence_bar,
    format_price,
    format_scan,
    format_signal,
    format_signal_plain,
)
from pockettrader.models import Direction, Signal, Vote


def make_signal(direction=Direction.CALL, confidence=0.72, notes=()):
    return Signal(
        symbol="BTC/USDT",
        timeframe="5m",
        direction=direction,
        confidence=confidence,
        price=64123.5,
        bar_time=datetime(2024, 5, 1, 12, 0, tzinfo=timezone.utc),
        expiry_minutes=15,
        votes=(
            Vote("ema_pullback", Direction.CALL, 0.8, "EMA9>EMA21"),
            Vote("bb_reversion", Direction.NONE, 0.0, "no setup"),
        ),
        notes=notes,
    )


def test_signal_renders_direction_price_and_votes():
    text = format_signal(make_signal())
    assert "CALL" in text
    assert "64,123.50" in text
    assert "72%" in text
    assert "ema_pullback" in text
    assert "15 min" in text


def test_no_trade_explains_itself():
    text = format_signal(make_signal(direction=Direction.NONE, confidence=0.1))
    assert "NO TRADE" in text
    assert "Why no trade" in text
    assert "bb_reversion" in text
    assert "min</b>" not in text   # no expiry suggested when there is no trade


def test_notes_are_surfaced():
    assert "ATR too wide" in format_signal(make_signal(notes=("ATR too wide",)))


def test_user_content_is_escaped():
    signal = Signal(
        symbol="<script>/USDT",
        timeframe="5m",
        direction=Direction.CALL,
        confidence=0.9,
        price=1.0,
        bar_time=datetime(2024, 5, 1, tzinfo=timezone.utc),
        expiry_minutes=15,
        votes=(Vote("x", Direction.CALL, 0.9, "a & b <tag>"),),
    )
    text = format_signal(signal)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "a &amp; b" in text


@pytest.mark.parametrize(
    "price,expected",
    [(64123.456, "64,123.46"), (12.3456789, "12.3457"), (0.00001234, "0.00001234")],
)
def test_prices_scale_their_precision(price, expected):
    assert format_price(price) == expected


def test_confidence_bar_is_fixed_width():
    assert len(confidence_bar(0.0)) == len(confidence_bar(1.0)) == 10
    assert confidence_bar(1.0) == "█" * 10
    assert confidence_bar(0.0) == "░" * 10
    assert confidence_bar(2.0) == "█" * 10   # clamped


def test_scan_lists_actionable_signals_first():
    signals = [make_signal(), make_signal(direction=Direction.NONE, confidence=0.2)]
    text = format_scan(signals)
    assert "1 of 2 symbols actionable" in text
    assert "BTC/USDT" in text
    assert "Signals are indicator output" in text


def test_scan_with_nothing_to_do_names_the_closest_call():
    text = format_scan([make_signal(direction=Direction.NONE, confidence=0.31)])
    assert "Nothing to do" in text
    assert "31%" in text


def test_scan_reports_per_symbol_errors():
    text = format_scan([make_signal()], errors={"DOGE/USDT": "not listed"})
    assert "DOGE/USDT" in text and "not listed" in text


def test_plain_rendering_is_one_line():
    line = format_signal_plain(make_signal())
    assert "\n" not in line
    assert "CALL" in line and "BTC/USDT" in line
