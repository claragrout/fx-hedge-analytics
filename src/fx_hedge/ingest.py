"""Load the client cashflow file and turn it into validated ``CashflowRecord`` objects.

Rows are read lazily (generators) and validated one at a time, so the file is
never held in memory as a second, raw copy. A row that fails validation is
rejected and logged; the rest of the file carries on.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

import openpyxl
from pydantic import PositiveInt, TypeAdapter, ValidationError

from .models import CashflowRecord, DataIssue, Severity

#: Column headers present in the data, exactly as written in the file.
EXPECTED_HEADERS: frozenset[str] = frozenset(
    {
        "ID",
        "Fund Name",
        "Date",
        "Cashflow Type",
        "Local Currency",
        "Cashflow Amount Local",
        "Cashflow Amount Base",
        "Base Currency",
    }
)


class SchemaError(ValueError):
    """The file cannot be processed at all (e.g. a required column is missing)."""


@dataclass
class IngestionResult:
    records: list[CashflowRecord] = field(default_factory=list)
    issues: list[DataIssue] = field(default_factory=list)
    rows_read: int = 0

    def issues_by_severity(self, severity: Severity) -> list[DataIssue]:
        return [i for i in self.issues if i.severity is severity]


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def header_to_field(header: str) -> str:
    """``'Cashflow Amount Local'`` -> ``'cashflow_amount_local'`` (the model field name)."""
    return header.lower().replace(" ", "_")


def _check_headers(header: list[Any]) -> list[str]:
    """Require exactly the expected headers; return the matching field names in column order."""
    if len(header) != len(set(header)):
        raise SchemaError(f"duplicate column headers: {header}")
    missing = EXPECTED_HEADERS - set(header)
    unexpected = set(header) - EXPECTED_HEADERS
    if missing:
        raise SchemaError(f"missing columns: {sorted(missing)}")
    if unexpected:
        raise SchemaError(f"unexpected columns: {sorted(map(str, unexpected))}")
    return [header_to_field(h) for h in header]


def _rows(fields: list[str], body: Iterable[Iterable[Any]]) -> Iterator[dict[str, Any]]:
    """Yield each data row as a dict keyed by field name."""
    for values in body:
        yield dict(zip(fields, values))


def read_raw_rows(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield raw rows from a CSV or XLSX file, one at a time.

    Values are passed through untouched (strings stay strings) so that all
    cleaning happens in one place: the Pydantic model.
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            try:
                header = next(reader)
            except StopIteration as exc:
                raise SchemaError("empty file") from exc
            yield from _rows(_check_headers(header), reader)
    elif suffix == ".xlsx":
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            if len(wb.worksheets) != 1:
                names = [ws.title for ws in wb.worksheets]
                raise SchemaError(f"expected exactly one worksheet, found {len(names)}: {names}")
            it = wb.worksheets[0].iter_rows(values_only=True)
            try:
                header = list(next(it))
            except StopIteration as exc:
                raise SchemaError("empty workbook") from exc
            yield from _rows(_check_headers(header), it)
        finally:
            wb.close()
    else:
        raise SchemaError(f"unsupported file type {suffix!r}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

#: The same rule the model applies to ``id``, reused to label rejected rows.
_ROW_ID = TypeAdapter(PositiveInt)


def _best_effort_row_id(value: Any) -> int | None:
    """The row's id if it is valid, else ``None`` (used only to label issues)."""
    if value is None:
        return None
    try:
        return _ROW_ID.validate_python(value)
    except ValidationError:
        return None


def _validation_error_issues(row_id: int | None, raw: dict[str, Any], exc: ValidationError) -> list[DataIssue]:
    issues = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "__row__"
        raw_value = raw.get(loc)
        msg = err["msg"].removeprefix("Value error, ")
        issues.append(
            DataIssue(
                row_id=row_id,
                field=loc,
                severity=Severity.REJECTED,
                message=msg,
                raw_value=None if raw_value is None else str(raw_value),
            )
        )
    return issues


def validate_rows(raw_rows: Iterable[dict[str, Any]]) -> IngestionResult:
    result = IngestionResult()
    for raw in raw_rows:
        result.rows_read += 1
        row_id = _best_effort_row_id(raw.get("id"))
        row_issues: list[DataIssue] = []
        try:
            rec = CashflowRecord.model_validate(raw, context={"issues": row_issues, "row_id": row_id})
        except ValidationError as exc:
            result.issues.extend(_validation_error_issues(row_id, raw, exc))
            continue
        result.issues.extend(row_issues)
        result.records.append(rec)
    return result


def load_cashflows(path: str | Path) -> IngestionResult:
    return validate_rows(read_raw_rows(path))
