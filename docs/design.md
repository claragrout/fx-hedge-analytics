## Part 1:

Please see below for the data model definitions for the cashflow, NAV schedule, and FX hedge trade entities. Cashlflow record is extracted from the input data, with each row in the data representing a cashflow record. This forms a collection of records. The records present different funds, each fund will hold multiple deals, and each deal within a fund has a different local currency. Additionally, there are no overlapping deals. 

Over time we may need the following:
- Deal identification number as there may be mulitple deals within a fund with the same local currency. This would mean we could generate a NAV schedule per deal
- New Cashflow types like arrangement or commitment
- A different hedge policy as the brief fixes 100% of NAV and 3-month rolls, this could change
- Once trades are actually placed, we could add an exectution rate to HedgeTrade

class CashflowType(str, Enum):
    INVESTMENT = "Investment"
    INTEREST = "Interest"
    PRINCIPAL_REPAYMENT = "Principal Repayment"


class TradeSide(str, Enum):
    SELL = "SELL"
    BUY = "BUY"


class CashflowRecord(BaseModel):
    """One projected cashflow: one row of the client file."""
    id: int                          # row ID from the file (> 0)
    fund_name: str                   # e.g. "Fund I"
    date: dt.date                    # date the cashflow is paid
    cashflow_type: CashflowType      # Investment, Interest or Principal Repayment
    local_currency: str              # currency of the deal: EUR, GBP or USD
    cashflow_amount_local: float     # amount in the local currency (investment negative)
    cashflow_amount_base: float      # same amount in the fund's base currency
    base_currency: str               # the fund's reporting currency: EUR


class NavPoint(BaseModel):
    """NAV on one date of a currency's schedule."""
    nav_date: dt.date                # a cashflow date
    cashflow_local: float            # cashflow paid on nav_date
    nav_local: float                 # PV at the currency IRR of cashflows on or after nav_date


class CurrencyAnalytics(BaseModel):
    """The NAV schedule for one fund in one currency, with the IRR it is discounted at."""
    currency: str                    # the local currency of the schedule
    irr: float                       # currency-level IRR (annual)
    nav_schedule: list[NavPoint]     # one NavPoint per cashflow date


class FundAnalytics(BaseModel):
    """All analytics for one fund."""
    fund_name: str
    base_currency: str                    # the fund's reporting currency: EUR
    fund_irr: float                       # IRR of all the fund's cashflows in the base currency
    currencies: list[CurrencyAnalytics]   # one NAV schedule (and IRR) per currency


class HedgeTrade(BaseModel):
    """One recommended FX forward at 100% of NAV."""
    fund_name: str
    exposure_currency: str           # currency being hedged: GBP or USD
    base_currency: str               # currency received: EUR
    side: TradeSide                  # direction on the exposure currency
    notional_local: float            # amount of the exposure currency (>= 0)
    trade_date: dt.date              # when the forward is agreed
    maturity_date: dt.date           # when it settles (after trade_date)


## Part 2: Pipeline design
 
### The pipeline as built
 
The prototype runs as one command per file and fund
(`python -m fx_hedge <file> --fund "Fund I" --out output`). Each step is a function
whose output is the next step's input:
 
```
cashflow file (.xlsx )
   │
   ▼  1. Read            ingest.read_raw_rows        → raw rows (one dict per row)
   │                     checks headers and sheets; a bad file stops here (SchemaError)
   ▼  2. Validate        ingest.validate_rows        → CashflowRecords + DataIssues
   │                     corrects known errors, rejects rows it can't trust
   ▼  3. Analytics       analytics.compute_fund_analytics → FundAnalytics
   │                     fund IRR, currency IRRs, NAV schedule per currency
   ▼  4. Hedges          hedging.generate_hedges     → HedgeTrades
   │                     one 3-month forward per non-base currency per roll date
   ▼  5. Output          cli.main                    → 4 CSV files
                         1_ingestion_report, 2_irr, 3_nav, 4_hedges
```
 
### Trigger mechanism and step dependencies
 
**Dependencies.** The steps form a straight line: each needs the previous one's output
and nothing else.
 
| Step | Needs | Produces |
|---|---|---|
| Read | a file | raw rows |
| Validate | raw rows | valid `CashflowRecord`s, `DataIssue`s |
| Analytics | valid records for one fund | `FundAnalytics` (IRRs and NAV schedules) |
| Hedges | `FundAnalytics` | `HedgeTrade`s |
| Output | all of the above | report and result files |
 
Read and Validate are linked by a generator, so rows are read and validated one at a
time and the file is never held in memory twice.
 
**Stopping points.** Two conditions stop the pipeline before analytics:
- **A bad file** (wrong or missing columns, more than one sheet, empty, unsupported
  type) raises `SchemaError` during reading, so nothing further runs. This is already
  how the prototype behaves.
- **Rejected rows.**If any row is rejected, the
  pipeline stops after Validate, sends the ingestion report back to the client, and
  produces no analytics or hedges until a corrected file arrives. Corrections alone
  (e.g. `GPB` → `GBP`) don't stop it, only rejected.
**Triggers.**
- **Prototype:** run manually from the command line.
- **Production:** the pipeline runs **when a new cashflow file arrives** (uploaded by
  the client), since a new projection is the only thing that changes the results. It
  can also be **re-run on demand** for a file already received, e.g. after a code
  fix. Because the steps are repeatable (see *Idempotency*), a re-run is always safe.
**Funds are independent.** Analytics and hedges are calculated per fund
(`compute_fund_analytics(records, fund_name)`), so each fund's results depend only on
its own cashflows.
 
### Where each piece of logic lives
 
Each concern is in its own module, and the modules communicate only through the
Pydantic models.
 
| Concern | Module | Responsible for |
|---|---|---|
| Ingestion | `ingest.py`, `cleaning.py`, `CashflowRecord` | reading files; checking headers; correcting known errors; rejecting bad rows; logging `DataIssue`s |
| Analytics | `analytics.py` | IRRs (via `pyxirr`) and NAV schedules from validated records |
| Hedging | `hedging.py` | turning NAV schedules into forward trades |
| Output / serving | `cli.py` | arguments; running the steps in order; writing the CSV files |
 
The models between them act as the contract:
`CashflowRecord` → `FundAnalytics` → `HedgeTrade`.
 
Benefits of this separation:
- **Each layer can be tested on its own,** as the tests do: the cleaning tests need no
  analytics, and the analytics and hedging tests start from validated records.
- **Analytics and hedging are pure functions.** They do no file reading or writing, so
  the same code could serve a command line, a scheduled job or a web API.
- **The output can change without touching the calculations.** Replacing the CSV files
  with database tables or an API changes only `cli.py` (or whatever replaces it).
 
### Dirty data handling
 
Problems are handled at two levels.
 
**File level: the whole file is rejected** (`SchemaError`) if:
- its headers are not exactly the expected eight (missing, extra or duplicated columns)
- an Excel file has more than one worksheet
- it is empty, or not `.xlsx`
**Row level: each row is corrected, rejected, or accepted as is.**
 
| Outcome | When | Effect |
|---|---|---|
| **Corrected** | a known error with only one possible fix | row kept; logged with the original value |
| **Rejected** | anything that fails validation | program stops; error on the row logged |
 
The corrections are limited to the errors found in the data:
- the currency typo `GPB` → `GBP` (row 17)
- dates with extra spaces, seconds or a trailing backtick, normalised to a plain date
  (rows 21 and 51)
Everything else is rejected rather than guessed.
 
Principles:
- **Never fix silently.** Every correction is recorded as a `DataIssue` with the value
  as it appeared in the file.
- **Never guess.** Only errors with a single certain fix are corrected.
- **Report everything at once.** All errors on a row are collected, so the client can
  fix them in one pass.
The ingestion report (`1_fund_i_ingestion_report.csv`) lists every corrected and
rejected row. For the sample: 126 rows read, 126 valid, 3 corrected, 0 rejected.
 
### Idempotency
 
**Idempotent** means running the pipeline again on the same input gives the same
result and creates nothing extra.
 
**In the prototype:**
- every step is deterministic: the same file always produces the same records, IRRs,
  NAVs and hedges (no random values or timestamps)
- output files are overwritten, not appended to, so a re-run replaces the previous
  output rather than duplicating it
### Pseudocode
 main(path, fund, out)                                        # cli.py
    result = load_cashflows(path)
    analytics = compute_fund_analytics(result.records, fund)
    hedges = generate_hedges(analytics)
    write 1_<fund>_ingestion_report.csv   from result.issues (corrected and rejected rows)
    write 2_<fund>_irr.csv                fund IRR, then one IRR per currency
    write 3_<fund>_nav.csv                NAV schedule per currency
    write 4_<fund>_hedges.csv             hedge trades


load_cashflows(path)                                         # ingest.py
    return validate_rows(read_raw_rows(path))


read_raw_rows(path)                                          # ingest.py (generator)
    if path is .xlsx:
        require exactly one worksheet            (otherwise SchemaError)
        header = first row                       (empty sheet -> SchemaError)
    else:
        SchemaError("unsupported file type")
    fields = check_headers(header)
    for each remaining row:
        yield {field: value for field, value in zip(fields, row)}


check_headers(header)                                        # ingest.py
    if a header is duplicated, missing or not in EXPECTED_HEADERS:
        SchemaError
    return each header lower-cased with spaces as underscores
        e.g. "Cashflow Amount Local" -> "cashflow_amount_local"


validate_rows(rows)                                          # ingest.py
    for each raw row:
        rows_read += 1
        row_id = raw "id" if it is a positive integer, else None
        try:
            record = CashflowRecord.model_validate(raw)
                date:      parse_date      -> strip backtick, collapse spaces,
                                              read dd/mm/yyyy [HH:MM[:SS]], must be midnight
                currency:  parse_currency  -> EUR/GBP/USD as is; "GPB" -> "GBP"
                (any correction is logged as a CORRECTED issue)
                business rules: no zero amounts; investment negative, others positive;
                                local and base same sign; equal if same currency
        except ValidationError:
            log every error on the row as a REJECTED issue
            skip the row
        keep record


compute_fund_analytics(records, fund)                        # analytics.py
    fund_records = records belonging to fund       (none -> error)
    base = the fund's single base currency         (more than one -> error)
    for each currency in the fund, in alphabetical order:
        rows = the fund's cashflows in that currency
        irr = xirr([(date, cashflow_amount_local) for each row])
        schedule = nav_schedule(rows, irr)
    fund_irr = xirr([(date, cashflow_amount_base) for each fund record])
    return FundAnalytics(fund, base, fund_irr, [currency, irr, schedule for each currency])


xirr(flows)                                                  # analytics.py
    rate = pyxirr.xirr(dates, amounts)
    invalid cashflows or no solution -> IRRError
    return rate


nav_schedule(rows, irr)                                      # analytics.py
    flows = (date, cashflow_amount_local) for each row, sorted by date
    if any date repeats: error
    for each (date, cashflow) in flows:
        nav = present value at irr, on date, of all flows dated on or after date
              (nav_at, via pyxirr.xnpv), rounded to 2 dp
        if date is the first date and nav != 0: error    # the brief's NAV(0) = 0 check
        add NavPoint(date, cashflow, nav)
    return the NavPoints


generate_hedges(analytics)                                   # hedging.py
    for each currency except the base currency:
        for each consecutive pair of NAV points (point, next_point):
            exposure = point.nav_local - point.cashflow_local   # value still held after today's cashflow
            if abs(exposure) < 0.01: skip
            side = SELL if exposure > 0 else BUY
            add HedgeTrade(fund, currency, base, side, abs(exposure),
                           trade_date = point date, maturity_date = next_point date)
    return trades sorted by currency, then trade date


## Part 4

### Architecting the backend to serve analytics and hedge recommendations to a client facing web-application.

```text
Client browser
   │
   ▼
Route 53
   │
   ▼
CloudFront (Web app stored in S3)
   │
   ▼
API Gateway ── Cognito (sign-in, per-fund access)
   │
   ▼
Backend API service (Lambda) ──► RDS PostgreSQL (cashflows, IRRs, NAVs, hedges, issues)

Client uploads file ──► S3 (raw files) ──► Eventbridge event ──► Run API on container (Lambda)
                                                     runs fx_hedge: ingest → analytics → hedges
```

User requests frontend using route 53 domain name. They authenticate with cognito and are redirected to the frontend. User retrieves presigned URL from backend in GET request and uploads fund data to the presigned url. Once the file has finished uploading, this adds to an SQS queue, 

Tradeoffs

Eventbridge & ECS task vs Lambda
Gains:
- Simple setup: S3 triggers lambda directly
- No servers to maintain, AWS starts the lambda and stops afterwards.
- Automatic scaling: many uploads result in parallel runs
- You only pay for invocations, whereas with ECS you pay for every hour that the container is running.

Downside of Lambda:
- Limits on time, CPU and memory. If calculations get more complicated or more data needs to be processed at once, we would need to move to something like ECS and Eventbridge
- 15 mins cold start, however starting large containers can take a while.
- A burst of requests can start hundreds of environments, each wanting its own database connection. RDS Proxy pools those connections. With low traffic client app, it's unlikely to be a problem at first.

API Gateway vs ALB
Gains:
- Verifies cognito sign-in token for each request
- Capping how many requests are accepted, preventing DoS attack
- Usage per client for REST API only
- Request validation - rejecting malformed requests before they reach backend
- Caching - storing responses for time, so repeated requests have lower latency

Downsides of API Gateway:
- Payload size are capped
- Requests must complete in 29 seconds - longer requests allowed in ALB however with presigned url for file upload to S3, this isn't something we should be concerned about.
- ALB can perform health checks of containers. Seeing as we're using a Lambda this isn't concerning.

### Changes to design if cashflow projections are revised mid-quarter and hedges are already in flight

1. Upload: the client's updated data file is saved next to the original, the pipeline stores it as a new version.
2. Recalculate: the pipeline reruns the calculations on the new figures and works out how much foreign currency the fund now holds, as of the day the update arrived.
3. Compare: a new step compares that amount with the currency trades already agreed, which are saved in the database:
- small difference: do nothing now; the next quarterly trade uses the updated amount
- large difference: suggest an extra trade to make up the shortfall, or one to cancel out the excess
- major change (e.g. a loan repaid early): suggest ending the existing trade early
4. Future trades: recalculated only for the quarters after the trades already agreed.
5. Web app shows the difference and suggests trades.