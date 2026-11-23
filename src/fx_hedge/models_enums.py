"""Enumerations shared by models and cleaning (kept separate to avoid import cycles)."""

from __future__ import annotations

from enum import Enum


class CashflowType(str, Enum):
    INVESTMENT = "Investment"
    INTEREST = "Interest"
    PRINCIPAL_REPAYMENT = "Principal Repayment"


class Severity(str, Enum):
    """How a data-quality issue was resolved."""

    CORRECTED = "corrected"  # value was fixed deterministically; row kept
    REJECTED = "rejected"  # row excluded from analytics


class TradeSide(str, Enum):
    SELL = "SELL"
    BUY = "BUY"
