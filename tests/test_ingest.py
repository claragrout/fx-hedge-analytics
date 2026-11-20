import csv
import datetime as dt

import openpyxl
import pytest
from pydantic import ValidationError

from fx_hedge.ingest import (
    EXPECTED_HEADERS,
    SchemaError,
    _best_effort_row_id,
    header_to_field,
    load_cashflows,
    validate_rows,
)
from fx_hedge.models import CashflowRecord, CashflowType, Severity

from .conftest import make_record

HEADER = ["ID", "Fund Name", "Date", "Cashflow Type", "Local Currency",
          "Cashflow Amount Local", "Cashflow Amount Base", "Base Currency"]


def raw(row_id, date="31/12/2025 00:00", typ="Interest", ccy="GBP", local=2500000, base=2864345,
        fund="Fund T", base_ccy="EUR"):
    return {"id": row_id, "fund_name": fund, "date": date, "cashflow_type": typ,
            "local_currency": ccy, "cashflow_amount_local": local, "cashflow_amount_base": base,
            "base_currency": base_ccy}


def test_headers_map_to_model_fields():
    assert {header_to_field(h) for h in EXPECTED_HEADERS} == set(CashflowRecord.model_fields)


# ---------------------------------------------------------------------------
# Row-level model
# ---------------------------------------------------------------------------


class TestCashflowRecord:
    def test_corrections_are_recorded_in_context(self):
        issues = []
        rec = CashflowRecord.model_validate(
            raw(17, ccy="GPB", date="30/09/2027  00:00:00`"), context={"issues": issues, "row_id": 17}
        )
        assert rec.local_currency == "GBP"
        assert rec.date == dt.date(2027, 9, 30)
        assert {i.field for i in issues} == {"local_currency", "date"}
        assert all(i.severity is Severity.CORRECTED and i.row_id == 17 for i in issues)

    def test_numeric_strings_accepted(self):
        # CSV cells are always text
        rec = CashflowRecord.model_validate(raw("3", local="2500000", base="2864345"))
        assert (rec.id, rec.cashflow_amount_local) == (3, 2_500_000.0)

    @pytest.mark.parametrize("field, value", [
        ("cashflow_amount_local", "2,500,000"),
        ("cashflow_amount_local", "nan"),
        ("id", 0),
        ("id", 17.5),
        ("cashflow_type", "Fee"),
        ("cashflow_type", "interest"),
    ])
    def test_rejects_values_outside_the_data(self, field, value):
        with pytest.raises(ValidationError):
            CashflowRecord.model_validate({**raw(1), field: value})

    def test_investment_must_be_negative(self):
        with pytest.raises(ValidationError, match="sign convention"):
            make_record(cashflow_type="Investment", cashflow_amount_local=100.0, cashflow_amount_base=115.0)

    def test_inflow_must_be_positive(self):
        with pytest.raises(ValidationError, match="sign convention"):
            make_record(cashflow_type="Interest", cashflow_amount_local=-100.0, cashflow_amount_base=-115.0)

    def test_local_base_sign_mismatch(self):
        with pytest.raises(ValidationError, match="inconsistent signs"):
            make_record(cashflow_amount_local=100.0, cashflow_amount_base=-115.0)

    def test_same_currency_amounts_must_match_exactly(self):
        with pytest.raises(ValidationError, match="identical"):
            make_record(local_currency="EUR", cashflow_amount_local=100.0, cashflow_amount_base=100.5)

    def test_same_currency_equal_amounts_accepted(self):
        rec = make_record(local_currency="EUR", cashflow_amount_local=100.0, cashflow_amount_base=100.0)
        assert rec.cashflow_amount_base == 100.0

    def test_zero_local_amount_rejected(self):
        with pytest.raises(ValidationError, match="zero cashflow amount"):
            make_record(cashflow_amount_local=0.0, cashflow_amount_base=0.0)

    def test_zero_base_amount_rejected(self):
        with pytest.raises(ValidationError, match="zero base amount"):
            make_record(cashflow_amount_local=100.0, cashflow_amount_base=0.0)

    def test_records_are_immutable(self):
        rec = make_record()
        with pytest.raises(ValidationError):
            rec.cashflow_amount_local = 1.0  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Row id labelling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value, expected", [(17, 17), ("17", 17), (None, None), ("abc", None), (0, None), (17.5, None)])
def test_best_effort_row_id(value, expected):
    assert _best_effort_row_id(value) == expected


# ---------------------------------------------------------------------------
# validate_rows
# ---------------------------------------------------------------------------


class TestValidateRows:
    def test_bad_row_is_rejected_not_fatal(self):
        res = validate_rows([raw(1), raw(2, ccy="XYZ"), raw(3, date="garbage")])
        assert [r.id for r in res.records] == [1]
        rejected = res.issues_by_severity(Severity.REJECTED)
        assert {(i.row_id, i.field) for i in rejected} == {(2, "local_currency"), (3, "date")}

    def test_all_field_errors_of_a_row_are_reported(self):
        res = validate_rows([raw(1, ccy="XYZ", date="garbage")])
        assert {i.field for i in res.issues} == {"local_currency", "date"}

    def test_business_rule_error_is_reported_against_the_row(self):
        res = validate_rows([raw(1, local=-5, base=-5.7)])
        [issue] = res.issues
        assert issue.field == "__row__" and "sign convention" in issue.message

    def test_row_without_id_is_labelled_none(self):
        res = validate_rows([raw(None)])
        assert {i.row_id for i in res.issues} == {None}

    def test_counts_rows_from_a_generator(self):
        res = validate_rows(raw(i) for i in range(1, 4))
        assert res.rows_read == 3


# ---------------------------------------------------------------------------
# File loading
# ---------------------------------------------------------------------------


def write_csv(path, header, rows):
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


def write_xlsx(path, sheets):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    wb.save(path)


class TestFileLoading:
    def test_dirty_csv(self, tmp_path):
        p = tmp_path / "cf.csv"
        write_csv(p, HEADER, [
            [1, "Fund T", "30/09/2025 00:00", "Investment", "GBP", "-100000000", "-114573786", "EUR"],
            [2, "Fund T", " 31/12/2025  00:00:00`", "Interest", "GPB", "2500000", "2864345", "EUR"],
            [3, "Fund T", "31/03/2026 00:00", "Interest", "ZZZ", "2500000", "2864345", "EUR"],
            [4, "Fund T", "30/06/2026 00:00", "Principal Repayment", "GBP", "100000000", "114573786", "EUR"],
        ])
        res = load_cashflows(p)
        assert res.rows_read == 4
        assert [r.id for r in res.records] == [1, 2, 4]
        assert res.records[1].local_currency == "GBP"
        assert res.records[1].date == dt.date(2025, 12, 31)
        assert res.records[1].cashflow_type is CashflowType.INTEREST
        assert {i.row_id for i in res.issues_by_severity(Severity.REJECTED)} == {3}
        assert {(i.row_id, i.field) for i in res.issues_by_severity(Severity.CORRECTED)} == {
            (2, "date"), (2, "local_currency")}

    def test_missing_column(self, tmp_path):
        p = tmp_path / "cf.csv"
        write_csv(p, HEADER[:-1], [[1, "F", "30/09/2025 00:00", "Interest", "GBP", "1", "1"]])
        with pytest.raises(SchemaError, match="missing columns.*Base Currency"):
            load_cashflows(p)

    def test_unexpected_column(self, tmp_path):
        p = tmp_path / "cf.csv"
        write_csv(p, HEADER + ["Comment"], [])
        with pytest.raises(SchemaError, match="unexpected columns.*Comment"):
            load_cashflows(p)

    def test_renamed_column_rejected(self, tmp_path):
        p = tmp_path / "cf.csv"
        write_csv(p, ["Fund" if h == "Fund Name" else h for h in HEADER], [])
        with pytest.raises(SchemaError):
            load_cashflows(p)

    def test_duplicate_column(self, tmp_path):
        p = tmp_path / "cf.csv"
        write_csv(p, HEADER + ["Date"], [])
        with pytest.raises(SchemaError, match="duplicate"):
            load_cashflows(p)

    def test_empty_csv(self, tmp_path):
        p = tmp_path / "cf.csv"
        p.write_text("")
        with pytest.raises(SchemaError, match="empty"):
            load_cashflows(p)

    def test_xlsx_single_sheet(self, tmp_path):
        p = tmp_path / "cf.xlsx"
        write_xlsx(p, {"Data": [HEADER, [1, "Fund T", "31/12/2025 00:00", "Interest", "GBP", 2500000, 2864345, "EUR"]]})
        res = load_cashflows(p)
        assert [r.id for r in res.records] == [1]

    def test_xlsx_multiple_sheets_rejected(self, tmp_path):
        p = tmp_path / "cf.xlsx"
        write_xlsx(p, {"Fund I": [HEADER], "Fund II": [HEADER]})
        with pytest.raises(SchemaError, match="exactly one worksheet.*Fund I.*Fund II"):
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
    def test_all_rows_valid(self, sample_result):
        assert sample_result.rows_read == 126
        assert len(sample_result.records) == 126
        assert not sample_result.issues_by_severity(Severity.REJECTED)

    def test_exactly_the_known_errors_corrected(self, sample_result):
        corrected = {(i.row_id, i.field) for i in sample_result.issues_by_severity(Severity.CORRECTED)}
        assert corrected == {(17, "local_currency"), (21, "date"), (51, "date")}
        assert len(sample_result.issues) == 3
        by_id = {r.id: r for r in sample_result.records}
        assert by_id[17].local_currency == "GBP"
        assert by_id[21].date == dt.date(2030, 9, 30)
        assert by_id[51].date == dt.date(2027, 9, 30)

    def test_funds_and_currencies(self, sample_result):
        groups = {(r.fund_name, r.local_currency) for r in sample_result.records}
        assert groups == {(f, c) for f in ("Fund I", "Fund II") for c in ("EUR", "GBP", "USD")}
        for key in groups:
            rows = [r for r in sample_result.records if (r.fund_name, r.local_currency) == key]
            assert len(rows) == 21
