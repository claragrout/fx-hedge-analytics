from __future__ import annotations

import datetime as dt
import re

# ---------------------------------------------------------------------------
# Currencies
# ---------------------------------------------------------------------------

#: Currencies present in the data.
CURRENCIES: frozenset[str] = frozenset({"EUR", "GBP", "USD"})

#: Typos present in the data, mapped to the intended currency.
CURRENCY_CORRECTIONS: dict[str, str] = {"GPB": "GBP"}


def parse_currency(raw: object) -> tuple[str, str | None]:
    if raw in CURRENCIES:
        return raw, None
    if raw in CURRENCY_CORRECTIONS:
        fixed = CURRENCY_CORRECTIONS[raw]
        return fixed, f"corrected currency typo {raw!r} -> {fixed!r}"
    raise ValueError(f"unknown currency code {raw!r}")


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

#: Accepted date formats. Dates are day-first (dd/mm/yyyy). The xlsx yields a
#: time component; CSV exports may carry the bare date.
DATE_FORMATS: tuple[str, ...] = (
    "%d/%m/%Y",
    "%d/%m/%Y %H:%M",
    "%d/%m/%Y %H:%M:%S",
)

#: Stray characters present in the data (a trailing backtick in row 51).
_DATE_JUNK = re.compile(r"`")

#: Plausible range for a cashflow date. Catches typos such as a two-digit
#: year, which ``%Y`` would otherwise read as e.g. year 25 AD.
EARLIEST_VALID_DATE = dt.date(1990, 1, 1)
LATEST_VALID_DATE = dt.date(2100, 12, 31)


def parse_date(raw: object) -> tuple[dt.date, str | None]:
    if not isinstance(raw, str):
        raise ValueError(f"date must be text, got {raw!r}")

    cleaned = _DATE_JUNK.sub("", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    for fmt in DATE_FORMATS:
        try:
            parsed = dt.datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
        if parsed.time() != dt.time(0, 0):
            raise ValueError(f"date has a non-midnight time component: {raw!r}")
        if not EARLIEST_VALID_DATE <= parsed.date() <= LATEST_VALID_DATE:
            raise ValueError(f"date {raw!r} outside plausible range")
        note = None
        if cleaned != raw:
            note = f"normalised date {raw!r} -> {parsed.date().isoformat()}"
        return parsed.date(), note
    raise ValueError(f"unparseable date {raw!r}")
