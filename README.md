# Fund-Level FX Risk Analytics & Hedge Recommendations

Reads projected deal cashflows for private credit funds, validates and cleans
them with Pydantic, and computes:

* **IRR** per currency (local cashflows) and per fund (aggregate base-currency cashflows)
* **NAV schedule** per currency, with NAV(0)=0 and NAV(T) = exit value
* **FX hedge recommendations**: 3-month rolling forwards at 100% of NAV for
  every non-base currency

| Deliverable | Location |
|---|---|
| Part 1 (schema), Part 2 (pipeline), Part 4 (trade-offs) | [`docs/DESIGN.md`](docs/DESIGN.md) |
| Part 3 (implementation) | [`src/fx_hedge/`](src/fx_hedge) |
| Tests (129) | [`tests/`](tests) |
| Sample input | [`data/CST_Case_Study_data.xlsx`](data/CST_Case_Study_data.xlsx) |
| Generated Fund I outputs | [`output/`](output) |

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest                                   # 129 tests
fx-hedge data/CST_Case_Study_data.xlsx --fund "Fund I" --out output
# or without installing:
PYTHONPATH=src python -m fx_hedge data/CST_Case_Study_data.xlsx --fund "Fund I"
```

Requires Python ≥ 3.11, `pydantic>=2.5` and `openpyxl` (for `.xlsx`; `.csv` works without it).

## Results: Fund I (base EUR)

| Scope | IRR (annual, Act/365F) |
|---|---|
| EUR position (local) | **7.8870%** |
| GBP position (local) | **9.9507%** |
| USD position (local) | **12.0515%** |
| **Fund I (EUR base cashflows)** | **10.0476%** |

NAV sanity checks (all pass, and are asserted in tests):

| Currency | NAV(30-Sep-2025) | NAV(30-Sep-2030) | Exit value in file |
|---|---|---|---|
| EUR | 0.00 | 150,000,000.00 | 150,000,000 |
| GBP | 0.00 | 100,000,000.00 | 100,000,000 (EUR 114,573,786) |
| USD | 0.00 | 200,000,000.00 | 200,000,000 (EUR 170,212,766) |

The full schedules are in [`output/fund_i_nav_schedule.csv`](output/fund_i_nav_schedule.csv),
which includes an aggregate EUR total per date. The 40 recommended forwards
(20 quarterly rolls × GBP and USD) are in [`output/fund_i_hedges.csv`](output/fund_i_hedges.csv),
and the console output is in [`output/fund_i_report.txt`](output/fund_i_report.txt).
Fund II has identical yields at 1.4× the size (`--fund "Fund II"`).

## Data issues found in the sample

| Row ID | Field | Raw value | Handling |
|---|---|---|---|
| 17 | Local Currency | `GPB` | **Corrected** → `GBP` via a reviewed alias table. Confirmed by the dataset-level check: the row's implied FX rate (1.145738) matches the other GBP rows. |
| 21 | Date | `' 30/09/2030  00:00:00'` (leading space, double space, seconds) | **Corrected** → 2030-09-30 |
| 51 | Date | `'30/09/2027  00:00:00\`'` (double space, seconds, trailing backtick) | **Corrected** → 2027-09-30 |
| 27, 51 | Cashflow Amount Local | cell number format `General` instead of `#,##0` used everywhere else | Values are numerically fine. The parser also accepts the string forms such formatting produces in CSV exports (`"2,500,000"`, `(1,000)`, `1.000.000,50`, `GBP 2,500,000`, …). |
| all 6 positions | n/a | 19 quarterly coupons over 20 quarters: **no interest on the 30-Sep-2030 maturity date** | **Warning**, not imputed. Either the last coupon is missing from the projection, or the final period really pays no interest. That needs client confirmation. It lowers e.g. the GBP IRR from ~10.38% (2.5%/qtr compounded) to 9.95%. |

Every correction and warning is recorded as a `DataIssue` (row, field, raw
value, severity, message) and written to [`output/data_quality_issues.json`](output/data_quality_issues.json).
Nothing is fixed silently.

## Approach

```
read_raw_rows  →  CashflowRecord (Pydantic, per row)  →  validate_dataset (cross-row)  →  analytics  →  hedging
  (csv/xlsx)        cleaning.py parsers in                 duplicates, FX-rate               xirr,         3M roll,
  values kept       "before" validators; corrections      consistency, base ccy,            nav_schedule  % of NAV_ex,
  as raw strings    logged via validation context         schedule shape                                  deterministic ids
```

* **`cleaning.py`**: pure parsers for dates, amounts, currencies and cashflow
  types. Each returns `(value, note)` and raises on anything it would have to
  guess at, which keeps them independent of Pydantic and easy to unit-test.
* **`models.py`**: `CashflowRecord` calls the parsers from `mode="before"`
  validators and enforces business rules in a `model_validator`:
  Investment < 0 and inflows > 0, local and base signs agree, base == local
  when the currencies match. Corrections are appended to a list passed in
  `model_validate(..., context=...)`. Output models are `NavPoint`,
  `CurrencyAnalytics`, `FundAnalytics` and `HedgeTrade`, all frozen.
* **`ingest.py`**: header mapping with aliases (unknown columns produce a
  warning, missing ones raise `SchemaError`). It does row validation with
  quarantine, then dataset checks: duplicate IDs, duplicate business keys,
  one base currency per fund, implied-FX-rate consistency within a position,
  position completeness, and the missing-maturity-coupon check.
* **`analytics.py`**: `xirr` (Newton with a bisection fallback; matches
  Excel `XIRR`), `xnpv`, `nav_at`, `nav_schedule`, `compute_fund_analytics`.
* **`hedging.py`**: an end-of-month-aware roll calendar, hedge sizing, and
  deterministic trade IDs.

### Testing

`pytest` runs 129 tests covering:
* parser edge cases, accepted and rejected (parametrised)
* model business rules, and correction logging through the validation context
* dataset rules, using synthetic dirty CSVs written to `tmp_path`
* the sample file: exactly the three expected corrections, zero rejections,
  and the six expected warnings
* IRR: closed-form cases, Excel's documented `XIRR` example, netting and
  ordering invariance, and error cases
* NAV: NAV(0)=0, NAV(T)=exit, and the **roll-forward identity**
  `NAV_ex(tᵢ)·(1+IRR)^Δt = NAV(tᵢ₊₁)`, which proves the NAV accretes at the IRR
* Fund I regression values for every IRR and terminal NAV
* hedges: currency coverage, 20 contiguous quarterly rolls, notional = NAV_ex,
  hedge-ratio scaling, idempotent IDs, and BUY when exposure is net short

## Assumptions

1. **Deal = (fund, currency).** The file has no deal identifier, and each
   fund has exactly one investment per currency. A production feed must carry
   a stable deal ID (see the schema in DESIGN.md).
2. **Slash dates are day-first** (UK/EU convention). Every unambiguous row in
   the file is `dd/mm/yyyy`. A time component must be midnight.
3. **Day count is Act/365F** (Excel `XIRR`/`XNPV` semantics). IRRs are annual
   effective rates.
4. **Currency-level IRR uses local-currency cashflows**, i.e. the asset's yield
   free of FX. **The fund-level IRR uses the summed base-currency cashflows
   from the file.** These embed one constant FX rate per position, so the
   base and local IRRs per currency coincide (`irr_base` is reported too).
   With a real FX curve they would differ by the FX carry and P&L.
5. **The NAV convention follows the brief.** NAV(t) = PV at the position IRR
   of all cashflows dated **on or after** t. This "cum-flow" definition is
   what makes NAV(0)=0 (the investment cancels the PV of the inflows) and
   NAV(T) = terminal cashflow. The schedule is evaluated on every cashflow
   date. `nav_at()` supports arbitrary dates.
6. **Hedge notional = 100% × NAV_ex(t)**, the PV of cashflows strictly
   **after** the roll date. This is the value at risk over the next quarter
   (cum-flow NAV is 0 at inception, which would leave the initial
   exposure unhedged). NAV_ex(t₀) equals the invested amount exactly.
7. **Hedge direction:** the fund is long local-currency assets, so it
   SELLS local and BUYS EUR forward. A net-negative exposure flips to BUY.
8. **Pricing:** no market data was supplied. `reference_rate` is the spot rate
   implied by the projection (taken from each position's largest flow, to
   avoid rounding noise in the coupons). Forward points and live quotes come
   from a market data service in production.
9. **Base amounts are rounded to whole units** in the source, so the FX
   consistency check uses a 1e-4 relative tolerance, and same-currency rows
   allow a 1-unit difference.
10. **Currency typos are only corrected from an explicit alias map.** There is
    no fuzzy matching, and unknown codes are rejected.
11. The brief mentions a CSV, but the file supplied was `.xlsx`. Both are
    supported, and the CSV path is covered by tests.

## Limitations / next steps

* Amounts are `float` in the analytics layer (fine for IRR/NAV precision).
  Persistence should use `Decimal`/`NUMERIC` (see the schema).
* No FX forward curve, hedge book or rebalancing logic in code. These are
  designed in DESIGN.md (Part 2 and Part 4).
* NAV is reported per currency position. A fund-level NAV discounted at the
  fund IRR could be added, but it is not the sum of the per-currency NAVs.
