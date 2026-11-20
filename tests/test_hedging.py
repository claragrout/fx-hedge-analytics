import datetime as dt

import pytest

from fx_hedge.analytics import compute_fund_analytics
from fx_hedge.hedging import add_months, generate_hedges, roll_schedule
from fx_hedge.models import CashflowRecord, TradeSide

from .conftest import bond_records


@pytest.mark.parametrize(
    "d, n, expected",
    [
        (dt.date(2025, 9, 30), 3, dt.date(2025, 12, 31)),
        (dt.date(2025, 12, 31), 3, dt.date(2026, 3, 31)),
        (dt.date(2026, 3, 31), 3, dt.date(2026, 6, 30)),
        (dt.date(2025, 11, 30), 3, dt.date(2026, 2, 28)),
        (dt.date(2025, 1, 15), 3, dt.date(2025, 4, 15)),
        (dt.date(2024, 1, 31), 1, dt.date(2024, 2, 29)),
    ],
)
def test_add_months(d, n, expected):
    assert add_months(d, n) == expected


def test_roll_schedule_contiguous():
    windows = roll_schedule(dt.date(2025, 9, 30), dt.date(2026, 9, 30))
    assert windows[0] == (dt.date(2025, 9, 30), dt.date(2025, 12, 31))
    assert windows[-1][1] == dt.date(2026, 9, 30)
    assert all(a[1] == b[0] for a, b in zip(windows, windows[1:]))


def test_roll_schedule_stub_capped_at_end():
    assert roll_schedule(dt.date(2025, 9, 30), dt.date(2025, 11, 15))[-1] == (dt.date(2025, 9, 30), dt.date(2025, 11, 15))


@pytest.fixture(scope="module")
def hedges(sample_result, fund1):
    return generate_hedges(sample_result.records, fund1)


class TestFundIHedges:
    def test_only_non_base_currencies(self, hedges):
        assert {h.exposure_currency for h in hedges} == {"GBP", "USD"}
        assert all(h.base_currency == "EUR" for h in hedges)

    def test_quarterly_rolls_cover_life(self, hedges):
        for ccy in ("GBP", "USD"):
            hs = [h for h in hedges if h.exposure_currency == ccy]
            assert len(hs) == 20
            assert hs[0].trade_date == dt.date(2025, 9, 30)
            assert hs[-1].maturity_date == dt.date(2030, 9, 30)
            assert all(a.maturity_date == b.trade_date for a, b in zip(hs, hs[1:]))

    def test_notional_is_100pct_of_nav_ex_flow(self, hedges, fund1):
        for ccy in ("GBP", "USD"):
            nav = {p.nav_date: p.nav_ex_local for p in fund1.currency(ccy).nav_schedule}
            for h in (h for h in hedges if h.exposure_currency == ccy):
                assert h.notional_local == pytest.approx(nav[h.trade_date], abs=0.01)
                assert h.side is TradeSide.SELL

    def test_first_hedge_equals_investment(self, hedges):
        first = {h.exposure_currency: h for h in hedges if h.trade_date == dt.date(2025, 9, 30)}
        assert first["GBP"].notional_local == pytest.approx(100_000_000, abs=0.01)
        assert first["USD"].notional_local == pytest.approx(200_000_000, abs=0.01)
        assert first["GBP"].notional_base == pytest.approx(114_573_786, abs=1)

    def test_trade_ids_deterministic_and_unique(self, sample_result, fund1, hedges):
        again = generate_hedges(sample_result.records, fund1)
        assert [h.trade_id for h in hedges] == [h.trade_id for h in again]
        assert len({h.trade_id for h in hedges}) == len(hedges)

    def test_new_projection_version_new_ids(self, sample_result, fund1, hedges):
        v2 = generate_hedges(sample_result.records, fund1, projection_version="v2")
        assert not {h.trade_id for h in hedges} & {h.trade_id for h in v2}


def test_base_only_fund_needs_no_hedges():
    recs = bond_records(ccy="EUR")
    assert generate_hedges(recs, compute_fund_analytics(recs, "Fund T")) == []


def test_net_short_exposure_buys_local():
    # A position whose remaining value is negative (e.g. a future funding
    # commitment exceeding inflows) is hedged by BUYING the local currency forward.
    base = dt.date(2025, 3, 31)
    rows = [
        ("Investment", base, -100.0),
        ("Interest", add_months(base, 3), 200.0),
        ("Investment", add_months(base, 6), -60.0),
    ]
    recs = [
        CashflowRecord(id=i + 1, fund_name="Fund T", date=d.strftime("%d/%m/%Y 00:00"), cashflow_type=t,
                       local_currency="USD", cashflow_amount_local=a, cashflow_amount_base=a * 0.9,
                       base_currency="EUR")
        for i, (t, d, a) in enumerate(rows)
    ]
    fa = compute_fund_analytics(recs, "Fund T")
    hedges = generate_hedges(recs, fa)
    assert hedges[-1].side is TradeSide.BUY
