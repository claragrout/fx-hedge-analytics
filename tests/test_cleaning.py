import datetime as dt

import pytest

from fx_hedge.cleaning import parse_currency, parse_date


class TestParseDate:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("30/09/2025 00:00", dt.date(2025, 9, 30)),
            (" 30/09/2030  00:00:00", dt.date(2030, 9, 30)),  # sample row 21
            ("30/09/2027  00:00:00`", dt.date(2027, 9, 30)),  # sample row 51
            ("01/02/2026 00:00", dt.date(2026, 2, 1)),  # day-first
        ],
    )
    def test_accepts(self, raw, expected):
        assert parse_date(raw)[0] == expected

    def test_clean_input_has_no_note(self):
        assert parse_date("30/09/2025 00:00")[1] is None

    def test_seconds_alone_is_not_a_correction(self):
        # A valid format with nothing removed is read as-is, not "corrected".
        assert parse_date("30/09/2025 00:00:00")[1] is None

    @pytest.mark.parametrize("raw", ["30/09/2027  00:00:00`", " 30/09/2030  00:00:00"])
    def test_cleaned_input_has_note(self, raw):
        assert "normalised" in parse_date(raw)[1]

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            dt.date(2025, 9, 30),  # not text
            "",
            "31/02/2026 00:00",  # no such day
            "not a date",
            "2025-09-30",  # format not in the data
            "30/09/2025 14:30",  # non-midnight time
            "30/09/25 00:00",  # two-digit year -> year 25, outside range
        ],
    )
    def test_rejects(self, raw):
        with pytest.raises(ValueError):
            parse_date(raw)


class TestParseCurrency:
    @pytest.mark.parametrize("raw", ["EUR", "GBP", "USD"])
    def test_accepts_known(self, raw):
        assert parse_currency(raw) == (raw, None)

    def test_corrects_typo_from_data(self):
        value, note = parse_currency("GPB")
        assert value == "GBP" and "typo" in note

    @pytest.mark.parametrize("raw", [None, "", "XYZ", "CHF", "gbp", " GBP"])
    def test_rejects_anything_else(self, raw):
        with pytest.raises(ValueError):
            parse_currency(raw)
