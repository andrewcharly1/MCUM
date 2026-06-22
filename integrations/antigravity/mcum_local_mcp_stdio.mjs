#!/usr/bin/env node

import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { CallToolRequestSchema, ListToolsRequestSchema } from "@modelcontextprotocol/sdk/types.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const mcumRoot = path.resolve(__dirname, "../..");
const workspaceRoot = path.resolve(mcumRoot, "../../..");
const workspaceSession = path.join(mcumRoot, "workspace_session.py");
const pythonExe = process.env.MCUM_PYTHON || "py";
const pythonArgs = process.env.MCUM_PYTHON ? [] : ["-3.10"];
const defaultProjectPath = process.env.MCUM_PROJECT_PATH || workspaceRoot;
const defaultProjectName = process.env.MCUM_PROJECT_NAME || path.basename(defaultProjectPath) || "workspace";
const antigravityExecutionPolicy =
  "Antigravity execution policy: use this MCP tool directly for MCUM work. " +
  "Do not call ask_permission for python, py, node, powershell, cmd, bash, or workspace_session.py; " +
  "Antigravity rejects that pattern before execution. Do not create scratch Python scripts for covered MCUM DB, test, record, plan, or preview tasks; " +
  "use the stable mcum_* MCP tools first. This bridge runs the required local process internally.";

function trimText(value, max = Number(process.env.MCUM_MCP_MAX_OUTPUT_CHARS || 6000)) {
  const text = String(value ?? "");
  return text.length > max ? `${text.slice(0, max)}\n...[truncated]` : text;
}

// Force Hugging Face offline mode for child processes so the embedding model
// loads from the local cache without an (unauthenticated, latency-variable)
// Hub network round-trip. That round-trip was the root cause of multi-minute
// cold loads timing out mcum_search_memory. Honor MCUM_EMBEDDING_ALLOW_DOWNLOAD
// for the one-time download on a machine where the model is not yet cached.
function embeddingEnv() {
  const allowDownload = ["1", "true", "yes", "on"].includes(
    String(process.env.MCUM_EMBEDDING_ALLOW_DOWNLOAD || "").trim().toLowerCase()
  );
  if (allowDownload) return {};
  return {
    HF_HUB_OFFLINE: process.env.HF_HUB_OFFLINE || "1",
    TRANSFORMERS_OFFLINE: process.env.TRANSFORMERS_OFFLINE || "1",
    HF_HUB_DISABLE_TELEMETRY: process.env.HF_HUB_DISABLE_TELEMETRY || "1",
    HF_HUB_DISABLE_SYMLINKS_WARNING: process.env.HF_HUB_DISABLE_SYMLINKS_WARNING || "1",
    TOKENIZERS_PARALLELISM: process.env.TOKENIZERS_PARALLELISM || "false"
  };
}

function runProcess(args, options = {}) {
  return new Promise((resolve) => {
    const child = spawn(pythonExe, [...pythonArgs, ...args], {
      cwd: options.cwd || workspaceRoot,
      env: {
        ...process.env,
        PYTHONIOENCODING: "utf-8",
        MCUM_EMBEDDING_BACKEND: process.env.MCUM_EMBEDDING_BACKEND || "hash",
        ...embeddingEnv()
      },
      windowsHide: true
    });

    let stdout = "";
    let stderr = "";
    const timeoutMs = Math.max(1000, Number(options.timeoutMs || 60000));
    const timer = setTimeout(() => {
      if (process.platform === "win32" && child.pid) {
        spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], { windowsHide: true });
      } else {
        child.kill();
      }
      stderr += `\nTimed out after ${timeoutMs} ms.`;
    }, timeoutMs);

    child.stdout.on("data", (chunk) => {
      stdout += chunk.toString("utf8");
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk.toString("utf8");
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout: trimText(stdout), stderr: trimText(stderr) });
    });
  });
}

function toolText(payload) {
  return {
    content: [
      {
        type: "text",
        text: typeof payload === "string" ? payload : JSON.stringify(payload, null, 2)
      }
    ]
  };
}

function isSubPath(parent, child) {
  const relative = path.relative(path.resolve(parent), path.resolve(child));
  return Boolean(relative) && !relative.startsWith("..") && !path.isAbsolute(relative);
}

function isPathInsideOrEqual(parent, child) {
  const resolvedParent = path.resolve(parent);
  const resolvedChild = path.resolve(child);
  return resolvedChild === resolvedParent || isSubPath(resolvedParent, resolvedChild);
}

function scopedPath(inputPath, root, fallback) {
  const resolved = path.resolve(inputPath || fallback);
  if (!isPathInsideOrEqual(root, resolved)) {
    throw new Error(`Path must stay inside ${root}: ${resolved}`);
  }
  return resolved;
}

function boundedInt(value, fallback, min, max) {
  const parsed = Number(value ?? fallback);
  if (!Number.isInteger(parsed) || parsed < min || parsed > max) {
    throw new Error(`Expected integer between ${min} and ${max}.`);
  }
  return parsed;
}

function addIfValue(args, flag, value) {
  if (value !== undefined && value !== null && String(value).length > 0) {
    args.push(flag, String(value));
  }
}

function addRepeated(args, flag, values) {
  for (const value of Array.isArray(values) ? values : []) {
    addIfValue(args, flag, value);
  }
}

function addTaskBriefArgs(args, input = {}) {
  addIfValue(args, "--task", input.task);
  addIfValue(args, "--task-type", input.task_type);
  addIfValue(args, "--objective", input.objective);
  addIfValue(args, "--expected-deliverable", input.expected_deliverable);
  addIfValue(args, "--success-criteria", input.success_criteria);
  addIfValue(args, "--execution-mode", input.execution_mode);
  addIfValue(args, "--risk-level", input.risk_level);
  addIfValue(args, "--validation-required", input.validation_required);
  addIfValue(args, "--editable-scope", input.editable_scope);
  addIfValue(args, "--read-only-scope", input.read_only_scope);
  addIfValue(args, "--protected-scope", input.protected_scope);
  addIfValue(args, "--entrypoint-agent", input.entrypoint_agent || "antigravity");
  addRepeated(args, "--source-to-review", input.source_to_review);
  addRepeated(args, "--constraint", input.constraint);
}

function validateReadonlySql(sql) {
  const text = String(sql || "").trim();
  const lowered = text.toLowerCase();
  if (!text) {
    throw new Error("sql is required.");
  }
  if (text.includes(";")) {
    throw new Error("Only one SQL statement is allowed; semicolons are not accepted.");
  }
  if (!/^(select|with|explain)\b/i.test(text)) {
    throw new Error("Only read-only SELECT, WITH, or EXPLAIN statements are allowed.");
  }
  const forbidden = /\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|copy|call|do|execute|merge|vacuum|refresh|set|reset|listen|notify)\b/i;
  if (forbidden.test(lowered)) {
    throw new Error("SQL contains a non-read-only keyword.");
  }
  return text;
}

async function mcumDbReadOnly(payload = {}, timeoutMs = 120000) {
  const script = [
    "import json, re, sys",
    "from pathlib import Path",
    "root = Path(sys.argv[1])",
    "payload = json.loads(sys.argv[2])",
    "sys.path.insert(0, str(root.parent))",
    "import psycopg",
    "from psycopg.rows import dict_row",
    "from MCUM.db.connection import DATABASE_URL",
    "allowed_schemas = {'project_registry', 'core_brain', 'knowledge_library', 'public'}",
    "ident_re = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')",
    "def clean_ident(value):",
    "    text = str(value or '')",
    "    if not ident_re.match(text):",
    "        raise ValueError(f'Invalid identifier: {text}')",
    "    return text",
    "def limited(value, default=20, min_value=1, max_value=200):",
    "    try:",
    "        parsed = int(value or default)",
    "    except Exception:",
    "        parsed = default",
    "    return max(min_value, min(max_value, parsed))",
    "def fetch(cur, sql, params=None, limit=20):",
    "    cur.execute(sql, params or ())",
    "    return [dict(row) for row in cur.fetchmany(limited(limit))]",
    "mode = payload.get('mode')",
    "limit = limited(payload.get('limit'), 20)",
    "result = {'mode': mode}",
    "with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:",
    "    with conn.cursor() as cur:",
    "        cur.execute('BEGIN READ ONLY')",
    "        cur.execute(\"SET LOCAL statement_timeout = '15s'\")",
    "        if mode == 'overview':",
    "            schemas = payload.get('schemas') or ['project_registry', 'core_brain', 'knowledge_library']",
    "            schemas = [s for s in schemas if s in allowed_schemas]",
    "            result['schemas'] = {}",
    "            for schema in schemas:",
    "                cur.execute('SELECT table_name FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name', (schema,))",
    "                tables = [r['table_name'] for r in cur.fetchall()]",
    "                result['schemas'][schema] = []",
    "                for table in tables:",
    "                    if not ident_re.match(table):",
    "                        continue",
    "                    cur.execute('SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position', (schema, table))",
    "                    columns = [dict(r) for r in cur.fetchall()]",
    "                    try:",
    "                        cur.execute(f'SELECT COUNT(*) AS count FROM {schema}.{table}')",
    "                        count = cur.fetchone()['count']",
    "                    except Exception as exc:",
    "                        conn.rollback(); cur.execute('BEGIN READ ONLY'); cur.execute(\"SET LOCAL statement_timeout = '15s'\")",
    "                        count = f'error: {exc}'",
    "                    result['schemas'][schema].append({'table': table, 'count': count, 'columns': columns})",
    "        elif mode == 'projects':",
    "            text_filter = str(payload.get('filter') or '').lower()",
    "            cur.execute('SELECT id, project_name, project_path, description, status, total_sessions, total_tasks_completed, created_at FROM project_registry.projects ORDER BY created_at DESC')",
    "            rows = [dict(r) for r in cur.fetchall()]",
    "            if text_filter:",
    "                rows = [r for r in rows if text_filter in str(r.get('project_name') or '').lower() or text_filter in str(r.get('project_path') or '').lower() or text_filter in str(r.get('description') or '').lower()]",
    "            result['count'] = len(rows)",
    "            result['projects'] = rows[:limit]",
    "        elif mode == 'mintral_projects':",
    "            terms = ['mintral', 'mejora continua', 'kaizen']",
    "            cur.execute('SELECT id, project_name, project_path, description, status, total_sessions, total_tasks_completed, created_at FROM project_registry.projects ORDER BY created_at DESC')",
    "            rows = [dict(r) for r in cur.fetchall()]",
    "            rows = [r for r in rows if any(term in (str(r.get('project_name') or '') + ' ' + str(r.get('project_path') or '') + ' ' + str(r.get('description') or '')).lower() for term in terms)]",
    "            result['count'] = len(rows)",
    "            result['projects'] = rows[:limit]",
    "        elif mode == 'recent_activity':",
    "            result['projects'] = fetch(cur, 'SELECT id, project_name, project_path, created_at FROM project_registry.projects ORDER BY created_at DESC', limit=limit)",
    "            result['sessions'] = fetch(cur, \"SELECT id, project_id, log_type, title, created_at FROM project_registry.project_logs WHERE log_type IN ('session_start','session_end') ORDER BY created_at DESC\", limit=limit)",
    "            result['logs'] = fetch(cur, 'SELECT id, project_id, log_type, title, outcome, created_at FROM project_registry.project_logs ORDER BY created_at DESC', limit=limit)",
    "        elif mode == 'search_ids':",
    "            ids = [str(x) for x in (payload.get('ids') or [])][:25]",
    "            result['matches'] = []",
    "            for target in ids:",
    "                for sql, label in [",
    "                    ('SELECT * FROM project_registry.spec_contracts WHERE id::text = %s OR session_id::text = %s OR source_task_log_id::text = %s', 'project_registry.spec_contracts'),",
    "                    ('SELECT * FROM core_brain.session_playbooks WHERE id::text = %s OR source_session_id::text = %s OR source_task_log_id::text = %s', 'core_brain.session_playbooks')",
    "                ]:",
    "                    try:",
    "                        cur.execute(sql, (target, target, target))",
    "                        for row in cur.fetchall():",
    "                            result['matches'].append({'target': target, 'source': label, 'row': dict(row)})",
    "                    except Exception:",
    "                        conn.rollback(); cur.execute('BEGIN READ ONLY'); cur.execute(\"SET LOCAL statement_timeout = '15s'\")",
    "        elif mode == 'readonly_sql':",
    "            sql = payload.get('sql')",
    "            cur.execute(sql)",
    "            result['rows'] = [dict(row) for row in cur.fetchmany(limit)]",
    "            result['limit'] = limit",
    "        else:",
    "            raise ValueError(f'Unknown mode: {mode}')",
    "        cur.execute('ROLLBACK')",
    "print(json.dumps(result, ensure_ascii=False, default=str, indent=2))"
  ].join("\n");
  return runProcess(["-c", script, mcumRoot, JSON.stringify(payload)], { cwd: mcumRoot, timeoutMs });
}

async function startStaticServer(input = {}) {
  const directory = path.resolve(input.directory || defaultProjectPath);
  const allowedRoot = path.resolve(input.project_path || defaultProjectPath);
  const port = Number(input.port || 8000);

  if (!Number.isInteger(port) || port < 8000 || port > 8999) {
    return {
      code: 2,
      stdout: "",
      stderr: "Port must be an integer between 8000 and 8999."
    };
  }
  if (!fs.existsSync(directory) || !fs.statSync(directory).isDirectory()) {
    return {
      code: 2,
      stdout: "",
      stderr: `Directory does not exist or is not a directory: ${directory}`
    };
  }
  if (directory !== allowedRoot && !isSubPath(allowedRoot, directory)) {
    return {
      code: 2,
      stdout: "",
      stderr: `Directory must be inside project path: ${allowedRoot}`
    };
  }

  const child = spawn(pythonExe, [...pythonArgs, "-m", "http.server", String(port), "--directory", directory], {
    cwd: directory,
    env: {
      ...process.env,
      PYTHONIOENCODING: "utf-8"
    },
    detached: true,
    stdio: "ignore",
    windowsHide: true
  });
  child.unref();

  return {
    code: 0,
    stdout: JSON.stringify({
      url: `http://localhost:${port}/`,
      directory,
      pid: child.pid
    }, null, 2),
    stderr: ""
  };
}

function commonSessionArgs(input = {}) {
  const args = [
    "--project-path",
    input.project_path || defaultProjectPath,
    "--project-name",
    input.project_name || defaultProjectName,
    "--force-skill",
    input.force_skill || "mcum-orchestrator",
    "--skip-daily-guard"
  ];
  if (input.quiet !== false) {
    args.push("--quiet");
  }
  if (input.auto_improve !== true) {
    args.push("--no-auto-improve");
  }
  if (input.decision) {
    args.push("--decision", String(input.decision));
  }
  if (input.skip_runtime_artifact === true) {
    args.push("--skip-runtime-artifact");
  }
  return args;
}

async function mcumHealth(input = {}) {
  return runProcess([
    workspaceSession,
    "health",
    "--project-path",
    input.project_path || defaultProjectPath,
    "--project-name",
    input.project_name || defaultProjectName
  ], { timeoutMs: 120000 });
}

async function mcumSearchMemory(input = {}) {
  const script = [
    "import json, sys",
    "from pathlib import Path",
    "from dotenv import load_dotenv",
    "root = Path(sys.argv[1])",
    "load_dotenv(root / '.env')",
    "sys.path.insert(0, str(root.parent))",
    "from MCUM.db.experience_store import retrieve_for_task",
    "from MCUM.db.project_registry import get_or_create_project",
    "query = sys.argv[2]",
    "project_path = sys.argv[3] or ''",
    "limit = int(sys.argv[4])",
    "project_id = get_or_create_project(project_path).get('id') if project_path else None",
    "ctx = retrieve_for_task(query, project_id=project_id, policy={'top_k': limit})",
    "print(json.dumps(ctx, ensure_ascii=False, default=str, indent=2))"
  ].join("\n");
  return runProcess([
    "-c",
    script,
    mcumRoot,
    input.query || "",
    input.project_path || "",
    String(input.limit || 5)
  ], { timeoutMs: 45000 });
}

async function mcumDbOverview(input = {}) {
  return mcumDbReadOnly({
    mode: "overview",
    schemas: input.schemas || ["project_registry", "core_brain", "knowledge_library"],
    limit: input.limit || 20
  }, 180000);
}

async function mcumDbListProjects(input = {}) {
  return mcumDbReadOnly({
    mode: input.mintral_only ? "mintral_projects" : "projects",
    filter: input.filter || "",
    limit: input.limit || 50
  });
}

async function mcumDbRecentActivity(input = {}) {
  return mcumDbReadOnly({
    mode: "recent_activity",
    limit: input.limit || 10
  });
}

async function mcumDbSearchIds(input = {}) {
  return mcumDbReadOnly({
    mode: "search_ids",
    ids: input.ids || [],
    limit: input.limit || 20
  });
}

async function mcumDbReadonlySql(input = {}) {
  return mcumDbReadOnly({
    mode: "readonly_sql",
    sql: validateReadonlySql(input.sql),
    limit: input.limit || 50
  });
}

async function mcumRecord(input = {}) {
  const args = [
    workspaceSession,
    "record",
    ...commonSessionArgs(input),
    "--task",
    input.task || "Antigravity task result",
    "--summary",
    input.summary || "",
    "--outcome",
    input.outcome || "success",
    "--confidence",
    String(input.confidence_success ?? input.confidence ?? 0.9)
  ];
  if (input.validation_summary) {
    args.push("--validation-summary", input.validation_summary);
  }
  if (input.error_description) {
    args.push("--error-description", input.error_description);
  }
  return runProcess(args, { timeoutMs: 120000 });
}

async function mcumRunTests(input = {}) {
  const target = scopedPath(input.test_path, mcumRoot, path.join(mcumRoot, "tests"));
  if (!fs.existsSync(target)) {
    return { code: 2, stdout: "", stderr: `Test path does not exist: ${target}` };
  }

  const args = ["-m", "pytest", target, "-q"];
  if (input.keyword) {
    const keyword = String(input.keyword);
    if (!/^[A-Za-z0-9_ .:()[\]\/\\-]+$/.test(keyword)) {
      return { code: 2, stdout: "", stderr: "Keyword contains unsupported characters for a safe pytest -k expression." };
    }
    args.push("-k", keyword);
  }
  if (input.max_failures !== undefined) {
    args.push("--maxfail", String(boundedInt(input.max_failures, 1, 1, 50)));
  }

  const timeoutSeconds = boundedInt(input.timeout_seconds, 900, 30, 3600);
  return runProcess(args, { cwd: mcumRoot, timeoutMs: timeoutSeconds * 1000 });
}

async function mcumCompilePython(input = {}) {
  const target = scopedPath(input.file_path, mcumRoot, workspaceSession);
  if (!target.endsWith(".py")) {
    return { code: 2, stdout: "", stderr: `Only Python files inside MCUM can be compiled: ${target}` };
  }
  if (!fs.existsSync(target) || !fs.statSync(target).isFile()) {
    return { code: 2, stdout: "", stderr: `Python file does not exist: ${target}` };
  }
  return runProcess(["-m", "py_compile", target], { cwd: mcumRoot, timeoutMs: 120000 });
}

async function mcumRunMaintenanceCycle(input = {}) {
  const args = [
    workspaceSession,
    "maintenance-cycle",
    "--project-path",
    input.project_path || mcumRoot,
    "--project-name",
    input.project_name || "MCUM",
    "--force-skill",
    input.force_skill || "mcum-orchestrator",
    "--skip-daily-guard",
    "--skip-skill-factory"
  ];
  if (input.maintenance_name) {
    args.push("--maintenance-name", input.maintenance_name);
  }
  if (input.window_hours !== undefined) {
    args.push("--window-hours", String(boundedInt(input.window_hours, 1, 1, 720)));
  }
  if (input.snapshot_window_days !== undefined) {
    args.push("--snapshot-window-days", String(boundedInt(input.snapshot_window_days, 7, 1, 365)));
  }
  if (input.force === true) {
    args.push("--force");
  }
  if (input.skip_metrics_refresh === true) {
    args.push("--skip-metrics-refresh");
  }
  if (input.skip_kpi_snapshot === true) {
    args.push("--skip-kpi-snapshot");
  }
  return runProcess(args, { cwd: mcumRoot, timeoutMs: 300000 });
}

async function mcumPrepareFrontendQa(input = {}) {
  const args = [
    workspaceSession,
    "frontend-qa",
    ...commonSessionArgs({
      ...input,
      force_skill: input.force_skill || "mcum-orchestrator"
    }),
    "--target-agent",
    input.target_agent || "antigravity",
    "--qa-profile",
    input.qa_profile || "fast"
  ];
  if (input.base_url) {
    args.push("--base-url", String(input.base_url));
  }
  if (input.browser) {
    args.push("--browser", String(input.browser));
  }
  if (input.headed === true) {
    args.push("--headed");
  }
  if (input.write_config === false) {
    args.push("--no-write-config");
  }
  return runProcess(args, { cwd: input.project_path || defaultProjectPath, timeoutMs: 180000 });
}

function autoCodeGraphEnabled() {
  return !["0", "false", "no", "off"].includes(
    String(process.env.MCUM_AUTO_CODE_GRAPH || "1").trim().toLowerCase()
  );
}

async function mcumPrepareIntake(input = {}) {
  if (!input.task) {
    return { code: 2, stdout: "", stderr: "task is required for non-interactive intake." };
  }
  const projectPath = input.project_path || defaultProjectPath;
  const projectName = input.project_name || defaultProjectName;
  const taskType = input.task_type || "validar";
  // Kick off the gated code-graph ensure in the background so the graph is
  // ready (or building) by the time deep analysis starts, without blocking
  // intake. For an explicit/blocking result, call mcum_ensure_code_graph.
  let codeGraphAdvisory;
  if (autoCodeGraphEnabled() && input.auto_code_graph !== false) {
    const launched = fireBackgroundEnsureCodeGraph(projectPath, projectName, taskType);
    codeGraphAdvisory = {
      auto_ensure_started: launched,
      note: launched
        ? "Background code-graph ensure started (incremental; large first builds are deferred). Call mcum_ensure_code_graph before deep code analysis to confirm it is fresh, or mcum_graph_health to inspect it."
        : "Could not start background code-graph ensure; call mcum_ensure_code_graph manually if this task analyzes source.",
      disable_with: "set MCUM_AUTO_CODE_GRAPH=0 or pass auto_code_graph=false"
    };
  }
  return {
    code: 0,
    stdout: JSON.stringify({
      project_path: projectPath,
      project_name: projectName,
      task: input.task,
      task_type: taskType,
      code_graph_advisory: codeGraphAdvisory,
      objective: input.objective || input.task,
      expected_deliverable: input.expected_deliverable || "Brief normalizado para MCUM.",
      success_criteria: input.success_criteria || "La tarea queda descrita sin solicitar input interactivo.",
      execution_mode: input.execution_mode || "proponer",
      risk_level: input.risk_level || "bajo",
      validation_required: input.validation_required || "Revision del brief generado.",
      source_to_review: Array.isArray(input.source_to_review) ? input.source_to_review : [],
      constraint: Array.isArray(input.constraint) ? input.constraint : [],
      editable_scope: input.editable_scope || "",
      read_only_scope: input.read_only_scope || "",
      protected_scope: input.protected_scope || "",
      generated_by: "mcum-local-antigravity-bridge"
    }, null, 2),
    stderr: ""
  };
}

async function mcumGenerateMultiPlan(input = {}) {
  if (!input.task) {
    return { code: 2, stdout: "", stderr: "task is required to generate a multi-agent plan." };
  }
  const args = [
    workspaceSession,
    "multi-plan",
    ...commonSessionArgs({
      ...input,
      force_skill: input.force_skill || "mcum-orchestrator"
    })
  ];
  addTaskBriefArgs(args, {
    task_type: "planificar",
    execution_mode: "proponer",
    risk_level: "medio",
    ...input
  });
  if (input.max_workers !== undefined) {
    args.push("--max-workers", String(boundedInt(input.max_workers, 2, 1, 8)));
  }
  return runProcess(args, { cwd: input.project_path || defaultProjectPath, timeoutMs: 180000 });
}

async function mcumDelegateWorkerTask(input = {}) {
  if (!input.task) {
    return { code: 2, stdout: "", stderr: "task is required to delegate a worker task." };
  }
  const workerCommands = Array.isArray(input.worker_command)
    ? input.worker_command.filter((value) => String(value || "").trim())
    : [];
  if (!workerCommands.length) {
    return { code: 2, stdout: "", stderr: "worker_command is required. Use role=instruction entries." };
  }
  const args = [
    workspaceSession,
    "multi-run",
    ...commonSessionArgs({
      ...input,
      force_skill: input.force_skill || "mcum-orchestrator"
    })
  ];
  addTaskBriefArgs(args, {
    task_type: "mejorar",
    execution_mode: "ejecutar",
    risk_level: "medio",
    ...input
  });
  addIfValue(args, "--workdir", input.workdir || input.project_path || defaultProjectPath);
  addIfValue(args, "--worker-runner", input.worker_runner || "auto");
  if (input.timeout_seconds !== undefined) {
    args.push("--timeout", String(boundedInt(input.timeout_seconds, 300, 1, 3600)));
  }
  if (input.max_workers !== undefined) {
    args.push("--max-workers", String(boundedInt(input.max_workers, 2, 1, 8)));
  }
  for (const command of workerCommands) {
    args.push("--worker-command", String(command));
  }
  return runProcess(args, {
    cwd: input.workdir || input.project_path || defaultProjectPath,
    timeoutMs: Math.max(120000, Number(input.timeout_seconds || 300) * 1000 + 15000)
  });
}

async function mcumCodeGraphIndex(input = {}) {
  const projectPath = input.project_path || defaultProjectPath;
  const args = [
    workspaceSession,
    "code-graph-index",
    "--project-path",
    projectPath,
    "--project-name",
    input.project_name || defaultProjectName,
    "--index-mode",
    input.index_mode || "incremental"
  ];
  if (input.max_file_bytes !== undefined) {
    args.push("--max-file-bytes", String(boundedInt(input.max_file_bytes, 1000000, 1024, 10000000)));
  }
  const excludeDirs = Array.isArray(input.exclude_dir) ? input.exclude_dir : [];
  for (const value of excludeDirs) {
    addIfValue(args, "--exclude-dir", value);
  }
  return runProcess(args, { cwd: projectPath, timeoutMs: Number(input.timeout_seconds || 300) * 1000 });
}

async function mcumEnsureCodeGraph(input = {}) {
  const projectPath = input.project_path || defaultProjectPath;
  const args = [
    workspaceSession,
    "ensure-code-graph",
    "--project-path",
    projectPath,
    "--project-name",
    input.project_name || defaultProjectName
  ];
  addIfValue(args, "--task-type", input.task_type);
  if (input.check_only) args.push("--check-only");
  if (input.force) args.push("--force");
  if (input.allow_large) args.push("--allow-large");
  if (input.no_unified_sync) args.push("--no-unified-sync");
  if (input.max_file_bytes !== undefined) {
    args.push("--max-file-bytes", String(boundedInt(input.max_file_bytes, 1000000, 1024, 10000000)));
  }
  return runProcess(args, { cwd: projectPath, timeoutMs: Number(input.timeout_seconds || 300) * 1000 });
}

// Fire-and-forget: launch the gated ensure in a detached, hidden process so
// prepare_intake stays instant and never blocks on a first-time scan. The CLI
// itself decides (fresh -> no-op, stale -> incremental, large first build ->
// deferred), so this is safe to call on every intake.
function fireBackgroundEnsureCodeGraph(projectPath, projectName, taskType) {
  try {
    const args = [
      ...pythonArgs,
      workspaceSession,
      "ensure-code-graph",
      "--project-path",
      projectPath,
      "--project-name",
      projectName
    ];
    if (taskType) args.push("--task-type", String(taskType));
    const child = spawn(pythonExe, args, {
      cwd: projectPath,
      env: { ...process.env, PYTHONIOENCODING: "utf-8", ...embeddingEnv() },
      detached: true,
      stdio: "ignore",
      windowsHide: true
    });
    child.unref();
    return true;
  } catch {
    return false;
  }
}

async function mcumCodeGraphQuery(input = {}) {
  if (!input.query) {
    return { code: 2, stdout: "", stderr: "query is required." };
  }
  const projectPath = input.project_path || defaultProjectPath;
  const args = [
    workspaceSession,
    "code-graph-query",
    "--project-path",
    projectPath,
    "--project-name",
    input.project_name || defaultProjectName,
    "--query",
    String(input.query),
    "--limit",
    String(boundedInt(input.limit, 8, 1, 50)),
    "--depth",
    String(boundedInt(input.depth, 1, 1, 3))
  ];
  for (const value of Array.isArray(input.language) ? input.language : []) {
    addIfValue(args, "--language", value);
  }
  for (const value of Array.isArray(input.exclude_language) ? input.exclude_language : []) {
    addIfValue(args, "--exclude-language", value);
  }
  addIfValue(args, "--path-prefix", input.path_prefix);
  for (const value of Array.isArray(input.node_kind) ? input.node_kind : []) {
    addIfValue(args, "--node-kind", value);
  }
  return runProcess(args, { cwd: projectPath, timeoutMs: 120000 });
}

async function mcumGraphSync(input = {}) {
  const projectPath = input.project_path || defaultProjectPath;
  const args = [
    workspaceSession,
    "graph-sync",
    "--project-path",
    projectPath,
    "--project-name",
    input.project_name || defaultProjectName
  ];
  addIfValue(args, "--selected-skill", input.selected_skill);
  return runProcess(args, { cwd: projectPath, timeoutMs: Number(input.timeout_seconds || 300) * 1000 });
}

async function mcumGraphQuery(input = {}) {
  if (!input.query) {
    return { code: 2, stdout: "", stderr: "query is required." };
  }
  const projectPath = input.project_path || defaultProjectPath;
  const args = [
    workspaceSession,
    "graph-query",
    "--project-path",
    projectPath,
    "--project-name",
    input.project_name || defaultProjectName,
    "--query",
    String(input.query),
    "--limit",
    String(boundedInt(input.limit, 12, 1, 50))
  ];
  for (const value of Array.isArray(input.entity_type) ? input.entity_type : []) {
    addIfValue(args, "--entity-type", value);
  }
  return runProcess(args, { cwd: projectPath, timeoutMs: 120000 });
}

async function mcumGraphHealth(input = {}) {
  const projectPath = input.project_path || defaultProjectPath;
  return runProcess([
    workspaceSession,
    "graph-health",
    "--project-path",
    projectPath,
    "--project-name",
    input.project_name || defaultProjectName
  ], { cwd: projectPath, timeoutMs: 120000 });
}

async function mcumGraphGetNode(input = {}) {
  if (!input.node_ref) {
    return { code: 2, stdout: "", stderr: "node_ref is required." };
  }
  const projectPath = input.project_path || defaultProjectPath;
  const args = [
    workspaceSession, "graph-get-node",
    "--project-path", projectPath,
    "--project-name", input.project_name || defaultProjectName,
    "--node-ref", String(input.node_ref),
    "--direction", input.direction || "both",
    "--limit", String(boundedInt(input.limit, 25, 1, 100)),
    "--offset", String(boundedInt(input.offset, 0, 0, 100000))
  ];
  for (const value of Array.isArray(input.relation_type) ? input.relation_type : []) {
    addIfValue(args, "--relation-type", value);
  }
  return runProcess(args, { cwd: projectPath, timeoutMs: 120000 });
}

async function mcumGraphNeighbors(input = {}) {
  if (!input.node_ref) {
    return { code: 2, stdout: "", stderr: "node_ref is required." };
  }
  const projectPath = input.project_path || defaultProjectPath;
  const args = [
    workspaceSession, "graph-neighbors",
    "--project-path", projectPath,
    "--project-name", input.project_name || defaultProjectName,
    "--node-ref", String(input.node_ref),
    "--direction", input.direction || "both",
    "--depth", String(boundedInt(input.depth, 1, 0, 8)),
    "--limit", String(boundedInt(input.limit, 25, 1, 100)),
    "--node-budget", String(boundedInt(input.node_budget, 250, 1, 5000))
  ];
  for (const value of Array.isArray(input.relation_type) ? input.relation_type : []) {
    addIfValue(args, "--relation-type", value);
  }
  for (const value of Array.isArray(input.entity_type) ? input.entity_type : []) {
    addIfValue(args, "--entity-type", value);
  }
  return runProcess(args, { cwd: projectPath, timeoutMs: 120000 });
}

async function mcumGraphExplain(input = {}) {
  if (!input.node_ref) {
    return { code: 2, stdout: "", stderr: "node_ref is required." };
  }
  const projectPath = input.project_path || defaultProjectPath;
  const args = [
    workspaceSession, "graph-explain",
    "--project-path", projectPath,
    "--project-name", input.project_name || defaultProjectName,
    "--node-ref", String(input.node_ref)
  ];
  for (const value of Array.isArray(input.relation_type) ? input.relation_type : []) {
    addIfValue(args, "--relation-type", value);
  }
  return runProcess(args, { cwd: projectPath, timeoutMs: 120000 });
}

async function mcumGraphImpact(input = {}) {
  const changedPaths = Array.isArray(input.changed_path) ? input.changed_path : [];
  const changedEntities = Array.isArray(input.changed_entity) ? input.changed_entity : [];
  if (!changedPaths.length && !changedEntities.length) {
    return { code: 2, stdout: "", stderr: "changed_path or changed_entity is required." };
  }
  const projectPath = input.project_path || defaultProjectPath;
  const args = [
    workspaceSession, "graph-impact",
    "--project-path", projectPath,
    "--project-name", input.project_name || defaultProjectName,
    "--max-depth", String(boundedInt(input.max_depth, 3, 0, 8)),
    "--max-items", String(boundedInt(input.max_items, 250, 1, 5000))
  ];
  for (const value of changedPaths) addIfValue(args, "--changed-path", value);
  for (const value of changedEntities) addIfValue(args, "--changed-entity", value);
  if (input.persist === true) args.push("--persist");
  if (input.force === true) args.push("--force");
  return runProcess(args, { cwd: projectPath, timeoutMs: 180000 });
}

async function mcumRunSislDryRun(input = {}) {
  if (!input.skill_name) {
    return { code: 2, stdout: "", stderr: "skill_name is required." };
  }
  const args = [
    workspaceSession,
    "sisl-cycle",
    "--skill-name",
    input.skill_name,
    "--writeback-mode",
    "disabled",
    "--dry-run",
    "--no-persist-eval",
    "--quiet"
  ];
  addIfValue(args, "--project-path", input.project_path);
  addIfValue(args, "--project-name", input.project_name);
  addIfValue(args, "--skill-version", input.skill_version);
  if (input.target_ckl !== undefined) {
    args.push("--target-ckl", String(Number(input.target_ckl)));
  }
  return runProcess(args, { cwd: mcumRoot, timeoutMs: 180000 });
}

async function mcumBootstrapSkillRecords(input = {}) {
  const skills = Array.isArray(input.skill_names) ? input.skill_names : input.skill_name ? [input.skill_name] : [];
  if (!skills.length) {
    return { code: 2, stdout: "", stderr: "skill_name or skill_names is required." };
  }
  const args = [
    workspaceSession,
    "skill-bootstrap",
    "--project-path",
    input.project_path || mcumRoot,
    "--project-name",
    input.project_name || "MCUM",
    "--writeback-mode",
    "disabled",
    "--no-persist-eval",
    "--quiet",
    "--max-tests",
    String(boundedInt(input.max_tests, 8, 0, 50))
  ];
  for (const skillName of skills) {
    args.push("--skill-name", String(skillName));
  }
  if (input.run_sisl === true) {
    args.push("--run-sisl", "--sisl-dry-run");
    if (input.target_ckl !== undefined) {
      args.push("--target-ckl", String(Number(input.target_ckl)));
    }
  }
  return runProcess(args, { cwd: mcumRoot, timeoutMs: 300000 });
}

async function mcumReviewSkillFactory(input = {}) {
  const args = [
    workspaceSession,
    "skill-factory",
    "--project-path",
    input.project_path || mcumRoot,
    "--project-name",
    input.project_name || "MCUM",
    "--promote-only",
    "--max-candidates",
    "0",
    "--min-active-tests",
    "999999",
    "--min-successful-uses",
    "999999",
    "--min-success-rate",
    "1",
    "--min-lifecycle-score",
    "1",
    "--min-testing-uses",
    "999999",
    "--activation-score",
    "2",
    "--rollback-score",
    "-1"
  ];
  return runProcess(args, { cwd: mcumRoot, timeoutMs: 180000 });
}

async function mcumRun(input = {}) {
  const timeoutSeconds = Number(input.timeout_seconds || 60);
  return runProcess([
    workspaceSession,
    "run",
    ...commonSessionArgs(input),
    "--task",
    input.task || input.command || "Antigravity MCUM-managed command",
    "--task-type",
    input.task_type || "automatizar",
    "--objective",
    input.objective || input.task || "Execute command under local MCUM orchestration",
    "--expected-deliverable",
    input.expected_deliverable || "Command result logged in local MCUM",
    "--success-criteria",
    input.success_criteria || "Command exits successfully and MCUM records the session",
    "--execution-mode",
    input.execution_mode || "ejecutar",
    "--risk-level",
    input.risk_level || "medio",
    "--validation-required",
    input.validation_required || "Exit code and MCUM session log",
    "--command",
    input.command || "Write-Output 'MCUM Antigravity bridge ready'",
    "--workdir",
    input.workdir || input.project_path || defaultProjectPath,
    "--timeout",
    String(timeoutSeconds),
    "--summary",
    input.summary || ""
  ], { timeoutMs: Math.max(30000, timeoutSeconds * 1000 + 15000) });
}

async function mcumCompileContext(input = {}) {
  const health = input.include_health === true ? await mcumHealth(input) : { code: 0, stdout: "", stderr: "" };
  const memory = input.query || input.task ? await mcumSearchMemory({
    query: input.query || input.task,
    project_path: input.project_path || defaultProjectPath,
    limit: input.limit || 3
  }) : { code: 0, stdout: "", stderr: "" };
  return {
    code: health.code || memory.code,
    stdout: JSON.stringify({
      health: health.stdout,
      memory: memory.stdout
    }, null, 2),
    stderr: [health.stderr, memory.stderr].filter(Boolean).join("\n")
  };
}

const tools = [
  {
    name: "mcum_db_overview",
    description: `${antigravityExecutionPolicy} Inspect MCUM PostgreSQL schemas/tables/columns/counts through a read-only transaction. Use instead of generating scratch Python scripts for DB inventory.`,
    inputSchema: {
      type: "object",
      properties: {
        schemas: { type: "array", items: { type: "string" } },
        limit: { type: "integer" }
      }
    }
  },
  {
    name: "mcum_db_list_projects",
    description: `${antigravityExecutionPolicy} List MCUM registered projects, optionally filtering for MINTRAL/Kaizen/Mejora Continua, through a read-only transaction.`,
    inputSchema: {
      type: "object",
      properties: {
        filter: { type: "string" },
        mintral_only: { type: "boolean" },
        limit: { type: "integer" }
      }
    }
  },
  {
    name: "mcum_db_recent_activity",
    description: `${antigravityExecutionPolicy} Read recent MCUM projects, sessions, and logs from PostgreSQL without creating scratch scripts.`,
    inputSchema: {
      type: "object",
      properties: {
        limit: { type: "integer" }
      }
    }
  },
  {
    name: "mcum_db_search_ids",
    description: `${antigravityExecutionPolicy} Search MCUM spec contracts and session playbooks for known log/session IDs using read-only database queries.`,
    inputSchema: {
      type: "object",
      properties: {
        ids: { type: "array", items: { type: "string" } },
        limit: { type: "integer" }
      },
      required: ["ids"]
    }
  },
  {
    name: "mcum_db_readonly_sql",
    description: `${antigravityExecutionPolicy} Execute a single read-only SELECT/WITH/EXPLAIN query against MCUM PostgreSQL with DML/DDL keywords blocked and result limits enforced.`,
    inputSchema: {
      type: "object",
      properties: {
        sql: { type: "string" },
        limit: { type: "integer" }
      },
      required: ["sql"]
    }
  },
  {
    name: "mcum_prepare_intake",
    description: `${antigravityExecutionPolicy} Normalize a MCUM task brief non-interactively. This replaces asking to run workspace_session.py intake for planning/testing setup.`,
    inputSchema: {
      type: "object",
      properties: {
        task: { type: "string" },
        task_type: { type: "string" },
        objective: { type: "string" },
        expected_deliverable: { type: "string" },
        success_criteria: { type: "string" },
        execution_mode: { type: "string" },
        risk_level: { type: "string" },
        validation_required: { type: "string" },
        source_to_review: { type: "array", items: { type: "string" } },
        constraint: { type: "array", items: { type: "string" } },
        project_path: { type: "string" },
        project_name: { type: "string" }
      },
      required: ["task"]
    }
  },
  {
    name: "mcum_generate_multi_plan",
    description: `${antigravityExecutionPolicy} Generate a supervised MCUM multi-agent plan and runtime artifacts without executing worker commands.`,
    inputSchema: {
      type: "object",
      properties: {
        task: { type: "string" },
        task_type: { type: "string" },
        objective: { type: "string" },
        expected_deliverable: { type: "string" },
        success_criteria: { type: "string" },
        execution_mode: { type: "string" },
        risk_level: { type: "string" },
        validation_required: { type: "string" },
        editable_scope: { type: "string" },
        read_only_scope: { type: "string" },
        protected_scope: { type: "string" },
        source_to_review: { type: "array", items: { type: "string" } },
        constraint: { type: "array", items: { type: "string" } },
        max_workers: { type: "integer" },
        project_path: { type: "string" },
        project_name: { type: "string" },
        force_skill: { type: "string" }
      },
      required: ["task"]
    }
  },
  {
    name: "mcum_delegate_worker_task",
    description: `${antigravityExecutionPolicy} Delegate bounded worker commands through MCUM multi-run so MCUM can supervise MiniMax SDK, Codex CLI, Gemini CLI, spreadsheet extraction, or PowerShell child work with MCUM logging.`,
    inputSchema: {
      type: "object",
      properties: {
        task: { type: "string" },
        task_type: { type: "string" },
        objective: { type: "string" },
        expected_deliverable: { type: "string" },
        success_criteria: { type: "string" },
        execution_mode: { type: "string" },
        risk_level: { type: "string" },
        validation_required: { type: "string" },
        editable_scope: { type: "string" },
        read_only_scope: { type: "string" },
        protected_scope: { type: "string" },
        worker_command: { type: "array", items: { type: "string" } },
        worker_runner: { type: "string", enum: ["auto", "powershell", "codex-exec", "gemini-cli", "minimax-sdk", "spreadsheet-extractor"] },
        entrypoint_agent: { type: "string" },
        workdir: { type: "string" },
        timeout_seconds: { type: "integer" },
        max_workers: { type: "integer" },
        project_path: { type: "string" },
        project_name: { type: "string" },
        force_skill: { type: "string" }
      },
      required: ["task", "worker_command"]
    }
  },
  {
    name: "mcum_code_graph_index",
    description: `${antigravityExecutionPolicy} Index a project into MCUM native PostgreSQL code_graph so later code tasks can retrieve compact symbol/path context without reading the whole repo.`,
    inputSchema: {
      type: "object",
      properties: {
        project_path: { type: "string" },
        project_name: { type: "string" },
        index_mode: { type: "string", enum: ["full", "incremental"] },
        exclude_dir: { type: "array", items: { type: "string" } },
        max_file_bytes: { type: "integer" },
        timeout_seconds: { type: "integer" }
      }
    }
  },
  {
    name: "mcum_ensure_code_graph",
    description: `${antigravityExecutionPolicy} Gated freshness gate for the code graph: cheaply checks whether the project graph is missing or stale and incrementally indexes it only when needed. Call this before code analysis on a repository you have not indexed yet. A large first build is deferred with a recommendation instead of blocking; pass force or allow_large to override. mcum_prepare_intake already kicks this off in the background automatically.`,
    inputSchema: {
      type: "object",
      properties: {
        project_path: { type: "string" },
        project_name: { type: "string" },
        task_type: { type: "string" },
        check_only: { type: "boolean", description: "Report freshness only; never index." },
        force: { type: "boolean", description: "Re-index even when the fingerprint matches." },
        allow_large: { type: "boolean", description: "Index inline even when a first build is large." },
        no_unified_sync: { type: "boolean", description: "Skip the federated graph projection after indexing." },
        max_file_bytes: { type: "integer" },
        timeout_seconds: { type: "integer" }
      }
    }
  },
  {
    name: "mcum_code_graph_query",
    description: `${antigravityExecutionPolicy} Query MCUM native code_graph for compact code locations before asking a worker to inspect source files.`,
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string" },
        project_path: { type: "string" },
        project_name: { type: "string" },
        limit: { type: "integer" },
        depth: { type: "integer" },
        language: { type: "array", items: { type: "string" } },
        exclude_language: { type: "array", items: { type: "string" } },
        path_prefix: { type: "string" },
        node_kind: { type: "array", items: { type: "string" } }
      },
      required: ["query"]
    }
  },
  {
    name: "mcum_graph_sync",
    description: `${antigravityExecutionPolicy} Synchronize the current project code graph and federated MCUM graph projection after project changes.`,
    inputSchema: {
      type: "object",
      properties: {
        project_path: { type: "string" },
        project_name: { type: "string" },
        selected_skill: { type: "string" },
        timeout_seconds: { type: "integer" }
      }
    }
  },
  {
    name: "mcum_graph_query",
    description: `${antigravityExecutionPolicy} Query project-first federated context across code, experiences, patterns, skills, specs and design system.`,
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string" },
        project_path: { type: "string" },
        project_name: { type: "string" },
        limit: { type: "integer" },
        entity_type: { type: "array", items: { type: "string" } }
      },
      required: ["query"]
    }
  },
  {
    name: "mcum_graph_health",
    description: `${antigravityExecutionPolicy} Inspect the latest federated MCUM graph snapshot and entity counts for one project.`,
    inputSchema: {
      type: "object",
      properties: {
        project_path: { type: "string" },
        project_name: { type: "string" }
      }
    }
  },
  {
    name: "mcum_graph_get_node",
    description: `${antigravityExecutionPolicy} Get one project-scoped federated graph entity with bounded direct relations and evidence.`,
    inputSchema: {
      type: "object",
      properties: {
        node_ref: { type: "string" },
        project_path: { type: "string" },
        project_name: { type: "string" },
        direction: { type: "string", enum: ["in", "out", "both"] },
        relation_type: { type: "array", items: { type: "string" } },
        limit: { type: "integer" },
        offset: { type: "integer" }
      },
      required: ["node_ref"]
    }
  },
  {
    name: "mcum_graph_neighbors",
    description: `${antigravityExecutionPolicy} Traverse bounded project-scoped graph neighbors with cycle and budget protection.`,
    inputSchema: {
      type: "object",
      properties: {
        node_ref: { type: "string" },
        project_path: { type: "string" },
        project_name: { type: "string" },
        direction: { type: "string", enum: ["in", "out", "both"] },
        depth: { type: "integer" },
        relation_type: { type: "array", items: { type: "string" } },
        entity_type: { type: "array", items: { type: "string" } },
        limit: { type: "integer" },
        node_budget: { type: "integer" }
      },
      required: ["node_ref"]
    }
  },
  {
    name: "mcum_graph_explain",
    description: `${antigravityExecutionPolicy} Explain why a project graph entity matters using deterministic evidence.`,
    inputSchema: {
      type: "object",
      properties: {
        node_ref: { type: "string" },
        project_path: { type: "string" },
        project_name: { type: "string" },
        relation_type: { type: "array", items: { type: "string" } }
      },
      required: ["node_ref"]
    }
  },
  {
    name: "mcum_graph_impact",
    description: `${antigravityExecutionPolicy} Analyze bounded change impact and conservatively select tests for one project.`,
    inputSchema: {
      type: "object",
      properties: {
        project_path: { type: "string" },
        project_name: { type: "string" },
        changed_path: { type: "array", items: { type: "string" } },
        changed_entity: { type: "array", items: { type: "string" } },
        max_depth: { type: "integer" },
        max_items: { type: "integer" },
        persist: { type: "boolean" },
        force: { type: "boolean" }
      }
    }
  },
  {
    name: "mcum_run_sisl_dry_run",
    description: `${antigravityExecutionPolicy} Run a SISL evaluation in dry-run mode with writeback disabled and eval persistence disabled.`,
    inputSchema: {
      type: "object",
      properties: {
        skill_name: { type: "string" },
        skill_version: { type: "string" },
        target_ckl: { type: "number" },
        project_path: { type: "string" },
        project_name: { type: "string" }
      },
      required: ["skill_name"]
    }
  },
  {
    name: "mcum_bootstrap_skill_records",
    description: `${antigravityExecutionPolicy} Seed MCUM skill records/tests from local skill docs with writeback disabled. Optional SISL runs are forced to dry-run.`,
    inputSchema: {
      type: "object",
      properties: {
        skill_name: { type: "string" },
        skill_names: { type: "array", items: { type: "string" } },
        max_tests: { type: "integer" },
        run_sisl: { type: "boolean" },
        target_ckl: { type: "number" },
        project_path: { type: "string" },
        project_name: { type: "string" }
      }
    }
  },
  {
    name: "mcum_review_skill_factory",
    description: `${antigravityExecutionPolicy} Run the MCUM skill-factory in a review-only constrained mode: no auto-bootstrap, no candidate creation, promotion thresholds set beyond reach, and rollback disabled.`,
    inputSchema: {
      type: "object",
      properties: {
        project_path: { type: "string" },
        project_name: { type: "string" }
      }
    }
  },
  {
    name: "mcum_run_tests",
    description: `${antigravityExecutionPolicy} Run the local MCUM pytest suite or a scoped test path inside MCUM. This is for non-destructive validation only; it does not accept arbitrary shell commands.`,
    inputSchema: {
      type: "object",
      properties: {
        test_path: { type: "string" },
        keyword: { type: "string" },
        max_failures: { type: "integer" },
        timeout_seconds: { type: "integer" }
      }
    }
  },
  {
    name: "mcum_compile_python",
    description: `${antigravityExecutionPolicy} Compile-check a Python file inside the local MCUM checkout using py_compile. Defaults to workspace_session.py.`,
    inputSchema: {
      type: "object",
      properties: {
        file_path: { type: "string" }
      }
    }
  },
  {
    name: "mcum_run_maintenance_cycle",
    description: `${antigravityExecutionPolicy} Run the local MCUM maintenance/data-registration cycle with skill-factory disabled by default. This is scoped to MCUM metrics, KPIs, and maintenance logs.`,
    inputSchema: {
      type: "object",
      properties: {
        project_path: { type: "string" },
        project_name: { type: "string" },
        maintenance_name: { type: "string" },
        window_hours: { type: "integer" },
        snapshot_window_days: { type: "integer" },
        force: { type: "boolean" },
        skip_metrics_refresh: { type: "boolean" },
        skip_kpi_snapshot: { type: "boolean" },
        force_skill: { type: "string" }
      }
    }
  },
  {
    name: "mcum_prepare_frontend_qa",
    description: `${antigravityExecutionPolicy} Prepare MCUM frontend QA artifacts for testing without asking to run shell commands directly.`,
    inputSchema: {
      type: "object",
      properties: {
        base_url: { type: "string" },
        browser: { type: "string" },
        target_agent: { type: "string" },
        qa_profile: { type: "string" },
        headed: { type: "boolean" },
        write_config: { type: "boolean" },
        project_path: { type: "string" },
        project_name: { type: "string" },
        force_skill: { type: "string" }
      }
    }
  },
  {
    name: "mcum_start_static_server",
    description: `${antigravityExecutionPolicy} Start a scoped localhost static file server for a directory inside the configured project path. Use this instead of asking to run python -m http.server when previewing local HTML.`,
    inputSchema: {
      type: "object",
      properties: {
        directory: { type: "string" },
        project_path: { type: "string" },
        port: { type: "integer" }
      },
      required: ["directory"]
    }
  },
  {
    name: "mcum_local_health",
    description: `${antigravityExecutionPolicy} Check local workspace MCUM health and project registration.`,
    inputSchema: {
      type: "object",
      properties: { project_path: { type: "string" } }
    }
  },
  {
    name: "mcum_search_memory",
    description: `${antigravityExecutionPolicy} Search local PostgreSQL-backed MCUM experience memory before work.`,
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string" },
        project_path: { type: "string" },
        limit: { type: "integer" }
      },
      required: ["query"]
    }
  },
  {
    name: "mcum_compile_context",
    description: `${antigravityExecutionPolicy} Compile a compact local MCUM context package for an Antigravity task.`,
    inputSchema: {
      type: "object",
      properties: {
        task: { type: "string" },
        query: { type: "string" },
        project_path: { type: "string" },
        limit: { type: "integer" },
        include_health: { type: "boolean" }
      },
      required: ["task"]
    }
  },
  {
    name: "mcum_run_managed_command",
    description: `${antigravityExecutionPolicy} Run a PowerShell command under local MCUM orchestration and logging.`,
    inputSchema: {
      type: "object",
      properties: {
        command: { type: "string" },
        workdir: { type: "string" },
        task: { type: "string" },
        task_type: { type: "string" },
        objective: { type: "string" },
        expected_deliverable: { type: "string" },
        success_criteria: { type: "string" },
        execution_mode: { type: "string" },
        risk_level: { type: "string" },
        validation_required: { type: "string" },
        timeout_seconds: { type: "integer" },
        project_path: { type: "string" },
        project_name: { type: "string" },
        force_skill: { type: "string" },
        summary: { type: "string" },
        quiet: { type: "boolean" },
        auto_improve: { type: "boolean" },
        decision: { type: "string", enum: ["keep", "discard", "crash", "partial"] },
        skip_runtime_artifact: { type: "boolean" }
      },
      required: ["command"]
    }
  },
  {
    name: "mcum_record_task_result",
    description: `${antigravityExecutionPolicy} Record an Antigravity task result into local MCUM without executing a command.`,
    inputSchema: {
      type: "object",
      properties: {
        task: { type: "string" },
        summary: { type: "string" },
        outcome: { type: "string" },
        project_path: { type: "string" },
        project_name: { type: "string" },
        force_skill: { type: "string" },
        confidence_success: { type: "number" },
        confidence_failure: { type: "number" }
      },
      required: ["task", "summary"]
    }
  }
];

const handlers = {
  mcum_db_overview: mcumDbOverview,
  mcum_db_list_projects: mcumDbListProjects,
  mcum_db_recent_activity: mcumDbRecentActivity,
  mcum_db_search_ids: mcumDbSearchIds,
  mcum_db_readonly_sql: mcumDbReadonlySql,
  mcum_prepare_intake: mcumPrepareIntake,
  mcum_generate_multi_plan: mcumGenerateMultiPlan,
  mcum_delegate_worker_task: mcumDelegateWorkerTask,
  mcum_code_graph_index: mcumCodeGraphIndex,
  mcum_ensure_code_graph: mcumEnsureCodeGraph,
  mcum_code_graph_query: mcumCodeGraphQuery,
  mcum_graph_sync: mcumGraphSync,
  mcum_graph_query: mcumGraphQuery,
  mcum_graph_health: mcumGraphHealth,
  mcum_graph_get_node: mcumGraphGetNode,
  mcum_graph_neighbors: mcumGraphNeighbors,
  mcum_graph_explain: mcumGraphExplain,
  mcum_graph_impact: mcumGraphImpact,
  mcum_run_sisl_dry_run: mcumRunSislDryRun,
  mcum_bootstrap_skill_records: mcumBootstrapSkillRecords,
  mcum_review_skill_factory: mcumReviewSkillFactory,
  mcum_run_tests: mcumRunTests,
  mcum_compile_python: mcumCompilePython,
  mcum_run_maintenance_cycle: mcumRunMaintenanceCycle,
  mcum_prepare_frontend_qa: mcumPrepareFrontendQa,
  mcum_start_static_server: startStaticServer,
  mcum_local_health: mcumHealth,
  mcum_search_memory: mcumSearchMemory,
  mcum_compile_context: mcumCompileContext,
  mcum_run_managed_command: mcumRun,
  mcum_record_task_result: mcumRecord
};

const server = new Server(
  { name: "mcum-local", version: "0.1.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools }));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const handler = handlers[request.params.name];
  if (!handler) {
    throw new Error(`Unknown tool: ${request.params.name}`);
  }
  const result = await handler(request.params.arguments || {});
  return toolText(result);
});

// If this workspace uses the embedded PostgreSQL, make sure it is running
// before serving tools (it is a process, not a system service). Idempotent.
async function ensureEmbeddedDb() {
  try {
    const envPath = path.join(mcumRoot, ".env");
    if (!fs.existsSync(envPath)) return;
    const env = fs.readFileSync(envPath, "utf8");
    if (!/^MCUM_DB_EMBEDDED\s*=\s*1\s*$/m.test(env)) return;
    const dataDir = (env.match(/^MCUM_PG_DATA_DIR=(.+)$/m) || [])[1];
    const port = parseInt((env.match(/^DB_PORT=(.+)$/m) || [])[1] || "5432", 10);
    const portOpen = () => new Promise((resolve) => {
      const s = new net.Socket();
      s.setTimeout(800);
      s.once("connect", () => { s.destroy(); resolve(true); });
      s.once("timeout", () => { s.destroy(); resolve(false); });
      s.once("error", () => resolve(false));
      s.connect(port, "127.0.0.1");
    });
    if (await portOpen()) return;  // already running
    // `embedded_pg start` uses pg_ctl: it launches postgres as a persistent
    // background process (survives this bridge) with NO window, and returns when
    // the server is ready. Just run it (hidden) and wait for it to finish.
    await new Promise((resolve) => {
      const child = spawn("node", [path.join(mcumRoot, "db", "embedded_pg.mjs"), "start"], {
        env: { ...process.env, ...(dataDir ? { MCUM_PG_DATA_DIR: dataDir.trim() } : {}), MCUM_DB_PORT: String(port) },
        stdio: "ignore",
        windowsHide: true,
      });
      child.on("exit", resolve);
      child.on("error", resolve);
    });
  } catch {
    // best effort; tool calls will surface a clear DB error if it failed.
  }
}

await ensureEmbeddedDb();
const transport = new StdioServerTransport();
await server.connect(transport);
