"""FX hedge recommendations: 3-month rolling forwards at a % of NAV.

For each non-base currency position the fund is long the local-currency NAV,
so the hedge SELLS local / BUYS base forward. At each roll date ``t`` a new
forward is struck for ``hedge_ratio x NAV_ex(t)``. That is the value still
outstanding after any cashflow paid on ``t``, which is the exposure carried
over the next quarter. The forward matures at the next roll date, where it is
settled and replaced (the "roll").

No market data is supplied, so the reference rate is the spot rate implied by
the projection. In production this would be the forward rate from a pricing
service.
"""

from __future__ import annotations

import calendar
import datetime as dt
import hashlib
from typing import Sequence

from .analytics import nav_at, xirr
from .models import CashflowRecord, FundAnalytics, HedgeTrade, TradeSide


def add_months(d: dt.date, months: int) -> dt.date:
    """Add calendar months, keeping month-end dates on month-end (quarter-end rolls)."""
    is_eom = d.day == calendar.monthrange(d.year, d.month)[1]
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    last = calendar.monthrange(y, m)[1]
    return dt.date(y, m, last if is_eom else min(d.day, last))


def roll_schedule(start: dt.date, end: dt.date, tenor_months: int = 3) -> list[tuple[dt.date, dt.date]]:
    """(trade_date, maturity_date) windows covering [start, end)."""
    if end <= start:
        return []
    windows = []
    t = start
    while t < end:
        nxt = min(add_months(t, tenor_months), end)
        windows.append((t, nxt))
        t = nxt
    return windows


def trade_id(fund: str, ccy: str, trade_date: dt.date, maturity: dt.date, version: str) -> str:
    """Deterministic id: re-running the same projection version yields the same ids (idempotent upserts)."""
    key = f"{fund}|{ccy}|{trade_date.isoformat()}|{maturity.isoformat()}|{version}"
    return "FWD-" + hashlib.sha256(key.encode()).hexdigest()[:16].upper()


def generate_hedges(
    records: Sequence[CashflowRecord],
    analytics: FundAnalytics,
    hedge_ratio: float = 1.0,
    tenor_months: int = 3,
    projection_version: str = "v1",
) -> list[HedgeTrade]:
    if not 0.0 <= hedge_ratio <= 1.5:
        raise ValueError("hedge_ratio outside sane bounds [0, 1.5]")
    base = analytics.base_currency
    trades: list[HedgeTrade] = []
    for ca in analytics.currencies:
        if ca.currency == base:
            continue
        rows = [r for r in records if r.fund_name == analytics.fund_name and r.local_currency == ca.currency]
        flows = [(r.cashflow_date, r.amount_local) for r in rows]
        rate = ca.irr if ca.irr is not None else xirr(flows)
        fx = ca.nav_schedule[0].fx_rate
        start, end = min(d for d, _ in flows), max(d for d, _ in flows)
        for trade_date, maturity in roll_schedule(start, end, tenor_months):
            exposure = nav_at(flows, rate, trade_date, include_same_day=False)
            notional = abs(exposure) * hedge_ratio
            if notional < 0.01:
                continue
            side = TradeSide.SELL if exposure > 0 else TradeSide.BUY
            trades.append(
                HedgeTrade(
                    trade_id=trade_id(analytics.fund_name, ca.currency, trade_date, maturity, projection_version),
                    fund_name=analytics.fund_name,
                    exposure_currency=ca.currency,
                    base_currency=base,
                    side=side,
                    notional_local=round(notional, 2),
                    trade_date=trade_date,
                    maturity_date=maturity,
                    hedge_ratio=hedge_ratio,
                    reference_rate=fx,
                    notional_base=round(notional * fx, 2),
                )
            )
    return sorted(trades, key=lambda t: (t.exposure_currency, t.trade_date))
