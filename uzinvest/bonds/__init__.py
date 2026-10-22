"""Облигационная математика: модель, календарь, доходность."""

from .model import Bond, BondError, Cashflow, Position, add_months
from .schedule import (
    cashflows,
    coupon_dates,
    monthly_totals,
    next_coupon,
    portfolio_cashflows,
)
from .yields import (
    accrued_interest,
    compare_with_deposit,
    coupon_payment,
    effective_annual_rate,
    price_from_yield,
    real_rate,
    simple_annual_rate,
    total_with_reinvestment,
    yield_to_maturity,
)

__all__ = [
    "Bond",
    "BondError",
    "Cashflow",
    "Position",
    "add_months",
    "cashflows",
    "coupon_dates",
    "monthly_totals",
    "next_coupon",
    "portfolio_cashflows",
    "accrued_interest",
    "compare_with_deposit",
    "coupon_payment",
    "effective_annual_rate",
    "price_from_yield",
    "real_rate",
    "simple_annual_rate",
    "total_with_reinvestment",
    "yield_to_maturity",
]
