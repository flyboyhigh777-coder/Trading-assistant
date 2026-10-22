"""Выпуски с реальных скриншотов GoInvest — на них считаем в тестах."""

from __future__ import annotations

from datetime import date

import pytest

from uzinvest.bonds import Bond
from uzinvest.config import Settings


@pytest.fixture
def hmmt3b() -> Bond:
    """1 000 000 сум, 26%, купон месячный, 3 года. Купон = 21 666,67."""
    return Bond(
        isin="UZ6058537AA0",
        ticker="HMMT3B",
        issuer="Hamkorbank",
        face_value=1_000_000,
        coupon_rate="0.26",
        issue_date=date(2025, 1, 15),
        maturity_date=date(2028, 1, 15),
        per_year=12,
    )


@pytest.fixture
def bfmt3b6() -> Bond:
    """100 000 сум, 24%, купон КАЖДЫЕ 30 ДНЕЙ — не месяц."""
    return Bond(
        isin="UZ6057687AF3",
        ticker="BFMT3B6",
        issuer="Ipoteka-bank",
        face_value=100_000,
        coupon_rate="0.24",
        issue_date=date(2025, 3, 1),
        maturity_date=date(2028, 3, 1),
        period_days=30,
    )


@pytest.fixture
def dmmt2b3() -> Bond:
    """1 185 000 сум, 28%, 30 дней, 2 года — самый доходный на скриншоте."""
    return Bond(
        isin="UZ6058027AC8",
        ticker="DMMT2B3",
        issuer="Adelta",
        face_value=1_185_000,
        coupon_rate="0.28",
        issue_date=date(2025, 6, 10),
        maturity_date=date(2027, 6, 10),
        period_days=30,
    )


@pytest.fixture
def catalog(hmmt3b, bfmt3b6, dmmt2b3) -> dict[str, Bond]:
    return {b.isin: b for b in (hmmt3b, bfmt3b6, dmmt2b3)}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(subscribers_path=tmp_path / "subscribers.json")
