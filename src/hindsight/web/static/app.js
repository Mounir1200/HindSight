"use strict";

const runButton = document.querySelector("#run-demo");
const runButtonLabel = document.querySelector("#run-button-label");
const runPanel = document.querySelector(".run-panel");
const runStatus = document.querySelector("#run-status");
const emptyState = document.querySelector("#empty-state");
const dashboard = document.querySelector("#dashboard");
const errorPanel = document.querySelector("#error-panel");
const errorTitle = document.querySelector("#error-title");
const errorMessage = document.querySelector("#error-message");
const platformBadges = document.querySelector("#platform-badges");
const demoNav = document.querySelector("#demo-nav");
const technicalNavLink = document.querySelector("#technical-nav-link");
const incidentCount = document.querySelector("#incident-count");
const auditCount = document.querySelector("#audit-count");
const incidentList = document.querySelector("#incident-list");
const auditList = document.querySelector("#audit-list");
const caseRegister = document.querySelector("#case-register");
const workspaceState = document.querySelector("#workspace-state");
const workspaceStateKicker = document.querySelector("#workspace-state-kicker");
const workspaceStateTitle = document.querySelector("#workspace-state-title");
const workspaceStateCopy = document.querySelector("#workspace-state-copy");
const prepareDemoButton = document.querySelector("#prepare-demo");
const retryWorkspaceButton = document.querySelector("#retry-workspace");
let lastWorkspace = null;

const dateFormatter = new Intl.DateTimeFormat("en-GB", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "UTC",
});

const decimalFormatter = new Intl.NumberFormat("en-GB", {
  maximumFractionDigits: 2,
  minimumFractionDigits: 0,
});

const percentageFormatter = new Intl.NumberFormat("en-GB", {
  style: "percent",
  maximumFractionDigits: 1,
});

const labels = {
  applied: "Applied",
  already_remediated: "Already remediated — safe no-op",
  audit_in_progress: "An audit is already in progress.",
  billing_agent: "Billing agent",
  calculate_call_charge: "Calculate the call charge",
  closed: "Closed",
  cockroachdb: "CockroachDB",
  cockroachdb_distributed_vector_index: "Distributed vector index",
  cockroachdb_managed_mcp: "CockroachDB Managed MCP",
  completed: "Completed",
  corrected: "Corrected",
  counterfactual_current_truth: "Counterfactual current truth",
  decision_input: "Decision input",
  delayed_tariff_ingestion: "Delayed tariff ingestion",
  deterministic_temporal_engine: "Deterministic temporal engine",
  distributed_vector_index: "Distributed vector index",
  in_memory: "In-memory",
  open: "Open report",
  reported: "Reported",
  read_only: "Read-only",
  replay: "Replay",
  structured_exact: "Structured exact match",
  synthetic_replay: "Synthetic replay",
  telecom_adapter: "Telecom adapter",
  temporal_sql: "Temporal SQL",
  wrong_not_knowable: "Wrong, not knowable",
  not_recorded_at_decision: "Not yet recorded when the decision was made",
  no_reported_incident: "No reported incident is available to audit.",
  invalid_demo_result: "The audit returned an invalid result.",
  memory_evaluation: "Memory evaluation case",
  reported_incident: "Reported incident",
  synthetic_fixture: "Synthetic fixture",
  advisory_explanation: "Advisory explanation",
};

function valueAt(object, path, fallback = null) {
  let value = object;
  for (const key of path.split(".")) {
    if (value === null || value === undefined || typeof value !== "object") {
      return fallback;
    }
    value = value[key];
  }
  return value === null || value === undefined ? fallback : value;
}

function displayLabel(value, fallback = "—") {
  if (value === null || value === undefined || value === "") {
    return fallback;
  }
  const key = String(value);
  return labels[key] || key.replaceAll("_", " ");
}

function setText(id, value, fallback = "—") {
  const element = document.getElementById(id);
  if (!element) return;
  element.textContent = value === null || value === undefined || value === "" ? fallback : String(value);
}

function setTime(id, value) {
  const element = document.getElementById(id);
  if (!element) return;
  if (!value) {
    element.textContent = "—";
    element.removeAttribute("datetime");
    element.removeAttribute("title");
    return;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    element.textContent = String(value);
    return;
  }
  element.dateTime = date.toISOString();
  element.title = String(value);
  element.textContent = dateFormatter.format(date);
}

function formatMoney(value, currency = "EUR") {
  if (value === null || value === undefined || value === "") return "—";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return `${value} ${currency}`;
  try {
    return new Intl.NumberFormat("en-GB", {
      style: "currency",
      currency: currency || "EUR",
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(amount);
  } catch {
    return `${decimalFormatter.format(amount)} ${currency || "EUR"}`;
  }
}

function formatRate(value, currency = "EUR") {
  if (value === null || value === undefined || value === "") return "—";
  const amount = Number(value);
  const formatted = Number.isFinite(amount) ? decimalFormatter.format(amount) : String(value);
  return `${formatted} ${currency || "EUR"}/min`;
}

function formatDuration(seconds) {
  const total = Number(seconds);
  if (!Number.isFinite(total) || total < 0) return "—";
  if (total % 3600 === 0) return `${decimalFormatter.format(total / 3600)} h`;
  if (total >= 3600) return `${decimalFormatter.format(total / 3600)} h`;
  if (total >= 60) return `${decimalFormatter.format(total / 60)} min`;
  return `${decimalFormatter.format(total)} s`;
}

function formatBoolean(value) {
  return value === true ? "Yes" : value === false ? "No" : "—";
}

function hasNumber(value) {
  return value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value));
}

function booleanBadge(value) {
  const element = document.createElement("span");
  const tone = value === true ? "boolean-yes" : value === false ? "boolean-no" : "boolean-unknown";
  element.className = `boolean-label ${tone}`;
  element.textContent = formatBoolean(value);
  return element;
}

function setStatusChip(id, text, tone = "neutral") {
  const element = document.getElementById(id);
  if (!element) return;
  element.textContent = text || "—";
  element.className = `status-chip status-${tone}`;
}

function formatDateTime(value) {
  if (!value) return "\u2014";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : dateFormatter.format(date);
}

function addRecordDetail(list, label, value) {
  const item = document.createElement("div");
  const term = document.createElement("dt");
  const description = document.createElement("dd");
  term.textContent = label;
  description.textContent = value === null || value === undefined || value === "" ? "\u2014" : String(value);
  item.append(term, description);
  list.append(item);
}

function caseRecord(record, kind) {
  const item = document.createElement("li");
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  const primary = document.createElement("span");
  const subject = document.createElement("strong");
  const context = document.createElement("span");
  const impact = document.createElement("span");
  const impactValue = document.createElement("strong");
  const state = document.createElement("span");
  const facts = document.createElement("dl");
  const currency = record.currency || "EUR";

  item.className = "case-record";
  primary.className = "case-record-primary";
  impact.className = "case-record-impact";
  state.className = "case-record-state";
  facts.className = "case-record-details";
  subject.textContent = record.subject_id || record.case_id || "Unidentified case";
  context.textContent = `${displayLabel(record.service_type)} \u00b7 ${(record.route || "\u2014").replace("->", " \u2192 ")}`;

  if (kind === "incident") {
    impactValue.textContent = hasNumber(record.amount_at_issue)
      ? `${formatMoney(record.amount_at_issue, currency)} billed`
      : "Impact pending";
    state.textContent = displayLabel(record.status || "reported");
    addRecordDetail(facts, "Reported", formatDateTime(record.opened_at));
    addRecordDetail(facts, "Claim", record.claim);
    addRecordDetail(facts, "Source", displayLabel(record.source));
    addRecordDetail(facts, "Case ID", record.case_id);
    addRecordDetail(facts, "Decision link", record.decision_id);
  } else {
    impactValue.textContent = formatMoney(record.customer_impact, currency);
    state.textContent = displayLabel(record.verdict || record.status);
    addRecordDetail(facts, "Audited", formatDateTime(record.audited_at));
    addRecordDetail(facts, "Agent fault", formatBoolean(record.agent_fault));
    addRecordDetail(facts, "Knowledge gap", formatDuration(record.knowledge_gap_seconds));
    addRecordDetail(facts, "Root cause", displayLabel(record.root_cause));
    addRecordDetail(facts, "Audit role", displayLabel(record.audit_role));
    addRecordDetail(facts, "Case ID", record.case_id);
    addRecordDetail(facts, "Decision ID", record.decision_id);
  }

  primary.append(subject, context);
  impact.append(impactValue, state);
  summary.append(primary, impact);
  details.append(summary, facts);
  item.append(details);
  return item;
}

function renderCaseRecords(list, records, kind) {
  const fragment = document.createDocumentFragment();
  if (records.length === 0) {
    const empty = document.createElement("li");
    empty.className = "case-record-empty";
    empty.textContent = kind === "incident"
      ? "No reported incident is awaiting audit."
      : "No completed audit is recorded yet.";
    fragment.append(empty);
  } else {
    for (const record of records) fragment.append(caseRecord(record, kind));
  }
  list.replaceChildren(fragment);
}

function normalizeWorkspace(workspace) {
  const validStates = new Set(["empty", "prepared", "running", "completed"]);
  if (
    !workspace
    || typeof workspace !== "object"
    || !validStates.has(workspace.demo_state)
    || typeof workspace.can_run_demo !== "boolean"
    || typeof workspace.sample_already_audited !== "boolean"
    || !Array.isArray(workspace.reported_incidents)
    || !Array.isArray(workspace.past_audits)
    || workspace.can_run_demo !== (workspace.demo_state === "prepared")
    || (workspace.can_run_demo && workspace.reported_incidents.length === 0)
  ) {
    throw new Error("The case register returned an invalid state.");
  }
  return workspace;
}

function renderListMessage(list, message) {
  const item = document.createElement("li");
  item.className = "case-record-empty";
  item.textContent = message;
  list.replaceChildren(item);
}

function showWorkspaceState(kicker, title, copy, action = null) {
  workspaceStateKicker.textContent = kicker;
  workspaceStateTitle.textContent = title;
  workspaceStateCopy.textContent = copy;
  prepareDemoButton.hidden = action !== "prepare";
  retryWorkspaceButton.hidden = action !== "retry";
  workspaceState.hidden = false;
}

function setWorkspaceLoading() {
  caseRegister.dataset.state = "loading";
  caseRegister.setAttribute("aria-busy", "true");
  runPanel.hidden = true;
  runButton.disabled = true;
  emptyState.hidden = true;
  if (!lastWorkspace) {
    incidentCount.textContent = "\u2014";
    auditCount.textContent = "\u2014";
    renderListMessage(incidentList, "Loading case activity...");
    renderListMessage(auditList, "Loading audit history...");
  }
  showWorkspaceState(
    "Case source",
    "Loading the case register.",
    "Checking for reported incidents and completed audits.",
  );
}

function renderWorkspaceUnavailable() {
  caseRegister.dataset.state = "unavailable";
  caseRegister.setAttribute("aria-busy", "false");
  runPanel.hidden = true;
  runButton.disabled = true;
  emptyState.hidden = true;
  if (!lastWorkspace) {
    incidentCount.textContent = "\u2014";
    auditCount.textContent = "\u2014";
    renderListMessage(incidentList, "Case activity unavailable.");
    renderListMessage(auditList, "Audit history unavailable.");
  }
  showWorkspaceState(
    "Source unavailable",
    "The case register could not be reached.",
    lastWorkspace
      ? "The last known register remains visible. Audit actions stay disabled until the source responds."
      : "HindSight cannot verify whether an incident exists, so no audit action is available.",
    "retry",
  );
}

function renderWorkspace(workspace) {
  const normalized = normalizeWorkspace(workspace);
  const incidents = normalized.reported_incidents;
  const audits = normalized.past_audits;
  const isReplay = normalized.sample_already_audited;
  lastWorkspace = normalized;
  caseRegister.dataset.state = normalized.demo_state;
  caseRegister.setAttribute("aria-busy", "false");
  incidentCount.textContent = String(incidents.length);
  auditCount.textContent = String(audits.length);
  renderCaseRecords(incidentList, incidents, "incident");
  renderCaseRecords(auditList, audits, "audit");

  if (normalized.demo_state === "running") {
    runPanel.hidden = false;
    runButton.disabled = true;
    runButton.setAttribute("aria-busy", "true");
    runButtonLabel.textContent = "Audit in progress";
    runStatus.textContent = "Reconstructing the reported decision.";
    workspaceState.hidden = true;
    return;
  }

  runButton.removeAttribute("aria-busy");
  runButton.disabled = !normalized.can_run_demo;
  runButtonLabel.textContent = isReplay ? "Replay the audit" : "Run the audit";
  runPanel.hidden = !normalized.can_run_demo;
  if (normalized.can_run_demo) {
    runStatus.textContent = isReplay
      ? "The fixed sample is ready to replay. Existing audit history will not be duplicated."
      : `${incidents.length} reported incident${incidents.length === 1 ? " is" : "s are"} ready for audit.`;
    workspaceState.hidden = true;
    if (dashboard.hidden) emptyState.hidden = false;
    return;
  }

  emptyState.hidden = true;
  prepareDemoButton.textContent = isReplay ? "Replay sample scenario" : "Load sample incident";
  showWorkspaceState(
    audits.length > 0 ? "Queue clear" : "Case register clear",
    "No incident is waiting for audit.",
    isReplay
      ? "This fixed sample already exists in audit history. Replay it to verify the workflow without claiming a new production incident."
      : audits.length > 0
      ? "Completed reconstructions remain available below. Load the synthetic case to run another explicit audit."
      : "HindSight only exposes the audit action when a report exists. Load the synthetic hackathon case to test the workflow.",
    "prepare",
  );
}

async function loadWorkspace() {
  setWorkspaceLoading();
  try {
    renderWorkspace(await requestJson("/demo/workspace", {}, 10_000));
  } catch {
    renderWorkspaceUnavailable();
  }
}

function renderPlatformBadges(payload) {
  const vector = valueAt(payload, "learning_proof.vector_memory", {});
  const investigation = valueAt(payload, "bedrock_investigation", null);
  const safety = valueAt(investigation, "safety", {});
  const values = [
    displayLabel(payload.backend),
    vector.enabled ? "Vector index active" : "Vector index inactive",
  ];

  if (investigation) {
    values.push(`Bedrock · ${displayLabel(investigation.status)}`);
  }
  if (safety.context_transport) {
    values.push(`${displayLabel(safety.context_transport)} · ${displayLabel(safety.tool_access)}`);
  }

  platformBadges.replaceChildren();
  for (const value of values) {
    const item = document.createElement("li");
    item.textContent = value;
    platformBadges.append(item);
  }
  platformBadges.hidden = false;
}

function renderCaseOne(payload) {
  const decision = payload.decision || {};
  const comparison = payload.comparison || {};
  const verdict = payload.verdict || {};
  const truth = payload.current_truth || {};
  const knowledge = payload.known_at_decision || {};
  const currency = comparison.currency || decision.currency || "EUR";

  setText("case-one-id", valueAt(payload, "remediation.case_id"));
  setText("hero-overcharge", formatMoney(comparison.overcharge, currency));
  setText(
    "hero-comparison",
    `${formatMoney(comparison.billed_amount, currency)} charged · ${formatMoney(comparison.expected_amount, currency)} expected`,
  );
  setText("hero-verdict", displayLabel(verdict.category));
  setText("hero-fault", verdict.agent_fault === false ? "No" : formatBoolean(verdict.agent_fault));
  setText("hero-gap", formatDuration(verdict.knowledge_gap_seconds));

  setTime("truth-valid-date", truth.valid_from);
  setTime("truth-event-date", decision.event_time);
  setTime("truth-recorded-date", truth.recorded_at);
  setText("truth-rate", formatRate(truth.rate, currency));
  setText("truth-event-copy", `${formatDuration(decision.duration_seconds)} at the rate then available`);
  setText("truth-assertion", truth.assertion_id);
  setText("truth-version", truth.version_number ? ` · version ${truth.version_number}` : "", "");

  setTime("knowledge-known-date", knowledge.recorded_at || knowledge.valid_from || decision.event_time);
  setTime("knowledge-decision-date", knowledge.decision_time || decision.decided_at);
  setTime("knowledge-correction-date", truth.recorded_at);
  setText("knowledge-rate", formatRate(knowledge.rate, currency));
  setText("knowledge-assertion", knowledge.assertion_id);
  setText(
    "knowledge-version",
    knowledge.version_number ? ` · version ${knowledge.version_number}` : "",
    "",
  );

  setText("decision-event", decision.event_id || decision.subject_id || decision.call_id || payload.scenario);
  setText("decision-agent", displayLabel(decision.agent_id, "billing_agent"));
  setText("decision-action", displayLabel(decision.action, "Calculate the call charge"));
  setText("decision-duration", formatDuration(decision.duration_seconds));
  setText("decision-amount", formatMoney(decision.amount, decision.currency || currency));
  setText("decision-rate", formatRate(decision.selected_rate, decision.currency || currency));
  setTime("decision-date", decision.decided_at);
  setText("decision-id", decision.id);

  const explanation = verdict.agent_fault === false
    ? "The outcome was financially wrong, but the retroactive correction had not been recorded yet. The agent used the only evidence available when it decided."
    : "The temporal verdict assigns responsibility to the agent for this decision.";
  setText("verdict-explanation", explanation);
  setText("verdict-billed", formatMoney(comparison.billed_amount, currency));
  setText("verdict-expected", formatMoney(comparison.expected_amount, currency));
  setText("verdict-refund", formatMoney(comparison.overcharge, currency));
  setText("verdict-root-cause", displayLabel(verdict.root_cause));

  renderEvidence(Array.isArray(payload.evidence) ? payload.evidence : []);
  renderRemediation(payload.remediation || {}, currency);
}

function evidenceCell(label, content) {
  const cell = document.createElement("td");
  cell.dataset.label = label;
  if (content instanceof Node) {
    cell.append(content);
  } else {
    cell.textContent = content === null || content === undefined || content === "" ? "—" : String(content);
  }
  return cell;
}

function renderEvidence(evidence) {
  const rows = document.getElementById("evidence-rows");
  const fragment = document.createDocumentFragment();

  for (const item of evidence) {
    const row = document.createElement("tr");
    const identity = document.createElement("div");
    const name = document.createElement("strong");
    const assertion = document.createElement("code");
    name.textContent = displayLabel(item.evidence_type);
    assertion.textContent = item.assertion_id || "—";
    identity.append(name, document.createElement("br"), assertion);

    let retrieval = displayLabel(item.retrieval_method);
    if (item.retrieval_rank) retrieval += ` · rank ${item.retrieval_rank}`;
    if (hasNumber(item.retrieval_score)) {
      retrieval += ` · ${percentageFormatter.format(Number(item.retrieval_score))}`;
    }

    row.append(
      evidenceCell("Evidence", identity),
      evidenceCell("Available", booleanBadge(item.available_to_agent)),
      evidenceCell("Retrieved", booleanBadge(item.retrieved)),
      evidenceCell("Method", retrieval),
      evidenceCell("Shown to model", booleanBadge(item.was_presented_to_model)),
      evidenceCell("Used", booleanBadge(item.was_used_for_decision)),
      evidenceCell("Exclusion", displayLabel(item.exclusion_reason)),
    );
    fragment.append(row);
  }

  if (evidence.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 7;
    cell.textContent = "No evidence trace is available.";
    row.append(cell);
    fragment.append(row);
  }

  rows.replaceChildren(fragment);
}

function renderRemediation(remediation, currency) {
  const state = remediation.final_state || {};
  const attempts = Array.isArray(remediation.attempts) ? remediation.attempts : [];
  const statuses = new Map();

  setText(
    "remediation-invoice",
    `${formatMoney(state.invoice_amount, currency)} · ${displayLabel(state.invoice_status)}`,
  );
  setText("remediation-refund", `${formatMoney(state.refund_amount, currency)} · ${state.refund_count ?? "—"} total`);
  setText("remediation-dispute", displayLabel(state.dispute_status));
  setText("remediation-incidents", state.incident_count);
  setText("remediation-memories", state.procedural_memory_count);
  setText("remediation-runs", state.remediation_run_count);

  const list = document.getElementById("remediation-attempts");
  const fragment = document.createDocumentFragment();
  for (const attempt of attempts) {
    statuses.set(attempt.status, (statuses.get(attempt.status) || 0) + 1);
    const item = document.createElement("li");
    const status = document.createElement("strong");
    const runId = document.createElement("code");
    status.textContent = displayLabel(attempt.status);
    if (attempt.safe_noop) status.textContent += " · no side effect";
    runId.textContent = attempt.run_id || "—";
    item.append(status, runId);
    fragment.append(item);
  }
  if (attempts.length === 0) {
    const item = document.createElement("li");
    const status = document.createElement("strong");
    status.textContent = "No attempt recorded";
    item.append(status);
    fragment.append(item);
  }
  list.replaceChildren(fragment);

  const applied = statuses.get("applied") || 0;
  const noops = statuses.get("already_remediated") || 0;
  if (state.invoice_status === "corrected" && state.refund_count === 1) {
    setStatusChip("remediation-status", "Corrected · one refund", "success");
  } else {
    setStatusChip("remediation-status", displayLabel(state.invoice_status), "neutral");
  }

  const replayCopy = applied > 0
    ? `${applied} attempt applied, ${noops} replay with no effect. No duplicate refund.`
    : noops > 0
      ? `The durable state was already corrected: ${noops} replay with no effect and still one refund.`
      : "The final counters make idempotency verifiable.";
  setText("remediation-replay-note", replayCopy);
}

function renderChecklist(id, items, emptyCopy) {
  const list = document.getElementById(id);
  const fragment = document.createDocumentFragment();
  const values = Array.isArray(items) ? items : [];

  if (values.length === 0) {
    const item = document.createElement("li");
    item.textContent = emptyCopy;
    fragment.append(item);
  } else {
    for (const value of values) {
      const item = document.createElement("li");
      item.textContent = String(value);
      fragment.append(item);
    }
  }
  list.replaceChildren(fragment);
}

function renderLearning(payload) {
  const learning = payload.learning_proof || {};
  const secondCase = learning.second_case || {};
  const before = learning.before_memory || {};
  const after = learning.after_memory || {};
  const change = learning.measured_change || {};
  const reuse = change.procedural_memory_reuse || {};
  const currency = secondCase.currency || valueAt(payload, "comparison.currency", "EUR");

  setText("case-two-call", secondCase.call_id);
  setText("case-two-overcharge", `${formatMoney(secondCase.overcharge, currency)} overcharged · ${displayLabel(secondCase.verdict)}`);

  const beforeReuse = valueAt(reuse, "before.reused_cases", before.memory_reused ? 1 : 0);
  const afterReuse = valueAt(reuse, "after.reused_cases", after.memory_reused ? 1 : 0);
  setText("change-reuse", `${beforeReuse} → ${afterReuse}`);
  setText("change-checklist", `+${change.checklist_items_loaded ?? 0} steps`);
  setText("change-recommendation", change.recommendation_changed ? "Changed" : "Unchanged");
  setText("change-root-cause", change.suggested_root_cause_confirmed ? "Yes" : "No");

  setStatusChip("before-memory-status", before.memory_reused ? "Memory reused" : "No memory", "neutral");
  setText("before-recommendation", before.recommendation);
  setText("before-step-count", before.procedure_steps_reused ?? 0);
  renderChecklist("before-checklist", before.checklist, "No prior procedure is available.");

  setStatusChip("after-memory-status", after.memory_reused ? "Memory reused" : "No memory", after.memory_reused ? "success" : "neutral");
  setText("after-recommendation", after.recommendation);
  setText("after-step-count", after.procedure_steps_reused ?? 0);
  renderChecklist("after-checklist", after.checklist, "No checklist was retrieved.");
  setText("after-method", displayLabel(after.retrieval_method));
  setText("after-rank", after.retrieval_rank ? `#${after.retrieval_rank}` : "—");
  setText(
    "after-score",
    hasNumber(after.retrieval_score)
      ? percentageFormatter.format(Number(after.retrieval_score))
      : "—",
  );
  setText("after-root-cause", displayLabel(after.root_cause));

  const boundary = learning.advisory_boundary || {};
  setText("boundary-verdict", displayLabel(boundary.verdict_source));
  setText("boundary-financial", displayLabel(boundary.financial_source));

  renderAgentProof(payload);
}

function renderAdvisory(container, source) {
  const fragment = document.createDocumentFragment();
  const lines = String(source || "").split(/\r?\n/);
  for (const rawLine of lines) {
    const line = rawLine.trim();
    if (!line) continue;
    if (line.startsWith("### ")) {
      const heading = document.createElement("h4");
      heading.textContent = line.slice(4).replaceAll("**", "");
      fragment.append(heading);
    } else {
      const paragraph = document.createElement("p");
      paragraph.textContent = line.replace(/^[-*]\s+/, "").replaceAll("**", "");
      fragment.append(paragraph);
    }
  }
  if (!fragment.hasChildNodes()) {
    const paragraph = document.createElement("p");
    paragraph.textContent = "No advisory explanation is available.";
    fragment.append(paragraph);
  }
  container.replaceChildren(fragment);
}

function renderStringList(id, items, formatter = (value) => String(value)) {
  const list = document.getElementById(id);
  const fragment = document.createDocumentFragment();
  const values = Array.isArray(items) ? items : [];
  if (values.length === 0) {
    const item = document.createElement("li");
    item.textContent = "—";
    fragment.append(item);
  } else {
    for (const value of values) {
      const item = document.createElement("li");
      item.textContent = formatter(value);
      fragment.append(item);
    }
  }
  list.replaceChildren(fragment);
}

function renderAgentProof(payload) {
  const panel = document.getElementById("agent-proof");
  const investigation = payload.bedrock_investigation;
  if (!investigation) {
    panel.hidden = true;
    if (technicalNavLink) technicalNavLink.hidden = true;
    return;
  }

  const safety = investigation.safety || {};
  const vector = valueAt(payload, "learning_proof.vector_memory", {});
  panel.hidden = false;
  if (technicalNavLink) technicalNavLink.hidden = false;
  setStatusChip(
    "agent-status",
    displayLabel(investigation.status),
    investigation.status === "completed" ? "success" : "neutral",
  );
  renderAdvisory(document.getElementById("agent-explanation"), investigation.advisory_explanation);
  setText("agent-model", investigation.model_id);
  setText("agent-turns", investigation.model_turns);
  setText("agent-role", displayLabel(safety.model_output_role));
  setText("agent-transport", displayLabel(safety.context_transport));
  setText("agent-access", displayLabel(safety.tool_access));
  setText("agent-mutations", safety.mutations_performed);
  setText("vector-index", vector.index_name);
  setText("vector-model", vector.model_id);
  setText("vector-dimensions", vector.dimensions);
  setText("agent-run-id", investigation.agent_run_id);
  setText("context-snapshot-id", investigation.context_snapshot_id);
  renderStringList(
    "agent-tool-calls",
    investigation.tool_calls,
    (call) => `${displayLabel(call.tool_name)} · ${displayLabel(call.status)}`,
  );
  renderStringList("agent-request-ids", investigation.request_ids);
}

function renderDashboard(payload) {
  renderPlatformBadges(payload);
  renderCaseOne(payload);
  renderLearning(payload);
  setText("selected-audit-subject", valueAt(payload, "decision.subject_id"));
  setText(
    "selected-audit-state",
    `${displayLabel(valueAt(payload, "verdict.category"))} \u00b7 ${formatMoney(valueAt(payload, "comparison.overcharge"), valueAt(payload, "comparison.currency", "EUR"))} customer impact`,
  );
  emptyState.hidden = true;
  dashboard.hidden = false;
  if (demoNav) demoNav.hidden = false;
  document.body.classList.add("has-results");
}

async function parseError(response) {
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    const body = await response.json();
    const detail = body.error || body.detail;
    return detail ? displayLabel(detail) : `HTTP error ${response.status}`;
  }
  const body = await response.text();
  return body.trim() || `HTTP error ${response.status}`;
}

async function requestJson(path, options = {}, timeout = 10_000) {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, {
      ...options,
      headers: { Accept: "application/json", ...options.headers },
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(await parseError(response));
    return await response.json();
  } finally {
    window.clearTimeout(timeoutId);
  }
}

function preferredScrollBehavior() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
}

async function runDemo() {
  if (!lastWorkspace?.can_run_demo) return;
  const isReplay = lastWorkspace.sample_already_audited;
  runButton.disabled = true;
  runButton.setAttribute("aria-busy", "true");
  runButtonLabel.textContent = isReplay ? "Replaying audit" : "Auditing decision";
  runStatus.textContent = isReplay
    ? "Reconstructing the fixed sample decision again..."
    : "Reconstructing the reported decision...";
  errorPanel.hidden = true;

  try {
    const payload = await requestJson("/demo/seed", { method: "POST" }, 45_000);
    if (!payload || typeof payload !== "object" || !payload.decision || !payload.verdict) {
      throw new Error("The response does not contain the expected demo payload.");
    }
    renderWorkspace(payload.workspace);
    renderDashboard(payload);
    document.getElementById("case-one-title")?.focus({ preventScroll: true });
    document.getElementById("outcome")?.scrollIntoView({
      behavior: preferredScrollBehavior(),
      block: "start",
    });
  } catch (error) {
    const message = error instanceof DOMException && error.name === "AbortError"
      ? "The server did not respond within 45 seconds."
      : error instanceof Error
        ? error.message
        : "Unknown error.";
    errorTitle.textContent = "The audit could not run.";
    errorMessage.textContent = message;
    errorPanel.hidden = false;
    await loadWorkspace();
    errorPanel.focus({ preventScroll: true });
    errorPanel.scrollIntoView({ behavior: preferredScrollBehavior(), block: "center" });
  } finally {
    runButton.removeAttribute("aria-busy");
  }
}

async function prepareDemo() {
  const isReplay = lastWorkspace?.sample_already_audited === true;
  prepareDemoButton.disabled = true;
  caseRegister.setAttribute("aria-busy", "true");
  showWorkspaceState(
    isReplay ? "Replay intake" : "Synthetic intake",
    isReplay ? "Preparing the sample replay." : "Loading the sample incident.",
    isReplay
      ? "This reopens only the fixed sample scenario. It does not create a new production incident."
      : "This creates one explicit test report. It does not run the audit.",
  );
  errorPanel.hidden = true;
  try {
    renderWorkspace(await requestJson("/demo/prepare", { method: "POST" }, 10_000));
    document.getElementById("empty-title")?.focus({ preventScroll: true });
  } catch (error) {
    errorTitle.textContent = isReplay
      ? "The sample scenario could not be prepared for replay."
      : "The sample incident could not load.";
    errorMessage.textContent = error instanceof DOMException && error.name === "AbortError"
      ? "The server did not respond within 10 seconds."
      : error instanceof Error
        ? error.message
        : "Unknown error.";
    errorPanel.hidden = false;
    await loadWorkspace();
    errorPanel.focus({ preventScroll: true });
  } finally {
    prepareDemoButton.disabled = false;
  }
}

function retryWorkspace() {
  errorPanel.hidden = true;
  void loadWorkspace();
}

runButton.addEventListener("click", runDemo);
prepareDemoButton.addEventListener("click", prepareDemo);
retryWorkspaceButton.addEventListener("click", retryWorkspace);
void loadWorkspace();
