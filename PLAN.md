# Acme Corp — Agentic Invoice Processing

Historical plan: current behavior, including manual review and OCR, is described
in [README.md](README.md) and [docs/RELIABILITY.md](docs/RELIABILITY.md).

## Context

Galatiq's FDE technical assessment. Acme Corp (PE-backed manufacturing) loses $2M/year to manual
invoice processing: 30% error rate, 5-day delays. Build a working multi-agent prototype that
ingests messy invoices in any format, extracts structured data, validates against a SQLite
inventory, simulates VP approval with a reflection loop, and pays or rejects with a full audit
trail.

The rubric scores functionality, code quality, agentic sophistication, shipping mindset,
presentation, above-and-beyond, and UI/UX. The JD wants someone who ships production-grade
multi-agent systems, not demos.

**Design principles driving every decision below:**

- **Fewer tools** — one package, one DB, one process. The CLI and the web UI call the same
  service layer. No second dependency tree, no npm, no sidecar.
- **Less manual work** — the pipeline runs end to end unattended; a human is never asked to
  do what a rule or an agent can do.
- **Data in one place** — a single `acme.db` is the system of record. Inventory, invoices,
  every extraction attempt, every agent event, every decision and its reasoning, the payment
  ledger, and the LangGraph checkpoints all live there. Nothing important lives in a log file.
- **Prod-ready and durable, not a thrown-together POC** — typed throughout, tested, CI'd,
  containerized, with real timeout/retry/degradation behavior.

**Explicitly out of scope:** no business-impact/ROI layer — the system stays purely operational.
No dedicated fraud agent, no batch/portfolio mode, no human-in-the-loop review queue.

## Decisions

| Decision | Choice |
|---|---|
| LLM | xAI Grok, via the OpenAI-compatible REST API at `https://api.x.ai/v1` (raw `httpx`, no SDK) |
| Orchestration | LangGraph `StateGraph` with conditional edges + `SqliteSaver` checkpointing |
| Data | One `acme.db`, full system of record |
| CLI | `python main.py --invoice_path=...` (exactly as the case specifies), `rich` live trace |
| Web | FastAPI + SSE + hand-built single-page UI, no build step |
| Hardening | Docker, GitHub Actions CI, `pydantic-settings`, JSON logs w/ run correlation ID, bounded retries, graceful degradation |
| Above-and-beyond | Eval harness + golden set over all 17 invoices |

Note: the case README's `from xai import Grok` snippet is pseudo-code — no such package exists.
xAI's real API is OpenAI-compatible, so a thin typed `httpx` client gives us full control of
timeouts, retries, and structured-output parsing with zero SDK risk. This is called out in the
repo README so the reviewer sees it was a deliberate read of their brief, not an oversight.

## What the data actually contains

Traced every invoice against the seed inventory (WidgetA 15, WidgetB 10, GadgetX 5, FakeItem 0).
This table *is* the golden set:

| Invoice | Format | Trap | Expected |
|---|---|---|---|
| 1001 | txt | clean | PAID |
| 1002 | txt | typo'd labels (`INVOCE`/`Vndr`/`Amt`), GadgetX 20 > 5, $15K | REJECTED |
| 1003 | txt | FakeItem (0 stock), $100K, due "yesterday", urgency + wire-transfer | REJECTED |
| 1004 | json | clean | PAID |
| 1004_revised | json | **same invoice number as 1004**; GadgetX 5 exactly at stock limit | PAID solo; BLOCK if 1004 already paid |
| 1005 | json | GadgetX 8 > 5, $15,225 | REJECTED |
| 1006 | csv (key-value) | clean | PAID |
| 1007 | csv (tabular) | WidgetA 20 > 15 **and** WidgetB 15 > 10 | REJECTED |
| 1008 | txt (email body) | unknown items SuperGizmo/MegaSprocket, $9,900 just under the $10K bar | REJECTED |
| 1009 | json | empty vendor, null due date, qty **-5**, negative total | REJECTED |
| 1010 | txt | WidgetA on **two lines** (8 + 4 rush) — must aggregate to 12 before stock check | PAID |
| 1011 | pdf + txt | clean | PAID |
| 1012 | pdf + txt | OCR damage: `26-Jan-2O26`, `$3,500.O0`, `Widget A`/`Gadget X` spacing, $9,975 | PAID after repair |
| 1013 | pdf + json | 8 lines aggregating to 22/18/9 vs 15/10/5; subtotal+tax = 22,512.80 but total = 22,562.80 | REJECTED |
| 1014 | xml | currency is **EUR** | PAID w/ warning |
| 1015 | csv | clean | PAID |
| 1016 | json | WidgetC not in catalog | REJECTED |

1012 is the showcase: the extraction critique loop repairs `O`→`0`, and the arithmetic then
reconciles exactly (3000 + 3500 + 3000 = 9500, +475 tax = 9975). Self-correction with a
visible before/after.

## Architecture

```
main.py                      # thin shim → acme_ap.cli (satisfies the case's exact invocation)
pyproject.toml               # deps + ruff/mypy/pytest config
Dockerfile / compose.yaml
.github/workflows/ci.yml     # ruff → mypy --strict → pytest → eval scorecard
.env.example
README.md
acme_ap/
  config.py                  # pydantic-settings: key, model, thresholds, timeouts, db path
  logging.py                 # JSON logs; run_id contextvar threaded through every agent
  models.py                  # pydantic domain types
  db/
    schema.sql               # the one DB
    migrations.py            # versioned, idempotent
    repository.py            # ONLY module that touches sqlite3
    seed.py                  # README's seed inventory + catalog aliases
  llm/
    base.py                  # LLMClient protocol: complete_structured(prompt, schema) -> T
    xai.py                   # Grok over httpx; timeout, bounded retry w/ backoff
    stub.py                  # deterministic offline provider (CI + degradation path)
    factory.py
  ingestion/readers.py       # pdf (pdfplumber) / txt / json / csv / xml → RawDocument
  agents/
    base.py                  # Agent ABC; every run emits agent_events rows
    ingestion.py             # LLM structured extraction + schema-repair loop
    validation.py            # tool-calling agent over repository
    approval.py              # VP: propose → critique → revise
    payment.py               # mock_payment + idempotency + ledger
  graph.py                   # LangGraph wiring
  service.py                 # process_invoice(path) -> RunResult — shared by CLI and API
  cli.py                     # rich live trace
  api/
    app.py                   # FastAPI routes + static mount
    static/                  # index.html, app.js, styles.css
  eval.py                    # scorecard runner
tests/
  golden/expectations.yaml   # the table above, machine-readable
  test_readers.py test_validation.py test_approval.py test_graph_e2e.py test_api.py
data/invoices/               # the provided case data
```

### The one database

Single `acme.db`, migrated at startup:

- `inventory(item PK, stock, unit_price, category)` — README's seed, extended
- `item_aliases(alias PK, canonical_item FK)` — `Widget A`, `WidgetA `, `widgeta` → `WidgetA`
- `vendors(id, name, normalized_name, first_seen_at)`
- `invoices(id, invoice_number, vendor_id, currency, total, due_date, content_hash, source_path)`
- `line_items(id, invoice_id, raw_name, canonical_item, qty, unit_price, amount)`
- `runs(id, source_path, status, started_at, finished_at, provider, model)`
- `agent_events(id, run_id, agent, seq, kind, payload_json, latency_ms)` — the SSE feed *and* the audit trail, same rows
- `validation_findings(id, run_id, code, severity, item, message, evidence_json)`
- `decisions(id, run_id, outcome, rationale, critique_rounds_json, policy_version)`
- `payments(id, run_id, idempotency_key UNIQUE, vendor, amount, currency, status, paid_at)`
- LangGraph `SqliteSaver` checkpoint tables — same file

`agent_events` doing double duty as both the live stream and the permanent audit record is the
"data in one place" principle paying off concretely: the dashboard replays history through the
exact code path that renders a live run.

### The graph

`load → extract → critique → (extract | validate) → approve → (pay | reject)`

1. **load** — reader dispatch by extension. No LLM. Emits `RawDocument`.
2. **extract** — LLM structured output → `ExtractedInvoice` with per-field provenance.
3. **critique** *(self-correction loop 1, bounded at 2 retries)* — deterministic checks:
   pydantic schema, date parseability, currency, arithmetic recomputation, canonical item
   resolution. Repairable defects (1002's typo'd labels, 1012's OCR `O`/`0`) route back to
   `extract` with the specific critique text appended. Unrepairable ones pass through as findings.
4. **validate** — tool-using agent over `repository`: `lookup_item`, `check_stock`,
   `aggregate_lines`, `recompute_totals`, `check_duplicate_invoice`. Produces
   `ValidationReport[Finding]` with severity `INFO | WARN | BLOCK`.
5. **approve** *(self-correction loop 2)* — deterministic policy gate first, then the VP agent:
   proposer emits decision + rationale → critic challenges it against policy and findings →
   proposer revises. Bounded at 2 rounds; every round persisted to `decisions.critique_rounds_json`
   so the reasoning is inspectable, not just the verdict.
6. **pay** — `mock_payment(vendor, amount)`, idempotency key = content hash, ledger row.
7. **reject** — rejection + reasoning to `decisions`.

### Approval policy (config-driven, versioned)

- Any `BLOCK` finding → reject
- `total > $10,000` → heightened scrutiny: requires zero `WARN` findings *and* explicit critic sign-off
- Non-USD currency → `WARN` (1014)
- Due date in the past or unparseable → `WARN`, or `BLOCK` when paired with urgency language (1003)
- Arithmetic mismatch > $0.01 → `BLOCK` (1013's unexplained $50)
- Invoice number already paid → `BLOCK` (1004 vs 1004_revised)

The last two are two rules inside the validation agent, not the separate dedupe/integrity
subsystem that was cut — they're near-free here and the case README explicitly asks for
mismatches to be flagged. Easy to drop if you'd rather hold the line on scope.

### Degradation

No key, or xAI unreachable after bounded retries → fall back to the stub provider with a loud
`WARN` banner in CLI, API response, and UI. The pipeline still completes and the run is still
recorded, which is also what satisfies the case's "assume no internet" requirement.

### Web UI

One `uvicorn` process. `POST /runs` (start), `GET /runs` (history), `GET /runs/{id}`,
`GET /runs/{id}/events` (SSE), `GET /inventory`. Static single-page UI:

- **Left** — invoice picker (the `data/invoices` listing) + drop zone
- **Center** — live agent trace: one card per node, reasoning text, latency, retry count, and
  an expandable before/after diff on each self-correction
- **Right** — extracted fields w/ provenance, findings table with severity chips, the VP
  decision with critique rounds expandable, payment receipt
- **History tab** — past runs and the inventory, straight out of `acme.db`

Dark, technical, monospace-accented. Deliberate rather than templated; no framework.

## Prerequisite

The xAI key isn't in the environment. Before the first live run, add it — in a Claude Code
session you can type: `! echo 'XAI_API_KEY=xai-...' >> .env`

Everything (tests, eval, CI, container) runs without it via the stub provider.

## Verification

```bash
pip install -e ".[dev]"
python -m acme_ap.db.migrations && python -m acme_ap.db.seed

python main.py --invoice_path=data/invoices/invoice_1001.txt   # → PAID
python main.py --invoice_path=data/invoices/invoice_1013.pdf   # → REJECTED, stock + $50 gap
python main.py --invoice_path=data/invoices/invoice_1012.pdf   # → PAID, shows the OCR repair
python main.py --invoice_path=data/invoices/invoice_1009.json  # → REJECTED, negative qty

pytest                    # unit + e2e, deterministic on the stub provider
ruff check . && mypy .
python -m acme_ap.eval    # scorecard over all 17: field accuracy, decision accuracy, false-pay rate

uvicorn acme_ap.api.app:app --port 8000   # dashboard; run 1012 and watch the correction land
docker compose up
```

`false-pay rate` — anything paid that the golden set says must be rejected — is the metric that
gates CI at zero. That's the number Acme actually cares about.

## Working method

Deadline is Thursday Sep 3. Three build days.

The binding constraint is not build speed — it's that every layer has to be defensible in a live
technical round. Comprehension sets the pace, not typing.

**The rule: nothing lands in the repo that can't be justified out loud.**

For each module, in order:

1. **Why first** — what the layer is for, what it looks like done badly, and the one design
   decision actually worth defending.
2. **Build it.**
3. **Capture the defense** — the choice, the alternative rejected, and the reason go into
   `DECISIONS.md` as we go. If a piece can't be justified, it gets cut rather than kept.

`DECISIONS.md` isn't overhead. It becomes the README's architecture section *and* the prep sheet
for the follow-up round — which is the round that actually decides this.

Three questions to answer cold on every layer:

- Why does this exist, and what breaks without it?
- What was the alternative, and why is it worse *here*?
- How does it fail, and how would I know?

## Sessions

Fourteen self-contained sessions. Each one ends with something that runs and something you can
explain. Stop at any boundary and the repo is still coherent.

**Sessions 1–11 are the submittable system.** 12–14 are upside. If time runs out, you ship after
11 and you ship something complete.

---

### 1 — Scaffold & config · ~45m
**Build:** `pyproject.toml`, `config.py` (pydantic-settings), `logging.py`, `.env.example`
**Understand:** config as one validated typed object, not `os.getenv` scattered across 20 files. A
run correlation ID threaded through every log line.
**Defend:** *"Why pydantic-settings over environment variables?"* · *"What does a correlation ID
get you at 3am when one invoice out of 400 failed?"*
**Done:** printing `settings` shows validated config; a bad env var fails loudly at startup.

### 2 — Domain models · ~45m
**Build:** `models.py` — `RawDocument`, `LineItem`, `ExtractedInvoice`, `Finding`,
`ValidationReport`, `ApprovalDecision`, `PaymentReceipt`, `RunState`
**Understand:** the schema is the contract between agents — and it's what makes self-correction
possible at all. You cannot ask a model to fix its output unless something can tell it what was
wrong. This session is why session 6 works.
**Defend:** *"Why typed models instead of passing dicts around?"* · *"How does this enable the
critique loop?"*
**Done:** models round-trip through JSON in a test.

### 3 — The one database · ~75m
**Build:** `schema.sql`, `migrations.py`, `repository.py`, `seed.py`
**Understand:** one system of record. `repository.py` is the only module that touches `sqlite3`,
so storage can change without touching an agent. `agent_events` is simultaneously the live SSE
feed and the permanent audit trail.
**Defend:** *"Why one DB instead of the README's inventory.db plus log files?"* · *"What does
`agent_events` buy you twice?"*
**Done:** seed runs, a query returns WidgetA = 15, migrations are re-runnable.

### 4 — Readers · ~60m
**Build:** `ingestion/readers.py` (pdf/txt/json/csv/xml) + `test_readers.py`
**Understand:** deterministic before probabilistic. Never spend an LLM call on what a parser does
perfectly and for free.
**Defend:** *"Why not just hand the raw file to the LLM?"* — cost, determinism, testability.
**Done:** `pytest tests/test_readers.py` green on all five formats.

### 5 — LLM layer · ~60m
**Build:** `llm/base.py` (Protocol), `stub.py`, `xai.py`, `factory.py`
**Understand:** the provider sits behind a Protocol, so the whole system is testable offline,
swappable between vendors, and degradable when the API is down.
**Defend:** *"Why a Protocol instead of calling the API directly?"* · *"What happens when xAI
times out?"* · *"Why didn't you use the SDK in their README?"* — it doesn't exist; their API is
OpenAI-compatible.
**Done:** stub and live provider both return a validated `ExtractedInvoice` for 1001.

### 6 — Ingestion agent + critique loop · ~90m ⭐
**Build:** `agents/ingestion.py` — extract → validate → feed the specific failure back → re-extract
**Understand:** this is the agentic core of the whole submission. Not "call the LLM twice" — the
critique message names the exact defect, so the retry is informed rather than a reroll. Bounded
at 2.
**Defend:** *"Why bounded at 2 and not 5?"* — cost, oscillation, diminishing returns · *"What
exactly goes in the critique message?"* · *"Show me it repairing 1012."*
**Done:** 1012's `$3,500.O0` → `$3,500.00`, arithmetic reconciles to $9,975, before/after visible.

### 7 — Validation agent · ~75m
**Build:** `agents/validation.py` + its repository tools
**Understand:** tool use against real data. Aggregate line items *before* checking stock, or 1010
(WidgetA on two lines, 8 + 4) passes when it shouldn't. Three severities so a warning doesn't
carry the weight of a block.
**Defend:** *"Why aggregate first?"* · *"Why three severity levels instead of pass/fail?"*
**Done:** 1010 passes, 1013 blocks on both stock and the $50 gap, 1016 flags WidgetC.

### 8 — Approval agent + reflection · ~75m ⭐
**Build:** `agents/approval.py` — deterministic gate, then propose → critique → revise
**Understand:** the policy gate runs *before* the LLM, and the LLM can never overturn a hard rule.
It reasons within the rules, not around them. This is the answer to "would you let a model
approve a $100K payment?"
**Defend:** *"Why rules before the LLM and not the other way round?"* · *"What if proposer and
critic never agree?"*
**Done:** 1003 rejected with written reasoning, 1001 approved, both critique rounds persisted.

### 9 — Payment + graph wiring · ~60m
**Build:** `agents/payment.py`, `graph.py`, `service.py`
**Understand:** LangGraph state machine with conditional edges; checkpointing so a run is
resumable; idempotency key so a retry can't double-pay.
**Defend:** *"Why a checkpointer?"* · *"Why idempotency on a mock payment?"* — because the mock
is standing in for a banking API · *"Why is `service.py` separate from `cli.py`?"*
**Done:** full graph run on 1001 → PAID, every step in `agent_events`.

### 10 — CLI · ~45m
**Build:** `cli.py` with `rich` live trace
**Done:** `python main.py --invoice_path=data/invoices/invoice_1001.txt` works exactly as the
case specifies, with a readable agent trace.

### 11 — Golden set + eval harness · ~75m ⭐
**Build:** `tests/golden/expectations.yaml`, `eval.py`
**Understand:** false-pay rate — anything paid that should have been rejected — is the only
metric Acme actually cares about. Eval runs on the stub so it's deterministic and free.
**Defend:** *"Why is false-pay the gating metric and not accuracy?"* — accuracy hides the
expensive failure · *"Why does eval run on the stub?"*
**Done:** scorecard over all 17 invoices, false-pay rate 0. **Submittable from here.**

### 12 — API + SSE · ~75m
**Build:** `api/app.py` — routes over the same `service.py` the CLI uses
**Defend:** *"Why does the API share a service layer with the CLI?"* — one code path to keep correct.

### 13 — Web UI · ~90m
**Build:** `api/static/` — invoice picker, live agent trace, findings, decision, receipt

### 14 — Docker, CI, README, DECISIONS.md · ~75m
**Build:** container, GitHub Actions, README with architecture diagram, and the decision log
assembled into your interview prep sheet.

---

### Pacing

Two build days, not three — Monday is gone.

| | Sessions | |
|---|---|---|
| **Tue Sep 1** (today) | 1 – 6 | scaffold through the critique loop; 1012 repairs itself |
| **Wed Sep 2** | 7 – 11. **Scope freeze at day's end.** | full pipeline, eval clean, submittable |
| **Thu Sep 3** | 12 – 14 if 11 landed clean; otherwise README + send | |

The lost day comes out of the UI, not the pipeline. Sessions 12–13 are now explicitly
*only if session 11 is clean by Wednesday night* — a system that provably makes correct
decisions with a rich CLI beats a prettier one that pays a fraudulent invoice.

Order of sacrifice, most expendable first: **Docker → CI → web UI → eval harness → nothing else.**

## Open items for when you return

- Repo/GitHub publishing — deferred, we'll deal with it later
- `XAI_API_KEY` needs to land in `.env` before any live run
- Confirm whether the two extra validation rules (arithmetic recompute, already-paid dedupe)
  stay in or get cut
# Historical plan

This plan predates the requested reliability work. The current implementation
includes manual review, evidence scoring, and OCR; see
[docs/RELIABILITY.md](docs/RELIABILITY.md) and [README.md](README.md).
