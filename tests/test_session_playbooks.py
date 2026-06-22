from __future__ import annotations

from types import SimpleNamespace

from MCUM.db import session_playbooks


def test_score_playbooks_prefers_compact_complete_matches_when_similarity_is_close(monkeypatch) -> None:
    monkeypatch.setattr(session_playbooks, "_safe_embed", lambda text: None)

    rows = [
        {
            "id": "pb-compact",
            "title": "Wrapper validation guide",
            "task_description": "Validate wrapper output and close path safely",
            "objective": "Repair and validate wrapper close path",
            "output_summary": "Apply a focused wrapper fix, validate close path safety, and keep console output stable.",
            "validation_summary": "Validated with smoke tests and py_compile.",
            "commands": ["python -m py_compile workspace_session.py", "pytest -q"],
            "files_touched": ["workspace_session.py", "tests/test_workspace_session.py"],
            "reusable_when": "Wrapper output or close-path regressions appear.",
            "issues_avoided": ["false partial due to console encoding"],
            "confidence_score": 0.82,
            "reuse_count": 1,
        },
        {
            "id": "pb-verbose",
            "title": "Wrapper validation guide",
            "task_description": "Validate wrapper output and close path safely",
            "objective": "Repair and validate wrapper close path",
            "output_summary": ("Apply a focused wrapper fix and narrate every observed wrapper branch in detail. " * 26).strip(),
            "validation_summary": "",
            "commands": [
                "python -m py_compile workspace_session.py",
                "pytest -q",
                "python workspace_session.py run",
            ],
            "files_touched": [
                "workspace_session.py",
                "tests/test_workspace_session.py",
                "docs/wrapper-notes.md",
                "reports/wrapper-run.json",
                "reports/wrapper-run-2.json",
            ],
            "reusable_when": "Wrapper issues in general.",
            "issues_avoided": ["false partial due to console encoding"],
            "confidence_score": 0.82,
            "reuse_count": 1,
        },
    ]

    scored = session_playbooks._score_playbooks(
        "wrapper validation close path safely",
        rows,
        min_similarity=0.1,
    )

    assert [item["id"] for item in scored][:2] == ["pb-compact", "pb-verbose"]
    assert scored[0]["_compactness_score"] > scored[1]["_compactness_score"]
    assert scored[0]["_combined_score"] > scored[1]["_combined_score"]
    assert scored[0]["_compactness_profile"]["bloat_penalty"] == 0.0
    assert scored[1]["_compactness_profile"]["bloat_penalty"] > 0.0


class _CursorStub:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.executed: list[tuple[str, object]] = []

    def execute(self, query: str, params: object = None) -> None:
        self.executed.append((query, params))

    def fetchall(self) -> list[dict]:
        return list(self._rows)


class _CursorManager:
    def __init__(self, cursor: _CursorStub) -> None:
        self._cursor = cursor

    def __enter__(self) -> _CursorStub:
        return self._cursor

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _ConnManager:
    def __enter__(self) -> object:
        return SimpleNamespace()

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_retrieve_session_playbooks_applies_memory_governor_soft_filter(monkeypatch) -> None:
    rows = [
        {
            "id": "pb-good",
            "project_id": "project-1",
            "skill_name": "mcum-orchestrator",
            "title": "Validated wrapper guide",
            "task_description": "Repair wrapper safely",
            "objective": "Repair wrapper",
            "output_summary": "Apply the validated wrapper fix and close the session cleanly.",
            "validation_summary": "Validated with smoke tests.",
            "commands": ["pytest -q"],
            "files_touched": ["workspace_session.py"],
            "artifacts": [],
            "issues_avoided": ["false partial"],
            "reusable_when": "Wrapper regressions appear.",
            "outcome": "success",
            "confidence_score": 0.88,
            "reuse_count": 2,
            "last_reused_at": None,
            "created_at": None,
            "updated_at": None,
            "embedding": None,
        },
        {
            "id": "pb-noisy",
            "project_id": "project-1",
            "skill_name": "mcum-orchestrator",
            "title": "Verbose weak guide",
            "task_description": "Repair wrapper safely",
            "objective": "",
            "output_summary": ("Document every branch in very long form. " * 70).strip(),
            "validation_summary": "",
            "commands": ["pytest -q", "python workspace_session.py run", "python workspace_session.py record"],
            "files_touched": ["workspace_session.py", "tests/test_workspace_session.py", "notes.md", "report.json", "report-2.json"],
            "artifacts": [],
            "issues_avoided": [],
            "reusable_when": "",
            "outcome": "partial",
            "confidence_score": 0.31,
            "reuse_count": 0,
            "last_reused_at": None,
            "created_at": None,
            "updated_at": None,
            "embedding": None,
        },
    ]
    cursor = _CursorStub(rows)

    monkeypatch.setattr(session_playbooks, "_safe_embed", lambda text: None)
    monkeypatch.setattr(session_playbooks, "apply_memory_freshness", lambda items, **kwargs: items)
    monkeypatch.setattr(session_playbooks, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(session_playbooks, "get_cursor", lambda conn: _CursorManager(cursor))

    result = session_playbooks.retrieve_session_playbooks(
        "repair wrapper safely",
        skill_name="mcum-orchestrator",
        project_id="project-1",
        limit=2,
        min_similarity=0.1,
        allow_cross_project=True,
        policy={"memory_governor": {"enabled": True, "mode": "soft_filter"}},
    )

    assert [item["id"] for item in result["playbooks"]] == ["pb-good"]
    assert result["memory_governance"]["filtered_count"] == 1
    assert any("Memory governor filtered 1 playbook" in warning for warning in result["warnings"])


def test_retrieve_session_playbooks_assist_can_apply_local_filter(monkeypatch) -> None:
    rows = [
        {
            "id": "pb-good-1",
            "project_id": "project-1",
            "skill_name": "mcum-orchestrator",
            "title": "Validated wrapper guide",
            "task_description": "Repair wrapper safely",
            "objective": "Repair wrapper",
            "output_summary": "Apply the validated wrapper fix and close the session cleanly.",
            "validation_summary": "Validated with smoke tests.",
            "commands": ["pytest -q"],
            "files_touched": ["workspace_session.py"],
            "artifacts": [],
            "issues_avoided": ["false partial"],
            "reusable_when": "Wrapper regressions appear.",
            "outcome": "success",
            "confidence_score": 0.9,
            "reuse_count": 3,
            "last_reused_at": None,
            "created_at": None,
            "updated_at": None,
            "embedding": None,
        },
        {
            "id": "pb-good-2",
            "project_id": "project-1",
            "skill_name": "mcum-orchestrator",
            "title": "Validated validator guide",
            "task_description": "Repair wrapper safely",
            "objective": "Repair wrapper",
            "output_summary": "Run validator and confirm fix.",
            "validation_summary": "Validated with tests.",
            "commands": ["pytest -q"],
            "files_touched": ["tests/test_workspace_session.py"],
            "artifacts": [],
            "issues_avoided": ["unvalidated success"],
            "reusable_when": "Validation is required.",
            "outcome": "success",
            "confidence_score": 0.88,
            "reuse_count": 2,
            "last_reused_at": None,
            "created_at": None,
            "updated_at": None,
            "embedding": None,
        },
        {
            "id": "pb-noisy-1",
            "project_id": "project-2",
            "skill_name": "mcum-orchestrator",
            "title": "Verbose weak guide",
            "task_description": "Repair wrapper safely",
            "objective": "",
            "output_summary": ("Document every branch in very long form. " * 70).strip(),
            "validation_summary": "",
            "commands": ["pytest -q", "python workspace_session.py run", "python workspace_session.py record"],
            "files_touched": ["workspace_session.py", "tests/test_workspace_session.py", "notes.md", "report.json", "report-2.json"],
            "artifacts": [],
            "issues_avoided": [],
            "reusable_when": "",
            "outcome": "partial",
            "confidence_score": 0.16,
            "reuse_count": 0,
            "last_reused_at": None,
            "created_at": None,
            "updated_at": None,
            "embedding": None,
        },
        {
            "id": "pb-noisy-2",
            "project_id": "project-2",
            "skill_name": "mcum-orchestrator",
            "title": "Another verbose weak guide",
            "task_description": "Repair wrapper safely",
            "objective": "",
            "output_summary": ("Document every branch in very long form. " * 70).strip(),
            "validation_summary": "",
            "commands": ["pytest -q", "python workspace_session.py run", "python workspace_session.py record"],
            "files_touched": ["workspace_session.py", "tests/test_workspace_session.py", "notes.md", "report.json", "report-2.json"],
            "artifacts": [],
            "issues_avoided": [],
            "reusable_when": "",
            "outcome": "partial",
            "confidence_score": 0.14,
            "reuse_count": 0,
            "last_reused_at": None,
            "created_at": None,
            "updated_at": None,
            "embedding": None,
        },
    ]
    cursor = _CursorStub(rows)

    monkeypatch.setattr(session_playbooks, "_safe_embed", lambda text: None)
    monkeypatch.setattr(session_playbooks, "apply_memory_freshness", lambda items, **kwargs: items)
    monkeypatch.setattr(session_playbooks, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(session_playbooks, "get_cursor", lambda conn: _CursorManager(cursor))

    result = session_playbooks.retrieve_session_playbooks(
        "repair wrapper safely",
        skill_name="mcum-orchestrator",
        project_id="project-1",
        limit=3,
        min_similarity=0.1,
        allow_cross_project=True,
        policy={"memory_governor": {"enabled": True, "mode": "assist"}},
    )

    assert [item["id"] for item in result["playbooks"]] == ["pb-good-1", "pb-good-2"]
    assert result["memory_governance"]["adaptive_filter_applied"] is True
    assert result["memory_governance"]["effective_mode"] == "assist_plus_local_filter"
    assert result["memory_governance"]["filtered_count"] == 2
    assert any("adaptively filtered 2 playbook" in warning.lower() for warning in result["warnings"])
