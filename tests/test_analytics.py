import datetime as dt

import pytest

from fx_hedge.analytics import IRRError, compute_fund_analytics, nav_schedule, xirr

from .conftest import bond_records, make_record

D0 = dt.date(2025, 1, 1)


class TestXirr:
    @pytest.mark.parametrize("flows", [[(D0, 100.0)], [(D0, -100.0), (D0 + dt.timedelta(days=1), -1.0)], []])
    def test_invalid_flows_raise_irr_error(self, flows):
        with pytest.raises(IRRError):
            xirr(flows)


class TestNavSchedule:
    def test_roll_forward_identity(self):
        """NAV after t_i's cashflow, grown at the IRR, equals NAV(t_{i+1}): the NAV accretes at the IRR."""
        recs = bond_records()
        irr = xirr([(r.date, r.cashflow_amount_local) for r in recs])
        s = nav_schedule(recs, irr)
        for a, b in zip(s, s[1:]):
            nav_ex = a.nav_local - a.cashflow_local
            grown = nav_ex * (1 + irr) ** ((b.nav_date - a.nav_date).days / 365)  # Act/365F
            # abs tolerance covers nav_local being rounded to 2 dp
            assert grown == pytest.approx(b.nav_local, abs=0.05)

    def test_rejects_mixed_positions(self):
        with pytest.raises(ValueError, match="one fund and currency"):
            nav_schedule(bond_records(ccy="GBP") + bond_records(ccy="USD"), 0.1)

    def test_rejects_repeated_date(self):
        # make_record defaults to a GBP Interest flow on 30/09/2025, already a coupon date
        recs = bond_records() + [make_record(row_id=99)]
        with pytest.raises(ValueError, match="repeated cashflow date"):
            nav_schedule(recs, 0.1)

    def test_rejects_rate_that_is_not_the_irr(self):
        recs = bond_records()
        irr = xirr([(r.date, r.cashflow_amount_local) for r in recs])
        with pytest.raises(ValueError, match=r"NAV\(0\)"):
            nav_schedule(recs, irr + 0.01)


class TestFundI:
    EXPECTED_IRR = {"EUR": 0.0788700, "GBP": 0.0995066, "USD": 0.1205150}
    EXPECTED_FUND_IRR = 0.1004764
    TERMINAL = {"EUR": 150_000_000.0, "GBP": 100_000_000.0, "USD": 200_000_000.0}

    def test_currencies(self, fund1):
        assert fund1.base_currency == "EUR"
        assert [c.currency for c in fund1.currencies] == ["EUR", "GBP", "USD"]

    @pytest.mark.parametrize("ccy", ["EUR", "GBP", "USD"])
    def test_currency_irr_regression(self, fund1, ccy):
        assert fund1.currency(ccy).irr == pytest.approx(self.EXPECTED_IRR[ccy], abs=1e-6)

    def test_fund_irr_regression(self, fund1):
        assert fund1.fund_irr == pytest.approx(self.EXPECTED_FUND_IRR, abs=1e-6)

    @pytest.mark.parametrize("ccy", ["EUR", "GBP", "USD"])
    def test_nav_zero_at_start_and_exit_value_at_end(self, fund1, ccy):
        sched = fund1.currency(ccy).nav_schedule
        assert sched[0].nav_date == dt.date(2025, 9, 30)
        assert sched[0].nav_local == 0.0
        assert sched[-1].nav_date == dt.date(2030, 9, 30)
        assert sched[-1].nav_local == pytest.approx(self.TERMINAL[ccy], rel=1e-9)
        assert len(sched) == 21

    def test_fund_ii_same_yields_scaled(self, sample_result, fund1):
        f2 = compute_fund_analytics(sample_result.records, "Fund II")
        for c1, c2 in zip(fund1.currencies, f2.currencies):
            assert c2.irr == pytest.approx(c1.irr, abs=1e-9)
            assert c2.nav_schedule[5].nav_local == pytest.approx(c1.nav_schedule[5].nav_local * 1.4, rel=1e-9)

    def test_unknown_fund(self, sample_result):
        with pytest.raises(ValueError, match="no valid cashflows"):
            compute_fund_analytics(sample_result.records, "Fund X")
