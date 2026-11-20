"""Pure parsing/normalisation functions for dirty client cashflow fields.

Each parser returns ``(value, note)`` where ``note`` is ``None`` when the raw
input was already canonical, or a short human-readable description of the
correction applied. Unrecoverable input raises ``ValueError`` so that Pydantic
turns it into a validation error (and the row is quarantined).

Keeping these functions free of Pydantic makes them trivially unit-testable and
reusable from other ingestion paths (APIs, SFTP drops, etc.).
"""

from __future__ import annotations

import datetime as dt
import math
import re
from decimal import Decimal, InvalidOperation

from .models_enums import CashflowType

# ---------------------------------------------------------------------------
# Currencies
# ---------------------------------------------------------------------------

#: ISO 4217 codes accepted by the platform. In production this would be a
#: reference-data table, not a constant.
ISO_CURRENCIES: frozenset[str] = frozenset(
    {
        "AUD", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK", "EUR", "GBP", "HKD",
        "HUF", "IDR", "ILS", "INR", "JPY", "KRW", "MXN", "NOK", "NZD", "PLN",
        "SEK", "SGD", "THB", "TRY", "USD", "ZAR",
    }
)

#: Explicit, reviewed corrections for known typos / symbols. We deliberately do
#: *not* fuzzy-match arbitrary strings: silently mapping an unknown code to the
#: "closest" currency could put the wrong exposure on the book.
CURRENCY_ALIASES: dict[str, str] = {
    "GPB": "GBP",
    "GBR": "GBP",
    "UKP": "GBP",
    "STG": "GBP",
    "£": "GBP",
    "EUD": "EUR",
    "ERU": "EUR",
    "EURO": "EUR",
    "€": "EUR",
    "UDS": "USD",
    "USS": "USD",
    "US$": "USD",
    "$": "USD",
}


def parse_currency(raw: object) -> tuple[str, str | None]:
    if raw is None:
        raise ValueError("currency is missing")
    text = str(raw)
    code = re.sub(r"\s+", "", text).upper()
    if not code:
        raise ValueError("currency is blank")
    if code in ISO_CURRENCIES:
        note = None if text == code else f"normalised {text!r} -> {code!r}"
        return code, note
    if code in CURRENCY_ALIASES:
        fixed = CURRENCY_ALIASES[code]
        return fixed, f"corrected currency typo {text!r} -> {fixed!r}"
    raise ValueError(f"unknown currency code {text!r}")


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

#: Canonical source format observed in the client file.
CANONICAL_DATE_FORMAT = "%d/%m/%Y %H:%M"

#: Accepted formats, tried in order. Slash dates are interpreted DAY-FIRST
#: (UK/EU convention, consistent with every unambiguous row in the sample).
DATE_FORMATS: tuple[str, ...] = (
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%d-%m-%Y",
    "%d.%m.%Y",
    "%d %b %Y",
    "%d-%b-%Y",
)

_DATE_JUNK = re.compile(r"[`'\"‘’“” ]")


def parse_date(raw: object) -> tuple[dt.date, str | None]:
    if raw is None:
        raise ValueError("date is missing")
    if isinstance(raw, dt.datetime):
        if raw.time() != dt.time(0, 0):
            raise ValueError(f"date has a non-midnight time component: {raw!r}")
        return raw.date(), None
    if isinstance(raw, dt.date):
        return raw, None

    text = str(raw)
    cleaned = _DATE_JUNK.sub("", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        raise ValueError("date is blank")

    for fmt in DATE_FORMATS:
        try:
            parsed = dt.datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        if parsed.time() != dt.time(0, 0):
            raise ValueError(f"date has a non-midnight time component: {text!r}")
        if not dt.date(1990, 1, 1) <= parsed.date() <= dt.date(2100, 12, 31):
            raise ValueError(f"date {text!r} outside plausible range")
        note = None
        if cleaned != text or fmt != CANONICAL_DATE_FORMAT:
            note = f"normalised date {text!r} -> {parsed.date().isoformat()} (format {fmt!r})"
        return parsed.date(), note
    raise ValueError(f"unparseable date {text!r}")


# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------

_CCY_TOKENS = re.compile(r"[A-Za-z]{3}|[£€$¥]")
_THOUSANDS_ONLY_COMMAS = re.compile(r"^\d{1,3}(,\d{3})+(\.\d+)?$")
_THOUSANDS_ONLY_DOTS = re.compile(r"^\d{1,3}(\.\d{3})+(,\d+)?$")


def parse_amount(raw: object) -> tuple[float, str | None]:
    """Parse a monetary amount, tolerating common spreadsheet formatting.

    Handles thousands separators (``1,000,000`` / ``1.000.000,50`` /
    ``1 000 000`` / ``1'000'000``), accounting negatives ``(500)``, trailing
    minus ``500-``, unicode minus, and embedded currency symbols/codes.
    Ambiguous strings such as ``2,5`` are rejected rather than guessed.
    """
    if raw is None:
        raise ValueError("amount is missing")
    if isinstance(raw, bool):
        raise ValueError("amount must be numeric, got a boolean")
    if isinstance(raw, (int, float, Decimal)):
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError(f"amount is not finite: {raw!r}")
        return value, None

    text = str(raw)
    s = text.strip().replace("−", "-").replace(" ", "").replace(" ", "")
    s = _CCY_TOKENS.sub("", s)
    s = re.sub(r"[\s'_]", "", s)
    if not s:
        raise ValueError(f"amount is blank: {text!r}")

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if s.endswith("-"):
        negative, s = not negative, s[:-1]
    if s.startswith("-"):
        negative, s = not negative, s[1:]
    elif s.startswith("+"):
        s = s[1:]

    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):  # European: 1.000.000,50
            if not _THOUSANDS_ONLY_DOTS.match(s):
                raise ValueError(f"ambiguous amount {text!r}")
            s = s.replace(".", "").replace(",", ".")
        else:  # Anglo: 1,000,000.50
            if not _THOUSANDS_ONLY_COMMAS.match(s):
                raise ValueError(f"ambiguous amount {text!r}")
            s = s.replace(",", "")
    elif "," in s:
        if not _THOUSANDS_ONLY_COMMAS.match(s):
            raise ValueError(f"ambiguous amount {text!r} (comma is not a thousands separator)")
        s = s.replace(",", "")
    elif s.count(".") > 1:
        if not _THOUSANDS_ONLY_DOTS.match(s):
            raise ValueError(f"ambiguous amount {text!r}")
        s = s.replace(".", "")

    try:
        value = float(Decimal(s))
    except InvalidOperation as exc:
        raise ValueError(f"unparseable amount {text!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"amount is not finite: {text!r}")
    if negative:
        value = -value

    plain = f"{value:.10g}"
    note = None if text.strip() == plain else f"parsed amount {text!r} -> {value:,.2f}"
    return value, note


# ---------------------------------------------------------------------------
# Cashflow type / free text
# ---------------------------------------------------------------------------

CASHFLOW_TYPE_ALIASES: dict[str, CashflowType] = {
    "investment": CashflowType.INVESTMENT,
    "drawdown": CashflowType.INVESTMENT,
    "funding": CashflowType.INVESTMENT,
    "interest": CashflowType.INTEREST,
    "coupon": CashflowType.INTEREST,
    "principal repayment": CashflowType.PRINCIPAL_REPAYMENT,
    "principal": CashflowType.PRINCIPAL_REPAYMENT,
    "repayment": CashflowType.PRINCIPAL_REPAYMENT,
    "principal_repayment": CashflowType.PRINCIPAL_REPAYMENT,
}


def parse_cashflow_type(raw: object) -> tuple[CashflowType, str | None]:
    if raw is None:
        raise ValueError("cashflow type is missing")
    text = str(raw)
    key = re.sub(r"\s+", " ", text).strip().lower()
    if key not in CASHFLOW_TYPE_ALIASES:
        raise ValueError(f"unknown cashflow type {text!r}")
    value = CASHFLOW_TYPE_ALIASES[key]
    note = None if text == value.value else f"normalised cashflow type {text!r} -> {value.value!r}"
    return value, note


def clean_text(raw: object, field: str) -> tuple[str, str | None]:
    if raw is None:
        raise ValueError(f"{field} is missing")
    text = str(raw)
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        raise ValueError(f"{field} is blank")
    return cleaned, (None if cleaned == text else f"trimmed whitespace in {text!r}")
