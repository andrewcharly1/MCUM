# MCUM Architecture

## Three layers

| Layer | Tech | Responsibility |
|-------|------|----------------|
| **Agent bridge** | Node.js (`integrations/antigravity/mcum_local_mcp_stdio.mjs`) | Standard stdio MCP server exposing 30 `mcum_*` tools. Launched by each agent. |
| **Brain** | Python (`workspace_session.py`, `core/`, `db/`) | Orchestration, gates, embeddings (ONNX), pattern intelligence, SISL. |
| **Store** | PostgreSQL + pgvector | The only source of truth: experiences, playbooks, patterns, code graph, skills. |

Flow: **agent → Node bridge → Python brain → PostgreSQL**. The agent never touches the DB directly.

## PostgreSQL schemas

- `core_brain` — experiences, patterns, pattern_candidates, skill_versions, embeddings (`vector(384)` + HNSW).
- `project_registry` — projects, skill_catalog, logs, KPIs, daily-guard runs.
- `knowledge_library` — concepts and semantic library.
- `code_graph` — AST nodes/edges/files (tree-sitter, 36+ languages).
- `mcum_graph` — federated projection over code + memory + patterns + skills.

## Embeddings

Same `all-MiniLM-L6-v2` weights across backends (interchangeable, vectors identical):

- `onnx` (default) — ONNX Runtime via fastembed. ~2-3 s cold, ~220 MB RAM.
- `sentence-transformers` — PyTorch. Heavier; same vectors.
- `hash` — deterministic, no semantics (tests / ultralight).

pgvector `<=>` (cosine) with HNSW indexes powers semantic retrieval. Loads run fully
offline (`HF_HUB_OFFLINE=1` + `local_files_only`) so there is no Hub round-trip.

## The 7 non-negotiable gates

1. Intake — normalize objective/deliverables/scope/risk.
2. Retrieval — load project-local memory from PostgreSQL.
3. Worker selection — route to a specialist skill or direct implementation.
4. Artifact discipline — produce deliverables in the correct scope.
5. Validation evidence — run concrete validation (tests, counts, rubric).
6. MCUM record — persist task result + artifacts + validation.
7. Failure visibility — if recording fails, state the blocker; never fake success.

## Pattern Intelligence

Discovery runs in **shadow** mode, clustering experiences semantically. Candidates are
promoted only when every strict gate passes (`min_support`, `min_context_diversity`,
`min_cohesion`, `min_avg_confidence`, diversity via `projects_or_contexts`). Activation
re-verifies all gates; sub-threshold candidates stay drafts. Never force-activated.

## SISL (Self-Improving Skill Loop)

Autonomously rewrites targeted `SKILL.md` files only when a CKL quality gate shows
improvement with no regression, always writing a `.bak` backup first.

## Maintenance (Daily Guard)

A delta-driven cycle refreshes metrics/KPIs, audits memory governance, consolidates
duplicate experiences, and runs pattern analysis. Safe (cheap, idempotent) actions
always run; only adaptive actions are rate-capped per run.

## Code graph budget

The federated projection skips the heavy code projection for oversized graphs
(`MCUM_GRAPH_MAX_CODE_PROJECTION_NODES`, default 15000) so a workspace-root indexed
over many sibling folders never times out or goes stale.
