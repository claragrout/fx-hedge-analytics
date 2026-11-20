"""Load raw client files and turn them into validated ``CashflowRecord`` objects.

Two layers of checks:

1. Row level: :class:`~fx_hedge.models.CashflowRecord` (types, formats,
   sign conventions). Failures quarantine the row.
2. Dataset level, in :func:`validate_dataset`. These rules need several rows:
   duplicate ids, duplicate business keys, implied FX-rate consistency within
   a position, one base currency per fund, and position completeness.
"""

from __future__ import annotations

import csv
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from pydantic import ValidationError

from .models import CashflowRecord, CashflowType, DataIssue, Severity

#: Normalised source header -> model field. Extra columns are tolerated
#: (forward-compatible), missing required columns fail the whole file.
HEADER_MAP: dict[str, str] = {
    "id": "row_id",
    "fund name": "fund_name",
    "fund": "fund_name",
    "date": "cashflow_date",
    "cashflow date": "cashflow_date",
    "cashflow type": "cashflow_type",
    "type": "cashflow_type",
    "local currency": "local_currency",
    "currency": "local_currency",
    "cashflow amount local": "amount_local",
    "amount local": "amount_local",
    "cashflow amount base": "amount_base",
    "amount base": "amount_base",
    "base currency": "base_currency",
}
REQUIRED_FIELDS = frozenset(HEADER_MAP.values())

#: Max relative deviation of a row's implied FX rate from its position median.
#: Base amounts in the sample are rounded to integers (~1e-7 relative noise).
FX_RATE_TOLERANCE = 1e-4


class SchemaError(ValueError):
    """The file cannot be processed at all (e.g. a required column is missing)."""


@dataclass
class IngestionResult:
    records: list[CashflowRecord]
    issues: list[DataIssue] = field(default_factory=list)
    rows_read: int = 0

    @property
    def rejected_row_ids(self) -> set[int | None]:
        return {i.row_id for i in self.issues if i.severity is Severity.REJECTED}

    def issues_by_severity(self, severity: Severity) -> list[DataIssue]:
        return [i for i in self.issues if i.severity is severity]

    def for_fund(self, fund_name: str) -> list[CashflowRecord]:
        return [r for r in self.records if r.fund_name == fund_name]


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _normalise_header(h: Any) -> str:
    return re.sub(r"\s+", " ", str(h or "")).strip().lower()


def _map_headers(headers: Iterable[Any]) -> tuple[list[str | None], list[str]]:
    mapped: list[str | None] = []
    unknown: list[str] = []
    for h in headers:
        key = HEADER_MAP.get(_normalise_header(h))
        mapped.append(key)
        if key is None and _normalise_header(h):
            unknown.append(str(h))
    missing = REQUIRED_FIELDS - {m for m in mapped if m}
    if missing:
        raise SchemaError(f"missing required columns: {sorted(missing)}")
    return mapped, unknown


def _rows_from_table(header: list[Any], body: Iterable[Iterable[Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    mapped, unknown = _map_headers(header)
    rows: list[dict[str, Any]] = []
    for raw in body:
        values = list(raw)
        if all(v is None or (isinstance(v, str) and not v.strip()) for v in values):
            continue  # skip blank lines
        row: dict[str, Any] = {}
        for key, value in zip(mapped, values):
            if key is not None and key not in row:
                row[key] = value
        rows.append(row)
    return rows, unknown


def read_raw_rows(path: str | Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Read a CSV or XLSX file into raw dicts keyed by model field name.

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
            return _rows_from_table(header, reader)
    if suffix in {".xlsx", ".xlsm"}:
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover
            raise SchemaError("openpyxl is required to read Excel files") from exc
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb.worksheets[0]
            it = ws.iter_rows(values_only=True)
            try:
                header = list(next(it))
            except StopIteration as exc:
                raise SchemaError("empty workbook") from exc
            return _rows_from_table(header, it)
        finally:
            wb.close()
    raise SchemaError(f"unsupported file type {suffix!r}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validation_error_issues(row_id: int | None, raw: dict[str, Any], exc: ValidationError) -> list[DataIssue]:
    issues = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ())) or "__row__"
        raw_value = raw.get(loc) if loc in raw else None
        msg = err.get("msg", "invalid").removeprefix("Value error, ")
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


def _best_effort_row_id(raw: dict[str, Any]) -> int | None:
    try:
        return int(str(raw.get("row_id")).strip().split(".")[0])
    except (TypeError, ValueError):
        return None


def validate_rows(raw_rows: list[dict[str, Any]]) -> IngestionResult:
    records: list[CashflowRecord] = []
    issues: list[DataIssue] = []
    for raw in raw_rows:
        row_id = _best_effort_row_id(raw)
        row_issues: list[DataIssue] = []
        try:
            rec = CashflowRecord.model_validate(raw, context={"issues": row_issues, "row_id": row_id})
        except ValidationError as exc:
            issues.extend(_validation_error_issues(row_id, raw, exc))
            continue
        issues.extend(row_issues)
        records.append(rec)
    result = IngestionResult(records=records, issues=issues, rows_read=len(raw_rows))
    validate_dataset(result)
    return result


def validate_dataset(result: IngestionResult) -> None:
    """Apply cross-row rules in place (may reject records or add warnings)."""
    rejected: set[int] = set()

    # 1. Duplicate row ids: first occurrence wins, later ones quarantined.
    seen_ids: set[int] = set()
    for r in result.records:
        if r.row_id in seen_ids:
            result.issues.append(
                DataIssue(row_id=r.row_id, field="row_id", severity=Severity.REJECTED,
                          message="duplicate row id; later occurrence discarded")
            )
            rejected.add(id(r))
        seen_ids.add(r.row_id)

    # 2. Duplicate business key (same position, date, type). Without a deal id we
    #    cannot tell a genuine second deal from a double-load, so warn only.
    by_key: dict[tuple, list[CashflowRecord]] = defaultdict(list)
    for r in result.records:
        if id(r) not in rejected:
            by_key[(r.fund_name, r.local_currency, r.cashflow_date, r.cashflow_type)].append(r)
    for key, rows in by_key.items():
        if len(rows) > 1:
            ids = sorted(x.row_id for x in rows)
            for x in rows:
                result.issues.append(
                    DataIssue(row_id=x.row_id, field="__row__", severity=Severity.WARNING,
                              message=f"duplicate business key {key[0]}/{key[1]}/{key[2]}/{key[3].value} (rows {ids})")
                )

    # 3. One base currency per fund (majority wins, outliers rejected).
    fund_bases: dict[str, list[CashflowRecord]] = defaultdict(list)
    for r in result.records:
        if id(r) not in rejected:
            fund_bases[r.fund_name].append(r)
    for fund, rows in fund_bases.items():
        majority = statistics.mode(x.base_currency for x in rows)
        for x in rows:
            if x.base_currency != majority:
                result.issues.append(
                    DataIssue(row_id=x.row_id, field="base_currency", severity=Severity.REJECTED,
                              message=f"base currency {x.base_currency} differs from fund base {majority}")
                )
                rejected.add(id(x))

    # 4. Implied FX rate consistency within a position. A typo'd currency that
    #    we "corrected" is also confirmed here: its rate must match its peers.
    positions: dict[tuple[str, str], list[CashflowRecord]] = defaultdict(list)
    for r in result.records:
        if id(r) not in rejected:
            positions[r.position_key].append(r)
    for (fund, ccy), rows in positions.items():
        median_rate = statistics.median(x.implied_fx_rate for x in rows)
        for x in rows:
            dev = abs(x.implied_fx_rate / median_rate - 1.0)
            if dev > FX_RATE_TOLERANCE:
                result.issues.append(
                    DataIssue(row_id=x.row_id, field="amount_base", severity=Severity.WARNING,
                              message=(f"implied FX rate {x.implied_fx_rate:.6f} deviates {dev:.2%} from "
                                       f"position median {median_rate:.6f} ({fund}/{ccy})"))
                )

    # 5. Position completeness: an IRR needs at least one investment and one inflow.
    for (fund, ccy), rows in positions.items():
        types = {x.cashflow_type for x in rows}
        if CashflowType.INVESTMENT not in types or types == {CashflowType.INVESTMENT}:
            result.issues.append(
                DataIssue(row_id=None, field="__position__", severity=Severity.WARNING,
                          message=f"position {fund}/{ccy} lacks an investment or any inflow; IRR undefined")
            )

    # 6. Schedule shape: flag a missing coupon at maturity. If interest is
    #    paid periodically but the principal date carries no interest, the
    #    final accrual period is probably missing from the projection. It is
    #    kept as-is (we don't invent cashflows) but surfaced for review.
    for (fund, ccy), rows in positions.items():
        interest_dates = sorted({x.cashflow_date for x in rows if x.cashflow_type is CashflowType.INTEREST})
        principal_dates = sorted({x.cashflow_date for x in rows if x.cashflow_type is CashflowType.PRINCIPAL_REPAYMENT})
        if len(interest_dates) < 2 or not principal_dates:
            continue
        maturity = principal_dates[-1]
        gaps = [(b - a).days for a, b in zip(interest_dates, interest_dates[1:])]
        typical = statistics.median(gaps)
        final_gap = (maturity - interest_dates[-1]).days
        if maturity not in interest_dates and 0.5 * typical < final_gap <= 1.5 * typical:
            result.issues.append(
                DataIssue(row_id=None, field="__position__", severity=Severity.WARNING,
                          message=(f"position {fund}/{ccy}: no interest at maturity {maturity}; last coupon "
                                   f"{interest_dates[-1]} ({len(interest_dates)} coupons). Final period's "
                                   f"interest may be missing from the projection"))
            )

    keep = [r for r in result.records if id(r) not in rejected]
    result.records = sorted(keep, key=lambda r: (r.fund_name, r.local_currency, r.cashflow_date, r.row_id))


def load_cashflows(path: str | Path) -> IngestionResult:
    raw_rows, unknown_cols = read_raw_rows(path)
    result = validate_rows(raw_rows)
    for col in unknown_cols:
        result.issues.insert(
            0,
            DataIssue(row_id=None, field=col, severity=Severity.WARNING,
                      message=f"unrecognised column {col!r} ignored"),
        )
    return result
