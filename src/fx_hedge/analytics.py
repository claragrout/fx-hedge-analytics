from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Sequence

import pyxirr

from .models import CashflowRecord, CurrencyAnalytics, FundAnalytics, NavPoint

Flow = tuple[dt.date, float]


class IRRError(ValueError):
    pass


def xirr(flows: Sequence[Flow]) -> float:
    """
    """
    dates = [d for d, _ in flows]
    amounts = [a for _, a in flows]
    try:
        rate = pyxirr.xirr(dates, amounts)
    except pyxirr.InvalidPaymentsError as e:
        raise IRRError(str(e)) from e
    if rate is None:
        raise IRRError("IRR did not converge")
    return rate


def nav_at(flows: Sequence[Flow], rate: float, as_of: dt.date, include_same_day: bool = True) -> float:
    """"""
    remaining = [(d, a) for d, a in flows if (d >= as_of if include_same_day else d > as_of)]
    if not remaining:
        return 0.0
    # pyxirr.xnpv discounts to the earliest date; a zero flow pins that to ``as_of``.
    dates = [as_of] + [d for d, _ in remaining]
    amounts = [0.0] + [a for _, a in remaining]
    return pyxirr.xnpv(rate, dates, amounts)


def nav_schedule(
    records: Sequence[CashflowRecord],
    rate: float,
) -> list[NavPoint]:
    """"""
    if not records:
        return []
    
    keys = {(r.fund_name, r.local_currency) for r in records}
    if len(keys) != 1:
        raise ValueError(f"nav_schedule expects one fund and currency, got {sorted(keys)}")
    
    fund, ccy = next(iter(keys))
    flows = sorted((r.date, r.cashflow_amount_local) for r in records)

    if len({d for d, _ in flows}) != len(flows):
        raise ValueError(f"repeated cashflow date for {fund} / {ccy}")

    t0 = flows[0][0]

    points: list[NavPoint] = []
    for d, cf in flows:
        nav = round(nav_at(flows, rate, d, include_same_day=True), 2) + 0.0  # 2 dp; + 0.0 turns -0.0 into 0.0
        if d == t0 and nav != 0.0:
            raise ValueError(f"NAV(0) = {nav:,.2f}, expected 0: rate is not the IRR of these cashflows")

        points.append(
            NavPoint(
                nav_date=d,
                cashflow_local=cf,
                nav_local=nav,
            )
        )
    return points


def compute_fund_analytics(records: Sequence[CashflowRecord], fund_name: str) -> FundAnalytics:
    fund_records = [r for r in records if r.fund_name == fund_name]
    if not fund_records:
        raise ValueError(f"no valid cashflows for fund {fund_name!r}")
    bases = {r.base_currency for r in fund_records}
    if len(bases) != 1:
        raise ValueError(f"fund {fund_name!r} has multiple base currencies {sorted(bases)}")
    base = bases.pop()

    by_ccy: dict[str, list[CashflowRecord]] = defaultdict(list)
    for r in fund_records:
        by_ccy[r.local_currency].append(r)

    currencies: list[CurrencyAnalytics] = []
    for ccy in sorted(by_ccy):
        rows = by_ccy[ccy]
        irr = xirr([(r.date, r.cashflow_amount_local) for r in rows])
        currencies.append(
            CurrencyAnalytics(
                currency=ccy,
                irr=irr,
                nav_schedule=nav_schedule(rows, irr),
            )
        )

    fund_irr = xirr([(r.date, r.cashflow_amount_base) for r in fund_records])
    return FundAnalytics(fund_name=fund_name, base_currency=base, fund_irr=fund_irr, currencies=currencies)
