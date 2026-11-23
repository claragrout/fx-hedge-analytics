# FX Hedge Analytics

Reads a fund's projected cashflows, calculates IRRs and NAV schedules, and recommends
FX forward hedges at 100% of NAV for each non-base currency.

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run python -m fx_hedge data/CST_Case_Study_data.xlsx --fund "Fund I" --out output
uv run pytest
```

## Outputs

| File | Contents |
|---|---|
| `1_fund_i_ingestion_report.csv` | Every row that was corrected or rejected, and why |
| `2_fund_i_irr.csv` | Fund IRR (base currency) and one IRR per local currency |
| `3_fund_i_nav.csv` | NAV schedule per currency, one row per cashflow date |
| `4_fund_i_hedges.csv` | Recommended forwards: side, notional, trade and maturity dates |

## Approach

The pipeline is five steps. Each step's output is the next step's input.

```
.xlsx file ─► 1. Read ─► 2. Validate ─► 3. Analytics ─► 4. Hedges ─► 5. Output (CSV)
```

| Step | Module | What it does |
|---|---|---|
| Read | `ingest.py` | Checks the file has one sheet and the eight expected headers |
| Validate | `ingest.py`, `cleaning.py`, `models.py` | Turns each row into a `CashflowRecord`; corrects known errors, rejects the rest |
| Analytics | `analytics.py` | Fund IRR, currency IRRs and NAV schedules |
| Hedges | `hedging.py` | One forward per non-base currency per period |
| Output | `cli.py` | Writes the four CSV files |

Steps communicate only through Pydantic models
(`CashflowRecord` → `FundAnalytics` → `HedgeTrade`), so each can be tested on its own.
Analytics and hedging do no file I/O. Full design notes are in [docs/design.md](docs/design.md).

### Calculations

- **IRR**: XIRR (via `pyxirr`) on dated cashflows.
  - *Currency IRR*: the currency's cashflows in local currency.
  - *Fund IRR*: all the fund's cashflows in base currency (EUR).
- **NAV** on a date: present value, at the currency IRR, of all cashflows on or after
  that date. By construction NAV on the first date is 0; the code checks this.
- **Hedge** on each cashflow date (except the last):
  - Notional = NAV − that day's cashflow, i.e. the value still held once the cashflow is paid.
  - Side: SELL the local currency if the notional is positive, BUY if negative.
  - Trade date = this cashflow date; maturity = the next cashflow date (a 3-month roll).
  - Notionals below 0.01 are skipped.

### Dirty data

- **Whole file rejected** if headers are wrong, missing or duplicated, there is more
  than one sheet, the file is empty, or it is not `.xlsx`.
- **Row corrected** only when there is exactly one possible fix. Each correction is
  logged with the original value. In the sample data:
  - Row 17: currency `GPB` → `GBP`
  - Rows 21 and 51: dates with extra spaces or a trailing backtick
- **Row rejected** for anything else. If any row is rejected, the report is written
  and no analytics or hedges are produced.

Result for the sample file: 126 rows read, 126 valid, 3 corrected, 0 rejected.

## Assumptions

**Data**
- Each currency within a fund is one deal, and deals do not overlap.
- Each fund has a single base currency (EUR in the sample).
- Valid currencies are EUR, GBP and USD.
- Dates are day-first (`dd/mm/yyyy`) with no time of day.
- A currency has at most one cashflow per date.
- Signs: Investment is negative; Interest and Principal Repayment are positive.
  Local and base amounts have the same sign, and are equal when the currencies match.
- Zero amounts are errors.

**Analytics and hedging**
- XIRR uses actual days / 365.
- The base currency (EUR) needs no hedge.
- Hedge ratio is 100% of NAV, rolled on each cashflow date (quarterly in the sample).
- Hedges are sized at NAV after that day's cashflow, since that cashflow has already
  been received and is no longer at risk.
- No hedge is placed on the final cashflow date, as nothing remains to hedge.
- Notionals are in the local currency. Forward rates and pricing are out of scope.
- Results are rounded to 2 decimal places.

## Tests

`uv run pytest` runs 88 tests covering ingestion, cleaning, analytics and hedging.
