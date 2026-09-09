/* Acme AP dashboard.
 *
 * No framework and no build step. The page consumes the same API the CLI wraps,
 * and renders the agent trace live from server-sent events.
 */

const $ = (id) => document.getElementById(id);
const money = (v, ccy = "USD") =>
  v === null || v === undefined ? "—" : `${ccy} ${Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let currentStream = null;
let currentRunId = null;
let runGeneration = 0;
let reviewRunId = null;
let reviewInvoice = null;

function notice(message) {
  $("notice").textContent = message;
  $("notice").classList.toggle("hidden", !message);
}

async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail));
  return data;
}

window.addEventListener("unhandledrejection", (event) => notice(event.reason?.message || "The request failed. Please retry."));

/* ------------------------------------------------------------------ tabs */

const views = { process: "view-process", history: "view-history", inventory: "view-inventory", review: "view-review" };
for (const name of Object.keys(views)) {
  $(`tab-${name}`).addEventListener("click", () => {
    for (const [other, id] of Object.entries(views)) {
      $(id).classList.toggle("hidden", other !== name);
      $(`tab-${other}`).setAttribute("aria-selected", String(other === name));
    }
    if (name === "history") loadHistory();
    if (name === "inventory") loadInventory();
    if (name === "review") loadReviews();
    $("review-workspace").classList.add("hidden");
  });
}

/* ---------------------------------------------------------------- header */

async function loadHealth() {
  const h = await (await fetch("/api/health")).json();
  const badge = $("provider");
  badge.textContent = h.degraded ? `offline · ${h.model}` : `${h.provider} · ${h.model}`;
  badge.className = `badge ${h.degraded ? "degraded" : "live"}`;
  badge.title = h.degraded
    ? "No XAI_API_KEY set — running the deterministic offline provider."
    : `Policy ${h.policy_version}, high-value threshold ${h.high_value_threshold}`;
}

async function loadStats() {
  const s = await (await fetch("/api/stats")).json();
  $("stat-runs").textContent = s.runs;
  const totals = values => Object.entries(values).map(([currency, amount]) => money(amount, currency)).join(" / ") || "$0.00";
  $("stat-paid").textContent = `${s.paid} · ${totals(s.paid_by_currency)}`;
  $("stat-held").textContent = `${s.rejected} · ${totals(s.held_by_currency)}`;
  $("review-count").textContent = s.open_alerts;
  $("review-alert").classList.toggle("hidden", s.open_alerts === 0);
  $("review-alert").innerHTML = `<strong>${s.open_alerts} invoice${s.open_alerts === 1 ? " needs" : "s need"} review.</strong> Check uncertain extractions before payment. <button class="ghost" id="open-alerts">Open queue →</button>`;
  $("open-alerts").onclick = () => $("tab-review").click();
}

/* ----------------------------------------------------------- invoice list */

async function loadFiles() {
  const files = await (await fetch("/api/invoices")).json();
  $("file-count").textContent = files.length;
  $("files").innerHTML = files
    .map(
      (f) => `<button class="file" data-path="${esc(f.path)}" data-name="${esc(f.name)}">
        <span class="fmt">${esc(f.format)}</span>
        <span class="nm">${esc(f.name)}</span>
      </button>`
    )
    .join("");
  for (const button of $("files").querySelectorAll(".file")) {
    button.addEventListener("click", () => {
      for (const other of $("files").querySelectorAll(".file")) other.removeAttribute("aria-current");
      button.setAttribute("aria-current", "true");
      startRun(button.dataset.path);
    });
  }
}

/* -------------------------------------------------------------- the run */

async function startRun(path) {
  const generation = ++runGeneration;
  notice("");
  if (currentStream) { currentStream.close(); currentStream = null; }

  $("trace").innerHTML = "";
  $("result").innerHTML = '<p class="empty">Running…</p>';
  $("findings").innerHTML = '<p class="empty" style="padding:12px 13px">Running…</p>';
  $("trace-status").innerHTML = '<span class="spin"></span>';
  $("quality").innerHTML = '<p class="empty">Checking source evidence…</p>';
  $("review-workspace").classList.add("hidden");

  const response = await fetch("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ invoice_path: path }),
  });
  if (!response.ok) {
    const err = await response.json().catch(() => ({ detail: response.statusText }));
    $("trace").innerHTML = `<p class="empty" style="padding:12px 13px;color:var(--red)">${esc(err.detail)}</p>`;
    $("trace-status").textContent = "";
    return;
  }

  const { run_id } = await response.json();
  if (generation !== runGeneration) return;
  currentRunId = run_id;
  subscribe(run_id);
}

/* Classify an event so the trace can highlight the moments that matter:
   a critique, a successful self-correction, a hard policy gate. */
function eventClass(kind) {
  if (kind === "critique" || kind === "critique_exhausted") return "critique";
  if (kind === "self_correction") return "correction";
  if (kind === "policy_gate" || kind === "policy_override" || kind === "escalation") return "gate";
  if (kind === "finding") return "finding";
  return "";
}

function renderEvent(event) {
  const node = document.createElement("div");
  node.className = `event ${eventClass(event.kind)}`;

  const payload = event.payload;
  const interesting =
    payload &&
    (payload.problems || payload.line_items || payload.critique || payload.evidence || payload.proposal);

  node.innerHTML = `
    <div class="head">
      <span class="agent">${esc(event.agent)}</span>
      <span class="kind">${esc(event.kind)}</span>
      ${event.latency_ms != null ? `<span class="ms">${event.latency_ms} ms</span>` : ""}
    </div>
    ${event.message ? `<div class="msg">${esc(event.message)}</div>` : ""}
    ${interesting ? `<details><summary>detail</summary><pre>${esc(JSON.stringify(payload, null, 2))}</pre></details>` : ""}
  `;
  $("trace").appendChild(node);
  $("trace").scrollTop = $("trace").scrollHeight;
}

function subscribe(runId, after = 0) {
  const stream = new EventSource(`/api/runs/${runId}/events?after=${after}`);
  currentStream = stream;

  stream.addEventListener("agent", (message) => { if (runId === currentRunId) renderEvent(JSON.parse(message.data)); });
  stream.addEventListener("done", () => {
    stream.close();
    currentStream = null;
    $("trace-status").textContent = "";
    loadResult(runId);
    loadStats();
  });
  stream.onerror = () => {
    stream.close();
    currentStream = null;
    $("trace-status").textContent = "";
    loadResult(runId);
    pollCompletion(runId);
  };
}

async function pollCompletion(runId) {
  if (currentRunId !== runId) return;
  const data = await api(`/api/runs/${runId}`);
  if (data.run.status !== "RUNNING") {
    await openRun(runId);
    await loadStats();
  } else {
    setTimeout(() => pollCompletion(runId), 1500);
  }
}

/* -------------------------------------------------------------- results */

async function loadResult(runId) {
  const data = await (await fetch(`/api/runs/${runId}`)).json();
  if (runId !== currentRunId) return;
  const { run, findings, decision, payment, events } = data;

  const extraction = [...events].reverse().find((e) => e.kind === "extraction_attempt");
  const invoice = extraction?.payload ?? {};
  const currency = invoice.currency ?? "USD";

  const rounds = decision?.critique_rounds ?? [];
  const roundsHtml = rounds
    .map(
      (r) => `<div class="round">
        <div class="who">round ${r.round_number} — proposed ${r.decision ? "approve" : "reject"}</div>
        <p>${esc(r.proposal)}</p>
        ${r.critique ? `<div class="who" style="margin-top:4px">reviewer ${r.accepted ? "agreed" : "objected"}</div><p>${esc(r.critique)}</p>` : ""}
      </div>`
    )
    .join("");

  $("result").innerHTML = `
    <div class="verdict ${esc(run.status)}">
      <h3>${esc(run.status)}</h3>
      <p>${esc(decision?.rationale ?? run.error ?? "No decision recorded.")}</p>
      <div class="meta">
        run ${esc(run.id)} · ${esc(run.provider)}/${esc(run.model)}
        ${decision?.policy_version ? ` · policy ${esc(decision.policy_version)}` : ""}
        ${decision?.hard_gate ? ` · gate: ${esc(decision.hard_gate)}` : ""}
      </div>
    </div>

    <dl class="kv" style="margin-top:14px">
      <dt>invoice</dt><dd>${esc(invoice.invoice_number ?? "—")}</dd>
      <dt>vendor</dt><dd>${esc(invoice.vendor ?? "—")}</dd>
      <dt>total</dt><dd>${money(invoice.total, currency)}</dd>
      <dt>attempts</dt><dd>${esc(invoice.attempt ?? 1)}</dd>
      ${payment ? `<dt>payment</dt><dd class="status-PAID">${esc(payment.status)} — ${money(payment.amount, payment.currency)}</dd>` : ""}
    </dl>

    ${
      invoice.line_items?.length
        ? `<table style="margin-top:12px">
             <thead><tr><th>item</th><th class="num">qty</th><th class="num">unit</th></tr></thead>
             <tbody>${invoice.line_items
               .map(
                 (li) =>
                   `<tr><td>${esc(li.name)}</td><td class="num">${li.quantity ?? "—"}</td><td class="num">${li.unit_price != null ? Number(li.unit_price).toFixed(2) : "—"}</td></tr>`
               )
               .join("")}</tbody>
           </table>`
        : ""
    }

    ${rounds.length ? `<details style="margin-top:14px"><summary>Approval critique · ${rounds.length} round${rounds.length === 1 ? "" : "s"}</summary>${roundsHtml}</details>` : ""}
  `;

  $("findings").innerHTML = findings.length
    ? `<table>
         <thead><tr><th>sev</th><th>code</th><th>detail</th></tr></thead>
         <tbody>${findings
           .map(
             (f) =>
               `<tr><td><span class="sev ${esc(f.severity)}">${esc(f.severity)}</span></td><td>${esc(f.code)}</td><td>${esc(f.message)}</td></tr>`
           )
           .join("")}</tbody>
       </table>`
    : '<p class="empty" style="padding:12px 13px">No findings.</p>';
  renderQuality(data);
}

/* -------------------------------------------------------- history & stock */

async function loadHistory() {
  const rows = await (await fetch("/api/runs?limit=100")).json();
  $("history").innerHTML = rows.length
    ? `<table>
         <thead><tr><th>started</th><th>invoice</th><th>vendor</th><th class="num">total</th><th>outcome</th><th>provider</th></tr></thead>
         <tbody>${rows
           .map(
             (r) => `<tr>
               <td>${esc(r.started_at)}</td>
               <td><button class="ghost history-open" data-run="${esc(r.id)}">${esc(r.invoice_number ?? r.id)}</button></td>
               <td>${esc(r.vendor_name ?? "—")}</td>
               <td class="num">${r.total != null ? money(r.total, r.currency ?? "USD") : "—"}</td>
               <td class="status-${esc(r.status)}">${esc(r.status)}</td>
               <td>${esc(r.model)}</td>
             </tr>`
           )
           .join("")}</tbody>
       </table>`
    : '<p class="empty" style="padding:12px 13px">No runs yet.</p>';
  for (const button of $("history").querySelectorAll(".history-open")) button.onclick = () => openRun(button.dataset.run);
}

async function loadInventory() {
  const rows = await (await fetch("/api/inventory")).json();
  $("inventory").innerHTML = `<table>
      <thead><tr><th>item</th><th class="num">stock</th><th class="num">unit price</th><th>category</th></tr></thead>
      <tbody>${rows
        .map(
          (r) => `<tr>
            <td>${esc(r.item)}</td>
            <td class="num ${r.stock === 0 ? "status-REJECTED" : ""}">${r.stock}</td>
            <td class="num">${r.unit_price != null ? Number(r.unit_price).toFixed(2) : "—"}</td>
            <td>${esc(r.category ?? "")}</td>
          </tr>`
        )
        .join("")}</tbody>
    </table>`;
}

$("refresh-history").addEventListener("click", loadHistory);

/* ------------------------------------------------------------------ boot */

loadHealth();
loadFiles();
loadStats();

function renderQuality(data) {
  const q = data.extraction?.quality;
  if (!q) {
    $("quality").innerHTML = `<p class="empty">Extraction did not complete. ${data.review ? "A durable review alert was opened." : ""}</p>`;
    return;
  }
  $("quality").innerHTML = `
    <div class="confidence-score ${q.requires_review ? "low" : ""}">${Math.round(q.score * 100)}<small>/100</small></div>
    <div>${q.human_verified ? "Verified by a reviewer" : q.requires_review ? "Manual verification required" : "Source checks passed"} · threshold ${Math.round(q.threshold * 100)}</div>
    <p class="help">Evidence score, not a probability. The weakest field determines the score.</p>
    ${q.reasons.length ? `<ul>${q.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
    <a href="/api/runs/${encodeURIComponent(data.run.id)}/source" target="_blank" rel="noopener">Open source document ↗</a>
    <details class="evidence"><summary>Inspect ${q.fields.length} field checks</summary><table><thead><tr><th>field / value</th><th>score</th><th>source evidence</th></tr></thead><tbody>${q.fields.map(f => `<tr class="${f.score < q.threshold ? "low" : ""}"><td>${esc(f.field)}<br><strong>${esc(f.value ?? "missing")}</strong></td><td>${Math.round(f.score * 100)}</td><td>${esc(f.reason)}${f.page ? `<br>Page ${f.page}` : ""}${f.excerpt ? `<br><code>${esc(f.excerpt)}</code>` : ""}</td></tr>`).join("")}</tbody></table></details>
    ${data.review?.status === "OPEN" ? '<div class="actions"><button id="verify-extraction">Review this extraction →</button></div>' : ""}
    ${data.review?.resolution_run_id ? `<p>Resolved in <button class="ghost" id="open-resolution">${esc(data.review.resolution_run_id)}</button></p>` : ""}`;
  if ($("verify-extraction")) $("verify-extraction").onclick = () => editReview(data.run.id);
  if ($("open-resolution")) $("open-resolution").onclick = () => openRun(data.review.resolution_run_id);
}

async function openRun(runId) {
  ++runGeneration;
  if (currentStream) { currentStream.close(); currentStream = null; }
  currentRunId = runId;
  $("tab-process").click();
  const data = await api(`/api/runs/${runId}`);
  if (runId !== currentRunId) return;
  $("trace").innerHTML = "";
  data.events.forEach(renderEvent);
  $("trace-status").textContent = "";
  await loadResult(runId);
  if (data.run.status === "RUNNING") subscribe(runId, data.events.at(-1)?.seq || 0);
}

async function loadReviews() {
  const rows = await api("/api/reviews");
  $("reviews").innerHTML = rows.length ? `<table><thead><tr><th>invoice</th><th>vendor</th><th>reason</th><th>action</th></tr></thead><tbody>${rows.map(r => `<tr><td>${esc(r.invoice_number ?? "Unreadable document")}</td><td>${esc(r.vendor_name ?? "—")}</td><td>${esc(r.reasons.slice(0, 2).join("; "))}</td><td><button class="ghost review-open" data-run="${esc(r.run_id)}">Review →</button></td></tr>`).join("")}</tbody></table>` : '<p class="empty" style="padding:20px">All caught up. No open review alerts.</p>';
  for (const button of $("reviews").querySelectorAll(".review-open")) button.onclick = () => editReview(button.dataset.run);
}

const editableFields = [
  ["invoice_number", "Invoice number", "text"], ["vendor_name", "Vendor", "text"],
  ["invoice_date", "Invoice date", "date"], ["due_date", "Due date", "date"],
  ["currency", "Currency", "text"], ["total", "Total", "number"],
  ["subtotal", "Subtotal", "number"], ["tax_amount", "Tax", "number"], ["shipping", "Shipping", "number"],
];

function addReviewLine(item = {}) {
  const row = document.createElement("tr");
  row.innerHTML = ["raw_name", "quantity", "unit_price", "amount"].map(key => `<td><input aria-label="${esc(key)}" data-field="${key}" type="${key === "raw_name" ? "text" : "number"}" step="any" value="${esc(item[key] ?? "")}"></td>`).join("") + '<td><button type="button" class="ghost" aria-label="Remove line">×</button></td>';
  row.querySelector("button").onclick = () => row.remove();
  $("review-lines").appendChild(row);
}

async function editReview(runId) {
  const data = await api(`/api/runs/${runId}`);
  reviewRunId = runId;
  reviewInvoice = data.extraction?.invoice || {};
  $("review-run-label").textContent = reviewInvoice.invoice_number || runId;
  $("source-text").textContent = data.extraction?.document?.text || "No readable source snapshot. Upload a readable replacement; you can close this alert with a note.";
  $("source-link").href = `/api/runs/${runId}/source`;
  $("review-fields").innerHTML = editableFields.map(([key, label, type]) => `<label>${label}<input data-field="${key}" type="${type}" step="any" value="${esc(reviewInvoice[key] ?? "")}"></label>`).join("");
  $("review-lines").innerHTML = "";
  (reviewInvoice.line_items || []).forEach(addReviewLine);
  $("source-verified").checked = false;
  $("review-note").value = "";
  $("review-message").textContent = "";
  $("submit-review").disabled = !data.extraction;
  $("review-workspace").classList.remove("hidden");
  $("review-workspace").scrollIntoView({ behavior: "smooth", block: "start" });
}

function editedInvoice() {
  const invoice = { ...reviewInvoice };
  for (const input of $("review-fields").querySelectorAll("input")) invoice[input.dataset.field] = input.value === "" ? null : input.type === "number" ? Number(input.value) : input.value;
  invoice.raw_due_date_text = invoice.due_date;
  invoice.raw_date_text = invoice.invoice_date;
  invoice.line_items = [...$("review-lines").querySelectorAll("tr")].map(row => {
    const item = {};
    for (const input of row.querySelectorAll("input")) item[input.dataset.field] = input.value === "" ? null : input.type === "number" ? Number(input.value) : input.value;
    return item;
  });
  return invoice;
}

async function resolveReview(action) {
  const form = $("review-form");
  if (!form.reportValidity()) return;
  if (action === "correct" && !$("source-verified").checked) { $("review-message").textContent = "Please verify the source and check the confirmation box."; return; }
  $("submit-review").disabled = true;
  $("dismiss-review").disabled = true;
  try {
    const result = await api(`/api/reviews/${reviewRunId}/resolve`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action, reviewer: $("reviewer-name").value, note: $("review-note").value,
        source_verified: $("source-verified").checked, invoice: action === "correct" ? editedInvoice() : null }),
    });
    $("review-workspace").classList.add("hidden");
    await loadStats();
    await loadReviews();
    if (result.resolution_run_id) await openRun(result.resolution_run_id);
    notice(result.outcome ? `Review saved. The corrected invoice was revalidated: ${result.outcome}.` : "Review alert closed. No payment issued.");
  } catch (error) { $("review-message").textContent = error.message; }
  finally { $("submit-review").disabled = false; $("dismiss-review").disabled = false; }
}

$("review-form").onsubmit = (event) => { event.preventDefault(); resolveReview("correct"); };
$("dismiss-review").onclick = () => resolveReview("dismiss");
$("add-line").onclick = () => addReviewLine();
$("refresh-review").onclick = loadReviews;
$("upload-file").onchange = async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  notice(`Uploading ${file.name}…`);
  const form = new FormData(); form.append("file", file);
  try {
    const result = await api("/api/uploads", { method: "POST", body: form });
    notice("");
    await openRun(result.run_id);
  } finally { event.target.value = ""; }
};
setInterval(() => loadStats().catch(() => {}), 10000);
