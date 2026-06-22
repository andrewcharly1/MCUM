"use strict";

const state = {
  summary: {},
  connectors: [],
  agents: [],
  graph: {},
  errors: [],
};

const statuses = ["connected", "configured", "degraded", "failed", "disabled", "unknown"];
const titles = {
  summary: "Resumen",
  connectors: "Conectores",
  agents: "Agentes",
  graph: "Grafo",
};

const byId = (id) => document.getElementById(id);
const valueOf = (item, keys, fallback = "-") => {
  for (const key of keys) {
    if (item && item[key] !== undefined && item[key] !== null && item[key] !== "") return item[key];
  }
  return fallback;
};
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");
const formatNumber = (value) => {
  const number = Number(value);
  return Number.isFinite(number) ? new Intl.NumberFormat("es-CL").format(number) : "-";
};
const formatDate = (value) => {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? escapeHtml(value)
    : new Intl.DateTimeFormat("es-CL", { dateStyle: "short", timeStyle: "short" }).format(date);
};
const formatDuration = (value) => {
  if (value === undefined || value === null || value === "") return "-";
  const milliseconds = Number(value);
  if (!Number.isFinite(milliseconds)) return escapeHtml(value);
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  if (milliseconds < 60000) return `${(milliseconds / 1000).toFixed(1)} s`;
  return `${(milliseconds / 60000).toFixed(1)} min`;
};
const formatCost = (value) => {
  const number = Number(value);
  return Number.isFinite(number) ? `$${number.toFixed(number < 1 ? 4 : 2)}` : "-";
};
const statusBadge = (status) => {
  const normalized = statuses.includes(status) ? status : "unknown";
  return `<span class="status ${normalized}"><span></span>${escapeHtml(normalized)}</span>`;
};
const emptyRow = (columns, label = "Sin datos") =>
  `<tr><td class="empty" colspan="${columns}">${escapeHtml(label)}</td></tr>`;

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${path}: HTTP ${response.status}`);
  return response.json();
}

async function loadData() {
  byId("refresh").classList.add("loading");
  state.errors = [];
  const requests = [
    ["summary", "/api/summary"],
    ["connectors", "/api/connectors"],
    ["agents", "/api/agents"],
    ["graph", "/api/graph"],
  ];
  const results = await Promise.allSettled(requests.map((item) => fetchJson(item[1])));
  results.forEach((result, index) => {
    const key = requests[index][0];
    if (result.status === "fulfilled") {
      state[key] = key === "connectors" || key === "agents"
        ? (result.value.items || [])
        : result.value;
    } else {
      state.errors.push(result.reason.message);
    }
  });
  render();
  byId("refresh").classList.remove("loading");
  byId("updated-at").textContent = new Intl.DateTimeFormat("es-CL", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date());
}

function statCard(label, value, tone = "") {
  return `<article class="stat-card ${tone}">
    <span>${escapeHtml(label)}</span>
    <strong>${escapeHtml(value)}</strong>
  </article>`;
}

function renderSummary() {
  const summary = state.summary || {};
  const connectorStatuses = summary.connector_statuses || {};
  const connected = connectorStatuses.connected || 0;
  const issues = (connectorStatuses.degraded || 0) + (connectorStatuses.failed || 0);
  byId("summary-stats").innerHTML = [
    statCard("Proyectos activos", formatNumber(valueOf(summary, ["projects_active", "active_projects"], 0))),
    statCard("Conectores online", formatNumber(connected), "positive"),
    statCard("Agentes", formatNumber(summary.agents_total || state.agents.length)),
    statCard("Tareas", formatNumber(valueOf(summary, ["tasks_total", "task_count"], 0))),
    statCard("Tokens", formatNumber(valueOf(summary, ["tokens_total", "total_tokens"], 0))),
    statCard("Incidencias", formatNumber(issues), issues ? "negative" : "positive"),
  ].join("");

  byId("connector-total").textContent = summary.connectors_total ?? state.connectors.length;
  const maxStatus = Math.max(1, ...statuses.map((status) => connectorStatuses[status] || 0));
  byId("status-bars").innerHTML = statuses.map((status) => {
    const count = connectorStatuses[status] || 0;
    const width = Math.round((count / maxStatus) * 100);
    return `<div class="status-bar">
      <span>${escapeHtml(status)}</span>
      <div><i class="${status}" style="width:${width}%"></i></div>
      <strong>${formatNumber(count)}</strong>
    </div>`;
  }).join("");

  const alerts = [];
  state.connectors.forEach((item) => {
    if (["failed", "degraded", "unknown"].includes(item.status)) {
      alerts.push({
        title: valueOf(item, ["display_name", "name", "connector_key"], "Conector"),
        detail: valueOf(item, ["message", "detail", "error"], item.status),
        status: item.status,
      });
    }
  });
  if (["failed", "degraded", "unknown"].includes(state.graph.status)) {
    alerts.push({ title: "Grafo", detail: valueOf(state.graph, ["message", "error"], state.graph.status), status: state.graph.status });
  }
  byId("alert-total").textContent = alerts.length;
  byId("alerts").innerHTML = alerts.length
    ? alerts.slice(0, 8).map((item) => `<div class="alert-item">
        ${statusBadge(item.status)}
        <div><strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.detail)}</span></div>
      </div>`).join("")
    : '<div class="empty-block">Sin alertas</div>';

  const recent = [...state.agents].sort((a, b) =>
    String(b.last_activity_at || "").localeCompare(String(a.last_activity_at || ""))
  ).slice(0, 8);
  byId("recent-agents").innerHTML = recent.length
    ? recent.map((item) => `<tr>
        <td><strong>${escapeHtml(valueOf(item, ["display_name", "name", "agent", "worker"]))}</strong></td>
        <td>${escapeHtml(valueOf(item, ["role", "agent_profile"]))}</td>
        <td>${escapeHtml(valueOf(item, ["project_name", "project"]))}</td>
        <td>${statusBadge(item.status)}</td>
        <td>${formatDuration(valueOf(item, ["duration_ms", "latency_ms"], null))}</td>
        <td>${formatDate(item.last_activity_at)}</td>
      </tr>`).join("")
    : emptyRow(6);
}

function matchesFilter(item, query, status) {
  const haystack = JSON.stringify(item).toLowerCase();
  return (!query || haystack.includes(query)) && (!status || item.status === status);
}

function renderConnectors() {
  const query = byId("connector-search").value.trim().toLowerCase();
  const status = byId("connector-status").value;
  const items = state.connectors.filter((item) => matchesFilter(item, query, status));
  byId("connector-filter-count").textContent = items.length;
  byId("connectors-table").innerHTML = items.length
    ? items.map((item) => `<tr>
        <td><strong>${escapeHtml(valueOf(item, ["display_name", "name", "connector_key"]))}</strong>
          <small>${escapeHtml(valueOf(item, ["provider", "connector_key"], ""))}</small></td>
        <td>${escapeHtml(valueOf(item, ["connector_type", "type"]))}</td>
        <td>${statusBadge(item.status)}</td>
        <td>${formatDate(item.last_activity_at)}</td>
        <td>${formatDuration(valueOf(item, ["latency_ms"], null))}</td>
        <td class="detail-cell">${escapeHtml(valueOf(item, ["message", "detail", "error"], ""))}</td>
      </tr>`).join("")
    : emptyRow(6);
}

function renderAgents() {
  const query = byId("agent-search").value.trim().toLowerCase();
  const status = byId("agent-status").value;
  const items = state.agents.filter((item) => matchesFilter(item, query, status));
  byId("agent-filter-count").textContent = items.length;
  byId("agents-table").innerHTML = items.length
    ? items.map((item) => `<tr>
        <td><strong>${escapeHtml(valueOf(item, ["display_name", "name", "agent", "worker"]))}</strong>
          <small>${escapeHtml(valueOf(item, ["role", "agent_profile"], ""))}</small></td>
        <td>${escapeHtml(valueOf(item, ["model", "recommended_model"]))}
          <small>${escapeHtml(valueOf(item, ["provider"], ""))}</small></td>
        <td><strong>${escapeHtml(valueOf(item, ["project_name", "project"]))}</strong>
          <small>${escapeHtml(valueOf(item, ["task", "objective"], ""))}</small></td>
        <td>${statusBadge(item.status)}</td>
        <td>${formatDuration(valueOf(item, ["duration_ms", "latency_ms"], null))}</td>
        <td>${formatNumber(valueOf(item, ["tokens", "total_tokens"], null))}</td>
        <td>${formatCost(valueOf(item, ["cost", "cost_usd"], null))}</td>
      </tr>`).join("")
    : emptyRow(7);
}

function renderGraph() {
  const graph = state.graph || {};
  byId("graph-stats").innerHTML = [
    statCard("Archivos", formatNumber(valueOf(graph, ["files", "file_count"], 0))),
    statCard("Nodos", formatNumber(valueOf(graph, ["nodes", "node_count", "entity_count"], 0))),
    statCard("Relaciones", formatNumber(valueOf(graph, ["relations", "edges", "relation_count"], 0))),
    statCard("Comunidades", formatNumber(valueOf(graph, ["communities", "community_count"], 0))),
    statCard("Snapshots", formatNumber(valueOf(graph, ["snapshots", "snapshot_count"], 0))),
    statCard("No-change", formatDuration(valueOf(graph, ["no_change_latency_ms", "wall_clock_ms"], null))),
  ].join("");
  byId("graph-status").innerHTML = statusBadge(graph.status);
  const details = [
    ["Proyecto", valueOf(graph, ["project_name", "project"])],
    ["Version", valueOf(graph, ["version", "code_graph_version", "extractor_version"])],
    ["Ultimo sync", formatDate(valueOf(graph, ["last_activity_at", "last_sync_at"], null))],
    ["Snapshot", valueOf(graph, ["snapshot_id", "latest_snapshot_id"])],
    ["Modo", valueOf(graph, ["mode", "sync_mode"])],
  ];
  byId("graph-details").innerHTML = details.map((item) =>
    `<div><dt>${escapeHtml(item[0])}</dt><dd>${escapeHtml(item[1])}</dd></div>`
  ).join("");

  const coverage = graph.coverage || {};
  const coverageItems = Object.entries(coverage);
  byId("graph-coverage").innerHTML = coverageItems.length
    ? coverageItems.map(([key, value]) => {
        const numeric = Math.max(0, Math.min(100, Number(value) || 0));
        return `<div class="coverage-item">
          <div><span>${escapeHtml(key)}</span><strong>${numeric}%</strong></div>
          <div class="coverage-track"><i style="width:${numeric}%"></i></div>
        </div>`;
      }).join("")
    : '<div class="empty-block">Sin metricas</div>';

  const errors = Array.isArray(graph.errors) ? graph.errors : [];
  byId("graph-error-count").textContent = errors.length;
  byId("graph-errors").innerHTML = errors.length
    ? errors.map((item) => `<div class="graph-error">
        <span>${formatDate(valueOf(item, ["created_at", "time"], null))}</span>
        <strong>${escapeHtml(valueOf(item, ["code", "type"], "error"))}</strong>
        <p>${escapeHtml(valueOf(item, ["message", "detail"], item))}</p>
      </div>`).join("")
    : '<div class="empty-block">Sin errores</div>';
}

function render() {
  const banner = byId("error-banner");
  banner.hidden = state.errors.length === 0;
  banner.textContent = state.errors.join(" | ");
  renderSummary();
  renderConnectors();
  renderAgents();
  renderGraph();
}

document.querySelectorAll(".tab").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("active", item === button));
    document.querySelectorAll(".view").forEach((view) => view.classList.toggle("active", view.id === `view-${button.dataset.tab}`));
    byId("view-title").textContent = titles[button.dataset.tab];
  });
});
["connector-search", "connector-status"].forEach((id) => byId(id).addEventListener("input", renderConnectors));
["agent-search", "agent-status"].forEach((id) => byId(id).addEventListener("input", renderAgents));
byId("refresh").addEventListener("click", loadData);

loadData();
setInterval(loadData, 30000);
