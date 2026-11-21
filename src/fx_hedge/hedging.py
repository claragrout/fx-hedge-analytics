"""FX hedge recommendations: 3-month rolling forwards at 100% of NAV.

For each non-base currency position the fund is long the local-currency NAV,
so the hedge SELLS local / BUYS base forward.

The rolls follow the position's cashflow dates, which in the data are
consecutive quarter-ends (3 months apart). At each cashflow date ``t`` a
forward is struck for ``NAV_ex(t)``: the value still outstanding after the
cashflow paid on ``t``, i.e. the exposure carried over the next quarter. The
forward matures at the next cashflow date, where it is settled and replaced
(the "roll").

No market data is supplied, so the reference rate is the spot rate implied by
the projection. In production this would be the forward rate from a pricing
service.
"""

from __future__ import annotations

from .models import FundAnalytics, HedgeTrade, TradeSide


def generate_hedges(analytics: FundAnalytics) -> list[HedgeTrade]:
    base = analytics.base_currency
    trades: list[HedgeTrade] = []
    for ca in analytics.currencies:
        if ca.currency == base:
            continue
        schedule = ca.nav_schedule
        # Each consecutive pair of cashflow dates is one 3-month roll.
        for point, next_point in zip(schedule, schedule[1:]):
            exposure = point.nav_ex_local
            notional = abs(exposure)
            if notional < 0.01:
                continue
            side = TradeSide.SELL if exposure > 0 else TradeSide.BUY
            trades.append(
                HedgeTrade(
                    fund_name=analytics.fund_name,
                    exposure_currency=ca.currency,
                    base_currency=base,
                    side=side,
                    notional_local=round(notional, 2),
                    trade_date=point.nav_date,
                    maturity_date=next_point.nav_date,
                    reference_rate=point.fx_rate,
                    notional_base=round(notional * point.fx_rate, 2),
                )
            )
    return sorted(trades, key=lambda t: (t.exposure_currency, t.trade_date))
