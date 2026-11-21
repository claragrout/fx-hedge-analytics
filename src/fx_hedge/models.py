"""Pydantic models: the validated cashflow record plus analytics/hedge outputs.

Validation strategy
-------------------
* Field names mirror the file's column headers (``Fund Name`` -> ``fund_name``).
* Fields with known dirty values (dates, currencies) have ``mode="before"``
  validators that call the parsers in :mod:`fx_hedge.cleaning`. Every other
  field relies on Pydantic's own type checks.
* Corrections are *recorded* through the validation context
  (``context={"issues": [...]}``) so nothing is fixed silently.
* Anything that cannot be fixed raises, producing a ``ValidationError`` that
  the ingestion layer turns into a REJECTED issue.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from . import cleaning
from .models_enums import CashflowType, Severity, TradeSide

__all__ = [
    "CashflowRecord",
    "CashflowType",
    "CurrencyAnalytics",
    "DataIssue",
    "FundAnalytics",
    "HedgeTrade",
    "NavPoint",
    "Severity",
    "TradeSide",
]


class DataIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    row_id: int | None
    field: str
    severity: Severity
    message: str
    raw_value: str | None = None


def _record(info: ValidationInfo, raw: Any, note: str | None) -> None:
    """Log a correction made by a field validator as a CORRECTED issue."""
    if note is None or not info.context:
        return
    issues = info.context.get("issues")
    if issues is None:
        return
    issues.append(
        DataIssue(
            row_id=info.context.get("row_id"),
            field=info.field_name,
            severity=Severity.CORRECTED,
            message=note,
            raw_value=None if raw is None else str(raw),
        )
    )


class CashflowRecord(BaseModel):
    """One validated projected cashflow (one row of the client file)."""

    model_config = ConfigDict(frozen=True)

    id: int = Field(gt=0)
    fund_name: str
    date: dt.date
    cashflow_type: CashflowType
    local_currency: str
    cashflow_amount_local: float = Field(allow_inf_nan=False)
    cashflow_amount_base: float = Field(allow_inf_nan=False)
    base_currency: str

    # -- field-level cleaning -------------------------------------------------

    @field_validator("date", mode="before")
    @classmethod
    def _date(cls, v: Any, info: ValidationInfo) -> dt.date:
        value, note = cleaning.parse_date(v)
        _record(info, v, note)
        return value

    @field_validator("local_currency", "base_currency", mode="before")
    @classmethod
    def _currency(cls, v: Any, info: ValidationInfo) -> str:
        value, note = cleaning.parse_currency(v)
        _record(info, v, note)
        return value

    # -- cross-field business rules -------------------------------------------

    @model_validator(mode="after")
    def _business_rules(self) -> "CashflowRecord":
        local, base = self.cashflow_amount_local, self.cashflow_amount_base
        if local == 0:
            raise ValueError("zero cashflow amount")
        if base == 0:
            raise ValueError("zero base amount")
        expected_negative = self.cashflow_type is CashflowType.INVESTMENT
        if (local < 0) != expected_negative:
            raise ValueError(
                f"sign convention violated: {self.cashflow_type.value} must be "
                f"{'negative' if expected_negative else 'positive'}, got {local:,.2f}"
            )
        if (base < 0) != (local < 0):
            raise ValueError("local and base amounts have inconsistent signs")
        if self.local_currency == self.base_currency and local != base:
            raise ValueError(
                "local and base currency are identical but amounts differ "
                f"({local:,.2f} vs {base:,.2f})"
            )
        return self

    # -- convenience ------------------------------------------------------------

    @property
    def implied_fx_rate(self) -> float:
        """Base units per one unit of local currency, as embedded in the projection."""
        return self.cashflow_amount_base / self.cashflow_amount_local


# ---------------------------------------------------------------------------
# Analytics outputs
# ---------------------------------------------------------------------------


class NavPoint(BaseModel):
    """NAV of one currency position at one date.

    ``nav_local`` follows the specification: PV at the position IRR of all
    cashflows dated on or after ``nav_date`` (so NAV(0)=0 and NAV(T)=exit value).
    ``nav_ex_local`` is the same value excluding the cashflow paid on
    ``nav_date`` itself, i.e. the value still at risk after that date's flow
    settles. That is the exposure the hedging engine uses.
    """

    model_config = ConfigDict(frozen=True)

    fund_name: str
    currency: str
    nav_date: dt.date
    cashflow_local: float
    nav_local: float
    nav_ex_local: float
    fx_rate: float
    nav_base: float
    nav_ex_base: float


class CurrencyAnalytics(BaseModel):
    model_config = ConfigDict(frozen=True)

    fund_name: str
    currency: str
    irr: float
    irr_base: float
    nav_schedule: list[NavPoint]


class FundAnalytics(BaseModel):
    model_config = ConfigDict(frozen=True)

    fund_name: str
    base_currency: str
    fund_irr: float
    currencies: list[CurrencyAnalytics]

    def currency(self, code: str) -> CurrencyAnalytics:
        for c in self.currencies:
            if c.currency == code:
                return c
        raise KeyError(code)


class HedgeTrade(BaseModel):
    """A recommended FX forward at 100% of NAV. Notional is in the exposure (local) currency."""

    model_config = ConfigDict(frozen=True)

    fund_name: str
    exposure_currency: str
    base_currency: str
    side: TradeSide  # side on the exposure currency
    notional_local: float = Field(ge=0)
    trade_date: dt.date
    maturity_date: dt.date
    reference_rate: float
    notional_base: float
    pricing_source: str = "projection_implied_spot"

    @model_validator(mode="after")
    def _dates(self) -> "HedgeTrade":
        if self.maturity_date <= self.trade_date:
            raise ValueError("maturity must be after trade date")
        return self
