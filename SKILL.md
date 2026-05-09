---
name: mcum-orchestrator
description: |
  Primary orchestration and operational memory skill for the CERTIFICACION LABORAL workspace.
  Use it as the outer wrapper for any task on workspace projects so retrieval, logging,
  artifacts, and experience persistence stay aligned in PostgreSQL.
---

# MCUM Orchestrator

Use this skill as the mandatory outer orchestration layer for work inside
`C:\Users\carlo\OneDrive\Escritorio\CERTIFICACION LABORAL\` and its child projects.

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
4. When running scripts or commands, prefer an MCUM-managed execution path so outputs and artifacts can be logged.
5. Record task result, relevant artifacts, and any durable learning before closing the session.
6. Close the session consistently with task outcome and session-end logging.

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

## Key Outputs

- Reliable workspace-wide orchestration through MCUM
- Reproducible schema installation
- Reliable retrieval and session logging
- Clean skill metadata aligned with Codex skill mechanics
- Validated PostgreSQL state for `core_brain` and `project_registry`
