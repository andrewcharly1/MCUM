"""
PostgreSQL persistence for MCUM Spec Contracts.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .connection import get_db, get_cursor


VALID_STATUSES = {
    "draft",
    "auto_generated",
    "confirmed",
    "active",
    "fulfilled",
    "partial",
    "failed",
    "superseded",
}

_DDL_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS project_registry.spec_contracts (
        id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        project_id              UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
        task_id                 TEXT NOT NULL,
        session_id              TEXT,
        source_task_log_id      UUID REFERENCES project_registry.project_logs(id) ON DELETE SET NULL,
        status                  TEXT DEFAULT 'auto_generated'
            CHECK (status IN ('draft','auto_generated','confirmed','active','fulfilled','partial','failed','superseded')),
        spec_mode               TEXT NOT NULL DEFAULT 'lite',
        task_type               TEXT NOT NULL,
        objective               TEXT NOT NULL,
        expected_deliverable    TEXT NOT NULL,
        success_criteria        TEXT NOT NULL,
        execution_mode          TEXT NOT NULL,
        risk_level              TEXT DEFAULT 'medio',
        validation_required     TEXT,
        sources_to_review       JSONB NOT NULL DEFAULT '[]'::jsonb,
        constraints             JSONB NOT NULL DEFAULT '[]'::jsonb,
        contract_payload        JSONB NOT NULL DEFAULT '{}'::jsonb,
        metrics                 JSONB NOT NULL DEFAULT '{}'::jsonb,
        result_summary          TEXT,
        validation_evidence     JSONB NOT NULL DEFAULT '[]'::jsonb,
        artifacts               JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_by_skill        TEXT DEFAULT 'mcum-orchestrator',
        created_at              TIMESTAMPTZ DEFAULT NOW(),
        updated_at              TIMESTAMPTZ DEFAULT NOW(),
        fulfilled_at            TIMESTAMPTZ,
        UNIQUE (project_id, task_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_registry.spec_assumptions (
        id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        spec_contract_id    UUID NOT NULL REFERENCES project_registry.spec_contracts(id) ON DELETE CASCADE,
        assumption_code     TEXT NOT NULL,
        assumption_text     TEXT NOT NULL,
        status              TEXT NOT NULL DEFAULT 'inferred',
        risk_level          TEXT DEFAULT 'medium',
        created_at          TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (spec_contract_id, assumption_code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_registry.spec_scenarios (
        id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        spec_contract_id    UUID NOT NULL REFERENCES project_registry.spec_contracts(id) ON DELETE CASCADE,
        scenario_kind       TEXT NOT NULL,
        title               TEXT NOT NULL,
        given_text          TEXT,
        when_text           TEXT,
        then_text           TEXT,
        created_at          TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_registry.spec_acceptance_criteria (
        id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        spec_contract_id    UUID NOT NULL REFERENCES project_registry.spec_contracts(id) ON DELETE CASCADE,
        criteria_code       TEXT NOT NULL,
        criteria_text       TEXT NOT NULL,
        verification        TEXT,
        required            BOOLEAN NOT NULL DEFAULT TRUE,
        created_at          TIMESTAMPTZ DEFAULT NOW(),
        UNIQUE (spec_contract_id, criteria_code)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_registry.spec_trace_links (
        id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        spec_contract_id    UUID NOT NULL REFERENCES project_registry.spec_contracts(id) ON DELETE CASCADE,
        link_kind           TEXT NOT NULL,
        target_ref          TEXT NOT NULL,
        metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at          TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_spec_contracts_project_status
        ON project_registry.spec_contracts (project_id, status, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_spec_contracts_task_mode
        ON project_registry.spec_contracts (task_type, execution_mode, updated_at DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_spec_trace_links_contract
        ON project_registry.spec_trace_links (spec_contract_id, link_kind)
    """,
]


def _json(value: Any, fallback: Any) -> str:
    if value is None:
        value = fallback
    return json.dumps(value, ensure_ascii=False, default=str)


def _row_to_dict(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {key: (str(value) if hasattr(value, "hex") else value) for key, value in row.items()}


def ensure_spec_schema() -> None:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            for statement in _DDL_STATEMENTS:
                cur.execute(statement)


def upsert_spec_contract(
    *,
    project_id: str,
    task_id: str,
    task_brief: dict[str, Any],
    contract: dict[str, Any],
    session_id: str | None = None,
    source_task_log_id: str | None = None,
    created_by_skill: str = "mcum-orchestrator",
    ensure_schema: bool = True,
) -> dict[str, Any]:
    if ensure_schema:
        ensure_spec_schema()

    status = str(contract.get("status") or "auto_generated")
    if status not in VALID_STATUSES:
        status = "auto_generated"

    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO project_registry.spec_contracts (
                    project_id,
                    task_id,
                    session_id,
                    source_task_log_id,
                    status,
                    spec_mode,
                    task_type,
                    objective,
                    expected_deliverable,
                    success_criteria,
                    execution_mode,
                    risk_level,
                    validation_required,
                    sources_to_review,
                    constraints,
                    contract_payload,
                    metrics,
                    created_by_skill,
                    updated_at
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, NOW()
                )
                ON CONFLICT (project_id, task_id)
                DO UPDATE SET
                    session_id = COALESCE(EXCLUDED.session_id, project_registry.spec_contracts.session_id),
                    source_task_log_id = COALESCE(EXCLUDED.source_task_log_id, project_registry.spec_contracts.source_task_log_id),
                    status = EXCLUDED.status,
                    spec_mode = EXCLUDED.spec_mode,
                    task_type = EXCLUDED.task_type,
                    objective = EXCLUDED.objective,
                    expected_deliverable = EXCLUDED.expected_deliverable,
                    success_criteria = EXCLUDED.success_criteria,
                    execution_mode = EXCLUDED.execution_mode,
                    risk_level = EXCLUDED.risk_level,
                    validation_required = EXCLUDED.validation_required,
                    sources_to_review = EXCLUDED.sources_to_review,
                    constraints = EXCLUDED.constraints,
                    contract_payload = EXCLUDED.contract_payload,
                    metrics = EXCLUDED.metrics,
                    updated_at = NOW()
                RETURNING *
                """,
                (
                    project_id,
                    task_id,
                    session_id,
                    source_task_log_id,
                    status,
                    str(contract.get("mode") or "lite"),
                    str(task_brief.get("task_type") or "analizar"),
                    str(task_brief.get("objective") or ""),
                    str(task_brief.get("expected_deliverable") or ""),
                    str(task_brief.get("success_criteria") or ""),
                    str(task_brief.get("execution_mode") or "ejecutar"),
                    str(task_brief.get("risk_level") or "medio"),
                    str(task_brief.get("validation_required") or ""),
                    _json(task_brief.get("sources_to_review"), []),
                    _json(task_brief.get("constraints"), []),
                    _json(contract, {}),
                    _json(contract.get("summary"), {}),
                    created_by_skill,
                ),
            )
            row = _row_to_dict(cur.fetchone()) or {}
            contract_id = str(row.get("id") or "")

            if contract_id:
                cur.execute("DELETE FROM project_registry.spec_assumptions WHERE spec_contract_id = %s", (contract_id,))
                cur.execute("DELETE FROM project_registry.spec_scenarios WHERE spec_contract_id = %s", (contract_id,))
                cur.execute(
                    "DELETE FROM project_registry.spec_acceptance_criteria WHERE spec_contract_id = %s",
                    (contract_id,),
                )

                for item in contract.get("assumptions") or []:
                    cur.execute(
                        """
                        INSERT INTO project_registry.spec_assumptions (
                            spec_contract_id, assumption_code, assumption_text, status, risk_level
                        ) VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (spec_contract_id, assumption_code)
                        DO UPDATE SET
                            assumption_text = EXCLUDED.assumption_text,
                            status = EXCLUDED.status,
                            risk_level = EXCLUDED.risk_level
                        """,
                        (
                            contract_id,
                            str(item.get("code") or ""),
                            str(item.get("text") or ""),
                            str(item.get("status") or "inferred"),
                            str(item.get("risk") or "medium"),
                        ),
                    )

                for item in contract.get("scenarios") or []:
                    cur.execute(
                        """
                        INSERT INTO project_registry.spec_scenarios (
                            spec_contract_id, scenario_kind, title, given_text, when_text, then_text
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            contract_id,
                            str(item.get("kind") or "scenario"),
                            str(item.get("title") or ""),
                            str(item.get("given") or ""),
                            str(item.get("when") or ""),
                            str(item.get("then") or ""),
                        ),
                    )

                for item in contract.get("acceptance_criteria") or []:
                    cur.execute(
                        """
                        INSERT INTO project_registry.spec_acceptance_criteria (
                            spec_contract_id, criteria_code, criteria_text, verification, required
                        ) VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (spec_contract_id, criteria_code)
                        DO UPDATE SET
                            criteria_text = EXCLUDED.criteria_text,
                            verification = EXCLUDED.verification,
                            required = EXCLUDED.required
                        """,
                        (
                            contract_id,
                            str(item.get("code") or ""),
                            str(item.get("text") or ""),
                            str(item.get("verification") or ""),
                            bool(item.get("required", True)),
                        ),
                    )
            return row


def mark_spec_contract_result(
    *,
    contract_id: str | None = None,
    project_id: str | None = None,
    task_id: str | None = None,
    status: str,
    result_summary: str | None = None,
    validation_evidence: list[dict[str, Any]] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    source_task_log_id: str | None = None,
    trace_links: list[dict[str, Any]] | None = None,
    ensure_schema: bool = True,
) -> bool:
    if ensure_schema:
        ensure_spec_schema()
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid spec status: {status}")
    if not contract_id and not (project_id and task_id):
        return False

    clauses: list[str] = []
    params: list[Any] = []
    if contract_id:
        clauses.append("id = %s")
        params.append(contract_id)
    else:
        clauses.append("project_id = %s")
        clauses.append("task_id = %s")
        params.extend([project_id, task_id])

    fulfilled_at = datetime.now(timezone.utc) if status in {"fulfilled", "partial", "failed"} else None
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                f"""
                UPDATE project_registry.spec_contracts
                SET status = %s,
                    result_summary = COALESCE(%s, result_summary),
                    validation_evidence = %s,
                    artifacts = %s,
                    source_task_log_id = COALESCE(%s, source_task_log_id),
                    fulfilled_at = COALESCE(%s, fulfilled_at),
                    updated_at = NOW()
                WHERE {' AND '.join(clauses)}
                RETURNING id
                """,
                [
                    status,
                    result_summary,
                    _json(validation_evidence, []),
                    _json(artifacts, []),
                    source_task_log_id,
                    fulfilled_at,
                    *params,
                ],
            )
            row = cur.fetchone()
            if not row:
                return False
            resolved_contract_id = str(row["id"])
            for link in trace_links or []:
                target_ref = str(link.get("target_ref") or "").strip()
                if not target_ref:
                    continue
                cur.execute(
                    """
                    INSERT INTO project_registry.spec_trace_links (
                        spec_contract_id, link_kind, target_ref, metadata
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    (
                        resolved_contract_id,
                        str(link.get("link_kind") or "artifact"),
                        target_ref,
                        _json(link.get("metadata"), {}),
                    ),
                )
            return True


def get_spec_contract(
    *,
    project_id: str,
    task_id: str,
    ensure_schema: bool = True,
) -> dict[str, Any] | None:
    if ensure_schema:
        ensure_spec_schema()
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT *
                FROM project_registry.spec_contracts
                WHERE project_id = %s AND task_id = %s
                LIMIT 1
                """,
                (project_id, task_id),
            )
            return _row_to_dict(cur.fetchone())
