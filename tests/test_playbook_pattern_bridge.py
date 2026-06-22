"""
Fase 3 - Tests del puente pattern <-> playbook.

Verifican que:
  - save_session_playbook acepta pattern_ids y los persiste en UUID[].
  - save_session_playbook actualiza la fila existente union pattern_ids
    (no destruye vinculos previos).
  - _save_session_playbook de session_manager pasa active_pattern_ids
    del contexto (mock) al save_session_playbook.
  - retrieve_session_playbooks sigue funcionando con playbooks
    retrocompatibles que no tienen pattern_ids.
"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from MCUM.core import session_manager
from MCUM.db import session_playbooks
from MCUM.db.connection import get_cursor, get_db


def _pick_project_id() -> str:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute("SELECT id FROM project_registry.projects LIMIT 1")
            row = cur.fetchone()
            assert row is not None
            return str(row["id"])


def _make_playbook(
    project_id: str,
    *,
    task_suffix: str,
    pattern_ids: list[str] | None = None,
) -> str:
    """Inserta un playbook via API y devuelve su id. Caller limpia."""
    return session_playbooks.save_session_playbook(
        project_id=project_id,
        skill_name="qa-mcum-phase3",
        task_description=f"qa phase3 task {task_suffix}",
        title=f"QA Phase 3 Playbook {task_suffix}",
        output_summary=f"qa phase3 output {task_suffix}",
        validation_summary="validated ok",
        commands=["echo a", "echo b"],
        files_touched=["foo.txt"],
        outcome="success",
        confidence_score=0.85,
        pattern_ids=pattern_ids,
    )


def _fetch(playbook_id: str) -> dict[str, Any] | None:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                "SELECT id, pattern_ids, pattern_alignment_score "
                "FROM core_brain.session_playbooks WHERE id = %s",
                (playbook_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def _cleanup(playbook_id: str) -> None:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                "DELETE FROM core_brain.session_playbooks WHERE id = %s",
                (playbook_id,),
            )


def _insert_active_pattern(skill_name: str) -> str:
    pattern_id = str(uuid.uuid4())
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                INSERT INTO core_brain.patterns (
                    id, name, description, category, status, pattern_key,
                    scope_type, scope_skill_name, promotion_criteria_met,
                    support_count, context_diversity, cohesion_score, avg_score
                ) VALUES (
                    %s, %s, %s, 'implementation_recipe', 'active', %s,
                    'skill', %s, TRUE,
                    5, 5, 0.92, 0.88
                )
                """,
                (
                    pattern_id,
                    f"QA phase3 active pattern {pattern_id[:8]}",
                    "Active pattern used to verify playbook alignment.",
                    f"test:phase3:{pattern_id}",
                    skill_name,
                ),
            )
    return pattern_id


def _cleanup_pattern(pattern_id: str) -> None:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute("DELETE FROM core_brain.patterns WHERE id = %s", (pattern_id,))


def test_save_playbook_persists_pattern_ids() -> None:
    project_id = _pick_project_id()
    pid1, pid2 = str(uuid.uuid4()), str(uuid.uuid4())
    playbook_id = _make_playbook(
        project_id, task_suffix="persist", pattern_ids=[pid1, pid2]
    )
    try:
        row = _fetch(playbook_id)
        assert row is not None
        # psycopg retorna uuid[] como lista de UUID
        ids = [str(x) for x in (row["pattern_ids"] or [])]
        assert ids == [pid1, pid2]
    finally:
        _cleanup(playbook_id)


def test_save_playbook_without_pattern_ids_keeps_empty_array() -> None:
    """Retrocompatibilidad: playbooks existentes o sin pattern_ids
    deben persistir como array vacio, no NULL ni error."""
    project_id = _pick_project_id()
    playbook_id = _make_playbook(project_id, task_suffix="noids")
    try:
        row = _fetch(playbook_id)
        assert row is not None
        assert list(row["pattern_ids"] or []) == []
        assert row["pattern_alignment_score"] is None
    finally:
        _cleanup(playbook_id)


def test_save_playbook_dedupes_pattern_ids() -> None:
    project_id = _pick_project_id()
    pid = str(uuid.uuid4())
    playbook_id = _make_playbook(
        project_id,
        task_suffix="dedup",
        pattern_ids=[pid, pid, pid, "  ", ""],
    )
    try:
        row = _fetch(playbook_id)
        ids = [str(x) for x in (row["pattern_ids"] or [])]
        assert ids == [pid]
    finally:
        _cleanup(playbook_id)


def test_save_playbook_merges_existing_pattern_ids() -> None:
    """Cuando save_session_playbook reusa una fila existente (mismo
    project+skill+task+output), debe unir pattern_ids nuevos con los
    existentes, no sobreescribirlos."""
    project_id = _pick_project_id()
    pid_old = str(uuid.uuid4())
    pid_new = str(uuid.uuid4())
    # Primera creacion con pid_old
    playbook_id = _make_playbook(
        project_id, task_suffix="merge", pattern_ids=[pid_old]
    )
    try:
        # Re-llamada con pid_new (mismo task_description + output_summary)
        same_id = _make_playbook(
            project_id, task_suffix="merge", pattern_ids=[pid_new]
        )
        assert same_id == playbook_id
        row = _fetch(playbook_id)
        ids = [str(x) for x in (row["pattern_ids"] or [])]
        # Union preserva orden: viejo primero, nuevo despues, sin duplicados
        assert ids == [pid_old, pid_new]
    finally:
        _cleanup(playbook_id)


def _stub_session() -> Any:
    """Construye un OrchestratorSession minimo via __new__ con los
    atributos que _save_session_playbook y _log leen."""
    session = session_manager.OrchestratorSession.__new__(session_manager.OrchestratorSession)
    session.project_path = "."
    session.session_id = "sess-test"
    session.verbose = False
    session.task_brief = {}
    return session


def test_session_manager_passes_active_pattern_ids_to_playbook(monkeypatch) -> None:
    """Cuando _save_session_playbook se invoca, debe tomar
    self._ctx.active_patterns y pasarlos como pattern_ids a
    save_session_playbook."""
    project_id = _pick_project_id()
    pid_a = str(uuid.uuid4())
    pid_b = str(uuid.uuid4())
    captured: dict[str, Any] = {}

    def fake_save(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "fake-playbook-id"

    monkeypatch.setattr(session_manager, "save_session_playbook", fake_save)

    session = _stub_session()
    pid_holder = SimpleNamespace(
        project_id=project_id,
        active_patterns=[
            {"id": pid_a, "name": "pat-a"},
            {"id": pid_b, "name": "pat-b"},
            {"id": pid_a, "name": "pat-a-dup"},  # dedup esperado
        ],
    )
    session._ctx = pid_holder

    result = SimpleNamespace(
        outcome="success",
        task_description="phase3 bridge test",
        skill_used="qa-mcum-phase3",
        confidence_score=0.9,
        artifacts=[],
        output_summary="out",
        validation_summary="valid",
        playbook_data={},
    )

    playbook_id = session._save_session_playbook(result, task_log_id="log-1")
    assert playbook_id == "fake-playbook-id"
    assert "pattern_ids" in captured, "save_session_playbook debe recibir pattern_ids"
    assert captured["pattern_ids"] == [pid_a, pid_b]
    assert captured["project_id"] == project_id


def test_session_manager_no_active_patterns_keeps_empty(monkeypatch) -> None:
    """Si no hay active_patterns en el contexto, pattern_ids debe
    ser una lista vacia (no None) - mantiene contrato del tipo array."""
    captured: dict[str, Any] = {}

    def fake_save(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "fake-id"

    monkeypatch.setattr(session_manager, "save_session_playbook", fake_save)

    session = _stub_session()
    pid_holder = SimpleNamespace(
        project_id="00000000-0000-0000-0000-000000000000",
        active_patterns=None,
    )
    session._ctx = pid_holder

    result = SimpleNamespace(
        outcome="success",
        task_description="empty patterns test",
        skill_used="qa-mcum-phase3",
        confidence_score=0.9,
        artifacts=[],
        output_summary=None,
        validation_summary=None,
        playbook_data={},
    )

    session._save_session_playbook(result, task_log_id="log-2")
    assert captured.get("pattern_ids") == []


def test_retrieve_playbooks_handles_legacy_empty_pattern_ids() -> None:
    """retrieve_session_playbooks debe funcionar con playbooks que
    no tienen pattern_ids (no rompe retrocompatibilidad)."""
    project_id = _pick_project_id()
    # Playbook sin pattern_ids (legacy o pre-Fase 3)
    playbook_id = _make_playbook(project_id, task_suffix="legacy")
    try:
        out = session_playbooks.retrieve_session_playbooks(
            "qa phase3 task legacy",
            skill_name="qa-mcum-phase3",
            project_id=project_id,
            limit=3,
        )
        # Si aparece en resultados, pattern_ids debe ser []
        for pb in out["playbooks"]:
            if str(pb["id"]) == playbook_id:
                assert list(pb.get("pattern_ids") or []) == []
                break
    finally:
        _cleanup(playbook_id)


def test_retrieve_playbook_returns_pattern_ids_and_active_alignment() -> None:
    project_id = _pick_project_id()
    pattern_id = _insert_active_pattern("qa-mcum-phase3")
    playbook_id = _make_playbook(
        project_id,
        task_suffix=f"aligned-{pattern_id[:8]}",
        pattern_ids=[pattern_id],
    )
    try:
        out = session_playbooks.retrieve_session_playbooks(
            f"qa phase3 task aligned {pattern_id[:8]}",
            skill_name="qa-mcum-phase3",
            project_id=project_id,
            limit=20,
            min_similarity=0.0,
            active_pattern_ids=[pattern_id],
        )
        playbook = next(item for item in out["playbooks"] if str(item["id"]) == playbook_id)
        assert playbook["pattern_ids"] == [pattern_id]
        assert playbook["pattern_alignment_score"] == pytest.approx(1.0)
    finally:
        _cleanup(playbook_id)
        _cleanup_pattern(pattern_id)


def test_helpers_normalize_and_merge() -> None:
    """Tests unitarios de los helpers puros."""
    assert session_playbooks._normalize_pattern_ids(None) == []
    assert session_playbooks._normalize_pattern_ids([]) == []
    assert session_playbooks._normalize_pattern_ids(["a", "a", "b", "", "  "]) == ["a", "b"]
    assert session_playbooks._merge_unique_ordered(["a", "b"], ["b", "c"]) == ["a", "b", "c"]
    assert session_playbooks._merge_unique_ordered([], None) == []


def test_requested_pattern_alignment_distinguishes_current_pattern() -> None:
    rows = [
        {"id": "pb-current", "pattern_ids": ["pat-current"]},
        {"id": "pb-other", "pattern_ids": ["pat-other"]},
        {"id": "pb-legacy", "pattern_ids": []},
    ]
    aligned = session_playbooks._apply_requested_pattern_alignment(rows, ["pat-current"])
    scores = {row["id"]: row["pattern_alignment_score"] for row in aligned}
    assert scores == {
        "pb-current": 1.0,
        "pb-other": 0.0,
        "pb-legacy": None,
    }
