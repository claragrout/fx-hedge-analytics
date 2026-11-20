import csv
import datetime as dt

import pytest
from pydantic import ValidationError

from fx_hedge.ingest import SchemaError, load_cashflows, validate_rows
from fx_hedge.models import CashflowRecord, CashflowType, Severity

from .conftest import make_record

HEADER = ["ID", "Fund Name", "Date", "Cashflow Type", "Local Currency",
          "Cashflow Amount Local", "Cashflow Amount Base", "Base Currency"]


def raw(row_id, date="31/12/2025 00:00", typ="Interest", ccy="GBP", local="2500000", base="2864345",
        fund="Fund T", base_ccy="EUR"):
    return {"row_id": row_id, "fund_name": fund, "cashflow_date": date, "cashflow_type": typ,
            "local_currency": ccy, "amount_local": local, "amount_base": base, "base_currency": base_ccy}


# ---------------------------------------------------------------------------
# Row-level model
# ---------------------------------------------------------------------------


class TestCashflowRecord:
    def test_corrections_are_recorded_in_context(self):
        issues = []
        rec = CashflowRecord.model_validate(raw(17, ccy="GPB", local="2,500,000"), context={"issues": issues, "row_id": 17})
        assert rec.local_currency == "GBP"
        assert rec.amount_local == 2_500_000
        fields = {i.field for i in issues}
        assert fields == {"local_currency", "amount_local"}
        assert all(i.severity is Severity.CORRECTED and i.row_id == 17 for i in issues)

    def test_investment_must_be_negative(self):
        with pytest.raises(ValidationError, match="sign convention"):
            make_record(cashflow_type="Investment", amount_local=100.0, amount_base=115.0)

    def test_inflow_must_be_positive(self):
        with pytest.raises(ValidationError, match="sign convention"):
            make_record(cashflow_type="Interest", amount_local=-100.0, amount_base=-115.0)

    def test_local_base_sign_mismatch(self):
        with pytest.raises(ValidationError, match="inconsistent signs"):
            make_record(amount_local=100.0, amount_base=-115.0)

    def test_same_currency_amounts_must_match(self):
        with pytest.raises(ValidationError, match="identical"):
            make_record(local_currency="EUR", amount_local=100.0, amount_base=120.0)

    def test_zero_amount_rejected(self):
        with pytest.raises(ValidationError, match="zero"):
            make_record(amount_local=0.0, amount_base=0.0)

    def test_records_are_immutable(self):
        rec = make_record()
        with pytest.raises(ValidationError):
            rec.amount_local = 1.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Dataset-level validation
# ---------------------------------------------------------------------------


class TestValidateRows:
    def test_bad_row_is_quarantined_not_fatal(self):
        res = validate_rows([raw(1), raw(2, ccy="XYZ"), raw(3, date="garbage")])
        assert [r.row_id for r in res.records] == [1]
        rejected = res.issues_by_severity(Severity.REJECTED)
        assert {(i.row_id, i.field) for i in rejected} == {(2, "local_currency"), (3, "cashflow_date")}

    def test_duplicate_row_id_rejected(self):
        res = validate_rows([raw(1), raw(1, date="31/03/2026 00:00")])
        assert len(res.records) == 1
        assert any(i.field == "row_id" and i.severity is Severity.REJECTED for i in res.issues)

    def test_duplicate_business_key_warns(self):
        res = validate_rows([raw(1), raw(2)])
        assert len(res.records) == 2
        assert sum(1 for i in res.issues if "duplicate business key" in i.message) == 2

    def test_fx_rate_outlier_warns(self):
        res = validate_rows([raw(1), raw(2, date="31/03/2026 00:00"), raw(3, date="30/06/2026 00:00", base="3500000")])
        warns = [i for i in res.issues if i.field == "amount_base"]
        assert [i.row_id for i in warns] == [3]

    def test_base_currency_outlier_rejected(self):
        res = validate_rows([raw(1), raw(2, date="31/03/2026 00:00"), raw(3, date="30/06/2026 00:00", base_ccy="USD")])
        assert [r.row_id for r in res.records] == [1, 2]

    def test_missing_maturity_coupon_warns(self):
        rows = [raw(1, date="30/09/2025 00:00", typ="Investment", local="-100", base="-115")]
        rows += [raw(i + 2, date=d, local="2", base="2.3") for i, d in enumerate(["31/12/2025", "31/03/2026", "30/06/2026"])]
        rows.append(raw(9, date="30/09/2026", typ="Principal Repayment", local="100", base="115"))
        res = validate_rows(rows)
        assert any("no interest at maturity" in i.message for i in res.issues)


class TestFileLoading:
    def write_csv(self, path, header, rows):
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(header)
            w.writerows(rows)

    def test_dirty_csv(self, tmp_path):
        p = tmp_path / "cf.csv"
        self.write_csv(p, HEADER + ["Comment"], [
            [1, " Fund T ", "30/09/2025 00:00", "Investment", "gbp", "(100,000,000)", "-114,573,786", "EUR", "x"],
            [2, "Fund T", "2025-12-31", "interest", "GPB", "2,500,000", "2864345", "EUR", ""],
            ["", "", "", "", "", "", "", "", ""],
            [3, "Fund T", "31/03/2026 00:00", "Interest", "ZZZ", "2500000", "2864345", "EUR", ""],
            [4, "Fund T", "30/06/2026 00:00", "Principal Repayment", "GBP", "100000000", "114573786", "EUR", ""],
        ])
        res = load_cashflows(p)
        assert res.rows_read == 4  # blank line skipped
        assert [r.row_id for r in res.records] == [1, 2, 4]
        r1 = res.records[0]
        assert (r1.fund_name, r1.local_currency, r1.amount_local) == ("Fund T", "GBP", -100_000_000)
        assert res.records[1].cashflow_type is CashflowType.INTEREST
        assert res.records[1].cashflow_date == dt.date(2025, 12, 31)
        assert any(i.field == "Comment" and i.severity is Severity.WARNING for i in res.issues)
        assert res.rejected_row_ids == {3}

    def test_missing_column_is_schema_error(self, tmp_path):
        p = tmp_path / "cf.csv"
        self.write_csv(p, HEADER[:-1], [[1, "F", "30/09/2025", "Interest", "GBP", "1", "1"]])
        with pytest.raises(SchemaError, match="base_currency"):
            load_cashflows(p)

    def test_unsupported_extension(self, tmp_path):
        p = tmp_path / "cf.json"
        p.write_text("{}")
        with pytest.raises(SchemaError):
            load_cashflows(p)


# ---------------------------------------------------------------------------
# The provided sample
# ---------------------------------------------------------------------------


class TestSampleFile:
    def test_all_rows_survive(self, sample_result):
        assert sample_result.rows_read == 126
        assert len(sample_result.records) == 126
        assert not sample_result.issues_by_severity(Severity.REJECTED)

    def test_known_errors_corrected(self, sample_result):
        corrected = {(i.row_id, i.field) for i in sample_result.issues_by_severity(Severity.CORRECTED)}
        assert corrected == {(17, "local_currency"), (21, "cashflow_date"), (51, "cashflow_date")}
        by_id = {r.row_id: r for r in sample_result.records}
        assert by_id[17].local_currency == "GBP"
        assert by_id[21].cashflow_date == dt.date(2030, 9, 30)
        assert by_id[51].cashflow_date == dt.date(2027, 9, 30)

    def test_corrected_currency_confirmed_by_fx_rate(self, sample_result):
        # Row 17's implied rate matches the GBP position, so no FX warning is raised.
        assert not [i for i in sample_result.issues if i.field == "amount_base"]

    def test_positions(self, sample_result):
        keys = {r.position_key for r in sample_result.records}
        assert keys == {(f, c) for f in ("Fund I", "Fund II") for c in ("EUR", "GBP", "USD")}
        for key in keys:
            rows = [r for r in sample_result.records if r.position_key == key]
            assert len(rows) == 21

    def test_missing_final_coupon_flagged_for_every_position(self, sample_result):
        assert sum(1 for i in sample_result.issues if "no interest at maturity" in i.message) == 6
