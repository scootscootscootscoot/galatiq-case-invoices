# Decision log

The current reliability update is documented in [docs/RELIABILITY.md](docs/RELIABILITY.md).
It supersedes historical exclusions below: manual review, confidence scoring,
page-level OCR, and pre-payment identity reservations are now implemented.

Every non-obvious choice in this system, the alternative that was rejected, and
why. Written as the work happened rather than reconstructed afterwards.

---

## Orchestration

### LangGraph over CrewAI or a hand-rolled state machine

The case names LangGraph first and the role's description lists it as a
nice-to-have, but the substantive reason is control flow. This pipeline needs a
cycle (extract ⇄ critique) and three conditional branches. LangGraph expresses
those as edges. CrewAI's role-based abstraction reads well in a diagram but
makes explicit retry and branch conditions awkward.

**Rejected:** a custom state machine. It would have been ~200 lines and no
dependency, and for this graph size that is a defensible choice — but it gives
up checkpointing and the resumability that comes free with it.

### The critique loop is graph edges, not a `while` loop

The ingestion agent exposes `attempt()` and `critique()` separately so the graph
owns the cycle. A `while` loop inside the agent would behave identically and be
slightly simpler.

The reason not to: the graph should be an honest picture of the control flow. If
the most interesting behaviour in the system — the self-correction — is hidden
inside a function, the topology is decoration. `IngestionAgent.run()` still
contains the loop and is used by tests, so both forms exist and stay consistent.

### Checkpoints go in the same database

`SqliteSaver` points at `acme.db` rather than its own file. One artifact holds
the data, the reasoning and the resumable state; you can copy it, inspect it, or
attach it to a ticket.

---

## Data

### One database, not `inventory.db` plus log files

The case suggests `inventory.db`. This uses a single `acme.db` holding inventory,
vendors, invoices, line items, runs, agent events, findings, decisions, payments
and checkpoints.

**Why:** "data in one place" was an explicit design principle, and the payoff is
concrete. Every question an operator asks — what did we pay this vendor, why was
this rejected, what did the agent see — is one SQL query against one file. Runs
stored as JSONL alongside a database would mean two sources of truth about the
same event, and they would eventually disagree.

**Cost:** more schema up front, and a migration story the case did not ask for.

### `agent_events` is both the live stream and the audit trail

The same rows drive the dashboard's server-sent events and the permanent record.

The alternative — an in-memory queue for live updates, a table for history — has
one obvious failure mode: the thing you watched and the thing you can replay are
produced by different code and drift. Here a run watched live and the same run
replayed a year later render through one path. It also means a run started in
one process is watchable from another, and a browser that reconnects mid-run
resumes from a sequence number.

**Cost:** the SSE endpoint polls the database at 200ms rather than being pushed
to. At this scale that is free; at ten thousand concurrent runs it would not be,
and the answer then is a real broker.

### `repository.py` is the only module that touches `sqlite3`

Agents receive a `Repository` and call methods. Swapping SQLite for Postgres is
a change to one file. Tests get isolation by constructing a `Repository` over a
temporary path — no mocking, no patching.

---

## Extraction

### Deterministic parsing before the model, not after

JSON, XML and CSV carry their own schema. A parser reads them exactly, every
time, for nothing. The model's job starts where the ambiguity starts.

This is also what makes the offline provider real rather than a mock: it is the
deterministic parser wired to the `LLMClient` Protocol, so the whole system runs
with no network and CI stays free and repeatable.

### The model returns strings; our code normalises them

`RawExtraction` types every field as `str | None`. The model transcribes; our
normalisers parse.

**Why:** asking a model for ISO dates and clean floats invites it to silently
"fix" `$3,500.O0` to `3500`, which destroys the evidence that the document was
damaged. Here the damage arrives intact, our code repairs it, and both the
before and after are visible in the trace. The system prompt says so explicitly:
*"Transcribe values verbatim, including anything that looks damaged."*

### The critique is deterministic and specific

Retries are driven by our own arithmetic, date and item checks — never by asking
the model to grade itself, which mostly produces agreement.

Each retry carries the exact defect, so the second call is informed rather than
a reroll. Bounded at three attempts: unbounded self-correction is how an agent
spends real money refusing to concede that a field is genuinely absent.

Only defects a re-read could plausibly fix are critiqued. A genuinely missing
due date is a business finding for validation, not a reason to burn three model
calls. Anything that survives the loop is passed downstream as an
`EXTRACTION_DEGRADED` finding rather than being dropped — invoice 1009 is
internally inconsistent, exhausts the loop, and says so.

---

## Validation

### Aggregate quantities before checking stock

Invoice 1010 orders WidgetA on two lines (8 + 4 rush). Invoice 1013 spreads 22
WidgetA across three lines against stock of 15. Per-line checks pass both;
per-product checks fail the second, which is right.

### No fuzzy matching on item names

The first implementation used `difflib` at 0.85 similarity. It resolved
`WidgetC` to `WidgetB` — a real product the invoice never mentioned — and
invoice 1016 would have been paid.

Item codes are identifiers, not prose: one character different means a different
product, not a typo. Resolution is now exact → curated alias → normalised form →
single-character OCR repair that must still land exactly on a catalog row.
Everything else is `UNKNOWN_ITEM`.

The asymmetry is the point. A false unknown costs a human thirty seconds; a
false match costs the invoice amount. `tests/test_repository.py` pins this.

### Three severities, not pass/fail

`INFO` records something worth seeing (an item name was repaired; the invoice is
above the scrutiny threshold). `WARN` is advisory and feeds the model's
reasoning. `BLOCK` rejects unconditionally.

Two levels would force a choice between blocking on a EUR-denominated invoice —
which is payable, just noteworthy — and staying silent about it.

### Due dates are judged against the invoice's own timeline

Comparing due dates to today would flag every historical invoice as overdue and
say nothing useful; the sample data is dated January 2026. A due date *before*
its own invoice date is different — that is impossible on its face, and it is
how invoice 1003 manufactures urgency. Past-due relative to today is `INFO`.

### Arithmetic recomputation and duplicate detection stayed in

Both were scoped out as separate subsystems. Both survived as two rules inside
the validation agent because they were nearly free there and the case explicitly
asks for mismatches to be flagged.

They earned it. Recomputation caught the $50 gap in 1013 *and* a $110
discrepancy in 1007 that I had not spotted when I read the data by hand.
Duplicate detection is what stops 1004_revised from being paid on top of 1004.

---

## Approval

### The policy gate runs before the model, and the model cannot overrule it

If validation raised anything blocking, the invoice is rejected and no model is
called. Within the rules the model reasons freely; it cannot reason around them.
A second assertion after the critique loop re-checks the same condition, because
one guard on a payment path is not enough.

This is the answer to "would you let an LLM approve a $100,000 payment?"

### Proposer and reviewer see identical facts

The same evidence packet goes to both roles; only the system prompt differs. A
reviewer with less context would rubber-stamp; with more, it would be
adjudicating on evidence the proposer never had.

### Non-convergence defaults to reject

If the reviewer has not signed off after two rounds, the invoice is held and
marked escalated. Deadlock on a payment decision resolves to not paying.

### Every round is persisted

`decisions.critique_rounds` holds the full exchange. The audit trail shows the
reasoning, not just the verdict — which is the entire justification for letting
a model near this decision at all.

---

## Payment

### Idempotency keyed on content, not invoice number

The key is `{invoice_number}:{content_hash}` with a `UNIQUE` constraint. A
re-run of the same document is the same payment and is suppressed. A genuinely
revised document has different content, so it proceeds to its own duplicate
check rather than being silently swallowed.

The case supplies a two-line `mock_payment`. It is treated as what it stands in
for — a banking API — because a retry that pays a vendor twice is the most
expensive bug this system could ship, and a mock hides exactly that failure.

---

## Providers

### A `Protocol`, not a vendor SDK

Buys three things: the system is testable with no network, a different model is
a config change, and there is a well-defined place to fall back to.

### Raw `httpx` rather than an xAI SDK

The case's `from xai import Grok` is pseudo-code; no such package exists. xAI's
API is OpenAI-compatible, so a thin typed client gives full control of timeout,
retry and parsing with no SDK to go stale. Retries are bounded, use exponential
backoff with jitter, and only fire on genuinely transient status codes — a 400
means the request was wrong and retrying it spends money to fail identically.

### Degradation over failure

No key or an unreachable API falls back to the deterministic provider with a
loud warning. Losing the reasoning model should cost you nuance, not the audit
trail.

---

## Interface

### FastAPI + SSE with a hand-built page, not Streamlit or React

One `uvicorn` process serves the API and the UI. No npm, no build step, no
second dependency tree inside a Python submission.

**Rejected — Streamlit:** re-runs the whole script on every interaction, which
fights a live agent trace, and it looks like every other Streamlit app.
**Rejected — React/Vite:** a higher ceiling, but a build step and a toolchain
that rots.

### The CLI and the API share `service.py`

One code path through the business logic. The terminal and the browser cannot
disagree about what the system decided.

### The API refuses paths outside the invoice directory

It accepts a path from the network, so it resolves and containment-checks every
one. Tested against traversal attempts.

---

### Heuristic parsers are split by input shape

The original heuristic module is now a package with four narrow responsibilities. `scalars.py` owns money, quantity, currency, and date normalisation; `structured.py` owns JSON, XML, flattened key/value, and wide-table parsing; `text.py` owns labelled free-text rows; and `schema.py` owns the model-facing `RawExtraction` conversion. `__init__.py` remains the public dispatch surface.

The boundary is deliberate: structured formats are attempted first, free-text parsing remains the final fallback, and all parser families share the same scalar repair rules. Keeping those responsibilities separate makes the retry/critique behavior easier to audit and keeps complexity and coverage changes localized to the format family being changed.
## Evaluation

### False-pay rate is the gating metric

Accuracy averages a cheap error with an expensive one. Wrongly holding a good
invoice costs a clerk five minutes; wrongly paying a fraudulent one costs the
invoice amount and is not recoverable. They should not share a score. CI fails
on a single false pay.

### The golden set is the specification

`tests/golden/expectations.yaml` was written by reading each document against
the seed inventory and deciding what a competent clerk should conclude — then
the pipeline was built to match it. A test asserts that no sample file is
missing from it, so a new document cannot quietly go unasserted.

### Each invoice is scored in its own database

Duplicate detection is stateful, so a shared database would make results depend
on execution order. The 1004 duplicate scenario is asserted separately and
deliberately, in `test_pipeline.py`.

---

## Known limits

Honest about what this does not do.

- **Stock is never decremented.** Validation reads availability; it does not
  reserve or consume it. Two invoices for the same 15 WidgetA both pass. Real
  use needs a reservation ledger.
- **No FX conversion.** A EUR invoice is flagged and paid at face value.
- **The offline provider is a parser, not a reasoner.** It mirrors the written
  policy exactly and abstains where a live model would weigh softer signals.
  This keeps the degraded path conservative rather than confidently wrong, but
  it is a floor, not a substitute.
- **No human-in-the-loop queue.** Rejections are terminal. The checkpointer is
  in place, so pausing mid-graph for a human decision is a small addition.
- **Vendor identity is name-normalised only.** "QuickShip Distributers
  (formerly FastShip Ltd.)" is one vendor to this system; no registry lookup.
- **Single-node SQLite.** Correct here, and the repository boundary is where
  that changes.
# Reliability update — policy 2026.09.2

The confidence gate, manual review, page-level OCR, and pre-payment reservation
are documented in [docs/RELIABILITY.md](docs/RELIABILITY.md). This update
supersedes historical scope exclusions and claims below about a missing review
queue, text-only PDF behavior, and automatic fallback during live API outages.
The current README and verification report describe the shipped behavior.
