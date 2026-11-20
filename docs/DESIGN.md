# Fund-Level Risk Analytics & Hedge Recommendations: Design

This document covers Part 1 (schema), Part 2 (pipeline) and Part 4 (trade-offs).
Part 3 is the code in `src/fx_hedge/`, described in the [README](../README.md).

Three principles shape everything below:

1. **Projections are versioned facts, not mutable rows.** Each client upload is
   an immutable *projection version*. Analytics and hedge recommendations are
   always computed *from* a specific version, so any number on screen can be
   traced back to the file that produced it.
2. **Analytics are pure functions.** IRR/NAV/hedge sizing take inputs
   (cashflows, methodology parameters, market data snapshot) and return outputs,
   with no I/O. That makes them testable, cacheable by input hash, and safe to
   re-run.
3. **Executed trades are a separate system of record.** A recommendation is a
   proposal. A booked forward is a contract that later projections cannot rewrite.

---

## Part 1: Schema design

### Entity overview

```
Client 1─* Fund 1─* Deal *─1 Currency (local)
             │        │
             │        └─* Cashflow *─1 ProjectionVersion *─1 SourceFile
             │
             ├─1 Currency (base)
             │
             ├─* AnalyticsRun ─* IrrResult      (per fund / per currency)
             │        │      └─* NavPoint       (per deal or fund×currency, per date)
             │        │
             │        └─* HedgeRecommendation ─* HedgeTrade (proposed)
             │
             └─* HedgeBookTrade (executed; links back to the HedgeTrade it came from)
```

* **Fund** has exactly one base (reporting) currency.
* **Deal** belongs to one fund and has one local currency. A fund's
  *currency exposure* is the aggregation of its deals by local currency. The sample
  data has no deal identifier, so the code treats `(fund, currency)` as one
  deal. In production the client must supply a stable `external_deal_id`.
* **Currency** is reference data (ISO 4217 code, minor units, active flag)
  rather than a DB enum, so new currencies need no migration.
* **Cashflow** belongs to a deal *and* a projection version. The same economic
  cashflow re-sent in the next upload is a new row under the new version.
  History is never overwritten.
* **NavPoint / IrrResult** belong to an **AnalyticsRun**, which pins the exact
  inputs: projection version, methodology version (day count, NAV
  convention), and market-data snapshot.
* **HedgeTrade** (proposed) belongs to a recommendation produced by a run.
  **HedgeBookTrade** (executed) is the system of record for what is actually
  live with a counterparty.

### Table sketches (PostgreSQL)

Amounts are `NUMERIC(20,4)`, never floats, and every amount column carries an
explicit currency column next to it. All tables have `created_at`/`created_by`;
they are omitted below for brevity.

```sql
CREATE TABLE currency (
    code            CHAR(3) PRIMARY KEY,            -- ISO 4217
    minor_units     SMALLINT NOT NULL,
    is_active       BOOLEAN  NOT NULL DEFAULT TRUE
);

CREATE TABLE fund (
    fund_id         UUID PRIMARY KEY,
    client_id       UUID NOT NULL REFERENCES client,
    name            TEXT NOT NULL,
    base_currency   CHAR(3) NOT NULL REFERENCES currency,
    UNIQUE (client_id, name)
);

CREATE TABLE deal (
    deal_id           UUID PRIMARY KEY,
    fund_id           UUID NOT NULL REFERENCES fund,
    external_deal_id  TEXT NOT NULL,                -- client's identifier
    local_currency    CHAR(3) NOT NULL REFERENCES currency,
    instrument_type   TEXT,                         -- term loan, unitranche, ...
    UNIQUE (fund_id, external_deal_id)
);

CREATE TABLE source_file (
    source_file_id  UUID PRIMARY KEY,
    client_id       UUID NOT NULL,
    content_sha256  CHAR(64) NOT NULL,              -- idempotency key
    object_uri      TEXT NOT NULL,                  -- immutable raw copy (S3/GCS)
    schema_version  TEXT NOT NULL,                  -- ingestion contract version
    received_at     TIMESTAMPTZ NOT NULL,
    UNIQUE (client_id, content_sha256)
);

CREATE TABLE projection_version (
    projection_version_id UUID PRIMARY KEY,
    fund_id         UUID NOT NULL REFERENCES fund,
    source_file_id  UUID NOT NULL REFERENCES source_file,
    as_of_date      DATE NOT NULL,                  -- "projection as at"
    status          TEXT NOT NULL CHECK (status IN
                    ('validating','validated','needs_review','rejected','superseded')),
    is_current      BOOLEAN NOT NULL DEFAULT FALSE,
    supersedes_id   UUID REFERENCES projection_version,
    UNIQUE (fund_id, source_file_id)
);
CREATE UNIQUE INDEX one_current_projection ON projection_version (fund_id) WHERE is_current;

CREATE TABLE cashflow (
    cashflow_id           BIGSERIAL PRIMARY KEY,
    projection_version_id UUID NOT NULL REFERENCES projection_version,
    deal_id               UUID NOT NULL REFERENCES deal,
    source_row_ref        TEXT NOT NULL,            -- e.g. file row id, for lineage
    cashflow_date         DATE NOT NULL,
    cashflow_type         TEXT NOT NULL REFERENCES cashflow_type,  -- Investment, Interest, Principal Repayment, (Fee, PIK…)
    is_actual             BOOLEAN NOT NULL DEFAULT FALSE,          -- realised vs projected
    amount_local          NUMERIC(20,4) NOT NULL,
    local_currency        CHAR(3) NOT NULL REFERENCES currency,
    amount_base           NUMERIC(20,4),            -- as supplied by client (informational)
    base_currency         CHAR(3) REFERENCES currency,
    attributes            JSONB NOT NULL DEFAULT '{}',              -- forward-compatible extras
    UNIQUE (projection_version_id, source_row_ref)
);

CREATE TABLE data_quality_issue (
    issue_id              BIGSERIAL PRIMARY KEY,
    source_file_id        UUID NOT NULL REFERENCES source_file,
    source_row_ref        TEXT,
    field                 TEXT NOT NULL,
    severity              TEXT NOT NULL CHECK (severity IN ('corrected','warning','rejected')),
    rule_code             TEXT NOT NULL,            -- e.g. CCY_ALIAS, DATE_FORMAT, SIGN_CONVENTION
    raw_value             TEXT,
    cleaned_value         TEXT,
    message               TEXT NOT NULL,
    resolved_by / resolved_at ...                   -- human review workflow
);

CREATE TABLE analytics_run (
    run_id                UUID PRIMARY KEY,
    fund_id               UUID NOT NULL REFERENCES fund,
    projection_version_id UUID NOT NULL REFERENCES projection_version,
    methodology_version   TEXT NOT NULL,            -- e.g. 'xirr-act365f/nav-cumflow@1.2'
    market_snapshot_id    UUID,                     -- FX spot/forward curve used, if any
    inputs_hash           CHAR(64) NOT NULL UNIQUE, -- idempotency: same inputs => same run
    status                TEXT NOT NULL,
    started_at / finished_at TIMESTAMPTZ
);

CREATE TABLE irr_result (
    run_id          UUID REFERENCES analytics_run,
    scope           TEXT NOT NULL CHECK (scope IN ('fund','currency','deal')),
    currency        CHAR(3),                        -- NULL for fund scope
    deal_id         UUID,                           -- set for deal scope
    irr             NUMERIC(12,10) NOT NULL,        -- annual effective
    PRIMARY KEY (run_id, scope, currency, deal_id)  -- (use COALESCE'd unique index in practice)
);

CREATE TABLE nav_point (
    run_id          UUID NOT NULL REFERENCES analytics_run,
    fund_id         UUID NOT NULL,
    currency        CHAR(3) NOT NULL,               -- exposure currency
    deal_id         UUID,                           -- NULL = currency aggregate
    nav_date        DATE NOT NULL,
    cashflow_local  NUMERIC(20,4) NOT NULL,
    nav_local       NUMERIC(20,4) NOT NULL,         -- spec NAV: PV of flows dated >= t
    nav_ex_local    NUMERIC(20,4) NOT NULL,         -- PV of flows dated > t (hedgeable exposure)
    fx_rate         NUMERIC(18,10) NOT NULL,
    nav_base        NUMERIC(20,4) NOT NULL,
    PRIMARY KEY (run_id, currency, deal_id, nav_date)
);

CREATE TABLE hedge_recommendation (
    recommendation_id UUID PRIMARY KEY,
    run_id          UUID NOT NULL REFERENCES analytics_run,
    policy          JSONB NOT NULL,                 -- {"hedge_ratio":1.0,"tenor":"3M","instrument":"FX_FORWARD"}
    hedge_book_version BIGINT NOT NULL,             -- book state it was diffed against
    status          TEXT NOT NULL CHECK (status IN ('proposed','approved','rejected','expired','executed')),
    expires_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE hedge_trade (                          -- proposed trades
    trade_id        TEXT PRIMARY KEY,               -- deterministic hash (see code)
    recommendation_id UUID NOT NULL REFERENCES hedge_recommendation,
    fund_id         UUID NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('NEW','ROLL','INCREASE','DECREASE','UNWIND')),
    exposure_currency CHAR(3) NOT NULL,
    base_currency   CHAR(3) NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('BUY','SELL')),   -- on exposure ccy
    notional_local  NUMERIC(20,4) NOT NULL CHECK (notional_local > 0),
    trade_date      DATE NOT NULL,
    maturity_date   DATE NOT NULL CHECK (maturity_date > trade_date),
    reference_rate  NUMERIC(18,10),
    replaces_book_trade_id UUID                     -- for ROLL/INCREASE/DECREASE/UNWIND
);

CREATE TABLE hedge_book_trade (                     -- executed trades: system of record
    book_trade_id   UUID PRIMARY KEY,
    fund_id         UUID NOT NULL,
    source_trade_id TEXT REFERENCES hedge_trade,
    counterparty_id UUID NOT NULL,
    external_ref    TEXT NOT NULL,                  -- bank confirmation id
    side, exposure_currency, base_currency, notional_local, trade_date, maturity_date,
    executed_rate   NUMERIC(18,10) NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('live','matured','unwound','cancelled')),
    UNIQUE (counterparty_id, external_ref)
);
```

The Pydantic models in `src/fx_hedge/models.py` (`CashflowRecord`, `NavPoint`,
`HedgeTrade`, `DataIssue`) are the application-side contracts for the
`cashflow`, `nav_point`, `hedge_trade` and `data_quality_issue` tables.

### Key modelling decisions

| Decision | Why |
|---|---|
| Cashflows keyed by projection version, not updated in place | Hedges already executed must be explainable against the projection that sized them. You also get "what changed between v3 and v4" diffs for free. |
| `is_actual` flag on cashflows | Once a coupon is paid, the realised amount replaces the projection. IRR-since-inception mixes actuals (past) with projections (future). |
| Store both `nav_local` (spec, cum-flow) and `nav_ex_local` | The spec's NAV(0)=0 / NAV(T)=exit convention is good for reporting identities. The hedgeable exposure on a date is the value *after* that date's flow settles. Storing both avoids the UI and the hedge engine disagreeing. |
| Client-supplied `amount_base` kept as informational only | The platform's own FX source should drive base valuations. The client's figure is used for cross-validation (implied-rate check). |
| `cashflow_type` and `currency` as reference tables | New types (Fee, PIK interest, Commitment) are an `INSERT`, not a schema migration. |
| Proposed vs executed trades in separate tables | Different owners, lifecycles and audit requirements. A recommendation can be regenerated, but a booked trade can't. |

### Schema evolution

* **Ingestion contract versioning.** Every file/API payload is validated
  against a versioned Pydantic model (`schema_version` stored on
  `source_file`). Header aliases (see `ingest.HEADER_MAP`) absorb cosmetic
  changes. Unknown columns are accepted with a warning and land in
  `cashflow.attributes` (JSONB), so a client adding "Facility ID" doesn't break
  ingestion, and the field can later be promoted to a real column.
* **Raw retention.** The original file is kept immutable in object storage. If
  parsing rules improve, any version can be re-parsed and re-validated.
* **Database migrations: expand → migrate → contract.** These are additive
  first (nullable column / new table), followed by a backfill, then a switch
  of readers, then removal of the old column in a later release. Alembic
  manages them, and no destructive change ships in the same release as the
  code that stops using it.
* **Methodology versioning.** `analytics_run.methodology_version` lets you
  change the day count or NAV convention without silently changing history. Old
  runs stay reproducible, and new runs are clearly labelled.
* **API versioning.** Client-facing responses are versioned (`/v1/...`), and
  breaking changes go to a new version with a deprecation window. Internal
  events use a schema registry (JSON Schema/Avro) with backward-compatible
  evolution rules.

---

## Part 2: Pipeline design

### Stages and ownership

```
 ┌─────────────┐   file.received   ┌──────────────────┐ projection.validated ┌──────────────────┐ analytics.completed ┌──────────────────┐
 │  Landing    │ ────────────────▶ │ Ingestion &      │ ──────────────────▶ │ Analytics engine │ ──────────────────▶ │ Hedge engine     │
 │ (upload/API │                   │ validation svc   │                     │ (IRR, NAV)       │                     │ (sizing, diff vs │
 │  /SFTP)     │                   │                  │                     │                  │   market.snapshot ─▶│  live book)      │
 └─────────────┘                   └──────────────────┘                     └──────────────────┘   hedgebook.changed▶└──────────────────┘
       │ raw file (immutable)          │ cashflow, dq_issue,                    │ analytics_run,                         │ hedge_recommendation,
       ▼                               │ projection_version                     │ irr_result, nav_point                  │ hedge_trade
   object store                        ▼                                        ▼                                        ▼
                                  ┌──────────────────────────────────────────────────────────────────────────────────────────────┐
                                  │  Serving layer: read models / API (read-only over the above, tenant-scoped)                    │
                                  └──────────────────────────────────────────────────────────────────────────────────────────────┘
```

| Concern | Owns (writes) | Code in this repo |
|---|---|---|
| Landing | `source_file`, raw object | n/a |
| Ingestion & validation | `projection_version`, `cashflow`, `data_quality_issue` | `cleaning.py`, `models.py`, `ingest.py` |
| Analytics | `analytics_run`, `irr_result`, `nav_point` | `analytics.py` (pure functions) |
| Hedge generation | `hedge_recommendation`, `hedge_trade` | `hedging.py` (pure sizing) |
| Execution / hedge book | `hedge_book_trade` | n/a (OMS / bank connectivity) |
| Serving | nothing (read-only) | `cli.py` stands in for it here |

Each stage writes only its own tables and communicates through events plus IDs.
No stage reaches into another's tables to mutate them. The analytics and
hedge-sizing logic are libraries with no I/O. The services around them handle
persistence, retries and events. The same library therefore runs in a batch
worker, in a notebook for analysts, or behind a synchronous "what-if" API call.

### Triggers and dependencies

* **Event-driven (primary).** A new file (UI upload, API post, SFTP drop)
  emits `file.received`, and each stage emits the event that triggers the next.
* **Scheduled.** (a) A daily market-data snapshot triggers hedge re-pricing and
  drift checks. (b) Each roll date (T-n business days before quarter end)
  triggers generation of roll trades even if no new projection arrived.
* **Manual.** Analysts can re-run any stage for a given projection version
  (backfills, methodology change). This is safe because every stage is
  idempotent.

Implementation: an orchestrator (Dagster/Airflow/Temporal) runs a per-fund
DAG instance. Alternatively, queue consumers (SQS/Kafka + workers) can be
chained by events. Funds are independent, so they partition and parallelise
naturally. Within a fund, runs are serialised with a per-fund lock (or a Kafka
partition key = `fund_id`), so two projection versions cannot race into the
hedge engine.

Dependency rules:

```
validate(projection)        requires: source_file landed
analytics(projection)       requires: projection.status == 'validated'
hedges(run)                 requires: analytics_run.status == 'succeeded'
                                      AND market snapshot for trade date
                                      AND current hedge book version (read)
publish(recommendation)     requires: hedges succeeded; supersedes older 'proposed' recs for the fund
```

### Pseudocode

```python
# --- Ingestion worker ----------------------------------------------------------
def on_file_received(evt):
    sha = evt.content_sha256
    if source_file_exists(evt.client_id, sha):            # idempotent: duplicate upload
        return ack()
    raw_rows = read_raw_rows(evt.object_uri)              # SchemaError -> reject whole file
    result   = validate_rows(raw_rows)                    # row + dataset rules (see ingest.py)

    with db.transaction():
        sf = insert_source_file(evt, sha)
        for fund, recs in group_by_fund(result.records):
            status = 'validated'
            if rejected_rows_affect(fund, result) or reject_ratio(fund) > MAX_REJECT_RATIO:
                status = 'needs_review'                   # gate: never hedge off a partial book
            pv = insert_projection_version(fund, sf, status)
            upsert_cashflows(pv, recs)                    # unique (pv, source_row_ref)
        insert_dq_issues(sf, result.issues)
        outbox.add('projection.validated', pv.id) for pv with status=='validated'
        outbox.add('projection.needs_review', ...)  # -> notify client/ops in UI
    # outbox relay publishes after commit (no lost / phantom events)

# --- Analytics worker ----------------------------------------------------------
def on_projection_validated(evt):
    pv       = load_projection_version(evt.id)
    method   = CURRENT_METHODOLOGY                        # e.g. 'xirr-act365f/nav-cumflow@1'
    key      = sha256(pv.id, method)
    if run_exists(key): return ack()                      # idempotent re-delivery
    with fund_lock(pv.fund_id):
        cashflows = load_cashflows(pv)                    # actuals + projections
        fa = compute_fund_analytics(cashflows, pv.fund)   # pure
        with db.transaction():
            run = insert_run(pv, method, key)
            insert_irr_results(run, fa); insert_nav_points(run, fa)
            mark_current(pv)                              # flips is_current, supersedes previous
            outbox.add('analytics.completed', run.id)

# --- Hedge worker ---------------------------------------------------------------
def on_analytics_completed_or_roll_date(evt):
    run    = latest_successful_run(evt.fund_id)
    mkt    = market_snapshot(as_of=today)
    book   = hedge_book(evt.fund_id)                      # live forwards + version
    target = generate_hedges(run.cashflows, run.analytics, policy)      # pure sizing
    orders = diff(target, book, tolerance=policy.rebalance_band)        # NEW/ROLL/INCREASE/DECREASE/UNWIND
    key    = sha256(run.id, mkt.id, book.version, policy)
    if recommendation_exists(key): return ack()
    with db.transaction():
        expire_open_recommendations(evt.fund_id)
        rec = insert_recommendation(run, policy, book.version, key)
        insert_hedge_trades(rec, orders)                  # deterministic trade ids
        outbox.add('hedge.recommendation.proposed', rec.id)
```

### Dirty data handling

Rules are tiered by **how safely they can be fixed** (implemented in the code):

| Severity | Meaning | Examples in sample | Effect |
|---|---|---|---|
| **corrected** | Deterministic fix with no judgement involved; logged with raw value | `GPB → GBP` (row 17); `' 30/09/2030  00:00:00'` (row 21); `'30/09/2027  00:00:00\`'` (row 51); `"2,500,000"` / `(100)` amounts | Row kept |
| **warning** | Plausible but suspicious | Implied FX rate outlier; duplicate business key; **no coupon on the maturity date in all 6 positions** | Row kept, surfaced for review |
| **rejected** | Cannot be fixed without guessing | Unknown currency `XYZ`; ambiguous `2,5`; wrong sign; base ≠ local for same-ccy row; duplicate id | Row quarantined |
| **file-level** | Contract broken | Missing required column, unreadable file | Whole file rejected |

Guidelines:
* **Never fuzzy-match** into financial meaning. Currency fixes come from a
  reviewed alias table, and the dataset-level implied-FX-rate check then
  *confirms* each one (row 17's rate matches the GBP position exactly).
* **Never invent cashflows.** The missing final-period coupon is flagged, not
  imputed. Only the client knows whether the deal pays it.
* **Gate downstream on completeness.** If any row of a fund is rejected, that
  fund's projection goes to `needs_review` and its previous version stays
  current for hedging. Hedging off a partially loaded book is worse than
  hedging off yesterday's.
* **Close the loop.** Issues are persisted with rule codes and shown to the
  client in the UI ("we corrected 3 values, please confirm"). Recurring
  corrections can drive per-client parsing profiles (e.g. a client known to
  send month-first dates).

### Idempotency and delivery semantics

* **Inputs:** `source_file` is unique on `(client_id, content_sha256)`, so a
  re-uploaded identical file is a no-op.
* **Stages:** each stage derives a deterministic key from its inputs
  (`analytics_run.inputs_hash`, the recommendation key) and short-circuits if
  it already exists. At-least-once delivery plus idempotent consumers gives
  effectively-once results.
* **Writes:** natural-key upserts (`UNIQUE (projection_version_id,
  source_row_ref)`) and a single transaction per stage. Events go through a
  **transactional outbox**, so an event is published only if its data
  committed.
* **Trades:** `trade_id` is a hash of fund, currency, trade/maturity dates and
  projection version (`hedging.trade_id`). Re-running produces the same IDs, so
  a retry can never double-book. The execution layer additionally dedupes on
  the counterparty's confirmation reference.

---

## Part 4: Trade-offs

### Serving analytics and hedge recommendations to a client-facing web app

**Precompute, then serve read models; compute synchronously only for cheap
what-ifs.**

* **Write side vs read side.** The pipeline above writes normalised results.
  A serving layer exposes tenant-scoped read models: materialised views or
  denormalised tables such as `fund_summary(fund_id, as_of, fund_irr, nav_base,
  hedge_ratio_actual, …)` and `nav_series(fund_id, currency, date, …)`. They
  are shaped for the screens, so page loads are single indexed reads (ms)
  regardless of how expensive the analytics were.
* **API.** A stateless API service (FastAPI) sits behind the gateway:
  `GET /v1/funds/{id}/analytics?run=latest`,
  `GET /v1/funds/{id}/nav?currency=GBP`, `GET /v1/funds/{id}/hedges`,
  `POST /v1/funds/{id}/scenarios` (what-if hedge ratio/tenor). Every response
  carries `projection_version`, `run_id`, `as_of` and `methodology_version`,
  so the UI always shows provenance ("based on projections uploaded 14 Nov").
* **Freshness.** While a new run computes, users keep seeing the last good
  run with a "recalculating…" badge driven by job status via SSE/WebSocket,
  never a half-written state. `is_current` flips atomically at the end of the
  run.
* **What-if.** Sizing hedges at a different ratio or tenor is a pure function
  over already-computed NAVs (microseconds), so it runs synchronously in the
  API process using the same library, with no database writes until the user
  saves a scenario. Heavy scenarios (e.g. stochastic FX simulations) go to the
  async job queue and return a job ID.
* **Multi-tenancy and security.** Use tenant ID in every key, Postgres
  row-level security as defence in depth, OAuth/OIDC with fund-level
  permissions, and an audit log of who viewed or approved what.
  Recommendations require explicit approval (four-eyes for large notionals)
  before anything flows to execution.
* **Scale.** The data is small (hundreds of deals × ~20 dates per fund), so
  Postgres plus a cache (Redis, keyed by `run_id`, so it never goes stale) is
  plenty. The bottleneck is correctness and auditability, not throughput.
  Internal analysts query the same tables through a read replica or a
  warehouse export, so they never load the OLTP primary.

**Trade-off accepted:** precomputation adds pipeline complexity and a short lag
after upload, in exchange for fast, consistent, auditable reads. The
alternative, computing on request, is simpler but makes every page load depend
on the analytics code path and makes "what did the client see on 3 Nov?" hard
to answer.

### Projections revised mid-quarter while hedges are in flight

A revised projection changes IRR, then NAV, then the *target* notional. The
forwards already traded do not change: they are contracts. So the system must
reconcile the **target** (from the new projection) with the **actual** (the
live hedge book), rather than regenerate the hedge schedule from scratch.

1. **Version, don't overwrite.** The revision becomes projection v(n+1). The
   live forwards stay linked to the version that sized them, which preserves
   the audit trail ("hedged 99.9m on v3; v4 cut NAV to 80m").
2. **Re-anchor the analytics.** Mid-life, the IRR is computed on *actual*
   past cashflows plus *revised* future ones. The NAV is then re-based at the
   revision date, so NAV(0)=0 at inception still holds for the original deal.
   The new schedule starts from the current NAV, not from zero.
3. **Diff and apply a tolerance band.** Compute the hedge ratio gap = live notional
   / new NAV_ex for each currency, then choose:
   * **Within band** (e.g. 95–105%): do nothing until the next quarterly roll,
     which re-sizes naturally. This avoids churning trades over noise.
   * **Outside band:** propose an `INCREASE` (new forward to the *same*
     maturity as the live one, to keep the roll calendar aligned) or a
     `DECREASE` (offsetting forward, or a partial unwind with the bank).
   * **Structural change** (prepayment, default, deal sold): `UNWIND` the
     hedge. Early termination realises the forward's mark-to-market in cash,
     so liquidity has to be checked first.
   * **Date shift** (maturity extended): extend at roll rather than at
     maturity of the current forward.
4. **Cost vs tracking error.** Every adjustment costs spread and has a cash
   impact (a forward settled or unwound at a loss needs cash the fund may
   not hold). The band, minimum trade size and "wait for roll" policy are
   per-fund configuration, chosen with the client. They are not hard-coded.
5. **Concurrency.** A recommendation is generated against a specific
   `hedge_book_version` and expires. If a trade is executed (book version
   changes) or a newer projection lands before approval, the recommendation is
   invalidated and regenerated. Optimistic concurrency stops someone approving
   a proposal sized against a stale book. Per-fund serialisation in the
   pipeline prevents two revisions racing.
6. **Communication.** The UI shows "projection revised; hedge ratio now 118%
   for GBP; proposed DECREASE 18.4m to 31 Dec". The client approves, and the
   diff, the reason and the approver are logged.
