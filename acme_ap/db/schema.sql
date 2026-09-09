-- Acme AP: single system of record.
--
-- Inventory, invoices, agent telemetry, decisions and the payment ledger all
-- live in one file. The LangGraph checkpointer writes its own tables here too,
-- so a run's full history -- data, reasoning and resumable state -- is one
-- artifact you can copy, inspect or attach to a ticket.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- --------------------------------------------------------------------------
-- Reference data
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS inventory (
    item        TEXT PRIMARY KEY,
    stock       INTEGER NOT NULL CHECK (stock >= 0),
    unit_price  REAL,
    category    TEXT
);

-- Documents spell items in ways a database never will: "Widget A", "widgeta",
-- "WidgetA ". Aliases keep that messiness in data rather than in agent prompts.
CREATE TABLE IF NOT EXISTS item_aliases (
    alias           TEXT PRIMARY KEY,
    canonical_item  TEXT NOT NULL REFERENCES inventory(item) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS vendors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    normalized_name  TEXT NOT NULL UNIQUE,
    first_seen_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- --------------------------------------------------------------------------
-- Runs and telemetry
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS runs (
    id            TEXT PRIMARY KEY,
    source_path   TEXT NOT NULL,
    content_hash  TEXT,
    status        TEXT NOT NULL,
    provider      TEXT,
    model         TEXT,
    degraded      INTEGER NOT NULL DEFAULT 0,
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT,
    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);

-- Doing double duty: this table is the live event stream the dashboard renders
-- and the permanent audit trail an auditor reads six months later. One writer,
-- one schema, no chance of the two disagreeing.
CREATE TABLE IF NOT EXISTS agent_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    agent       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    message     TEXT,
    payload     TEXT,
    latency_ms  INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_run ON agent_events(run_id, seq);

-- --------------------------------------------------------------------------
-- Invoice data
-- --------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    invoice_number  TEXT,
    vendor_id       INTEGER REFERENCES vendors(id),
    vendor_name     TEXT,
    invoice_date    TEXT,
    due_date        TEXT,
    currency        TEXT NOT NULL DEFAULT 'USD',
    subtotal        REAL,
    tax_amount      REAL,
    total           REAL,
    payment_terms   TEXT,
    content_hash    TEXT,
    source_path     TEXT
);

CREATE INDEX IF NOT EXISTS idx_invoices_number ON invoices(invoice_number);

CREATE TABLE IF NOT EXISTS line_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id      INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    raw_name        TEXT NOT NULL,
    canonical_item  TEXT,
    quantity        REAL,
    unit_price      REAL,
    amount          REAL,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS validation_findings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    code        TEXT NOT NULL,
    severity    TEXT NOT NULL,
    item        TEXT,
    message     TEXT NOT NULL,
    evidence    TEXT
);

CREATE INDEX IF NOT EXISTS idx_findings_run ON validation_findings(run_id);

CREATE TABLE IF NOT EXISTS decisions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    outcome          TEXT NOT NULL,
    approved         INTEGER NOT NULL,
    rationale        TEXT NOT NULL,
    critique_rounds  TEXT,
    policy_version   TEXT,
    hard_gate        TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- --------------------------------------------------------------------------
-- Money
-- --------------------------------------------------------------------------

-- The UNIQUE idempotency key is the entire point of this table. The mock payment
-- function stands in for a banking API, and a retry that double-pays a vendor is
-- the most expensive bug this system could ship.
CREATE TABLE IF NOT EXISTS payments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    idempotency_key  TEXT NOT NULL UNIQUE,
    vendor           TEXT NOT NULL,
    amount           REAL NOT NULL,
    currency         TEXT NOT NULL,
    status           TEXT NOT NULL,
    detail           TEXT,
    paid_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Source snapshots and quality survive file replacement and process restarts.
CREATE TABLE IF NOT EXISTS extraction_records (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    document_json TEXT NOT NULL,
    invoice_json TEXT NOT NULL,
    quality_json TEXT NOT NULL
);

-- One durable alert per run; resolving it never edits the original run.
CREATE TABLE IF NOT EXISTS reviews (
    run_id TEXT PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK(status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'DISMISSED')),
    reasons_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT,
    reviewer TEXT,
    note TEXT,
    corrected_json TEXT,
    resolution_run_id TEXT REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS idx_reviews_status ON reviews(status, created_at);

-- Reserve business identity BEFORE invoking the mock payment, across processes.
CREATE TABLE IF NOT EXISTS payment_claims (
    business_key TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
