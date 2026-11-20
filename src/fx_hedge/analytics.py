"""IRR and NAV analytics.

Conventions
-----------
* Time is measured in Act/365F years from a reference date, i.e. Excel
  ``XIRR``/``XNPV`` semantics. IRRs are annual effective rates.
* **Currency-level IRR**: computed on *local-currency* cashflows of the
  position (the asset's yield in its own currency, unaffected by FX).
* **Fund-level IRR**: computed on the fund's aggregate *base-currency*
  cashflows (all positions summed by date).
* **NAV(t)** (per specification): PV at the position IRR of all cashflows
  dated on or after ``t``. With this "cum-flow" definition NAV(t0) is 0 (the
  investment offsets the PV of the inflows by construction of the IRR), and
  NAV(T) equals the terminal cashflow.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Sequence

from .models import CashflowRecord, CurrencyAnalytics, FundAnalytics, NavPoint

Flow = tuple[dt.date, float]

DAYS_PER_YEAR = 365.0


class IRRError(ValueError):
    pass


def year_fraction(start: dt.date, end: dt.date) -> float:
    return (end - start).days / DAYS_PER_YEAR


def aggregate_by_date(flows: Sequence[Flow]) -> list[Flow]:
    totals: dict[dt.date, float] = defaultdict(float)
    for d, a in flows:
        totals[d] += a
    return sorted(totals.items())


def xnpv(rate: float, flows: Sequence[Flow], as_of: dt.date | None = None) -> float:
    """PV at ``as_of`` (default: first flow date) of ``flows`` at annual ``rate``."""
    if rate <= -1.0:
        raise IRRError("rate must be > -100%")
    if not flows:
        return 0.0
    t0 = as_of if as_of is not None else min(d for d, _ in flows)
    return sum(a / (1.0 + rate) ** year_fraction(t0, d) for d, a in flows)


def _dxnpv(rate: float, flows: Sequence[Flow], t0: dt.date) -> float:
    total = 0.0
    for d, a in flows:
        t = year_fraction(t0, d)
        total -= t * a / (1.0 + rate) ** (t + 1.0)
    return total


def xirr(flows: Sequence[Flow], guess: float = 0.1, tol: float = 1e-12, max_iter: int = 100) -> float:
    """Annual IRR of dated cashflows (Newton–Raphson with bracketing fallback)."""
    flows = aggregate_by_date(flows)
    if not any(a < 0 for _, a in flows) or not any(a > 0 for _, a in flows):
        raise IRRError("IRR requires at least one negative and one positive cashflow")
    t0 = flows[0][0]

    rate = guess
    for _ in range(max_iter):
        f = xnpv(rate, flows, t0)
        df = _dxnpv(rate, flows, t0)
        if df == 0:
            break
        step = f / df
        new_rate = rate - step
        if new_rate <= -0.999999:
            break
        if abs(new_rate - rate) < tol:
            return new_rate
        rate = new_rate

    # Fallback: bisection on a wide bracket.
    lo, hi = -0.99, 10.0
    f_lo, f_hi = xnpv(lo, flows, t0), xnpv(hi, flows, t0)
    if f_lo * f_hi > 0:
        raise IRRError("IRR did not converge and no sign change found in [-99%, 1000%]")
    for _ in range(500):
        mid = (lo + hi) / 2
        f_mid = xnpv(mid, flows, t0)
        if abs(f_mid) < 1e-9 or (hi - lo) / 2 < tol:
            return mid
        if (f_mid < 0) == (f_lo < 0):
            lo, f_lo = mid, f_mid
        else:
            hi = mid
    return (lo + hi) / 2


def nav_at(flows: Sequence[Flow], rate: float, as_of: dt.date, include_same_day: bool = True) -> float:
    """PV at ``as_of`` of the remaining cashflows, discounted at ``rate``."""
    remaining = [(d, a) for d, a in flows if (d >= as_of if include_same_day else d > as_of)]
    return xnpv(rate, remaining, as_of)


def position_fx_rate(records: Sequence[CashflowRecord]) -> float:
    """Projection-implied FX rate (base per local unit) for a position.

    Base amounts are rounded to whole units, so small coupons carry rounding
    noise (2,864,345 / 2,500,000 = 1.145738 vs the deal rate 1.14573786).
    The rate is taken from the largest flow, which has the least relative
    rounding error. (Dataset validation has already checked that all rows
    agree within tolerance.)
    """
    largest = max(records, key=lambda r: abs(r.cashflow_amount_local))
    return largest.implied_fx_rate


def nav_schedule(
    records: Sequence[CashflowRecord],
    rate: float | None = None,
    dates: Sequence[dt.date] | None = None,
) -> list[NavPoint]:
    """NAV schedule for one fund's cashflows in one currency.

    ``dates`` defaults to every cashflow date of the position. ``NAV(t0)`` is
    set to exactly 0 per the specification. The computed value is asserted
    to be ~0, which double-checks the IRR.
    """
    if not records:
        return []
    keys = {(r.fund_name, r.local_currency) for r in records}
    if len(keys) != 1:
        raise ValueError(f"nav_schedule expects one fund and currency, got {sorted(keys)}")
    fund, ccy = next(iter(keys))

    flows = aggregate_by_date([(r.date, r.cashflow_amount_local) for r in records])
    if rate is None:
        rate = xirr(flows)
    fx = position_fx_rate(records)

    flow_on = dict(flows)
    t0 = flows[0][0]
    schedule_dates = sorted(set(dates) if dates is not None else flow_on)
    points: list[NavPoint] = []
    for d in schedule_dates:
        nav = nav_at(flows, rate, d, include_same_day=True)
        if d == t0:
            scale = max(abs(a) for _, a in flows)
            if abs(nav) > 1e-6 * scale:
                raise IRRError(f"NAV(0) = {nav:,.2f} is not ~0; IRR is inconsistent")
            nav = 0.0
        cf = flow_on.get(d, 0.0)
        nav_ex = nav - cf
        points.append(
            NavPoint(
                fund_name=fund,
                currency=ccy,
                nav_date=d,
                cashflow_local=cf,
                nav_local=nav,
                nav_ex_local=nav_ex,
                fx_rate=fx,
                nav_base=nav * fx,
                nav_ex_base=nav_ex * fx,
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
        local_flows = [(r.date, r.cashflow_amount_local) for r in rows]
        base_flows = [(r.date, r.cashflow_amount_base) for r in rows]
        irr = xirr(local_flows)
        currencies.append(
            CurrencyAnalytics(
                fund_name=fund_name,
                currency=ccy,
                irr=irr,
                irr_base=xirr(base_flows),
                nav_schedule=nav_schedule(rows, irr),
            )
        )

    fund_irr = xirr([(r.date, r.cashflow_amount_base) for r in fund_records])
    return FundAnalytics(fund_name=fund_name, base_currency=base, fund_irr=fund_irr, currencies=currencies)


def fund_nav_base_total(analytics: FundAnalytics) -> list[tuple[dt.date, float, float]]:
    """Aggregate base-currency NAV across currencies: (date, nav_base, nav_ex_base)."""
    totals: dict[dt.date, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for c in analytics.currencies:
        for p in c.nav_schedule:
            totals[p.nav_date][0] += p.nav_base
            totals[p.nav_date][1] += p.nav_ex_base
    return [(d, v[0], v[1]) for d, v in sorted(totals.items())]
