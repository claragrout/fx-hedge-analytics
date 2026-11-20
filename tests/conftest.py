from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from fx_hedge.analytics import compute_fund_analytics
from fx_hedge.ingest import IngestionResult, load_cashflows
from fx_hedge.models import CashflowRecord, FundAnalytics

SAMPLE = Path(__file__).resolve().parents[1] / "data" / "CST_Case_Study_data.xlsx"


@pytest.fixture(scope="session")
def sample_result() -> IngestionResult:
    return load_cashflows(SAMPLE)


@pytest.fixture(scope="session")
def fund1(sample_result: IngestionResult) -> FundAnalytics:
    return compute_fund_analytics(sample_result.records, "Fund I")


def make_record(row_id: int = 1, **overrides) -> CashflowRecord:
    data = {
        "row_id": row_id,
        "fund_name": "Fund T",
        "cashflow_date": dt.date(2025, 9, 30),
        "cashflow_type": "Interest",
        "local_currency": "GBP",
        "amount_local": 100.0,
        "amount_base": 115.0,
        "base_currency": "EUR",
    }
    data.update(overrides)
    return CashflowRecord.model_validate(data)


def bond_records(
    fund: str = "Fund T",
    ccy: str = "GBP",
    base: str = "EUR",
    fx: float = 1.15,
    principal: float = 1_000_000.0,
    coupon: float = 25_000.0,
    quarters: int = 8,
    start: dt.date = dt.date(2025, 3, 31),
) -> list[CashflowRecord]:
    """A simple bullet loan: investment, quarterly coupons, coupon + principal at maturity."""
    from fx_hedge.hedging import add_months

    rows = [("Investment", start, -principal)]
    for q in range(1, quarters + 1):
        d = add_months(start, 3 * q)
        rows.append(("Interest", d, coupon))
    rows.append(("Principal Repayment", add_months(start, 3 * quarters), principal))
    rate = 1.0 if ccy == base else fx
    return [
        CashflowRecord(
            row_id=i + 1,
            fund_name=fund,
            cashflow_date=d,
            cashflow_type=t,
            local_currency=ccy,
            amount_local=a,
            amount_base=a * rate,
            base_currency=base,
        )
        for i, (t, d, a) in enumerate(rows)
    ]
