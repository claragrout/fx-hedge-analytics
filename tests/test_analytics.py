import datetime as dt

import pytest

from fx_hedge.analytics import (
    IRRError,
    compute_fund_analytics,
    fund_nav_base_total,
    nav_at,
    nav_schedule,
    xirr,
    xnpv,
    year_fraction,
)

from .conftest import bond_records

D0 = dt.date(2025, 1, 1)


class TestXirr:
    def test_one_year_ten_percent(self):
        flows = [(D0, -100.0), (D0 + dt.timedelta(days=365), 110.0)]
        assert xirr(flows) == pytest.approx(0.10, abs=1e-12)

    def test_two_years(self):
        flows = [(D0, -100.0), (D0 + dt.timedelta(days=730), 121.0)]
        assert xirr(flows) == pytest.approx(0.10, abs=1e-12)

    def test_negative_irr(self):
        flows = [(D0, -100.0), (D0 + dt.timedelta(days=365), 80.0)]
        assert xirr(flows) == pytest.approx(-0.20, abs=1e-12)

    def test_matches_excel_xirr(self):
        # Excel: =XIRR({-10000,2750,4250,3250,2750},{1/1/2008,1/3/2008,30/10/2008,15/2/2009,1/4/2009}) = 0.373362535
        flows = [
            (dt.date(2008, 1, 1), -10000.0),
            (dt.date(2008, 3, 1), 2750.0),
            (dt.date(2008, 10, 30), 4250.0),
            (dt.date(2009, 2, 15), 3250.0),
            (dt.date(2009, 4, 1), 2750.0),
        ]
        assert xirr(flows) == pytest.approx(0.373362535, abs=1e-8)

    def test_same_day_flows_are_netted(self):
        flows = [(D0, -60.0), (D0, -40.0), (D0 + dt.timedelta(days=365), 110.0)]
        assert xirr(flows) == pytest.approx(0.10)

    def test_order_independent(self):
        flows = [(D0 + dt.timedelta(days=365), 110.0), (D0, -100.0)]
        assert xirr(flows) == pytest.approx(0.10)

    @pytest.mark.parametrize("flows", [[(D0, 100.0)], [(D0, -100.0), (D0 + dt.timedelta(days=1), -1.0)], []])
    def test_requires_sign_change(self, flows):
        with pytest.raises(IRRError):
            xirr(flows)

    def test_npv_at_irr_is_zero(self):
        recs = bond_records()
        flows = [(r.date, r.cashflow_amount_local) for r in recs]
        assert xnpv(xirr(flows), flows) == pytest.approx(0.0, abs=1e-6)


class TestNavSchedule:
    def test_spec_sanity_checks(self):
        recs = bond_records()
        sched = nav_schedule(recs)
        assert sched[0].nav_local == 0.0  # NAV(0) = 0
        # terminal: final coupon + principal
        assert sched[-1].nav_local == pytest.approx(1_025_000.0)
        assert sched[-1].nav_ex_local == pytest.approx(0.0, abs=1e-6)
        assert sched[0].nav_ex_local == pytest.approx(1_000_000.0, rel=1e-9)

    def test_roll_forward_identity(self):
        """NAV_ex(t_i) grown at the IRR equals NAV(t_{i+1}): the NAV accretes at the IRR."""
        recs = bond_records()
        irr = xirr([(r.date, r.cashflow_amount_local) for r in recs])
        s = nav_schedule(recs, irr)
        for a, b in zip(s, s[1:]):
            grown = a.nav_ex_local * (1 + irr) ** year_fraction(a.nav_date, b.nav_date)
            assert grown == pytest.approx(b.nav_local, rel=1e-10)

    def test_base_conversion(self):
        s = nav_schedule(bond_records(fx=1.2))
        assert all(p.nav_base == pytest.approx(p.nav_local * 1.2) for p in s)

    def test_custom_dates_between_cashflows(self):
        recs = bond_records()
        mid = dt.date(2025, 5, 15)
        s = nav_schedule(recs, dates=[mid])
        flows = [(r.date, r.cashflow_amount_local) for r in recs]
        assert s[0].nav_local == pytest.approx(nav_at(flows, xirr(flows), mid))
        assert s[0].cashflow_local == 0.0

    def test_rejects_mixed_positions(self):
        with pytest.raises(ValueError, match="one fund and currency"):
            nav_schedule(bond_records(ccy="GBP") + bond_records(ccy="USD"))


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

    def test_fund_irr_is_blend(self, fund1):
        irrs = [c.irr for c in fund1.currencies]
        assert min(irrs) < fund1.fund_irr < max(irrs)

    @pytest.mark.parametrize("ccy", ["EUR", "GBP", "USD"])
    def test_nav_zero_at_start_and_exit_value_at_end(self, fund1, ccy):
        sched = fund1.currency(ccy).nav_schedule
        assert sched[0].nav_date == dt.date(2025, 9, 30)
        assert sched[0].nav_local == 0.0
        assert sched[-1].nav_date == dt.date(2030, 9, 30)
        assert sched[-1].nav_local == pytest.approx(self.TERMINAL[ccy], rel=1e-9)
        assert len(sched) == 21

    def test_base_terminal_matches_source_base_amount(self, fund1):
        # GBP principal row: 114,573,786 EUR in the source file
        assert fund1.currency("GBP").nav_schedule[-1].nav_base == pytest.approx(114_573_786, abs=0.5)

    def test_constant_fx_means_base_irr_equals_local(self, fund1):
        for c in fund1.currencies:
            assert c.irr_base == pytest.approx(c.irr, abs=1e-6)

    def test_fund_total_nav(self, fund1):
        totals = fund_nav_base_total(fund1)
        assert totals[0][1] == 0.0
        assert totals[-1][1] == pytest.approx(150_000_000 + 114_573_786 + 170_212_766, abs=2)

    def test_fund_ii_same_yields_scaled(self, sample_result, fund1):
        f2 = compute_fund_analytics(sample_result.records, "Fund II")
        for c1, c2 in zip(fund1.currencies, f2.currencies):
            assert c2.irr == pytest.approx(c1.irr, abs=1e-9)
            assert c2.nav_schedule[5].nav_local == pytest.approx(c1.nav_schedule[5].nav_local * 1.4, rel=1e-9)

    def test_unknown_fund(self, sample_result):
        with pytest.raises(ValueError, match="no valid cashflows"):
            compute_fund_analytics(sample_result.records, "Fund X")
