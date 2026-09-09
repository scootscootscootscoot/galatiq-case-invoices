# Understanding the reliability work

The business question is: **what evidence lets us move from “a model returned
some numbers” to “this invoice is safe to pay”?** This version adds an explicit
answer and a recovery path when the evidence is weak.

## The walkthrough to give in an interview

“The pipeline reads five formats into a common invoice schema. Agents can
extract and propose an approval, but deterministic checks control payment.
I compare the extracted fields with recognized source fields, check arithmetic
and inventory, and hold uncertain documents for a human. Corrections create a
new run and pass through the same business rules. Every step is auditable in one
SQLite database, and a payment reservation prevents concurrent duplicate calls.”

Then demonstrate three cases:

1. `invoice_1011.pdf`: readable source, matching fields, sufficient stock → paid.
2. `invoice_1006.csv`: the currency is absent → review required, with an 85/100
   evidence score below the 90 threshold. Verify the missing information, record
   the verification, and reprocess.
3. `invoice_1013.pdf`: products repeat across lines, collectively exceed stock,
   and the total contains an unexplained $50 → rejected.

The [video](demo.mp4) also demonstrates an actual image-only PDF, OCR,
duplicate-invoice protection, and the history view.

## What was wrong before

| Gap | Consequence | Change |
|---|---|---|
| Arithmetic checked only the extracted data | A fabricated vendor and internally consistent fabricated total could look valid | Compare payment fields with recognized source fields before payment |
| No confidence gate or review queue | Uncertain extraction had no clear operator workflow | Persistent alerts, a threshold, and correction/revalidation |
| PDF text and table extraction were concatenated | The same printed row could appear twice | One text representation per page; table count is metadata |
| Text parsing deduplicated identical rows | Two legitimate charges could become one | Preserve row multiplicity; aggregate only for stock validation |
| Only text-layer PDFs were readable | Image-only or partially unreadable PDFs could not be handled honestly | Page-level OCR and explicit missing-page evidence |
| Currency silently defaulted to USD | Currency-free CSVs were paid under an unverified assumption | Defaulted currency scores 0.85 and requires review at the default threshold |
| A line total could contradict quantity × price without a final hard block | A consistent headline total could conceal a bad row | Enforce line arithmetic in final business validation |
| Payment ledger uniqueness ran after the payment call | Concurrent requests could both call the payment function | Reserve invoice identity transactionally before the call |
| Evaluation allowed a correct decision with incorrect fields | Field errors could pass the gate | Any field failure now fails evaluation; an empty evaluation also fails |
| Checkpoints ignored the repository's isolated database | Tests could write checkpoints to the operator database | Use the repository's actual database and close the connection |
| Startup reseeding replaced inventory rows | Operator changes could be lost | Insert missing reference rows without overwriting existing values |

Money multiplication, summation, and tolerance comparisons now use decimal
arithmetic. For example, a line priced at 2.675 rounds to 2.68 with half-up
rounding. The JSON and SQLite schema still expose numeric amounts; this is not
a general multicurrency accounting engine. Dashboard totals remain separated
by currency, with no invented exchange rate.

## How to read a confidence score

`EXTRACTION_CONFIDENCE_THRESHOLD=0.90` is a policy setting. The score is a
deterministic evidence rating, **not** a statistically calibrated chance of
being correct. Calling 99/100 “99% accurate” would misrepresent the system.

| Score or condition | Meaning |
|---|---|
| 0.99 | The value matches a recognized source field |
| 0.92 | The value matches after a known OCR-character normalization |
| 0.85 for currency | A default was used without a source currency code/symbol |
| 0.40 | The extracted value cannot be corroborated by the source parser |
| 0.00 | A required value is missing, a value disagrees, or line counts disagree |
| Structural ambiguity | Score capped at 0.65; always requires review |

The invoice score is the **minimum** of its checked fields. It is not an
average: nineteen strong fields cannot hide one weak payment total. Structural
issues trigger review even if the threshold is lowered. Examples include OCR,
an unreadable page, a conflicting total, an ambiguous slash date, duplicate JSON
keys, and some unrecognized or dropped rows.

Each report stores the checked field path, extracted and corroborated values,
reason, and an excerpt/page where located. Page references refer to the PDF page,
not a bounding box. CSV/XML evidence comes from a flattened representation.
The original file can be opened, and a SHA-256 check refuses a replacement file
that no longer matches the captured document. Saved text remains available.

Changing the threshold is a business policy change. It should be evaluated
against representative invoices and the cost of false payments and needless
holds. The supplied examples are too small to select an optimal threshold.

## Reading PDFs honestly

Native PDFs are read page by page using pdfplumber. Table extraction is not
appended to the text, because that would duplicate charges. Text remains in page
order and each page records its reading method.

Pages with very little text are rendered at a bounded resolution and passed to
local Tesseract. OCR calls have a timeout and a PDF has a page limit. A large
image on a text-bearing page triggers review because its content may be missing
from the text layer. Missing OCR, failed rendering, or unreadable pages are
explicit issues; another readable page cannot hide them.

All actual OCR output currently requires human verification. This is a deliberate
conservative policy while the project has no broad scan benchmark. The supplied
`1012.pdf` contains damaged characters **in a text layer**; it is different from
the genuinely image-only PDF generated and read during the demo.

The deterministic parser supports the case's layouts and common label variants.
Unfamiliar tables, merged cells, complex attachments, non-English documents, and
unusual number conventions may need manual transcription. A source verifier
using the same parser as the offline extractor has correlated blind spots.
The adversarial tests reduce known failures; they do not prove universal accuracy.

## What happens during review

An uncertain run gets one durable `reviews` row, a `review_required` event, a
dashboard banner, and a queue entry. The alert persists across browser reloads
and server restarts. A definite business failure can remain `REJECTED` while
also having an extraction review alert. An unreadable/corrupt file becomes
`FAILED` with an alert. A business-valid but uncertain extraction becomes
`REVIEW_REQUIRED`.

The reviewer opens the original and captured text, checks every line and
payment field, and provides a name, note, and explicit verification. If currency
is absent, the original alone cannot establish it: confirmation must come from
the supplier or another trusted business record. The demo simulates that step
and says so in its note.

An atomic update claims the review. Two reviewers cannot resolve the same alert
simultaneously. Corrections create a new run using the saved source snapshot.
The original machine extraction and score are immutable. A before/after event
records the correction and reviewer, and both runs are linked. The machine
score is not inflated after human verification; the new report explicitly marks
that verification.

The corrected invoice still has to satisfy completeness, line arithmetic,
subtotal/total, product resolution, stock, duplicate, and approval rules.
Human verification grants no bypass of those rules. Closing an alert without
payment is a separate action requiring a note. A file that never produced a
readable snapshot needs a readable replacement upload.

## Payment guarantees—and their limits

The payment ledger retains its unique content-based key. A new `payment_claims`
table additionally reserves the invoice-number identity inside a SQLite write
transaction **before** calling `mock_payment`. This protects simultaneous runs,
including different revisions with the same invoice number. A suppressed call
does not produce a `PAID` run.

The prototype follows the original case's global invoice-number namespace.
Real suppliers can reuse numbers; a production system should key identity by a
trusted vendor/account identity plus invoice number, not the vendor display name.

If a process crashes after a reservation, the reservation remains. That is
conservative: reconcile it rather than guessing whether a payment happened.
This is not exactly-once delivery to a real bank. Production payment integration
needs a provider idempotency key, durable outbox, a receipt/reconciliation state
machine, and recovery tooling. The inventory is a mock reference catalog; this
system does not decrement warehouse stock or perform purchase-order matching.

## What the evidence demonstrates

The 20-document golden set now expects nine payments, nine rejections, and two
reviews. The review changes are specifications of the new currency policy, not
claims of newly improved OCR accuracy. The scorecard checks a selected set of
fields, quantities, and finding codes—not every character on every invoice.

Additional tests inject a coherent invented extraction, retain repeated rows,
check partial and image-only PDFs, verify concurrent payment suppression, and
exercise upload → alert → correction → revalidation over HTTP. The recorded
browser test checks the same flow in Chromium, verifies expected outcomes,
checks for JavaScript errors, and opens the review queue at a mobile viewport.

See [VERIFICATION.md](VERIFICATION.md) for the final results and commands.
The original 150 tests were retained, with expectations changed where the new
policy intentionally changed behavior.

## Before using this with a real business

The implemented alert delivery is in-app: database, API, dashboard banner, and
SSE. No emails, Slack messages, or webhooks are sent. External notifications
would need a durable outbox, retry/acknowledgment behavior, routing, and an SLA.

The API is intended for a trusted local operator. Add authentication, roles,
authenticated reviewer identity, attachment security, retention policies, and
secret management before exposing it. Background runs use process-local
threads; a crash can leave a run or claimed review in progress. A durable worker
and restart reconciliation are necessary for unattended production operation.
Header summary counters currently cover the latest 500 runs.

No live xAI request was needed for the verified offline workflows. Provider
failure is visible and fails closed; the current system does not dynamically
switch a failing live run into offline approval. Structured API client behavior
is tested with simulated responses, not a claim about current model availability.

The next accuracy investment should be a representative, independently annotated
corpus: multiple suppliers and layouts, rotated/blurred scans, locale and
currency variants, and exact per-field/line labels. Measure automatic-payment
precision, false-pay rate, review recall, field error rates, and review workload
separately, then calibrate the evidence policy against those results.
