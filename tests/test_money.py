from decimal import Decimal

import pytest

from uzinvest.money import (
    MoneyError,
    format_percent,
    format_sum,
    from_tiyin,
    round_sum,
    to_decimal,
    to_tiyin,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1 185 000", Decimal("1185000")),
        ("21 666,67", Decimal("21666.67")),
        ("1 000 000", Decimal("1000000")),
        (1_000_000, Decimal("1000000")),
        (0.1, Decimal("0.1")),
    ],
)
def test_parses_local_number_formats(raw, expected):
    assert to_decimal(raw) == expected


def test_float_does_not_leak_binary_noise():
    # Через str, иначе 0.1 станет 0.1000000000000000055511151231257827
    assert to_decimal(0.1) == Decimal("0.1")


def test_garbage_is_rejected():
    with pytest.raises(MoneyError):
        to_decimal("не число")
    with pytest.raises(MoneyError):
        to_decimal(None)


def test_sums_are_formatted_with_a_narrow_space():
    assert format_sum(1163376) == "1 163 376 сум"
    assert format_sum("21666.666", decimals=2) == "21 666,67 сум"
    assert format_sum(5000, unit="") == "5 000"


def test_negative_sums_use_a_real_minus():
    assert format_sum(-5000).startswith("−")


def test_percent_uses_a_comma():
    assert format_percent("0.293301") == "29,33%"
    assert format_percent("0.26", decimals=0) == "26%"


@pytest.mark.parametrize(
    "sums,tiyin", [("15000.50", 1_500_050), (1, 100), ("0.01", 1), (0, 0)]
)
def test_tiyin_round_trip(sums, tiyin):
    # Payme принимает суммы в тийинах, ошибка здесь стоит ровно в 100 раз.
    assert to_tiyin(sums) == tiyin
    assert from_tiyin(tiyin) == to_decimal(sums).quantize(Decimal("0.01"))


def test_rounding_is_half_up_not_bankers():
    # Банковское округление дало бы 2, а в деньгах ждут 3.
    assert round_sum("2.5") == Decimal("3")
    assert round_sum("3.5") == Decimal("4")
