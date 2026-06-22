from __future__ import annotations

from contextlib import contextmanager
from io import BytesIO, StringIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from urllib import error
import uuid

import pytest

from MCUM.core import code_graph_indexer, code_graph_sync, minimax_credentials, minimax_worker
from MCUM.core.graph_extractors import media_adapters
from MCUM.core.graph_extractors.base import ArtifactBudget
from MCUM.core.minimax_credentials import MiniMaxCredentials
from MCUM.db import (
    code_graph_store,
    connection,
    design_system_store,
    embedder,
    experience_store,
    graph_intelligence_store,
    knowledge_library_semantic,
    knowledge_library_taxonomy_backfill,
    project_registry,
    skill_catalog,
    spec_store,
    unified_graph_store,
)


PROJECT_ID = "11111111-1111-4111-8111-111111111111"
ENTITY_ID = "22222222-2222-4222-8222-222222222222"
OTHER_ID = "33333333-3333-4333-8333-333333333333"


class FakeCursor:
    def __init__(
        self,
        *,
        one: list[dict[str, object] | None] | None = None,
        all_rows: list[list[dict[str, object]]] | None = None,
    ) -> None:
        self.executions: list[tuple[str, object]] = []
        self.one = list(one or [])
        self.all_rows = list(all_rows or [])
        self.rowcount = 1

    def execute(self, sql: str, params: object = None) -> None:
        self.executions.append((sql, params))

    def fetchone(self) -> dict[str, object] | None:
        if self.one:
            return self.one.pop(0)
        return {"id": ENTITY_ID}

    def fetchall(self) -> list[dict[str, object]]:
        return self.all_rows.pop(0) if self.all_rows else []


def _install_fake_db(monkeypatch: pytest.MonkeyPatch, module: object, cur: FakeCursor) -> None:
    @contextmanager
    def fake_db():
        yield object()

    @contextmanager
    def fake_cursor(_conn: object):
        yield cur

    monkeypatch.setattr(module, "get_db", fake_db)
    monkeypatch.setattr(module, "get_cursor", fake_cursor)


def _credential(protocol: str = "openai") -> MiniMaxCredentials:
    return MiniMaxCredentials(
        api_key="secret",
        base_url="https://minimax.example/v1",
        protocol=protocol,
        model="MiniMax-M3",
        source="test",
    )


def _run_minimax_main(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], payload: object) -> tuple[int, dict]:
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(payload)))
    code = minimax_worker.main()
    output = json.loads(capsys.readouterr().out)
    return code, output


def test_minimax_worker_helpers_and_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    assert minimax_worker._clip(None) == ""
    assert minimax_worker._clip("abcdef", 4).endswith("[clipped]")
    assert minimax_worker._as_dict([]) == {}
    assert minimax_worker._json_loads('{"ok": true}')["ok"] is True
    assert minimax_worker._parse_structured_content("") is None
    assert minimax_worker._parse_structured_content("```json\n{\"status\":\"partial\"}\n```") == {"status": "partial"}
    assert minimax_worker._parse_structured_content("[]") is None
    assert minimax_worker._summary_from_content("", None).startswith("MiniMax worker completed")
    assert minimax_worker._summary_from_content(" first\nsecond", None) == "first"
    assert minimax_worker._status_from_content({"status": "failure"}) == "failure"
    assert minimax_worker._status_from_content({"status": "odd"}) == "success"
    assert minimax_worker._join_url("https://x/", "/tail") == "https://x/tail"
    assert minimax_worker._anthropic_messages_url("https://x/v1/") == "https://x/v1/messages"
    assert minimax_worker._anthropic_messages_url("https://x") == "https://x/v1/messages"

    prompt = minimax_worker._build_worker_prompt(
        {
            "command": "inspect",
            "project_path": r"C:\repo",
            "worker": {"role": "reviewer", "mode": "read_only"},
            "worker_brief": {"objective": "review", "worker_context_slice": {"a": "b"}},
            "model_route": {"token_budget": {"context_in": 10, "output": 20}},
        },
        max_prompt_chars=220,
    )
    assert prompt.endswith("[prompt clipped by MCUM MiniMax worker]")

    captured: list[tuple[str, dict, dict, int]] = []

    def fake_post(url: str, headers: dict, body: dict, *, timeout: int) -> dict:
        captured.append((url, headers, body, timeout))
        if "chat/completions" in url:
            return {"choices": [{"message": {"content": "openai"}}], "usage": {"prompt_tokens": 2}}
        return {
            "content": [{"type": "text", "text": "a"}, {"type": "ignored"}, {"type": "text", "text": "b"}],
            "usage": {"input_tokens": 3, "output_tokens": 4},
        }

    monkeypatch.setattr(minimax_worker, "_post_json", fake_post)
    content, usage = minimax_worker._call_openai_compatible(
        credential=_credential(),
        prompt="p",
        system_prompt="s",
        temperature=0.1,
        max_output_tokens=20,
        timeout=5,
    )
    assert content == "openai" and usage["prompt_tokens"] == 2
    content, usage = minimax_worker._call_anthropic_compatible(
        credential=_credential("anthropic"),
        prompt="p",
        system_prompt="s",
        temperature=0.1,
        max_output_tokens=20,
        timeout=5,
    )
    assert content == "a\nb" and usage == {"input_tokens": 3, "output_tokens": 4}
    assert len(captured) == 2

    assert minimax_worker._normalize_usage({}, prompt="12345678", content="1234") == {
        "input_tokens": 2,
        "output_tokens": 1,
        "total_tokens": 3,
    }
    assert minimax_worker._normalize_usage(
        {"input_tokens": "2", "output_tokens": "3", "total_tokens": "9"},
        prompt="",
        content="",
    )["total_tokens"] == 9
    result = minimax_worker._result_payload(
        status="failure",
        summary="bad",
        credential=_credential(),
        error_message="boom",
    )
    assert result["available"] is True and result["error"] == "boom"


def test_minimax_post_json_success_and_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __init__(self, body: bytes) -> None:
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return self.body

    monkeypatch.setattr(minimax_worker.request, "urlopen", lambda *_args, **_kwargs: Response(b"[1,2]"))
    assert minimax_worker._post_json("https://x", {}, {}, timeout=1) == {"raw": [1, 2]}

    monkeypatch.setattr(minimax_worker.request, "urlopen", lambda *_args, **_kwargs: Response(b"not-json"))
    with pytest.raises(RuntimeError, match="non-JSON"):
        minimax_worker._post_json("https://x", {}, {}, timeout=1)

    http_error = error.HTTPError("https://x", 500, "bad", {}, BytesIO(b"provider failed"))
    monkeypatch.setattr(minimax_worker.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(http_error))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        minimax_worker._post_json("https://x", {}, {}, timeout=1)

    url_error = error.URLError("offline")
    monkeypatch.setattr(minimax_worker.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(url_error))
    with pytest.raises(RuntimeError, match="connection failed"):
        minimax_worker._post_json("https://x", {}, {}, timeout=1)


def test_minimax_main_contracts(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "stdin", StringIO("{broken"))
    assert minimax_worker.main() == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failure"

    code, output = _run_minimax_main(monkeypatch, capsys, [])
    assert code == 2 and "must be an object" in output["summary"]

    monkeypatch.setattr(minimax_worker, "resolve_minimax_credentials", lambda _policy: None)
    code, output = _run_minimax_main(monkeypatch, capsys, {})
    assert code == 2 and output["error"] == "missing_minimax_credentials"

    monkeypatch.setattr(minimax_worker, "resolve_minimax_credentials", lambda _policy: _credential())
    monkeypatch.setenv("MCUM_MINIMAX_DRY_RUN", "yes")
    code, output = _run_minimax_main(monkeypatch, capsys, {"command": "review", "model": "override"})
    assert code == 0 and output["summary"] == "MiniMax dry run completed."

    monkeypatch.delenv("MCUM_MINIMAX_DRY_RUN")
    monkeypatch.setattr(
        minimax_worker,
        "_call_openai_compatible",
        lambda **_kwargs: ('{"status":"partial","summary":"done"}', {"prompt_tokens": 4, "completion_tokens": 2}),
    )
    code, output = _run_minimax_main(monkeypatch, capsys, {"command": "review"})
    assert code == 0 and output["status"] == "partial" and output["usage"]["total_tokens"] == 6

    monkeypatch.setattr(minimax_worker, "resolve_minimax_credentials", lambda _policy: _credential("anthropic"))
    monkeypatch.setattr(minimax_worker, "_call_anthropic_compatible", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("down")))
    code, output = _run_minimax_main(monkeypatch, capsys, {"command": "review"})
    assert code == 1 and output["error"] == "down"


def test_code_graph_sync_all_operational_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    assert code_graph_sync.sync_project_code_graph(
        project_id=PROJECT_ID,
        project_path=".",
        project_name="demo",
        policy={"enabled": False},
        trigger="test",
    ) == {"status": "disabled", "trigger": "test"}

    calls: list[str] = []
    monkeypatch.setattr(code_graph_sync, "ensure_code_graph_schema", lambda: calls.append("schema"))
    monkeypatch.setattr(
        code_graph_sync,
        "get_code_graph_manifest",
        lambda **_kwargs: {"graph": {"extractor_version": "old"}, "files": {"a.py": {}}},
    )
    monkeypatch.setattr(
        code_graph_sync,
        "load_graph_policy",
        lambda: SimpleNamespace(
            features=SimpleNamespace(tree_sitter=True),
            priority_languages=["python"],
            budgets=SimpleNamespace(analytics=SimpleNamespace(max_nodes=50)),
        ),
    )
    monkeypatch.setattr(code_graph_sync, "scan_project_code_graph", lambda *_args, **_kwargs: {"stats": {"files": 1}})
    monkeypatch.setattr(code_graph_sync, "persist_index_result", lambda **_kwargs: {"status": "success"})
    monkeypatch.setattr(code_graph_sync, "backfill_experience_code_links", lambda **_kwargs: {"status": "success", "links_created": 2})
    result = code_graph_sync.sync_project_code_graph(
        project_id=PROJECT_ID,
        project_path=".",
        project_name="demo",
        policy={},
        trigger="session",
    )
    assert result["mode"] == "full" and result["extractor_changed"] is True and result["backfill"]["links_created"] == 2

    monkeypatch.setattr(code_graph_sync, "get_code_graph_manifest", lambda **_kwargs: {"graph": {"extractor_version": code_graph_sync.EXTRACTOR_VERSION}, "files": {}})
    monkeypatch.setattr(code_graph_sync, "persist_index_result", lambda **_kwargs: {"status": "no_changes"})
    result = code_graph_sync.sync_project_code_graph(
        project_id=PROJECT_ID,
        project_path=".",
        project_name=None,
    )
    assert result["mode"] == "incremental" and result["backfill"]["status"] == "skipped"

    stale: list[str] = []
    monkeypatch.setattr(code_graph_sync, "ensure_code_graph_schema", lambda: (_ for _ in ()).throw(RuntimeError("schema down")))
    monkeypatch.setattr(code_graph_sync, "mark_code_graph_stale", lambda **kwargs: stale.append(kwargs["error_message"]))
    result = code_graph_sync.sync_project_code_graph(project_id=PROJECT_ID, project_path=".", project_name=None)
    assert result["status"] == "failure" and stale == ["schema down"]
    monkeypatch.setattr(code_graph_sync, "mark_code_graph_stale", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("also down")))
    assert code_graph_sync.sync_project_code_graph(project_id=PROJECT_ID, project_path=".", project_name=None)["status"] == "failure"


def test_builtin_media_adapters(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pages = [SimpleNamespace(extract_text=lambda: " page one "), SimpleNamespace(extract_text=lambda: "")]
    monkeypatch.setitem(
        sys.modules,
        "pypdf",
        SimpleNamespace(PdfReader=lambda _path: SimpleNamespace(pages=pages, is_encrypted=True, metadata={"/A": "B"})),
    )
    pdf = media_adapters.extract_pdf(tmp_path / "a.pdf", enable_ocr=False, enable_transcription=False, budget=ArtifactBudget())
    assert pdf.metadata["pages"] == 2 and len(pdf.sections) == 1

    class FakeImage:
        width = 10
        height = 20
        format = "PNG"
        mode = "RGB"
        n_frames = 2

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setitem(sys.modules, "PIL", SimpleNamespace(Image=SimpleNamespace(open=lambda _path: FakeImage())))
    image = media_adapters.extract_image(tmp_path / "a.png", enable_ocr=False, enable_transcription=False, budget=ArtifactBudget())
    assert image.metadata["frames"] == 2

    class Capture:
        def __init__(self, opened: bool, fps: float = 4.0) -> None:
            self.opened = opened
            self.fps = fps
            self.released = False

        def isOpened(self) -> bool:
            return self.opened

        def get(self, key: int) -> float:
            return {1: self.fps, 2: 20, 3: 640, 4: 480}[key]

        def release(self) -> None:
            self.released = True

    opened = Capture(True)
    cv2 = SimpleNamespace(
        VideoCapture=lambda _path: opened,
        CAP_PROP_FPS=1,
        CAP_PROP_FRAME_COUNT=2,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
    )
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    video = media_adapters.extract_video(tmp_path / "a.mp4", enable_ocr=False, enable_transcription=False, budget=ArtifactBudget())
    assert video.metadata["duration_seconds"] == 5.0 and len(video.sections) == 5 and opened.released

    monkeypatch.setattr(cv2, "VideoCapture", lambda _path: Capture(False))
    with pytest.raises(ValueError, match="could not be opened"):
        media_adapters.extract_video(tmp_path / "bad.mp4", enable_ocr=False, enable_transcription=False, budget=ArtifactBudget())
    assert set(media_adapters.default_artifact_adapters()) == {"pdf", "image", "video"}


def test_graph_intelligence_store_load_and_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    assert graph_intelligence_store._valid_uuid(PROJECT_ID)
    assert not graph_intelligence_store._valid_uuid("bad")
    assert json.loads(graph_intelligence_store._json(None)) == {}
    with pytest.raises(ValueError):
        graph_intelligence_store.load_project_graph(project_id="bad")
    with pytest.raises(ValueError):
        graph_intelligence_store.persist_analytics_result({"project_id": "bad"})
    with pytest.raises(ValueError):
        graph_intelligence_store.persist_impact_result({"project_id": "bad"})
    with pytest.raises(ValueError):
        graph_intelligence_store.persist_comparison_result({"left_project_id": "bad"})
    with pytest.raises(ValueError):
        graph_intelligence_store.persist_artifact_result(project_id="bad", result={})
    with pytest.raises(ValueError):
        graph_intelligence_store.persist_artifact_result(project_id=PROJECT_ID, result={})

    cur = FakeCursor(
        one=[
            {"id": OTHER_ID},
            {"id": ENTITY_ID},
            {"id": OTHER_ID},
            {"id": ENTITY_ID},
            {"id": OTHER_ID},
            {"id": ENTITY_ID},
            {"id": OTHER_ID},
        ],
        all_rows=[
            [{"id": ENTITY_ID, "title": "node"}],
            [{"id": OTHER_ID, "source_entity_id": ENTITY_ID, "target_entity_id": ENTITY_ID}],
        ],
    )
    _install_fake_db(monkeypatch, graph_intelligence_store, cur)
    graph = graph_intelligence_store.load_project_graph(project_id=PROJECT_ID, max_nodes=1, max_edges=1, entity_types=["skill", ""])
    assert len(graph["nodes"]) == 1 and len(graph["edges"]) == 1 and graph["truncated"]

    analytics = graph_intelligence_store.persist_analytics_result(
        {
            "project_id": PROJECT_ID,
            "communities": [
                {
                    "community_key": "c1",
                    "members": [{"entity_id": ENTITY_ID}, {"entity_id": "bad"}],
                }
            ],
            "entity_metrics": [{"entity_id": ENTITY_ID}, {"entity_id": "bad"}],
            "surprising_connections": [{"source_entity_id": ENTITY_ID, "target_entity_id": OTHER_ID}, {"source_entity_id": "bad"}],
        }
    )
    assert analytics["communities"] == 1 and analytics["metrics"] == 2
    impact = graph_intelligence_store.persist_impact_result(
        {
            "project_id": PROJECT_ID,
            "impact_items": [{"entity_id": ENTITY_ID}, {"entity_id": "bad"}],
            "test_selection": {"tests": [{"test_entity_id": ENTITY_ID}, {"relative_path": "x.py"}]},
        }
    )
    assert impact["impact_items"] == 2 and impact["tests"] == 2
    comparison = graph_intelligence_store.persist_comparison_result(
        {
            "left_project_id": PROJECT_ID,
            "right_project_id": OTHER_ID,
            "matches": {"exact": [{"left_entity_id": ENTITY_ID}], "ambiguous": [{}]},
            "entities": {"added": [{}], "changed": [{"severity": "critical"}]},
        }
    )
    assert comparison["items"] == 4
    artifact = graph_intelligence_store.persist_artifact_result(
        project_id=PROJECT_ID,
        result={
            "source_path": "docs/a.md",
            "content_hash": "hash",
            "metadata": {"artifact_kind": "markdown"},
            "sections": [{"text": "body", "confidence": 0.8, "metadata": {"heading": "H"}}],
        },
    )
    assert artifact["sections"] == 1


def test_taxonomy_backfill_helpers_and_full_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    authorities = {
        "canonical": {"source_repository": "LOCAL_PDFS", "title": "PMBOK"},
        "secondary": {"source_repository": "LOCAL_PDFS", "title": "Rita Exam Prep"},
        "primary": {"source_repository": "LOCAL_PDFS", "title": "Internal Guide"},
        "community": {"source_repository": "devops-roadmap", "title": "Roadmap"},
        "internal": {"source_repository": "custom", "title": "Notes"},
    }
    for expected, document in authorities.items():
        assert knowledge_library_taxonomy_backfill._authority_for_document(document) == expected
    assert knowledge_library_taxonomy_backfill._document_text({"title": "A", "author": "B"}) == "A B"
    assert knowledge_library_taxonomy_backfill._section_text({"heading": "H", "section_type": "body"}) == "H body"
    assert knowledge_library_taxonomy_backfill._chunk_text({"content": "C"}) == "C"
    assert knowledge_library_taxonomy_backfill.TaxonomyBackfillReport(documents_scanned=2).to_dict()["documents_scanned"] == 2

    seed_cur = FakeCursor(one=[{"id": ENTITY_ID}, {"id": OTHER_ID}])
    monkeypatch.setattr(
        knowledge_library_taxonomy_backfill,
        "iter_methodology_definitions",
        lambda: [{"slug": "m", "name": "M", "description": "D", "authority_tier": "canonical", "repositories": [], "lenses": []}],
    )
    monkeypatch.setattr(
        knowledge_library_taxonomy_backfill,
        "iter_concept_definitions",
        lambda: [{"slug": "c", "name": "C", "concept_type": "practice", "description": "D", "aliases": [], "authority_tier": "canonical", "methodology_slug": "m"}],
    )
    assert knowledge_library_taxonomy_backfill._seed_methodologies(seed_cur) == {"m": ENTITY_ID}
    assert knowledge_library_taxonomy_backfill._seed_concepts(seed_cur) == {"c": OTHER_ID}

    grouped_cur = FakeCursor(
        all_rows=[
            [{"document_version_id": "v1", "section_id": "s1"}],
            [{"document_version_id": "v1", "chunk_id": "k1"}],
        ]
    )
    assert knowledge_library_taxonomy_backfill._sections_by_version(grouped_cur)["v1"][0]["section_id"] == "s1"
    assert knowledge_library_taxonomy_backfill._chunks_by_version(grouped_cur)["v1"][0]["chunk_id"] == "k1"

    cur = FakeCursor()
    _install_fake_db(monkeypatch, knowledge_library_taxonomy_backfill, cur)
    monkeypatch.setattr(knowledge_library_taxonomy_backfill, "_seed_methodologies", lambda _cur: {"m": ENTITY_ID})
    monkeypatch.setattr(knowledge_library_taxonomy_backfill, "_seed_concepts", lambda _cur: {"c": OTHER_ID})
    monkeypatch.setattr(
        knowledge_library_taxonomy_backfill,
        "_latest_completed_documents",
        lambda _cur: [{"document_id": "d1", "document_version_id": "v1", "title": "PMBOK", "source_repository": "LOCAL_PDFS", "authority_tier": "primary"}],
    )
    monkeypatch.setattr(
        knowledge_library_taxonomy_backfill,
        "_sections_by_version",
        lambda _cur: {"v1": [{"section_id": "s1", "heading": "practice"}]},
    )
    monkeypatch.setattr(
        knowledge_library_taxonomy_backfill,
        "_chunks_by_version",
        lambda _cur: {"v1": [{"chunk_id": "k1", "content": "practice"}]},
    )
    monkeypatch.setattr(
        knowledge_library_taxonomy_backfill,
        "score_methodology_matches",
        lambda *_args, **_kwargs: {"m": {"score": 0.9, "matched_terms": ["x"]}, "missing": {"score": 0.8}, "low": {"score": 0.1}},
    )
    monkeypatch.setattr(
        knowledge_library_taxonomy_backfill,
        "score_concept_matches",
        lambda *_args, **_kwargs: {"c": {"score": 0.9, "matched_terms": ["y"]}, "missing": {"score": 0.9}, "low": {"score": 0.1}},
    )
    report = knowledge_library_taxonomy_backfill.backfill_knowledge_library_taxonomy(clear_existing_links=True)
    assert report == {
        "documents_scanned": 1,
        "documents_reclassified": 1,
        "methodologies_seeded": 1,
        "concepts_seeded": 1,
        "document_methodologies_linked": 1,
        "document_concepts_linked": 1,
        "section_concepts_linked": 1,
        "chunk_concepts_linked": 1,
    }


def test_unified_graph_store_public_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    assert unified_graph_store._valid_uuid(PROJECT_ID)
    assert not unified_graph_store._valid_uuid(None)
    compact = unified_graph_store._compact_code_graph_sync(
        {"status": "success", "delta": {"changed_paths": list(map(str, range(50))), "unchanged_paths": [1, 2]}, "scan_stats": {"files_scanned": 3}}
    )
    assert len(compact["delta"]["changed_paths"]) == 40 and compact["delta"]["unchanged_count"] == 2
    assert unified_graph_store.sync_unified_project_graph(project_id="bad", trigger="test")["status"] == "invalid_project_id"
    assert unified_graph_store.get_unified_graph_health(project_id="bad")["status"] == "invalid_project_id"
    assert unified_graph_store.query_unified_graph(project_id="bad", query="x")["status"] == "invalid_project_id"
    assert unified_graph_store.find_unified_graph_path(project_id="bad", source_entity_id="x", target_entity_id="y")["status"] == "invalid_id"
    assert unified_graph_store.persist_context_pack(
        project_id="bad",
        session_id="s",
        agent_role="a",
        task_query="q",
        envelope={},
        token_budget=1,
        token_estimate=1,
    ) is None

    monkeypatch.setattr(unified_graph_store, "_require_unified_graph_schema", lambda: None)
    cur = FakeCursor(
        one=[
            {"projected": True},
            {"id": ENTITY_ID, "trigger": "test", "code_graph_version": 5},
            {"graph_version": 5},
            {"entity_path": [ENTITY_ID, OTHER_ID], "relation_path": [], "depth": 1},
            {"id": OTHER_ID},
        ],
        all_rows=[
            [{"entity_type": "skill", "count": 2}],
            [{"id": ENTITY_ID, "title": "node"}],
            [{"id": OTHER_ID, "relation_type": "USES_SKILL"}],
        ],
    )
    _install_fake_db(monkeypatch, unified_graph_store, cur)
    # Disable the oversized-graph node budget here so this contract exercises the
    # change-detection branch only (the budget guard is covered separately in
    # test_unified_graph_runtime).
    monkeypatch.setenv("MCUM_GRAPH_MAX_CODE_PROJECTION_NODES", "0")
    assert unified_graph_store._should_project_code(cur, PROJECT_ID, {"status": "no_changes"}) is False
    assert unified_graph_store._should_project_code(cur, PROJECT_ID, {"status": "success"}) is True
    health = unified_graph_store.get_unified_graph_health(project_id=PROJECT_ID, ensure_schema=False)
    assert health["status"] == "active" and health["entities_by_type"] == {"skill": 2}
    query = unified_graph_store.query_unified_graph(project_id=PROJECT_ID, query="skill", limit=100, entity_types=["skill", ""])
    assert len(query["entities"]) == 1 and len(query["relations"]) == 1
    path = unified_graph_store.find_unified_graph_path(
        project_id=PROJECT_ID,
        source_entity_id=ENTITY_ID,
        target_entity_id=OTHER_ID,
        max_depth=99,
    )
    assert path["status"] == "success"
    pack_id = unified_graph_store.persist_context_pack(
        project_id=PROJECT_ID,
        session_id="s",
        agent_role="a",
        task_query="q",
        envelope={"x": 1},
        token_budget=-1,
        token_estimate=-2,
        snapshot_id="bad",
    )
    assert pack_id == OTHER_ID

    monkeypatch.setattr(unified_graph_store, "_require_unified_graph_schema", lambda: (_ for _ in ()).throw(RuntimeError("down")))
    failure = unified_graph_store.sync_unified_project_graph(project_id=PROJECT_ID, trigger="test")
    assert failure["status"] == "failure" and failure["error"] == "down"


def test_code_graph_store_schema_manifest_and_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    schema_cur = FakeCursor()
    _install_fake_db(monkeypatch, code_graph_store, schema_cur)
    code_graph_store.ensure_code_graph_schema()
    assert len(schema_cur.executions) >= 20

    manifest_cur = FakeCursor(
        one=[{"id": ENTITY_ID, "graph_version": 1}],
        all_rows=[[{"relative_path": "a.py", "file_hash": "h"}]],
    )
    _install_fake_db(monkeypatch, code_graph_store, manifest_cur)
    manifest = code_graph_store.get_code_graph_manifest(project_id=PROJECT_ID, ensure_schema=False)
    assert manifest["files"]["a.py"]["file_hash"] == "h"

    empty_cur = FakeCursor(one=[None])
    _install_fake_db(monkeypatch, code_graph_store, empty_cur)
    assert code_graph_store.get_code_graph_manifest(project_id=PROJECT_ID, ensure_schema=False) == {"graph": None, "files": {}}
    monkeypatch.setattr(code_graph_store, "ensure_code_graph_schema", lambda: None)
    code_graph_store.mark_code_graph_stale(project_id=PROJECT_ID, error_message="x" * 3000)

    maps_cur = FakeCursor(
        all_rows=[
            [
                {"id": ENTITY_ID, "relative_path": "a.py", "qualified_name": "a.run"},
                {"id": OTHER_ID, "relative_path": "b.py", "qualified_name": "b.run"},
            ],
            [
                {"id": "e1", "source_ref": "a.run", "target_ref": "b.run", "source_node_id": None, "target_node_id": None},
                {"id": "e2", "source_ref": "a.run", "target_ref": "b.run", "source_node_id": ENTITY_ID, "target_node_id": OTHER_ID},
            ],
        ]
    )
    by_key, by_qualified = code_graph_store._node_maps(maps_cur, ENTITY_ID)
    assert by_key[("a.py", "a.run")] == ENTITY_ID and by_qualified["b.run"] == OTHER_ID
    monkeypatch.setattr(code_graph_store, "_node_maps", lambda *_args: (by_key, by_qualified))
    code_graph_store._refresh_unresolved_edge_targets(maps_cur, ENTITY_ID)
    assert any("UPDATE code_graph.edges" in sql for sql, _ in maps_cur.executions)

    insert_cur = FakeCursor(all_rows=[[{"id": ENTITY_ID, "relative_path": "a.py", "qualified_name": "a.run"}]])
    code_graph_store._insert_index_payload(
        insert_cur,
        graph_id=ENTITY_ID,
        project_id=PROJECT_ID,
        files=[{"relative_path": "a.py", "language": "python", "file_hash": "h"}],
        nodes=[{"relative_path": "a.py", "node_kind": "function", "name": "run", "qualified_name": "a.run"}],
        edges=[{"source_path": "a.py", "source_ref": "a.run", "target_ref": "external", "confidence": 0.8}],
    )
    assert len(insert_cur.executions) >= 3
    assert code_graph_store._relative_project_path("", str(tmp_path)) is None
    assert code_graph_store._relative_project_path("a.py", str(tmp_path)) == "a.py"
    assert code_graph_store._relative_project_path(str(tmp_path.parent / "outside.py"), str(tmp_path)) is None


def test_code_graph_store_index_link_backfill_and_retrieval(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(code_graph_store, "ensure_code_graph_schema", lambda: None)
    cur = FakeCursor(one=[{"id": ENTITY_ID}])
    _install_fake_db(monkeypatch, code_graph_store, cur)
    no_changes = code_graph_store.persist_index_result(
        project_id=PROJECT_ID,
        project_path=str(tmp_path),
        project_name="demo",
        mode="incremental",
        index_result={"delta": {"has_changes": False}, "stats": {"files_skipped": 2}},
        ensure_schema=False,
    )
    assert no_changes["status"] == "no_changes" and no_changes["files_skipped"] == 2

    monkeypatch.setattr(code_graph_store, "_upsert_graph", lambda *_args, **_kwargs: ENTITY_ID)
    monkeypatch.setattr(code_graph_store, "_insert_index_payload", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(code_graph_store, "_refresh_unresolved_edge_targets", lambda *_args: None)
    monkeypatch.setattr(code_graph_store, "_refresh_experience_link_targets", lambda *_args: None)
    full_cur = FakeCursor(one=[{"files_total": 1, "nodes_total": 1, "edges_total": 1}])
    _install_fake_db(monkeypatch, code_graph_store, full_cur)
    full = code_graph_store.persist_index_result(
        project_id=PROJECT_ID,
        project_path=str(tmp_path),
        project_name="demo",
        mode="full",
        index_result={
            "files": [{"relative_path": "a.py"}],
            "nodes": [{"qualified_name": "a"}],
            "edges": [{"target_ref": "b"}],
            "stats": {"files_scanned": 1, "tokens_indexed_estimate": 10},
            "metadata": {"extractor_version": "v2"},
        },
        ensure_schema=False,
    )
    assert full["status"] == "success" and full["files_total"] == 1

    incremental_cur = FakeCursor(one=[{"files_total": 0, "nodes_total": 0, "edges_total": 0}])
    _install_fake_db(monkeypatch, code_graph_store, incremental_cur)
    code_graph_store.persist_index_result(
        project_id=PROJECT_ID,
        project_path=str(tmp_path),
        project_name=None,
        mode="incremental",
        index_result={"delta": {"changed_paths": ["a.py"], "deleted_paths": ["b.py"]}},
        ensure_schema=False,
    )
    assert any("relative_path = ANY" in sql for sql, _ in incremental_cur.executions)

    not_indexed_cur = FakeCursor(one=[None])
    _install_fake_db(monkeypatch, code_graph_store, not_indexed_cur)
    assert code_graph_store.link_experience_to_code_graph(
        experience_id=ENTITY_ID,
        project_id=PROJECT_ID,
        paths=["a.py"],
        ensure_schema=False,
    )["status"] == "not_indexed"

    graph = {"id": ENTITY_ID, "project_path": str(tmp_path), "graph_version": 3}
    link_cur = FakeCursor(
        one=[
            graph,
            {"file_id": OTHER_ID, "file_hash": "h", "node_id": ENTITY_ID, "qualified_name": "a.run"},
            None,
        ]
    )
    _install_fake_db(monkeypatch, code_graph_store, link_cur)
    linked = code_graph_store.link_experience_to_code_graph(
        experience_id=ENTITY_ID,
        project_id=PROJECT_ID,
        paths=["a.py", "a.py"],
        evidence_refs=[{}, "bad", {"path": "missing.py", "line_start": 4}],
        confidence=2.0,
        ensure_schema=False,
    )
    assert linked["linked"] == 1 and linked["paths_considered"] == 3

    backfill_cur = FakeCursor(
        all_rows=[
            [
                {"id": ENTITY_ID, "source_artifacts": [{"path": "a.py"}, "bad"], "evidence_refs": []},
                {"id": OTHER_ID, "source_artifacts": [], "evidence_refs": "bad"},
            ]
        ]
    )
    _install_fake_db(monkeypatch, code_graph_store, backfill_cur)
    monkeypatch.setattr(code_graph_store, "link_experience_to_code_graph", lambda **kwargs: {"linked": 1 if kwargs["experience_id"] == ENTITY_ID else 0})
    backfill = code_graph_store.backfill_experience_code_links(project_id=PROJECT_ID, ensure_schema=False)
    assert backfill["experiences_scanned"] == 2 and backfill["links_created"] == 1

    assert code_graph_store._linked_experiences_for_hits(FakeCursor(), project_id=PROJECT_ID, hits=[]) == []
    hits_cur = FakeCursor(all_rows=[[{"id": ENTITY_ID, "title": "experience"}]])
    assert len(code_graph_store._linked_experiences_for_hits(
        hits_cur,
        project_id=PROJECT_ID,
        hits=[{"node_id": ENTITY_ID, "relative_path": "a.py"}],
    )) == 1
    assert code_graph_store.infer_code_graph_filters("Go backend") == {"languages": ["go"], "inferred": True}
    assert code_graph_store.infer_code_graph_filters("nothing explicit") == {}

    invalid = code_graph_store.retrieve_code_graph_context(project_id="bad", query="x")
    assert invalid["metadata"]["status"] == "invalid_project_id"
    absent_cur = FakeCursor(one=[{"table_name": None}])
    _install_fake_db(monkeypatch, code_graph_store, absent_cur)
    assert code_graph_store.retrieve_code_graph_context(project_id=PROJECT_ID, query="x")["metadata"]["status"] == "not_indexed"

    retrieval_cur = FakeCursor(
        one=[{"table_name": "code_graph.graphs"}],
        all_rows=[
            [{"node_id": ENTITY_ID, "relative_path": "a.py", "node_kind": "function", "qualified_name": "a.run", "line_start": 2, "line_end": 3}],
        ],
    )
    _install_fake_db(monkeypatch, code_graph_store, retrieval_cur)
    monkeypatch.setattr(code_graph_store, "_linked_experiences_for_hits", lambda *_args, **_kwargs: [{"id": OTHER_ID}])
    retrieval = code_graph_store.retrieve_code_graph_context(
        project_id=PROJECT_ID,
        query="run",
        languages=["Python", ""],
        exclude_languages=["SQL"],
        path_prefix=r"src\a",
        node_kinds=["Function"],
    )
    assert retrieval["enabled"] and retrieval["hits_retrieved"] == 1 and retrieval["linked_experiences"][0]["id"] == OTHER_ID


def test_embedder_hash_model_and_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    original_embed = embedder.embed
    monkeypatch.setattr(embedder, "EMBEDDING_BACKEND", "hash")
    assert embedder._use_sentence_transformers() is False
    assert embedder._hash_embedding("") == [0.0] * embedder.EMBEDDING_DIM
    vector = embedder._hash_embedding("alpha alpha beta")
    assert pytest.approx(sum(value * value for value in vector), rel=1e-6) == 1.0
    assert embedder.warmup_model() == embedder.FALLBACK_MODEL_NAME
    assert len(embedder.embed("alpha")) == embedder.EMBEDDING_DIM
    assert len(embedder.embed_batch(["a", "b"])) == 2
    with pytest.raises(ValueError, match="Dimension mismatch"):
        embedder.cosine_similarity([1], [1, 2])
    assert embedder.cosine_similarity([0, 0], [1, 1]) == 0.0
    assert embedder.cosine_similarity([1, 0], [1, 0]) == 1.0
    assert embedder.rank_by_similarity("a", []) == []

    monkeypatch.setattr(embedder, "embed", lambda text: [1.0, 0.0] if text != "opposite" else [-1.0, 0.0])
    ranked = embedder.rank_by_similarity(
        "query",
        [
            {"title": "same", "embedding": "[1, 0]"},
            {"title": "fallback"},
            {"title": ""},
            {"title": "low", "embedding": [-1, 0]},
        ],
        min_score=0.0,
    )
    assert [item["title"] for item in ranked] == ["same", "fallback"]
    assert embedder.build_experience_text({"title": "T", "content": '{"conclusion":"C","reasoning":"R"}', "task_description": "D"}) == "T | C | R | D"
    assert embedder.build_experience_text({"content": "[]"}) == ""

    class Encoded:
        def __init__(self, value: list[float]) -> None:
            self.value = value

        def tolist(self) -> list[float]:
            return self.value

    class Model:
        def encode(self, value: object, **_kwargs: object):
            if isinstance(value, list):
                return [Encoded([1.0]) for _ in value]
            return Encoded([1.0])

    monkeypatch.setattr(embedder, "embed", original_embed)
    monkeypatch.setattr(embedder, "_get_model", lambda: Model())
    assert embedder.embed("x") == [1.0] and embedder.embed_batch(["x"]) == [[1.0]]


def test_spec_and_design_system_store_contracts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert json.loads(spec_store._json(None, [])) == []
    assert spec_store._row_to_dict(None) is None
    assert spec_store._row_to_dict({"id": uuid.UUID(PROJECT_ID)})["id"] == PROJECT_ID

    spec_cur = FakeCursor(one=[{"id": ENTITY_ID, "status": "active"}, {"id": ENTITY_ID}, None, {"id": ENTITY_ID}])
    _install_fake_db(monkeypatch, spec_store, spec_cur)
    spec_store.ensure_spec_schema()
    contract = spec_store.upsert_spec_contract(
        project_id=PROJECT_ID,
        task_id="t",
        task_brief={"objective": "O"},
        contract={
            "status": "invalid",
            "assumptions": [{"code": "a", "text": "A"}],
            "scenarios": [{"title": "S"}],
            "acceptance_criteria": [{"code": "c", "text": "C"}],
        },
        ensure_schema=False,
    )
    assert contract["id"] == ENTITY_ID
    with pytest.raises(ValueError, match="Invalid spec status"):
        spec_store.mark_spec_contract_result(status="invalid", contract_id=ENTITY_ID, ensure_schema=False)
    assert spec_store.mark_spec_contract_result(status="active", ensure_schema=False) is False
    assert spec_store.mark_spec_contract_result(
        status="fulfilled",
        contract_id=ENTITY_ID,
        trace_links=[{"target_ref": ""}, {"target_ref": "artifact", "metadata": {"x": 1}}],
        ensure_schema=False,
    ) is True
    assert spec_store.mark_spec_contract_result(status="active", project_id=PROJECT_ID, task_id="t", ensure_schema=False) is False
    assert spec_store.get_spec_contract(project_id=PROJECT_ID, task_id="t", ensure_schema=False)["id"] == ENTITY_ID

    assert design_system_store._json_object('{"a":1}') == {"a": 1}
    assert design_system_store._json_object("bad") == {}
    assert design_system_store._json_list("[1]") == [1]
    assert design_system_store._json_list("{}") == []
    assert design_system_store._coerce_text_list("web, mobile, web") == ["web", "mobile"]
    normalized = design_system_store.normalize_design_system_spec(
        {"product_identity": {"product_name": "Demo", "platforms": ["web"]}, "design_tokens": {"color": "blue"}}
    )
    assert normalized["design_tokens"]["color"] == "blue"
    fields = design_system_store._extract_profile_fields(
        project_name=None,
        product_name=None,
        audience=None,
        platform_targets=None,
        normalized_spec=normalized,
    )
    assert fields["product_name"] == "Demo" and fields["platform_targets"] == ["web"]

    design_cur = FakeCursor(
        one=[
            {"id": ENTITY_ID},
            {"next_version": 2},
            {"id": OTHER_ID, "version_number": 2, "status": "approved", "source_kind": "manual"},
            {"version_id": OTHER_ID, "version_status": "approved"},
            None,
        ]
    )
    _install_fake_db(monkeypatch, design_system_store, design_cur)
    monkeypatch.setattr(design_system_store, "get_or_create_project", lambda **_kwargs: {"id": PROJECT_ID})
    monkeypatch.setattr(design_system_store, "normalize_project_path", lambda path: path)
    design_system_store.ensure_design_system_schema()
    saved = design_system_store.save_design_system_version(
        project_path=str(tmp_path),
        design_system=normalized,
        status="bad",
        source_kind="bad",
        ensure_schema=False,
    )
    assert saved["version_number"] == 2
    assert design_system_store.get_latest_design_system(project_path=str(tmp_path), status="approved", ensure_schema=False)["version_id"] == OTHER_ID
    assert design_system_store.get_latest_design_system(project_path=str(tmp_path), status=None, ensure_schema=False) is None

    spec_file = tmp_path / "spec.json"
    spec_file.write_text('{"a": 1}', encoding="utf-8")
    assert design_system_store._load_spec(SimpleNamespace(spec_file=str(spec_file), spec_json=None)) == {"a": 1}
    assert design_system_store._load_spec(SimpleNamespace(spec_file=None, spec_json='{"b":2}')) == {"b": 2}
    with pytest.raises(ValueError):
        design_system_store._load_spec(SimpleNamespace(spec_file=None, spec_json=None))
    monkeypatch.setattr(design_system_store, "save_design_system_version", lambda **_kwargs: {"status": "saved"})
    assert design_system_store.main(["upsert", "--project-path", str(tmp_path), "--spec-json", "{}"]) == 0
    assert "saved" in capsys.readouterr().out
    monkeypatch.setattr(design_system_store, "get_latest_design_system", lambda **_kwargs: None)
    assert design_system_store.main(["show", "--project-path", str(tmp_path)]) == 0


def test_connection_helpers_pool_contexts_and_health(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(connection.os, "name", "nt")
    assert connection._running_in_wsl() is False
    monkeypatch.setattr(connection.os, "name", "posix")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert connection._running_in_wsl() is True
    monkeypatch.delenv("WSL_DISTRO_NAME")
    monkeypatch.setattr(connection.platform, "release", lambda: "linux-microsoft")
    assert connection._running_in_wsl() is True

    monkeypatch.setattr(connection, "_running_in_wsl", lambda: True)
    monkeypatch.setenv("DB_HOST_WSL", "172.20.0.1")
    assert connection._resolve_runtime_db_host("localhost") == "172.20.0.1"
    monkeypatch.delenv("DB_HOST_WSL")
    monkeypatch.setattr(connection, "_detect_wsl_host_gateway", lambda: "172.21.0.1")
    assert connection._resolve_runtime_db_host("127.0.0.1") == "172.21.0.1"
    assert connection._resolve_runtime_db_host("remote") == "remote"
    assert connection._rewrite_database_url_host("", "x") == ""
    assert connection._rewrite_database_url_host("not-a-url", "x") == "not-a-url"
    rewritten = connection._rewrite_database_url_host("postgresql://u:p@localhost:5432/db", "2001:db8::1")
    assert "[2001:db8::1]:5432" in rewritten
    assert connection._rewrite_database_url_host("postgresql://remote/db", "x") == "postgresql://remote/db"
    assert connection._build_local_database_url().startswith("postgresql://")

    class Conn:
        def __init__(self) -> None:
            self.commits = 0
            self.rollbacks = 0
            self.closed = 0

        def commit(self) -> None:
            self.commits += 1

        def rollback(self) -> None:
            self.rollbacks += 1

        def close(self) -> None:
            self.closed += 1

        def cursor(self, **_kwargs: object):
            return SimpleNamespace(close=lambda: setattr(self, "closed", self.closed + 1))

    direct = Conn()
    monkeypatch.setattr(connection, "ConnectionPool", None)
    monkeypatch.setattr(connection.psycopg, "connect", lambda **_kwargs: direct)
    monkeypatch.setattr(connection, "_configure_connection", lambda conn: None)
    assert connection.get_connection() is direct
    with connection.get_db() as yielded:
        assert yielded is direct
    assert direct.commits == 1 and direct.closed == 1
    with connection.get_cursor(direct):
        pass
    assert direct.closed == 2

    failed = Conn()
    monkeypatch.setattr(connection.psycopg, "connect", lambda **_kwargs: failed)
    with pytest.raises(RuntimeError):
        with connection.get_db():
            raise RuntimeError("boom")
    assert failed.rollbacks == 1 and failed.closed == 1

    class PoolConnection:
        def __init__(self, conn: Conn) -> None:
            self.conn = conn

        def __enter__(self) -> Conn:
            return self.conn

        def __exit__(self, *_args: object) -> None:
            return None

    class Pool:
        def __init__(self, **_kwargs: object) -> None:
            self.conn = Conn()
            self.closed = False

        def connection(self) -> PoolConnection:
            return PoolConnection(self.conn)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(connection, "ConnectionPool", Pool)
    monkeypatch.setattr(connection, "_pool", None)
    pool = connection._get_pool()
    assert pool is connection._get_pool()
    with connection.get_db() as yielded:
        assert yielded is pool.conn
    assert pool.conn.commits == 1
    connection.shutdown_pool()
    assert pool.closed and connection._pool is None

    health_cur = FakeCursor(
        one=[
            {"version": "PostgreSQL test"},
            {"available": True},
            {"installed": False},
        ],
        all_rows=[[{"schema_name": "core_brain"}, {"schema_name": "project_registry"}]],
    )
    _install_fake_db(monkeypatch, connection, health_cur)
    health = connection.health_check()
    assert health["connected"] and health["schemas"]["core_brain"] and health["pgvector"] and not health["pgvector_installed"]

    @contextmanager
    def broken_db():
        raise ConnectionError("offline")
        yield

    monkeypatch.setattr(connection, "get_db", broken_db)
    assert connection.health_check()["error"] == "offline"


def test_minimax_credential_discovery_contracts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert minimax_credentials._clean_value('" x "') == "x"
    assert minimax_credentials._strip_inline_comment("value # note") == "value"
    assert minimax_credentials._strip_inline_comment('"value # kept"') == '"value # kept"'
    assert minimax_credentials.parse_env_file(tmp_path / "missing") == {}
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nBAD\n=empty\nKEY='value'\n", encoding="utf-8")
    assert minimax_credentials.parse_env_file(env_file) == {"KEY": "value"}
    invalid_json = tmp_path / "bad.json"
    invalid_json.write_text("{bad", encoding="utf-8")
    assert minimax_credentials._read_json(invalid_json) == {}
    data_file = tmp_path / "data.json"
    data_file.write_text('{"nested":[{"TOKEN":"secret"}]}', encoding="utf-8")
    assert minimax_credentials._find_key_recursive(minimax_credentials._read_json(data_file), {"TOKEN"}) == "secret"
    assert minimax_credentials._yaml_scalar("base_url: https://x # note", "base_url") == "https://x"
    assert minimax_credentials._yaml_scalar("", "base_url") == ""
    assert minimax_credentials._read_text(tmp_path / "missing") == ""
    assert minimax_credentials._protocol_from_base_url("https://anthropic.local") == "anthropic"
    assert minimax_credentials._protocol_from_base_url("https://x", "openai") == "openai"

    direct = minimax_credentials._credential_from_direct_values(
        values={"MINIMAX_TOKEN": "k", "MINIMAX_BASE_URL": "https://x", "MINIMAX_MODEL": "m"},
        source="test",
        policy={"protocol": "auto"},
    )
    assert direct is not None and direct.protocol == "openai"
    assert minimax_credentials._credential_from_direct_values(values={}, source="test", policy={}) is None
    assert minimax_credentials._credential_from_anthropic_values(token="", base_url="", model="", source="x") is None
    assert minimax_credentials._credential_from_anthropic_values(token="k", base_url="https://x", model="other", source="x") is None
    anthropic = minimax_credentials._credential_from_anthropic_values(token="k", base_url="", model="MiniMax-M3", source="x")
    assert anthropic is not None and anthropic.protocol == "anthropic"

    for key in ("MINIMAX_API_KEY", "MINIMAX_TOKEN", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(minimax_credentials, "_candidate_env_files", lambda: [])
    monkeypatch.setattr(minimax_credentials, "_candidate_claude_settings", lambda: [])
    monkeypatch.setattr(minimax_credentials, "_candidate_hermes_configs", lambda: [])
    assert minimax_credentials.resolve_minimax_credentials({}) is None
    assert minimax_credentials.minimax_credential_status({})["available"] is False

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "k")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://anthropic.local")
    credential = minimax_credentials.resolve_minimax_credentials({})
    assert credential is not None and credential.protocol == "anthropic"
    monkeypatch.setattr(minimax_credentials, "resolve_minimax_credentials", lambda _policy: anthropic)
    assert minimax_credentials.minimax_credential_status({})["available"] is True


def test_skill_catalog_operational_contracts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original_get_skill_record = skill_catalog.get_skill_record
    original_list_skill_catalog = skill_catalog.list_skill_catalog
    monkeypatch.setenv("MCUM_RUNTIME_ID", "custom")
    assert skill_catalog.get_runtime_id() == "custom"
    monkeypatch.delenv("MCUM_RUNTIME_ID")
    monkeypatch.setattr(skill_catalog.os, "name", "nt")
    assert skill_catalog.get_runtime_id() == "windows"
    assert skill_catalog._merge_runtime_metadata({"a": 1}, skill_path="", runtime_id="x") == {"a": 1}
    assert skill_catalog.resolve_skill_path(None) == ""
    assert skill_catalog._coerce_frontmatter_value("") == ""
    assert skill_catalog._coerce_frontmatter_value("[1,2]") == [1, 2]
    assert skill_catalog._coerce_frontmatter_value("true") is True
    assert skill_catalog._coerce_frontmatter_value("-2") == -2
    assert skill_catalog._coerce_frontmatter_value("1.5") == 1.5
    assert skill_catalog._normalize_string_list("a,b\na") == ["a", "b", "a"]
    assert skill_catalog._normalize_string_list(None) == []
    assert skill_catalog._extract_section_terms("## TRIGGER KEYWORDS\n- short\n## OTHER\n- no", ("TRIGGER KEYWORDS",)) == ["short"]
    routing = skill_catalog._build_routing_metadata({"routing_priority": "bad", "routing_enabled": "false"}, "description")
    assert routing["priority"] == 6 and routing["enabled"] is False

    no_frontmatter = tmp_path / "plain.md"
    no_frontmatter.write_text("# Plain", encoding="utf-8")
    assert skill_catalog._parse_frontmatter(no_frontmatter) == {}
    root = tmp_path / "skills"
    root.mkdir()
    (root / ".hidden").mkdir()
    skill_dir = root / "demo"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# Demo\nDoes work", encoding="utf-8")
    discovered = skill_catalog.discover_local_skills(root)
    assert discovered[0]["skill_name"] == "demo"

    stats_cur = FakeCursor(
        all_rows=[
            [{"skill_name": "demo", "experience_count": 2, "project_count": 1, "avg_confidence": 0.8}],
            [{"skill_name": "demo", "active_test_count": 3}],
            [{"skill_name": "demo", "last_used_at": "now"}],
            [{"skill_name": "demo", "last_improved_at": "now"}],
        ]
    )
    _install_fake_db(monkeypatch, skill_catalog, stats_cur)
    assert skill_catalog._load_skill_stats()["demo"]["active_test_count"] == 3

    sync_cur = FakeCursor()
    _install_fake_db(monkeypatch, skill_catalog, sync_cur)
    monkeypatch.setattr(skill_catalog, "discover_local_skills", lambda _root=None: discovered)
    monkeypatch.setattr(skill_catalog, "_load_skill_stats", lambda: {"demo": {"experience_count": 2}})
    monkeypatch.setattr(skill_catalog, "list_skill_catalog", lambda: [{"skill_name": "old", "metadata": {}}])
    assert skill_catalog.sync_skill_catalog(root)["skills_synced"] == 1

    monkeypatch.setattr(skill_catalog, "get_skill_record", lambda _name: {"metadata": {"old": True}})
    upsert_cur = FakeCursor(one=[{"skill_name": "demo", "status": "active"}, {"skill_name": "demo", "status": "active"}])
    _install_fake_db(monkeypatch, skill_catalog, upsert_cur)
    assert skill_catalog.upsert_skill_record(skill_name="demo", skill_dir_name="demo", skill_path=str(skill_dir), status="bad")["status"] == "active"
    with pytest.raises(ValueError):
        skill_catalog.update_skill_status("demo", "bad")
    assert skill_catalog.update_skill_status("demo", "degraded", {"why": "test"})
    assert skill_catalog.retire_skill_record("demo", reason="obsolete")
    assert skill_catalog.merge_skill_metadata("demo", {"x": 1})
    assert skill_catalog.mark_skill_used("demo")
    monkeypatch.setattr(skill_catalog, "get_skill_record", original_get_skill_record)
    assert skill_catalog.get_skill_record("demo")["skill_name"] == "demo"
    list_cur = FakeCursor(all_rows=[[{"skill_name": "demo"}], [{"skill_name": "demo"}]])
    _install_fake_db(monkeypatch, skill_catalog, list_cur)
    monkeypatch.setattr(skill_catalog, "list_skill_catalog", original_list_skill_catalog)
    assert len(skill_catalog.list_skill_catalog("active")) == 1
    assert len(skill_catalog.list_skill_catalog()) == 1


def test_knowledge_library_semantic_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    cur = FakeCursor(one=[{"regclass_name": None}])
    _install_fake_db(monkeypatch, knowledge_library_semantic, cur)
    knowledge_library_semantic.ensure_semantic_tables()
    assert len(cur.executions) == 3

    monkeypatch.setattr(
        knowledge_library_semantic,
        "iter_concept_definitions",
        lambda: [{"slug": "c", "methodology_slug": "m"}],
    )
    assert knowledge_library_semantic._concept_methodology_map() == {"c": "m"}
    assert "alias" in knowledge_library_semantic._concept_text_repr(
        {"concept_name": "C", "concept_slug": "c", "aliases": '["Alias"]', "methodology_slug": "m"}
    )
    assert "bad" in knowledge_library_semantic._concept_text_repr({"aliases": "bad"})

    monkeypatch.setattr(knowledge_library_semantic, "ensure_semantic_tables", lambda: None)
    monkeypatch.setattr(knowledge_library_semantic, "embed_batch", lambda texts: [[1.0, 0.0] for _ in texts])
    sync_cur = FakeCursor(
        all_rows=[
            [
                {"concept_id": ENTITY_ID, "concept_slug": "c", "concept_name": "C", "aliases": [], "model_name": "old", "text_repr": ""},
                {"concept_id": OTHER_ID, "concept_slug": "same", "concept_name": "Same", "aliases": [], "model_name": knowledge_library_semantic.MODEL_NAME, "text_repr": "Same | same"},
            ]
        ]
    )
    _install_fake_db(monkeypatch, knowledge_library_semantic, sync_cur)
    result = knowledge_library_semantic.sync_concept_embeddings()
    assert result["concepts_seen"] == 2 and result["embedded"] == 1

    no_change_cur = FakeCursor(
        all_rows=[[{"concept_id": ENTITY_ID, "concept_slug": "same", "concept_name": "Same", "aliases": [], "model_name": knowledge_library_semantic.MODEL_NAME, "text_repr": "Same | same"}]]
    )
    _install_fake_db(monkeypatch, knowledge_library_semantic, no_change_cur)
    assert knowledge_library_semantic.sync_concept_embeddings()["embedded"] == 0

    rank_cur = FakeCursor(
        all_rows=[
            [],
            [
                {"concept_id": ENTITY_ID, "concept_slug": "c", "concept_name": "C", "embedding": "[1,0]"},
                {"concept_id": OTHER_ID, "concept_slug": "bad", "concept_name": "Bad", "embedding": {}},
            ],
        ]
    )
    _install_fake_db(monkeypatch, knowledge_library_semantic, rank_cur)
    monkeypatch.setattr(knowledge_library_semantic, "sync_concept_embeddings", lambda: {"embedded": 1})
    monkeypatch.setattr(knowledge_library_semantic, "embed", lambda _text: [1.0, 0.0])
    monkeypatch.setattr(knowledge_library_semantic, "cosine_similarity", lambda _a, _b: 0.9)
    ranked = knowledge_library_semantic.rank_concepts_semantically("query", methodology_slugs=["m"], min_score=0.5)
    assert ranked[0]["concept_slug"] == "c" and ranked[0]["methodology_slug"] == "m"


def test_project_registry_profiles_and_crud(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert project_registry.normalize_project_path(r"C:\repo") == "C:/repo"
    assert project_registry._row_to_dict(None) is None
    assert project_registry._row_to_dict({"id": uuid.UUID(PROJECT_ID)})["id"] == PROJECT_ID
    assert project_registry._coerce_datetime("bad") is None
    assert project_registry._coerce_datetime("2026-01-01").tzinfo is not None
    assert project_registry.estimate_tokens(None) == 0
    assert project_registry.estimate_tokens("abcd") == 1
    assert project_registry._json_object("bad") == {}
    assert project_registry._json_list('["x"]') == ["x"]
    assert project_registry._safe_ratio("bad", 1) == 0.0
    assert project_registry._safe_ratio(1, 0) == 0.0
    assert project_registry._coerce_id_list('[{"pattern_id":"p1"},"p1","p2"]') == ["p1", "p2"]
    assert project_registry._effect_score("miss", selected=True) == -0.7
    assert project_registry._row_outcome_score("success") == 1.0

    logs = [
        {
            "skill_used": "actual",
            "outcome": "success",
            "context_tokens_in": 1200,
            "retrieval_latency_ms": 500,
            "task_wall_clock_ms": 12000,
            "pattern_ids_used": ["p1"],
            "log_metadata": {
                "selected_skill": "routed",
                "dispatch_method": "semantic",
                "task_brief": {"task_type": "analizar", "execution_mode": "ejecutar"},
                "compiled_context": {"selected_items_summary": {"memory": [{"token_cost": 100}]}},
                "context_effectiveness": {
                    "items": [{"section": "memory", "selected": True, "effectiveness": "high", "support_score": 1, "utility_reasons": ["useful", "useful"]}],
                    "summary": {"selected_items": 1, "high_value_selected": 1, "items_evaluated": 1},
                },
                "project_scope": "same_project",
            },
        },
        {
            "skill_used": "actual",
            "outcome": "partial",
            "log_metadata": {
                "selected_skill": "actual",
                "task_brief": {},
                "context_effectiveness": {
                    "items": [{"section": "memory", "selected": False, "effectiveness": "missed_opportunity", "utility_reasons": ["useful"]}],
                    "summary": {"selected_items": 0, "missed_opportunities": 1, "items_evaluated": 1},
                },
                "project_scope": "cross_project_fallback",
            },
        },
    ]
    context_profile = project_registry.derive_context_effectiveness_profile(logs, skill_name="actual", min_samples=1)
    assert context_profile["active"] and context_profile["pattern_usage_summary"]["rows"] == 1
    dispatch_profile = project_registry.derive_dispatch_performance_profile(logs, min_samples=1)
    assert dispatch_profile["active"] and "actual" in dispatch_profile["skill_summary"]
    retrieval_profile = project_registry.derive_retrieval_scope_profile(logs, skill_name="actual", min_samples=1)
    assert retrieval_profile["active"] and retrieval_profile["same_project"]["rows"] == 1

    wrapper_cur = FakeCursor(all_rows=[logs[:1], logs, logs[:1], logs, logs[:1], logs])
    _install_fake_db(monkeypatch, project_registry, wrapper_cur)
    assert project_registry.get_context_effectiveness_profile(project_id=PROJECT_ID, skill_name="actual", min_samples=2)["active"]
    assert project_registry.get_dispatch_performance_profile(project_id=PROJECT_ID, min_samples=2)["active"]
    assert project_registry.get_retrieval_scope_profile(project_id=PROJECT_ID, skill_name="actual", min_samples=2)["active"]

    crud_cur = FakeCursor(
        one=[
            None,
            {"id": PROJECT_ID, "project_path": str(tmp_path)},
            {"id": PROJECT_ID},
            {"id": ENTITY_ID},
            {"id": OTHER_ID},
            {"rows_refreshed": 2, "latest_day": "today"},
            {"id": ENTITY_ID},
            {"id": ENTITY_ID},
            {"id": ENTITY_ID},
        ],
        all_rows=[
            [{"id": PROJECT_ID}],
            [{"id": ENTITY_ID}],
            [{"id": ENTITY_ID}],
            [{"id": PROJECT_ID}],
            [{"id": PROJECT_ID}],
        ],
    )
    _install_fake_db(monkeypatch, project_registry, crud_cur)
    project = project_registry.get_or_create_project(str(tmp_path))
    assert project["id"] == PROJECT_ID
    assert project_registry.update_project_info(PROJECT_ID) is False
    assert project_registry.update_project_info(PROJECT_ID, tech_stack={"python": True})
    assert project_registry.get_project_by_path(str(tmp_path))["id"] == PROJECT_ID
    assert len(project_registry.list_projects("all")) == 1
    assert len(project_registry.list_projects("active")) == 1
    assert project_registry.log_entry(PROJECT_ID, "task", "title", context_tokens_in=2, context_tokens_out=3) == ENTITY_ID
    assert project_registry.record_agent_invocation(project_id=PROJECT_ID, session_id=None, task_log_id=None, task_id=None, agent_role="reviewer", runner="local", input_tokens=2, output_tokens=3) == OTHER_ID
    assert len(project_registry.get_project_logs(PROJECT_ID, "task")) == 1
    assert len(project_registry.get_project_logs(PROJECT_ID)) == 1
    assert project_registry.refresh_daily_metrics(PROJECT_ID)["rows_refreshed"] == 2
    assert len(project_registry.snapshot_project_kpis(project_id=PROJECT_ID)) == 1
    assert project_registry.get_latest_maintenance_run(project_id=PROJECT_ID)["id"] == ENTITY_ID
    assert project_registry.record_maintenance_run(project_id=PROJECT_ID, maintenance_name="coverage") == ENTITY_ID
    assert project_registry.update_maintenance_run(maintenance_run_id=ENTITY_ID, status="success")


def test_code_graph_indexer_multilanguage_and_edge_contracts(tmp_path: Path) -> None:
    assert code_graph_indexer._language_for_path(Path("a.unknown")) is None
    assert code_graph_indexer._read_text(b"\xff").endswith("\ufffd")
    python_nodes, python_edges = code_graph_indexer._parse_python(
        "async def run(a, /, *args, option=1, **kwargs):\n    return client.call(a)\n",
        "src/a.py",
    )
    assert any(node.signature and "*args" in node.signature and "**kwargs" in node.signature for node in python_nodes)
    assert any(edge.target_ref == "client.call" for edge in python_edges)
    error_nodes, error_edges = code_graph_indexer._parse_python("def broken(", "a.py")
    assert error_nodes[0].node_kind == "parse_error" and error_edges == []

    cases = {
        "javascript": ("import x from 'pkg';\nconst go = (a) => a;\nclass C {}", "pkg", "go"),
        "go": ('package demo\nimport "fmt"\nfunc Run(a string) {}\ntype Item struct {}', "fmt", "Run"),
        "sql": ("CREATE TABLE public.a(id int REFERENCES public.b); SELECT * FROM public.a;", "public.a", "a"),
        "powershell": ("function Invoke-Demo { }", None, "Invoke-Demo"),
        "dart": ("import 'pkg:x/x.dart';\nclass Demo {}\nFuture<void> run(String x) async { }", "pkg:x/x.dart", "run"),
        "text": ("plain", None, None),
    }
    for language, (target, edge_target, node_name) in cases.items():
        nodes, edges = code_graph_indexer._parse_regex_language(target, f"src/a.{language}", language)
        if edge_target:
            assert any(edge.target_ref == edge_target for edge in edges)
        if node_name:
            assert any(node.name == node_name for node in nodes)
    assert code_graph_indexer._line_number("a\nb\n", 2) == 2

    with pytest.raises(FileNotFoundError):
        code_graph_indexer.scan_project_code_graph(str(tmp_path / "missing"))
    (tmp_path / "ignore.bin").write_bytes(b"x")
    (tmp_path / "large.py").write_text("x" * 200, encoding="utf-8")
    (tmp_path / "ok.sql").write_text("CREATE VIEW v AS SELECT * FROM t;", encoding="utf-8")
    result = code_graph_indexer.scan_project_code_graph(str(tmp_path), max_file_bytes=100)
    assert result["stats"]["files_skipped"] >= 1
    assert any(node["node_kind"] == "sql_object" for node in result["nodes"])


def test_unified_graph_projection_internal_and_success_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    schema_cur = FakeCursor()
    _install_fake_db(monkeypatch, unified_graph_store, schema_cur)
    unified_graph_store.ensure_unified_graph_schema()
    assert len(schema_cur.executions) >= 10

    projection_cur = FakeCursor()
    unified_graph_store._reconcile_projection(projection_cur, PROJECT_ID, include_code=True)
    unified_graph_store._reconcile_projection(projection_cur, PROJECT_ID, include_code=False)
    unified_graph_store._upsert_entities(projection_cur, PROJECT_ID, "demo", include_code=True)
    unified_graph_store._upsert_relations(projection_cur, PROJECT_ID, include_code=True)
    assert len(projection_cur.executions) >= 20

    monkeypatch.setattr(unified_graph_store, "_require_unified_graph_schema", lambda: None)
    monkeypatch.setattr(unified_graph_store, "_should_project_code", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(unified_graph_store, "_reconcile_projection", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(unified_graph_store, "_upsert_entities", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(unified_graph_store, "_upsert_relations", lambda *_args, **_kwargs: None)
    cur = FakeCursor(
        one=[
            {"entities": 4, "active_entities": 3},
            {"relations": 2},
            {"graph_version": 5, "source_hash": "h"},
            {"id": ENTITY_ID, "created_at": "now"},
        ]
    )
    _install_fake_db(monkeypatch, unified_graph_store, cur)
    synced = unified_graph_store.sync_unified_project_graph(
        project_id=PROJECT_ID,
        trigger="test",
        selected_skill="demo",
        code_graph_sync={"status": "success"},
        metadata={"source": "coverage"},
    )
    assert synced["status"] == "success" and synced["entities"] == 4 and synced["relations"] == 2

    health_cur = FakeCursor(one=[None], all_rows=[[]])
    _install_fake_db(monkeypatch, unified_graph_store, health_cur)
    assert unified_graph_store.get_unified_graph_health(project_id=PROJECT_ID, ensure_schema=False)["status"] == "not_projected"
    empty_query_cur = FakeCursor(all_rows=[[]])
    _install_fake_db(monkeypatch, unified_graph_store, empty_query_cur)
    assert unified_graph_store.query_unified_graph(project_id=PROJECT_ID, query="missing")["relations"] == []
    not_found_cur = FakeCursor(one=[None])
    _install_fake_db(monkeypatch, unified_graph_store, not_found_cur)
    assert unified_graph_store.find_unified_graph_path(
        project_id=PROJECT_ID,
        source_entity_id=ENTITY_ID,
        target_entity_id=OTHER_ID,
    )["status"] == "not_found"


def test_project_registry_history_and_session_contracts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = "2026-06-13T12:00:00+00:00"
    anti_action = {
        "action": "tune_anti_loop_dispatch_bias",
        "status": "success",
        "result": {
            "policy_updated": True,
            "previous_values": {"score_boost": 0.08, "priority_boost": 0.5},
            "updated_values": {"score_boost": 0.09, "priority_boost": 0.6},
            "analysis": {"recommendation": "increase_bias"},
        },
    }
    memory_action = {
        "action": "tune_memory_governor",
        "status": "success",
        "result": {
            "policy_updated": True,
            "previous_values": {"assist_penalty_weight": 0.12},
            "updated_values": {"assist_penalty_weight": 0.13},
            "analysis": {"recommendation": "tighten"},
        },
    }
    runs = [{"id": ENTITY_ID, "finished_at": now, "actions_applied": [anti_action, memory_action]}]
    monkeypatch.setattr(project_registry, "get_recent_maintenance_runs", lambda **_kwargs: runs)
    anti_history = project_registry.summarize_anti_loop_dispatch_tuning_history(project_id=PROJECT_ID)
    memory_history = project_registry.summarize_memory_governor_tuning_history(project_id=PROJECT_ID)
    assert anti_history["last_direction"] == "increase"
    assert memory_history["last_direction"] == "tighten"
    assert project_registry.analyze_anti_loop_dispatch_effectiveness(
        project_id=PROJECT_ID,
        policy={"anti_loop_dispatch_tuning": {"enabled": False}},
    )["enabled"] is False
    assert project_registry.analyze_memory_governor_effectiveness(
        project_id=PROJECT_ID,
        policy={"memory_governor_tuning": {"enabled": False}},
    )["enabled"] is False

    recent_cur = FakeCursor(all_rows=[runs])
    _install_fake_db(monkeypatch, project_registry, recent_cur)
    assert len(project_registry.get_recent_maintenance_runs(project_id=PROJECT_ID, maintenance_name="daily", since=now, limit=0)) == 1

    monkeypatch.setattr(project_registry, "get_or_create_project", lambda _path: {"id": PROJECT_ID})
    logged: list[dict] = []
    monkeypatch.setattr(project_registry, "log_entry", lambda **kwargs: logged.append(kwargs) or ENTITY_ID)
    started = project_registry.log_session_start(str(tmp_path), task_description="cover", extra_metadata={"x": 1})
    ended = project_registry.log_session_end(PROJECT_ID, 60, tasks_completed=1, extra_metadata={"y": 2})
    assert started["log_id"] == ENTITY_ID and ended == ENTITY_ID and len(logged) == 2


def test_experience_store_validation_persistence_and_queries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    vector = [0.1] * experience_store.EMBEDDING_DIM
    with pytest.raises(ValueError, match="iterable"):
        experience_store._validate_embedding(None)
    with pytest.raises(ValueError, match="exactly"):
        experience_store._validate_embedding([1])
    with pytest.raises(ValueError, match="not numeric"):
        experience_store._validate_embedding(["bad"] * experience_store.EMBEDDING_DIM)
    with pytest.raises(ValueError, match="finite"):
        experience_store._validate_embedding([float("inf")] * experience_store.EMBEDDING_DIM)
    assert len(experience_store._validate_embedding(vector)) == experience_store.EMBEDDING_DIM
    assert experience_store._embedding_to_vector_literal(vector).startswith("[")
    monkeypatch.setattr(experience_store, "_is_pgvector_enabled", lambda force_refresh=False: False)
    assert json.loads(experience_store._embedding_to_sql(vector))[0] == 0.1
    assert experience_store._normalize_json_field(None) == {}
    assert experience_store._normalize_json_field("bad", []) == []
    assert experience_store._normalize_experience_row({"content": '{"x":1}', "evidence_refs": "[]"})["content"] == {"x": 1}
    assert experience_store._normalize_pattern_row({"evidence_ids": '["x"]'})["evidence_ids"] == ["x"]
    assert experience_store._normalize_retrieval_score_rows("bad") == []
    assert experience_store._normalize_retrieval_score_rows('[{"id":"x"},1]') == [{"id": "x"}]
    assert experience_store._keyword_overlap_score(set(), {"x"}) == 0.0
    assert experience_store._keyword_overlap_score({"x"}, {"x", "y"}) == 1.0
    assert experience_store._unique_id_list([{"id": "x"}, {"id": "x"}, {}]) == ["x"]
    assert experience_store._default_applicability("T", None)["when"]
    assert experience_store._default_not_applicable()["when_not"]
    assert experience_store._load_retrieval_policy({"max_experiences": 1})["max_experiences"] == 1

    monkeypatch.setattr(experience_store, "embed", lambda _text: vector)
    experience_store._QUERY_EMBED_CACHE.clear()
    assert experience_store._embed_query_cached("") == vector
    assert experience_store._embed_query_cached("query") is experience_store._embed_query_cached("query")
    monkeypatch.setattr(experience_store, "_find_duplicate_experience", lambda **_kwargs: ENTITY_ID)
    cur = FakeCursor(one=[{"id": OTHER_ID}])
    _install_fake_db(monkeypatch, experience_store, cur)
    duplicate = experience_store.save_experience(
        category="testing_strategy",
        title="T",
        content={"conclusion": "C"},
        skill_name="demo",
    )
    assert duplicate == ENTITY_ID
    monkeypatch.setattr(experience_store, "_find_duplicate_experience", lambda **_kwargs: None)
    created = experience_store.save_experience(
        category="testing_strategy",
        title="T2",
        content={},
        skill_name="demo",
        initial_score=0.5,
    )
    assert created == OTHER_ID
    with pytest.raises(ValueError, match="Invalid category"):
        experience_store.save_experience(category="bad", title="x", content={}, skill_name="demo")
    with pytest.raises(ValueError, match="initial_score"):
        experience_store.save_experience(category="testing_strategy", title="x", content={}, skill_name="demo", initial_score=2)

    assert experience_store.update_confidence(ENTITY_ID, 0.8, new_context=True)
    with pytest.raises(ValueError):
        experience_store.update_confidence(ENTITY_ID, 2)
    assert experience_store.adjust_confidence(ENTITY_ID, -0.1)
    assert experience_store.add_conflict(ENTITY_ID, OTHER_ID)

    query_cur = FakeCursor(
        all_rows=[
            [{"id": ENTITY_ID, "content": '{"conclusion":"C"}', "current_confidence": 0.8}],
            [{"id": OTHER_ID, "evidence_ids": "[]"}],
            [{"id": OTHER_ID, "evidence_ids": "[]"}],
        ]
    )
    _install_fake_db(monkeypatch, experience_store, query_cur)
    monkeypatch.setattr(experience_store, "apply_memory_freshness", lambda items, **_kwargs: items)
    found = experience_store.search_by_keywords(["Term"], category="testing_strategy", skill_name="demo", project_id=PROJECT_ID)
    assert found[0]["_combined_score"] == 0.8
    assert experience_store.get_failure_patterns(query_text=None, project_id=PROJECT_ID)[0]["id"] == OTHER_ID
    patterns = experience_store._fetch_active_pattern_candidates(project_id=PROJECT_ID, skill_name="demo")
    assert patterns[0]["evidence_ids"] == []
    score, reasons = experience_store._score_pattern_candidate(
        "demo pattern",
        {"name": "demo pattern", "avg_score": 0.8, "experience_count": 3, "context_diversity": 2},
    )
    assert score > 0 and reasons


def test_experience_store_feedback_budget_runs_and_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    assert experience_store._extract_keywords("The QUICK quick brown fox") == ["quick", "quick", "brown"]
    assert experience_store._estimate_retrieval_item_tokens({"title": "x"}) >= 1
    items = [{"id": "a", "title": "a"}, {"id": "b", "title": "b"}]
    monkeypatch.setattr(experience_store, "_estimate_retrieval_item_tokens", lambda _item: 10)
    budgeted = experience_store._apply_token_budget(items, items, items, items, budget=25)
    assert budgeted[4] == 20 and budgeted[5]
    summary = experience_store._summarize_scope_learning_profile({"active": True, "sample_count": 2, "score_delta": 0.2})
    assert summary["active"] is True
    adapted = experience_store._adapt_retrieval_policy_from_scope_learning(
        dict(experience_store.DEFAULT_RETRIEVAL_POLICY),
        {"active": True, "sample_count": 2, "eager_cross_project": True, "recommended_cross_project_memories": 2},
    )
    assert adapted["max_cross_project_memories"] == 2
    extended = experience_store._extend_unique_items(
        [{"id": "a", "project_id": PROJECT_ID}],
        [{"id": "a"}, {"id": "b", "project_id": OTHER_ID}],
        limit=2,
        exclude_project_id=PROJECT_ID,
    )
    assert [item["id"] for item in extended] == ["a", "b"]
    filtered, removed = experience_store._apply_feedback_signal_filters(
        [{"id": "a"}, {"id": "b"}],
        positive_ids={"a"},
        negative_ids={"b"},
        role_label="experience",
    )
    assert filtered[0]["_feedback_boost"] == 1 and removed == 1

    run_cur = FakeCursor(one=[{"id": ENTITY_ID}])
    _install_fake_db(monkeypatch, experience_store, run_cur)
    run_id = experience_store.record_retrieval_run(
        "s",
        PROJECT_ID,
        "demo",
        "context",
        {
            "experiences": [{"id": ENTITY_ID, "_similarity": 0.8}],
            "failure_patterns": [{"id": OTHER_ID}],
            "conflict_cases": [],
            "active_patterns": [{"id": PROJECT_ID}],
            "policy_applied": {},
        },
        "use",
    )
    assert run_id == ENTITY_ID
    assert experience_store.finalize_retrieval_run(run_id, "success")

    empty_cur = FakeCursor(all_rows=[[]])
    _install_fake_db(monkeypatch, experience_store, empty_cur)
    assert experience_store.compute_and_store_missing_embeddings() == 0
    rows_cur = FakeCursor(all_rows=[[{"id": ENTITY_ID, "title": "A", "content": '{"conclusion":"C"}'}, {"id": OTHER_ID, "title": "B", "content": {}}]])
    _install_fake_db(monkeypatch, experience_store, rows_cur)
    monkeypatch.setattr(experience_store, "embed", lambda text: [0.1] * experience_store.EMBEDDING_DIM if text.startswith("A") else (_ for _ in ()).throw(RuntimeError("bad")))
    monkeypatch.setattr(experience_store, "_embedding_to_sql", lambda _value: "vector")
    with pytest.warns(RuntimeWarning):
        assert experience_store.compute_and_store_missing_embeddings() == 1

    graph_cur = FakeCursor(
        all_rows=[
            [{"id": OTHER_ID, "title": "B", "smart_summary": None, "relation_type": "supports", "context_note": None, "direction": "outgoing"}],
            [],
        ]
    )
    _install_fake_db(monkeypatch, experience_store, graph_cur)
    assert experience_store.get_graph_connections(ENTITY_ID, depth=0) == []
    assert len(experience_store.get_graph_connections(ENTITY_ID, depth=2)) == 1
    monkeypatch.setattr(
        experience_store,
        "semantic_search",
        lambda **_kwargs: [{"id": ENTITY_ID, "title": "Law", "content": {"conclusion": "summary"}, "_similarity": 0.9, "current_confidence": 0.8}],
    )
    monkeypatch.setattr(experience_store, "get_graph_connections", lambda *_args, **_kwargs: [{"id": OTHER_ID}])
    legal = experience_store.consulta_legal_expandida("law")
    assert legal["total_directos"] == 1 and legal["total_expandidos"] == 1
