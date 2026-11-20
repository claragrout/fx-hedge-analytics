"""Pydantic models: the validated cashflow record plus analytics/hedge outputs.

Validation strategy
-------------------
* ``CashflowRecord`` validators run in ``mode="before"`` and delegate to the
  pure parsers in :mod:`fx_hedge.cleaning`.
* Deterministic corrections (typo map, date normalisation, amount formatting)
  are *recorded* through the validation context (``context={"issues": [...]}``)
  so that nothing is fixed silently.
* Anything that cannot be fixed deterministically raises, producing a
  ``ValidationError`` that the ingestion layer turns into a REJECTED issue.
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

#: Base amounts in the sample are rounded to whole units, so a base-currency
#: position can differ from its local amount by < 1 unit.
SAME_CCY_TOLERANCE = 1.0


class DataIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    row_id: int | None
    field: str
    severity: Severity
    message: str
    raw_value: str | None = None


def _record(info: ValidationInfo, field: str, raw: Any, note: str | None) -> None:
    if note is None or not info.context:
        return
    issues = info.context.get("issues")
    if issues is None:
        return
    row_id = info.context.get("row_id")
    issues.append(
        DataIssue(
            row_id=row_id,
            field=field,
            severity=Severity.CORRECTED,
            message=note,
            raw_value=None if raw is None else str(raw),
        )
    )


class CashflowRecord(BaseModel):
    """One validated projected cashflow."""

    model_config = ConfigDict(frozen=True, str_strip_whitespace=False)

    row_id: int = Field(gt=0)
    fund_name: str
    cashflow_date: dt.date
    cashflow_type: CashflowType
    local_currency: str = Field(pattern=r"^[A-Z]{3}$")
    amount_local: float
    amount_base: float
    base_currency: str = Field(pattern=r"^[A-Z]{3}$")

    # -- field-level cleaning -------------------------------------------------

    @field_validator("row_id", mode="before")
    @classmethod
    def _row_id(cls, v: Any) -> int:
        if isinstance(v, bool):
            raise ValueError("row id must be an integer")
        if isinstance(v, float):
            if not v.is_integer():
                raise ValueError(f"row id must be an integer, got {v!r}")
            return int(v)
        if isinstance(v, str):
            s = v.strip()
            if not s.isdigit():
                raise ValueError(f"row id must be an integer, got {v!r}")
            return int(s)
        return v

    @field_validator("fund_name", mode="before")
    @classmethod
    def _fund_name(cls, v: Any, info: ValidationInfo) -> str:
        value, note = cleaning.clean_text(v, "fund name")
        _record(info, "fund_name", v, note)
        return value

    @field_validator("cashflow_date", mode="before")
    @classmethod
    def _date(cls, v: Any, info: ValidationInfo) -> dt.date:
        value, note = cleaning.parse_date(v)
        _record(info, "cashflow_date", v, note)
        return value

    @field_validator("cashflow_type", mode="before")
    @classmethod
    def _type(cls, v: Any, info: ValidationInfo) -> CashflowType:
        value, note = cleaning.parse_cashflow_type(v)
        _record(info, "cashflow_type", v, note)
        return value

    @field_validator("local_currency", "base_currency", mode="before")
    @classmethod
    def _ccy(cls, v: Any, info: ValidationInfo) -> str:
        value, note = cleaning.parse_currency(v)
        _record(info, info.field_name or "currency", v, note)
        return value

    @field_validator("amount_local", "amount_base", mode="before")
    @classmethod
    def _amount(cls, v: Any, info: ValidationInfo) -> float:
        value, note = cleaning.parse_amount(v)
        _record(info, info.field_name or "amount", v, note)
        return value

    # -- cross-field business rules -------------------------------------------

    @model_validator(mode="after")
    def _business_rules(self) -> "CashflowRecord":
        if self.amount_local == 0:
            raise ValueError("zero cashflow amount")
        expected_negative = self.cashflow_type is CashflowType.INVESTMENT
        if (self.amount_local < 0) != expected_negative:
            raise ValueError(
                f"sign convention violated: {self.cashflow_type.value} must be "
                f"{'negative' if expected_negative else 'positive'}, got {self.amount_local:,.2f}"
            )
        if (self.amount_base < 0) != (self.amount_local < 0) or self.amount_base == 0:
            raise ValueError("local and base amounts have inconsistent signs")
        if (
            self.local_currency == self.base_currency
            and abs(self.amount_local - self.amount_base) > SAME_CCY_TOLERANCE
        ):
            raise ValueError(
                "local and base currency are identical but amounts differ "
                f"({self.amount_local:,.2f} vs {self.amount_base:,.2f})"
            )
        return self

    # -- convenience ------------------------------------------------------------

    @property
    def implied_fx_rate(self) -> float:
        """Base units per one unit of local currency, as embedded in the projection."""
        return self.amount_base / self.amount_local

    @property
    def position_key(self) -> tuple[str, str]:
        """The sample has no deal id; one deal per (fund, currency) is assumed."""
        return (self.fund_name, self.local_currency)


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
    """A recommended FX forward. Notional is in the exposure (local) currency."""

    model_config = ConfigDict(frozen=True)

    trade_id: str
    fund_name: str
    exposure_currency: str
    base_currency: str
    side: TradeSide  # side on the exposure currency
    notional_local: float = Field(ge=0)
    trade_date: dt.date
    maturity_date: dt.date
    hedge_ratio: float
    reference_rate: float
    notional_base: float
    pricing_source: str = "projection_implied_spot"

    @model_validator(mode="after")
    def _dates(self) -> "HedgeTrade":
        if self.maturity_date <= self.trade_date:
            raise ValueError("maturity must be after trade date")
        return self
