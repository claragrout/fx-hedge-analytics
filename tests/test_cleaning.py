import datetime as dt

import pytest

from fx_hedge.cleaning import parse_amount, parse_cashflow_type, parse_currency, parse_date
from fx_hedge.models_enums import CashflowType


class TestParseDate:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("30/09/2025 00:00", dt.date(2025, 9, 30)),
            (" 30/09/2030  00:00:00", dt.date(2030, 9, 30)),  # sample row 21
            ("30/09/2027  00:00:00`", dt.date(2027, 9, 30)),  # sample row 51
            ("2025-12-31", dt.date(2025, 12, 31)),
            ("31.03.2026", dt.date(2026, 3, 31)),
            ("31-Mar-2026", dt.date(2026, 3, 31)),
            ("01/02/2026", dt.date(2026, 2, 1)),  # day-first
            (dt.datetime(2026, 6, 30), dt.date(2026, 6, 30)),
            (dt.date(2026, 6, 30), dt.date(2026, 6, 30)),
        ],
    )
    def test_accepts(self, raw, expected):
        assert parse_date(raw)[0] == expected

    def test_canonical_has_no_note(self):
        assert parse_date("30/09/2025 00:00")[1] is None

    def test_dirty_has_note(self):
        assert "normalised" in parse_date("30/09/2027  00:00:00`")[1]

    @pytest.mark.parametrize("raw", [None, "", "   ", "31/02/2026", "13/13/2026", "not a date", "30/09/2025 14:30", "30/09/1850"])
    def test_rejects(self, raw):
        with pytest.raises(ValueError):
            parse_date(raw)


class TestParseAmount:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            (2500000, 2500000.0),
            (-1.5, -1.5),
            ("2500000", 2500000.0),
            ("2,500,000", 2500000.0),
            ("-100,000,000", -100000000.0),
            ("(100,000,000)", -100000000.0),
            ("100000000-", -100000000.0),
            ("−100", -100.0),
            ("1.000.000,50", 1000000.5),
            ("1,000,000.50", 1000000.5),
            ("1 000 000", 1000000.0),
            ("1'000'000", 1000000.0),
            ("GBP 2,500,000", 2500000.0),
            ("€3,000,000", 3000000.0),
            (" 6000000 ", 6000000.0),
        ],
    )
    def test_accepts(self, raw, expected):
        assert parse_amount(raw)[0] == pytest.approx(expected)

    def test_formatted_string_is_noted(self):
        assert parse_amount("2,500,000")[1] is not None
        assert parse_amount(2500000)[1] is None

    @pytest.mark.parametrize("raw", [None, "", "abc", "2,5", "1,00,000", True, float("nan"), float("inf"), "1.2.3,4,5"])
    def test_rejects(self, raw):
        with pytest.raises(ValueError):
            parse_amount(raw)


class TestParseCurrency:
    @pytest.mark.parametrize("raw, expected", [("GBP", "GBP"), (" gbp ", "GBP"), ("GPB", "GBP"), ("£", "GBP"), ("usd", "USD")])
    def test_accepts(self, raw, expected):
        assert parse_currency(raw)[0] == expected

    def test_typo_is_noted(self):
        value, note = parse_currency("GPB")
        assert value == "GBP" and "typo" in note

    @pytest.mark.parametrize("raw", [None, "", "XYZ", "GB", "POUND"])
    def test_rejects_unknown(self, raw):
        with pytest.raises(ValueError):
            parse_currency(raw)


class TestParseCashflowType:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Interest", CashflowType.INTEREST),
            ("  interest ", CashflowType.INTEREST),
            ("PRINCIPAL  REPAYMENT", CashflowType.PRINCIPAL_REPAYMENT),
            ("Coupon", CashflowType.INTEREST),
            ("Drawdown", CashflowType.INVESTMENT),
        ],
    )
    def test_accepts(self, raw, expected):
        assert parse_cashflow_type(raw)[0] is expected

    def test_rejects(self):
        with pytest.raises(ValueError):
            parse_cashflow_type("Fee")
