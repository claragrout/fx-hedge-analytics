"""Command-line entry point.

    python -m fx_hedge data/CST_Case_Study_data.xlsx --fund "Fund I" --out output
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .analytics import compute_fund_analytics, fund_nav_base_total
from .hedging import generate_hedges
from .ingest import load_cashflows
from .models import Severity


def _fmt(x: float) -> str:
    return f"{x:>18,.2f}"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Fund risk analytics & FX hedge recommendations")
    p.add_argument("path", type=Path, help="cashflow file (.csv or .xlsx)")
    p.add_argument("--fund", default="Fund I")
    p.add_argument("--out", type=Path, default=None, help="directory for CSV/JSON outputs")
    args = p.parse_args(argv)

    result = load_cashflows(args.path)
    print(f"Read {result.rows_read} rows -> {len(result.records)} valid records")
    for sev in Severity:
        items = result.issues_by_severity(sev)
        print(f"  {sev.value:<9} {len(items)}")
        for i in items:
            print(f"    row {i.row_id!s:>4} {i.field:<15} {i.message}")

    fa = compute_fund_analytics(result.records, args.fund)
    print(f"\n{fa.fund_name} (base {fa.base_currency})")
    print(f"  Fund-level IRR (base ccy cashflows): {fa.fund_irr:.4%}")
    for c in fa.currencies:
        print(f"  {c.currency} IRR (local): {c.irr:.4%}   (in {fa.base_currency} at projected FX: {c.irr_base:.4%})")

    for c in fa.currencies:
        print(f"\nNAV schedule {fa.fund_name} / {c.currency} (local)")
        print(f"  {'date':<10} {'cashflow':>18} {'NAV (cum-flow)':>18} {'NAV ex-flow':>18} {'NAV base':>18}")
        for pt in c.nav_schedule:
            print(f"  {pt.nav_date} {_fmt(pt.cashflow_local)} {_fmt(pt.nav_local)} {_fmt(pt.nav_ex_local)} {_fmt(pt.nav_base)}")

    hedges = generate_hedges(fa)
    print(f"\nHedge recommendations ({len(hedges)} forwards, 100% of NAV, 3M rolling)")
    for h in hedges:
        print(f"  {h.trade_date} -> {h.maturity_date} {h.side.value} "
              f"{h.exposure_currency} {h.notional_local:>16,.2f} / BUY {h.base_currency} {h.notional_base:>16,.2f}")

    if args.out:
        out = args.out
        out.mkdir(parents=True, exist_ok=True)
        slug = args.fund.lower().replace(" ", "_")
        (out / f"{slug}_analytics.json").write_text(fa.model_dump_json(indent=2))
        (out / "data_quality_issues.json").write_text(
            json.dumps([i.model_dump(mode="json") for i in result.issues], indent=2)
        )
        with (out / f"{slug}_nav_schedule.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["fund", "currency", "date", "cashflow_local", "nav_local", "nav_ex_local", "fx_rate", "nav_base", "nav_ex_base"])
            for c in fa.currencies:
                for pt in c.nav_schedule:
                    w.writerow([pt.fund_name, pt.currency, pt.nav_date, f"{pt.cashflow_local:.2f}", f"{pt.nav_local:.2f}",
                                f"{pt.nav_ex_local:.2f}", f"{pt.fx_rate:.8f}", f"{pt.nav_base:.2f}", f"{pt.nav_ex_base:.2f}"])
            for d, nb, nxb in fund_nav_base_total(fa):
                w.writerow([fa.fund_name, f"TOTAL_{fa.base_currency}", d, "", "", "", "", f"{nb:.2f}", f"{nxb:.2f}"])
        with (out / f"{slug}_hedges.csv").open("w", newline="") as fh:
            w = csv.writer(fh)
            fields = list(type(hedges[0]).model_fields) if hedges else []
            w.writerow(fields)
            for h in hedges:
                d = h.model_dump(mode="json")
                w.writerow([d[f] for f in fields])
        print(f"\nWrote outputs to {out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
