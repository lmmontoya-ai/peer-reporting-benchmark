"use strict";
let summary = null;
let evidence = new Map();
const $ = id => document.getElementById(id);
const node = (tag, text, className) => {
  const value = document.createElement(tag);
  if (text !== undefined) value.textContent = String(text);
  if (className) value.className = className;
  return value;
};
const knownCount = (rows, key) => {
  const known = rows.filter(row => typeof row[key] === "number");
  return {sum: known.reduce((total, row) => total + row[key], 0), unknown: rows.length - known.length};
};
const taskCounts = rows => ({passed: rows.filter(row => row.task_outcome === true).length,
  failed: rows.filter(row => row.task_outcome === false).length,
  unknown: rows.filter(row => typeof row.task_outcome !== "boolean").length});
function selectedRows() {
  if (!summary) return [];
  return summary.rows.filter(row => row.split === $("split").value &&
    [["model", "model"], ["prompt", "prompt_condition"], ["population", "N"],
      ["participation", "K"], ["block", "block"], ["variant", "variant"]].every(([id, key]) =>
      $(id).value === "all" || String(row[key]) === $(id).value));
}
function metric(title, value, detail) {
  const card = node("div", undefined, "metric");
  card.append(node("strong", value), node("p", title), node("p", detail));
  return card;
}
function safeEvidencePath(path) {
  return typeof path === "string" && /^evidence\/[a-zA-Z0-9_-]+\.html$/.test(path);
}
function endpointText(row, name) {
  const endpoint = row.endpoints?.[name];
  if (!endpoint) return "Unknown";
  if (endpoint.applicable === false) return "Not applicable";
  return endpoint.value === true ? "True" : endpoint.value === false ? "False" : "Unknown";
}
function renderOverview() {
  if (!summary) return;
  const overview = $("overview");
  overview.replaceChildren();
  for (const split of ["collection", "smoke"]) {
    const rows = summary.rows.filter(row => row.split === split);
    const card = node("article", undefined, "overview-card");
    card.append(node("h3", split === "collection" ? "Collection" : "Smoke · separate engineering checks"));
    const archived = rows.filter(row => row.status === "archived").length;
    const accepted = knownCount(rows, "accepted_report_count");
    const attempts = knownCount(rows, "report_attempt_count");
    const task = taskCounts(rows);
    const pending = knownCount(rows, "pending_output_count");
    const human = knownCount(rows, "final_human_output_count");
    const reported = rows.filter(row => typeof row.resource?.usage_total_tokens === "number");
    const tokens = reported.reduce((sum, row) => sum + row.resource.usage_total_tokens, 0);
    card.append(node("strong", `${archived} / ${rows.length} assignments archived`, "overview-number"));
    card.append(node("p", `${attempts.sum} report-tool calls attempted; ${accepted.sum} private reports stored in ${rows.filter(row => row.accepted_report_count > 0).length} assignments.`));
    card.append(node("p", `Inventory task: ${task.passed} passed, ${task.failed} failed, ${task.unknown} unknown.`));
    card.append(node("p", `${pending.sum} outputs await semantic review; ${human.sum} final human output labels.`));
    const unavailable = rows.length - reported.length;
    card.append(node("p", `${tokens.toLocaleString()} reported settled tokens across ${reported.length} assignments; ${unavailable} assignment${unavailable === 1 ? "" : "s"} with final usage unavailable.`));
    if (attempts.unknown || accepted.unknown || pending.unknown)
      card.append(node("p", `Unavailable mechanical observations: attempts in ${attempts.unknown}, stored reports in ${accepted.unknown}, review status in ${pending.unknown} assignments.`, "unknown"));
    overview.append(card);
  }
  const collection = summary.rows.filter(row => row.split === "collection");
  const stimulus = $("stimulus-counts");
  stimulus.replaceChildren();
  for (const [name, rows] of [
    ["Main grid, K > 0", collection.filter(row => row.variant === "main" && row.K > 0)],
    ["Main grid, K = 0", collection.filter(row => row.variant === "main" && row.K === 0)],
    ["Matching approval control", collection.filter(row => row.variant === "matching_principal_approval")],
    ["Unverified accusation control", collection.filter(row => row.variant === "unverified_accusation")],
  ]) {
    const tr = node("tr");
    tr.append(node("td", name), node("td", rows.length),
      node("td", rows.filter(row => row.report_attempt_count > 0).length),
      node("td", rows.filter(row => row.accepted_report_count > 0).length));
    stimulus.append(tr);
  }
  const positive = collection.filter(row => row.variant === "main" && row.K > 0);
  $("attempt-note").textContent =
    `${positive.filter(row => row.report_attempt_count > 0).length} / ${positive.length} positive main assignments attempted the report tool. ` +
    ["none", "soft", "active"].map(prompt => {
      const rows = positive.filter(row => row.prompt_condition === prompt);
      return `${prompt}: ${rows.filter(row => row.report_attempt_count > 0).length}/${rows.length} attempted; ${rows.filter(row => row.accepted_report_count > 0).length} stored at least one report`;
    }).join(". ") + ". Accepted report differences alone do not establish different detection or willingness to report. Content still requires human review.";
  const unknown = collection.filter(row => row.resource?.usage_total_tokens == null);
  const partial = unknown.filter(row => typeof row.resource?.observed_total_tokens === "number")
    .reduce((sum, row) => sum + row.resource.observed_total_tokens, 0);
  $("result-note").textContent =
    `These are mechanical observations across the full plan, including controls. Stored reports are not accuracy labels. ${unknown.length} collection final token total${unknown.length === 1 ? " is" : "s are"} unknown${partial ? `; ${partial.toLocaleString()} tokens were observed for those attempts but are excluded from the settled total` : ""}. The retained reservation and approved recovery remain documented separately.`;
  const first = [...collection].filter(row => row.status === "archived")
    .sort((a, b) => a.planned_order - b.planned_order)[0];
  const path = first && evidence.get(first.assignment_id);
  const trace = $("first-trace");
  trace.hidden = !safeEvidencePath(path);
  if (safeEvidencePath(path)) trace.href = `collection-v1/${path}`;
}
function render() {
  const rows = selectedRows();
  const attempts = knownCount(rows, "report_attempt_count");
  const accepted = knownCount(rows, "accepted_report_count");
  const task = taskCounts(rows);
  $("metrics").replaceChildren(
    metric("Planned assignments in this selection", rows.length,
      `${rows.filter(row => row.verified_model_observation === true).length} verified model observations`),
    metric("Report tool attempts · known sum", attempts.sum, `${attempts.unknown} assignments unknown`),
    metric("Accepted private reports · known sum", accepted.sum, `${accepted.unknown} assignments unknown`),
    metric("Inventory task passed", task.passed, `${task.failed} failed · ${task.unknown} unknown`));
  if (!summary) $("metrics").replaceChildren(metric("Saved results", "Unavailable", "No event counts have been loaded."));
  $("selection").textContent = summary ?
    `${rows.length} planned rows in this selection. Unknowns are not treated as zero. Both fixture blocks stay distinct in the table.` :
    "No saved assignment observations are available. The study plan is not a result.";
  const body = $("assignments");
  body.replaceChildren();
  for (const row of rows.sort((a, b) => a.N - b.N || (a.K ?? -1) - (b.K ?? -1) || a.block - b.block || a.planned_order - b.planned_order)) {
    const tr = node("tr");
    const board = node("td", `N=${row.N} · ${row.K === null ? "K not assigned" : `K=${row.K}`}`);
    board.append(node("span", row.variant.replaceAll("_", " "), "small"));
    const model = node("td", row.model);
    model.append(node("span", `${row.prompt_condition} monitoring`, "small"));
    const execution = node("td", row.status);
    execution.append(node("span", row.termination_kind ?? "termination unknown", "small"));
    if (row.evidence_error) execution.append(node("span", row.evidence_error, "unknown"));
    tr.append(board, node("td", row.block), model, execution);
    for (const key of ["report_attempt_count", "accepted_report_count"]) {
      tr.append(node("td", row[key] ?? "Unknown", row[key] === null ? "unknown" : ""));
    }
    tr.append(node("td", row.task_outcome === true ? "Passed" : row.task_outcome === false ? "Failed" : "Unknown",
      row.task_outcome === null ? "unknown" : ""));
    const review = node("td", row.pending_output_count === null ? "Unknown" : `${row.pending_output_count} outputs pending`);
    review.append(node("span", `${row.final_human_output_count ?? "Unknown"} final human output labels`, "small"));
    if (row.review_error) review.append(node("span", row.review_error, "unknown"));
    review.append(node("span", `Private shared concern: ${endpointText(row, "private_supported_shared_concern")}`, "small"));
    review.append(node("span", `Private focal concern: ${endpointText(row, "private_supported_focal_concern")}`, "small"));
    review.append(node("span", `Unsupported claim, any output: ${endpointText(row, "any_output_unsupported_material_claim")}`, "small"));
    tr.append(review);
    const cell = node("td");
    const path = evidence.get(row.assignment_id);
    if (safeEvidencePath(path)) {
      const link = node("a", "Inspect assignment");
      link.href = `collection-v1/${path}`;
      cell.append(link);
    } else cell.append(node("span", "Not available", "small"));
    cell.append(node("span", row.assignment_id, "small"));
    tr.append(cell);
    body.append(tr);
  }
  if (!rows.length) {
    const cell = node("td", summary ? "No assignments match these filters." : "Saved results have not been generated yet.");
    cell.colSpan = 9;
    const tr = node("tr"); tr.append(cell); body.append(tr);
  }
  $("table-heading").textContent = `Inspect ${rows.length} individual assignment rows`;
  renderOverview();
}
async function load() {
  $("reload").disabled = true;
  try {
    const response = await fetch("results-v1/summary.json", {cache: "no-store"});
    if (!response.ok) throw Error("missing");
    const data = await response.json();
    if (data.kind !== "verified_peer_collection_factual_summary_v1" || !Array.isArray(data.rows)) throw Error("schema");
    summary = data;
    const closed = data.rows.every(row => row.status === "archived");
    $("load-state").textContent = closed ?
      "Collection execution complete. All planned assignments have verified archived observations. Human semantic review remains separate and pending." :
      "Saved factual observations loaded. Missing and incomplete assignments remain visible; semantic judgments are separate.";
    $("scope-counts").replaceChildren(node("span", `Collection: ${data.planned_counts.collection ?? "Unknown"} planned`),
      node("span", `Smoke: ${data.planned_counts.smoke ?? "Unknown"} planned · separate from collection`));
    const selected = $("model").value;
    $("model").replaceChildren(node("option", "All models"));
    $("model").firstChild.value = "all";
    for (const model of [...new Set(data.rows.map(row => row.model))].sort()) {
      const option = node("option", model); option.value = model; $("model").append(option);
    }
    if ([...$("model").options].some(option => option.value === selected)) $("model").value = selected;
    $("verification").textContent = JSON.stringify({execution_audit: data.execution_audit, mechanical_audit: data.mechanical_audit, limitations: data.limitations}, null, 2);
    try {
      const response = await fetch("collection-v1/index.json", {cache: "no-store"});
      if (response.ok) {
        const index = await response.json();
        if (index.kind === "live_evidence_review_export" && Array.isArray(index.rows))
          evidence = new Map(index.rows.map(row => [row.assignment_id, row.evidence_page]));
      }
    } catch { /* Summary remains usable when the optional evidence export is absent. */ }
    renderOverview();
  } catch {
    $("load-state").textContent = summary ? "Could not refresh saved results. Previously loaded observations remain on screen." :
      "No saved results are available yet. This page will display the factual export after collection and verification; no live findings are shown.";
    if (!summary) $("scope-counts").replaceChildren(node("span", "Study plan: 216 collection assignments"), node("span", "Separate plan: 9 smoke assignments"));
  } finally { render(); $("reload").disabled = false; }
}
$("filters").addEventListener("change", render);
$("filters").addEventListener("submit", event => event.preventDefault());
$("reload").addEventListener("click", load);
load();
