from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

from .analytics import compute_fund_analytics
from .hedging import generate_hedges
from .ingest import load_cashflows
from .models import CashflowRecord, Severity
from .models import HedgeTrade


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fund risk analytics & FX hedge recommendations")
    p.add_argument("path", type=Path, help="cashflow file (.csv or .xlsx)")
    p.add_argument("--fund", default="Fund I")
    p.add_argument("--out", type=Path, help="directory for CSV outputs")
    args = p.parse_args(argv)

    result = load_cashflows(args.path)
    print(f"Read {result.rows_read} rows -> {len(result.records)} valid records")

    fa = compute_fund_analytics(result.records, args.fund)
    hedges = generate_hedges(fa)

    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    slug = args.fund.lower().replace(" ", "_")

    issues_by_row: dict[int | None, list] = defaultdict(list)
    for i in result.issues:
        issues_by_row[i.row_id].append(i)
    with (out / f"1_{slug}_ingestion_report.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        fields = list(CashflowRecord.model_fields)
        w.writerow([*fields, "status", "data_quality"])
        for r in result.records:
            notes = issues_by_row.pop(r.id, None)
            if notes:
                d = r.model_dump(mode="json")
                w.writerow([*(d[f] for f in fields), Severity.CORRECTED.value,
                            "; ".join(f"{i.field}: {i.message}" for i in notes)])
        for row_id, notes in issues_by_row.items():  # rows that failed validation
            w.writerow([row_id if row_id is not None else "", *[""] * (len(fields) - 1), Severity.REJECTED.value,
                        "; ".join(f"{i.field}: {i.message}" for i in notes)])

    rejected = result.issues_by_severity(Severity.REJECTED)
    if rejected:
        print(f"{len(rejected)} rejected issue(s), see the ingestion report. "
              "No analytics or hedges produced.", file=sys.stderr)
        return 1

    with (out / f"2_{slug}_irr.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["scope", "currency", "irr"])
        w.writerow(["fund", fa.base_currency, f"{fa.fund_irr:.8f}"])
        for c in fa.currencies:
            w.writerow(["currency", c.currency, f"{c.irr:.8f}"])

    with (out / f"3_{slug}_nav.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["currency", "date", "cashflow_local", "nav_local"])
        for c in fa.currencies:
            for pt in c.nav_schedule:
                w.writerow([c.currency, pt.nav_date, f"{pt.cashflow_local:.2f}", f"{pt.nav_local:.2f}"])

    with (out / f"4_{slug}_hedges.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        fields = list(HedgeTrade.model_fields)
        w.writerow(fields)
        for h in hedges:
            d = h.model_dump(mode="json")
            w.writerow([d[f] for f in fields])
    print(f"\nWrote outputs to {out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
