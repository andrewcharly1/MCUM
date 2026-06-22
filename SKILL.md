---
name: mcum-orchestrator
description: |
  Primary orchestration and operational memory skill for the this workspace.
  Use it as the outer wrapper for any task on workspace projects so retrieval, logging,
  artifacts, and experience persistence stay aligned in PostgreSQL.
routing_triggers: ["mcum", "mcum-orchestrator", "memoria mcum", "schema mcum", "postgresql mcum", "retrieval mcum", "session flow", "project registry", "experience store", "skill catalog", "sisl mcum"]
routing_anti: ["dashboard html", "flutter ui", "liquidacion sueldo"]
routing_priority: 9
---

# MCUM Orchestrator

Use this skill as the mandatory outer orchestration layer for work inside
`C:\Users\dev\workspace\` and its child projects.

MCUM may delegate specialist skills when needed, but it remains responsible for session
start, retrieval, logging, artifacts, and session end.

## Use When

- The user wants to analyze, review, debug, correct, refactor, test, or validate any project in this workspace.
- The user wants to run scrapers, training jobs, extraction flows, or document-processing pipelines.
- The user wants to inspect or repair MCUM PostgreSQL data.
- The user wants to validate `schema.sql`, setup, retrieval, logging, or session flow.
- The user wants to query or clean `core_brain` and `project_registry`.
- The user wants to improve the dispatcher, session manager, experience store, or SISL code.

## Do Not Use When

- The user explicitly asks to avoid MCUM.
- The task is outside this workspace and unrelated to the local MCUM runtime.

## Workflow

1. Start an MCUM-managed session for the target project before substantial work.
2. Retrieve existing context from PostgreSQL for the task and project path.
3. If a specialist skill is needed, treat it as a downstream worker under MCUM rather than replacing MCUM.
4. Before creating scripts or running terminal commands, check whether an existing MCP tool covers the task.
5. When running scripts or commands is unavoidable, prefer an MCUM-managed execution path so outputs and artifacts can be logged.
6. Treat temporary scratch files as exceptional artifacts, not the default execution mechanism.
7. Record task result, relevant artifacts, and any durable learning before closing the session.
8. Close the session consistently with task outcome and session-end logging. Coordinator
   close must always finish with the project-scoped `task_end` graph update.

## Native Code Graph Contract

- The coordinator synchronizes `code_graph` at session begin and session close. Worker
  sessions defer synchronization to the coordinator to avoid duplicate scans and writes.
- Every explicit `project_path` is an isolated MCUM project. Never merge sibling folders
  under `the workspace` into one graph unless the user explicitly selects their
  common parent as the project path.
- Incremental synchronization compares the current source manifest with PostgreSQL and
  parses only new or modified files; deleted files are removed from the graph. A no-change
  cycle must report `files_indexed=0` and `tokens_indexed_estimate=0`.
- Coordinator close always executes a final `task_end` update. If `session_close` detected
  code changes, that delta is carried into the final federated projection; if code did not
  change, MCUM refreshes memory/spec/playbook entities without rewriting the code projection.
- `mcum_graph` is the federated projection over code, experiences, patterns, playbooks,
  skills, specs, and approved design systems. Source schemas remain the source of truth.
- Before asking a worker to inspect a large repository, query compact graph context with
  `mcum_graph_query`, `mcum_code_graph_query`, `workspace_session.py graph-query`, or
  `workspace_session.py code-graph-query`. Apply `language`, `exclude_language`,
  `path_prefix`, or `node_kind` filters when the task targets one layer.
- Supervised workers receive a bounded `worker_context_slice` derived from the coordinator's
  persisted `ProjectContextEnvelope`. Workers do not write graph or learning state directly.
- Persisted experiences are linked to stable project-relative paths and graph symbols in
  `code_graph.experience_links`. Graph retrieval automatically returns applicable linked
  experiences alongside code locations.
- PostgreSQL/PostgREST consumers may call `code_graph.context_pack_filtered`; do not import
  full source files into PostgreSQL or send the complete repository to a reasoning worker.
- Codex, Claude Code, OpenCode, and Antigravity remain host agents. MCUM remains the
  supervising orchestrator and provides the same graph, memory, routing, and audit contract
  to each host.

## Pattern Intelligence Contract

- `core_brain.pattern_candidates` is the staging boundary for empirically observed
  operational patterns. Discovery is semantic, incremental, cached, and excludes
  `regulatory_rule` experiences.
- Pattern discovery runs in `shadow` mode. Discovery itself never creates an active pattern
  or promotes a draft; it only stages candidates and evidence.
- Promotion happens in a separate governed step. When `pattern_policy.auto_promote` is
  `false`, `workspace_session.py pattern-accept --confirm` (then `pattern-activate --confirm`)
  is the only materialization path. When `auto_promote` is `true`, the maintenance cycle calls
  `auto_promote_ready_candidates`, which materializes each review-ready candidate and then
  re-checks EVERY quality gate via `activate_pattern`; candidates that fail any gate stay as
  drafts and are never force-activated. Manual `pattern-accept`/`pattern-activate` remain valid.
- Active retrieval excludes patterns with `health_state='degraded'`. Session closure records
  pattern usage and outcomes so utility is measured from real tasks.
- The SQL evidence trigger recomputes metrics only. Promotion decisions belong to the
  governed Python policy (auto-promote with full gate re-checks) or explicit human review.

## Non-Negotiable Session Gates

For any task inside `the workspace`, or any task explicitly executed under
MCUM control, the work is not complete until all gates below are satisfied. If any gate
fails, report the task as `partial` or `failure`; do not present it as fully complete.

1. `MCUM intake`: normalize the task objective, expected deliverables, editable scope,
   protected original files, validation requirements, and risk level before execution.
2. `MCUM retrieval`: inspect project-local memory or recent task context when relevant,
   especially when the user mentions prior errors, standards, formats, or patterns.
3. `Worker selection`: explicitly decide the downstream worker skill or tool path under
   MCUM control. Specialist skills may execute work, but MCUM remains the outer owner.
4. `Artifact discipline`: create or modify deliverables only in the intended scope, normally
   `output/`, unless the user explicitly requests a different destination.
5. `Validation evidence`: run concrete validations appropriate to the artifact type before
   closure, such as DOCX open/page count, XLSX formula/open checks, PDF export, tests, or
   source-to-pauta comparison.
6. `MCUM record`: persist the task result with artifacts and validation summary before the
   final user response. The final response should include the `mcum_session_id` when the
   record succeeds.
7. `Failure visibility`: if MCUM recording fails because of environment issues such as
   paging-file errors, embedding model failures, unavailable PostgreSQL, or locked files,
   retry once using the lightest available MCUM path. If it still fails, state the blocker
   clearly in the final response and mark the MCUM gate as not closed.

Never say "listo", "completado", "validado" or equivalent as the final status unless
validation evidence and MCUM recording both exist. If the deliverable exists but MCUM did
not record, say "artefacto generado y validado; registro MCUM pendiente/fallido".

## Universal MCUM Registration Rule

No MCUM-governed work may remain outside memory. This includes:

- academic deliverables, reports, forum answers, Word/PDF/Excel files;
- code edits, reviews, tests, debugging and validation runs;
- project analysis, data extraction, scrapers, dashboards and automation;
- correction-only tasks, even when the user asks only for a quick check;
- meta-work on skills, workflows, policies, MCUM itself, or this instruction set.

Every final response for MCUM-governed work must be backed by one of these states:

- `recorded`: MCUM accepted the result and returned a session/log id.
- `record_failed`: artifacts may exist, but MCUM failed after retry; final response states
  the failure, the reason, and that memory persistence is incomplete.
- `not_mcum_scope`: only allowed when the task is outside this workspace or the user
  explicitly opted out of MCUM.

## Strict Intake Gate

Before execution, MCUM should normalize a structured task brief and block work when the intake is incomplete.

Required brief fields:
- `project_path`
- `task_type`
- `objective`
- `expected_deliverable`
- `success_criteria`
- `execution_mode`

Recommended brief fields:
- `sources_to_review`
- `constraints`
- `risk_level`
- `validation_required`

Strict mode rules:
- Do not proceed if the task brief is incomplete or unconfirmed.
- Prefer project-local memory before cross-project retrieval.
- Do not mark a task as `success` without validation evidence.
- Do not mark a task as `success` without a persisted task result and artifact list.
- Do not provide a final success response before the MCUM record step succeeds.
- Do not materialize autonomous SISL feedback into `SKILL.md` unless execution policy explicitly enables writeback.

## Operating Rules

- Treat [db/schema.sql](./db/schema.sql) as the source of truth for database structure.
- Keep [install_schema.py](./install_schema.py) and [setup.py](./setup.py) aligned with that schema.
- For runtime flow, inspect:
  - [core/session_manager.py](./core/session_manager.py)
  - [db/project_registry.py](./db/project_registry.py)
  - [db/experience_store.py](./db/experience_store.py)
  - [core/dispatcher.py](./core/dispatcher.py)
- If retrieval quality is touched, also read [directives/retrieval_policy.json](./directives/retrieval_policy.json).
- Every persisted experience must include:
  - `conclusion`
  - `context`
  - `applicability`
  - `not_applicable_cases`
- Record session start, retrieval, task result, and session end consistently.
- Prefer explicit project paths and deterministic logging metadata.
- For workspace execution, prefer the MCUM wrapper CLI instead of raw script execution when practical.

## Tool-First And Scratch Policy

Do not create ad hoc `scratch/*.py` files for tasks already covered by MCUM tools.

Preferred order:

1. Use a stable MCP tool when one exists.
2. Use a generic safe MCP tool for dynamic cases, such as read-only SQL or scoped tests.
3. Use `workspace_session.py` only when the task needs MCUM session semantics that are not exposed through MCP.
4. Create a temporary scratch script only when the logic is new, non-trivial, and not covered by existing tools.

Use these MCP tools instead of scratch scripts:

- PostgreSQL inventory: `mcum_db_overview`
- Project listing and MINTRAL/Kaizen filtering: `mcum_db_list_projects`
- Recent projects/sessions/logs: `mcum_db_recent_activity`
- Session/log ID lookup: `mcum_db_search_ids`
- Dynamic database inspection: `mcum_db_readonly_sql`
- Tests: `mcum_run_tests`
- Python compile checks: `mcum_compile_python`
- Task result logging: `mcum_record_task_result`
- Static HTML previews: `mcum_start_static_server`
- Planning and non-mutating improvement review: `mcum_prepare_intake`, `mcum_generate_multi_plan`, `mcum_run_sisl_dry_run`, `mcum_review_skill_factory`

## Command Execution Contract

Before running commands under MCUM, check the command contract below. If a command fails
because of argument shape, encoding, locks, or shell mismatch, correct the pattern once
and record both the failed attempt and the corrected attempt in the task summary.

Correct contracts:

- `task_type` values are Spanish enums: `analizar`, `crear`, `corregir`, `mejorar`, `planificar`, `validar`, `automatizar`.
- `execution_mode` values are Spanish enums: `analizar`, `proponer`, `ejecutar`. Never use `managed_command`, `validation`, `execute`, or other English aliases.
- Use `mcum_prepare_intake` before substantial edits and `mcum_record_task_result` before final response.
- Use `mcum_run_managed_command` for workspace commands that should be logged; pass `force_skill="mcum-orchestrator"` when working inside this workspace.
- In PowerShell, prefer `-LiteralPath` for user paths, especially OneDrive paths, accents, spaces, brackets, or wildcard characters.
- For Python/openpyxl paths with accents, discover files with `Path(...).glob(...)` or pass paths through Python raw strings created from discovered `Path` objects; avoid manually typing lossy `?` replacement paths.
- If an output DOCX/XLSX is locked by Word/Excel/OneDrive, do not fight the lock. Generate a new final filename or `FINAL_SUBIR/` copy, then report the lock and the final file to use.
- When validating a generated DOCX, check both text and tables; Word tables are not included in paragraph-only extraction.
- If terminal output shows mojibake but Python `unicode_escape` or document internals are correct, treat it as console encoding noise and do not rewrite good text.
- Do not rely on process exit code alone for worker tasks; inspect the payload/artifact/result and persistence log.

Anti-patterns to avoid:

- Passing `execution_mode="managed_command"` to MCUM tools.
- Treating an Excel support/example file as the case source when the PDF/rubric contains the official case data.
- Saying a task is complete after file generation but before final rubric/specification review and MCUM record.
- Creating scratch scripts for database checks or command logging that have an MCP tool equivalent.
- Chaining destructive file operations across shells or using wildcard paths where `-LiteralPath` is safer.
- Using Spanish comments with accents inside `psql -c` queries — causes encoding errors like `secuencia de bytes no válida para codificación «UTF8»: 0xfa`. Use English-only comments or omit comments entirely.
- Using `ILIKE '%diseño%'` or any accented characters in SQL string patterns — same encoding error. Use ASCII-only patterns.
- Running parallel Bash calls to `psql` when the query correctness is uncertain — if one fails, all parallel calls cancel. Run sequentially.
- Querying a PostgreSQL table without first checking `information_schema.columns` for column names — columns like `skill_version` that don't exist cause immediate errors.

PostgreSQL psql encoding rules (strict):

1. Never use accents, eñes, or special Spanish characters (`ó, é, í, ú, á, ü, ñ`) in comments inside `-c` queries
2. Never use accented characters in SQL string patterns (`LIKE '%diseño%'`)
3. Always verify column names via `information_schema.columns` before querying an unfamiliar table
4. Use `LIMIT` instead of `-- comments` to paginate results
5. Execute psql queries sequentially when unsure; avoid parallel tool calls
6. For Windows PowerShell: use `2> $null` (with space), not `2>$null`
7. `core_brain` is a SCHEMA, not a table — query `core_brain.experiences`, `core_brain.patterns`, etc.
8. Use `pg_isready -h localhost -p 5432` to verify connectivity before running query commands

For the detailed checklist, read [references/mcum_command_execution_contract.md](./references/mcum_command_execution_contract.md).

Scratch file rules:

- Never embed database passwords or secrets in generated scratch files.
- Put unavoidable temporary files under `.agent/runtime/scratch/<task-id>/`, not under client conversation folders.
- Include metadata that says whether the file is `temp`, `artifact`, or `keep`.
- Remove `temp` scratch files after use or record why they were kept.
- If a scratch pattern is needed repeatedly, convert it into a generic MCP tool rather than generating more scripts.

## Key Outputs

- Reliable workspace-wide orchestration through MCUM
- Reproducible schema installation
- Reliable retrieval and session logging
- Clean skill metadata aligned with Codex skill mechanics
- Validated PostgreSQL state for `core_brain` and `project_registry`
