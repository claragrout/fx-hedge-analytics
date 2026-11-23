import datetime as dt

import pytest

from fx_hedge.analytics import compute_fund_analytics
from fx_hedge.hedging import generate_hedges
from fx_hedge.models import CashflowRecord, TradeSide

from .conftest import QUARTER_ENDS, bond_records


@pytest.fixture(scope="module")
def hedges(fund1):
    return generate_hedges(fund1)


class TestFundIHedges:
    def test_only_non_base_currencies(self, hedges):
        assert {h.exposure_currency for h in hedges} == {"GBP", "USD"}
        assert all(h.base_currency == "EUR" for h in hedges)

    def test_rolls_follow_cashflow_dates(self, hedges, fund1):
        for ccy in ("GBP", "USD"):
            hs = [h for h in hedges if h.exposure_currency == ccy]
            dates = [p.nav_date for p in fund1.currency(ccy).nav_schedule]
            assert len(hs) == 20
            assert [(h.trade_date, h.maturity_date) for h in hs] == list(zip(dates, dates[1:]))

    def test_notional_is_100pct_of_nav_ex_flow(self, hedges, fund1):
        for ccy in ("GBP", "USD"):
            nav = {p.nav_date: p.nav_local - p.cashflow_local for p in fund1.currency(ccy).nav_schedule}
            for h in (h for h in hedges if h.exposure_currency == ccy):
                assert h.notional_local == pytest.approx(nav[h.trade_date], abs=0.01)
                assert h.side is TradeSide.SELL

    def test_first_hedge_equals_investment(self, hedges):
        first = {h.exposure_currency: h for h in hedges if h.trade_date == dt.date(2025, 9, 30)}
        assert first["GBP"].notional_local == pytest.approx(100_000_000, abs=0.01)
        assert first["USD"].notional_local == pytest.approx(200_000_000, abs=0.01)


def test_base_only_fund_needs_no_hedges():
    recs = bond_records(ccy="EUR")
    assert generate_hedges(compute_fund_analytics(recs, "Fund T")) == []


def test_net_short_exposure_buys_local():
    # After the second date the only remaining flow is an outflow, so the
    # remaining value is negative and the hedge BUYS the local currency.
    rows = [
        ("Investment", QUARTER_ENDS[0], -100.0),
        ("Interest", QUARTER_ENDS[1], 200.0),
        ("Investment", QUARTER_ENDS[2], -60.0),
    ]
    recs = [
        CashflowRecord(id=i + 1, fund_name="Fund T", date=d.strftime("%d/%m/%Y 00:00"), cashflow_type=t,
                       local_currency="USD", cashflow_amount_local=a, cashflow_amount_base=a * 0.9,
                       base_currency="EUR")
        for i, (t, d, a) in enumerate(rows)
    ]
    hedges = generate_hedges(compute_fund_analytics(recs, "Fund T"))
    assert hedges[-1].side is TradeSide.BUY
