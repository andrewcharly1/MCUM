from __future__ import annotations

from MCUM.core.state_compiler import compile_state


def test_compile_state_selects_code_graph_hits_before_large_context() -> None:
    compiled = compile_state(
        session_id="session-code-graph",
        project_name="demo",
        project_id="project-1",
        project_scope="same_project",
        task_description="Fix login session validation",
        task_brief={
            "objective": "Fix login session validation",
            "expected_deliverable": "Patch auth flow",
            "success_criteria": "Login validates token",
            "execution_mode": "ejecutar",
            "risk_level": "medio",
            "sources_to_review": ["src/auth.py"],
        },
        skill_selected="mcum-orchestrator",
        skill_status="active",
        dispatch_confidence=0.9,
        dispatch_method="test",
        auto_dispatch_result=None,
        retrieval_mode="test",
        retrieval_latency_ms=3,
        experiences=[],
        failure_patterns=[],
        conflict_cases=[],
        playbooks=[],
        warnings=[],
        execution_policy={
            "state_compiler": {
                "max_context_tokens": 900,
                "max_code_graph_hits": 2,
                "max_experiences": 0,
                "max_knowledge_library_hits": 0,
                "max_active_patterns": 0,
                "max_failure_patterns": 0,
                "max_conflict_cases": 0,
                "max_playbooks": 0,
            }
        },
        code_graph_hits=[
            {
                "id": "node-1",
                "category": "code_graph",
                "title": "function auth.login",
                "relative_path": "src/auth.py",
                "language": "python",
                "node_kind": "function",
                "qualified_name": "auth.login",
                "signature": "login(user_id)",
                "line_start": 10,
                "line_end": 18,
                "score": 0.82,
                "content": {"context": "src/auth.py:10 kind=function edges=in:0 out:2"},
                "evidence_refs": [{"path": "src/auth.py", "line_start": 10, "line_end": 18}],
            }
        ],
    )

    context = compiled.to_context_block()

    assert compiled.selected_counts["code_graph"] == 1
    assert "## Code graph (1):" in context
    assert "auth.login @ src/auth.py:10-18" in context
    assert "read: src/auth.py:10" in context
