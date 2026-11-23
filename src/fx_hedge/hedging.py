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
            exposure = point.nav_local - point.cashflow_local
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
                )
            )
    return sorted(trades, key=lambda t: (t.exposure_currency, t.trade_date))
