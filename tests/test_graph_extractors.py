from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from MCUM.core.graph_extractors import (
    AdapterPayload,
    ArtifactBudget,
    ArtifactExtractor,
    ArtifactPolicy,
    TreeSitterExtractor,
)
from MCUM.core.graph_extractors import tree_sitter_extractor as tree_sitter_module


class FakeNode:
    def __init__(
        self,
        node_type: str,
        *,
        name: str | None = None,
        start_byte: int = 0,
        end_byte: int = 1,
        children: list["FakeNode"] | None = None,
        has_error: bool = False,
    ) -> None:
        self.type = node_type
        self.start_byte = start_byte
        self.end_byte = end_byte
        self.start_point = (0, start_byte)
        self.end_point = (0, end_byte)
        self.children = children or []
        self.has_error = has_error
        self._name = name

    def child_by_field_name(self, field: str) -> "FakeNameNode | None":
        if field == "name" and self._name is not None:
            return FakeNameNode(self._name)
        return None


class FakeNameNode:
    def __init__(self, name: str) -> None:
        self.text = name.encode("utf-8")


class FakeParser:
    def __init__(self, root: FakeNode) -> None:
        self.root = root

    def parse(self, source: bytes) -> object:
        assert isinstance(source, bytes)
        return type("FakeTree", (), {"root_node": self.root})()


def test_tree_sitter_injected_parser_extracts_symbols_and_provenance() -> None:
    method = FakeNode("method_definition", name="run", start_byte=20, end_byte=30)
    klass = FakeNode("class_definition", name="Worker", start_byte=5, end_byte=35, children=[method])
    root = FakeNode("module", children=[klass])
    extractor = TreeSitterExtractor("python", parser=FakeParser(root))

    result = extractor.extract("class Worker:\n    def run(self): pass\n", "src/worker.py")

    assert result.metadata["fallback"] is None
    assert [node.node_kind for node in result.nodes] == ["file", "class", "method"]
    assert result.nodes[1].provenance.extractor == "tree_sitter"
    assert result.nodes[1].confidence == 0.95
    assert len(result.edges) == 2
    assert result.edges[0].source_id == result.nodes[0].node_id
    assert result.edges[1].source_id == result.nodes[1].node_id


def test_tree_sitter_missing_grammar_returns_file_only_fallback(monkeypatch) -> None:
    monkeypatch.setattr(TreeSitterExtractor, "library_available", staticmethod(lambda: True))
    monkeypatch.setattr(TreeSitterExtractor, "language_pack_available", staticmethod(lambda: False))

    result = TreeSitterExtractor("python").extract("x = 1\n", "src/example.py")

    assert len(result.nodes) == 1
    assert result.nodes[0].node_kind == "file"
    assert result.metadata["fallback"] == "file_only"
    assert result.diagnostics[0].code == "grammar_missing"


def test_tree_sitter_builds_real_optional_parser_with_injected_grammar(monkeypatch) -> None:
    grammar = object()
    captured: list[object] = []
    root = FakeNode("module")

    class OptionalParser:
        def __init__(self, language: object) -> None:
            captured.append(language)

        def parse(self, source: bytes) -> object:
            return SimpleNamespace(root_node=root)

    monkeypatch.setattr(TreeSitterExtractor, "library_available", staticmethod(lambda: True))
    monkeypatch.setattr(TreeSitterExtractor, "language_pack_available", staticmethod(lambda: False))
    monkeypatch.setattr(
        tree_sitter_module.importlib,
        "import_module",
        lambda name: SimpleNamespace(Parser=OptionalParser),
    )

    result = TreeSitterExtractor("python", grammar=grammar).extract("x = 1\n", "src/example.py")

    assert captured == [grammar]
    assert result.metadata["fallback"] is None
    assert result.diagnostics == []


def test_tree_sitter_parser_failure_is_recoverable() -> None:
    class BrokenParser:
        def parse(self, source: bytes) -> object:
            raise RuntimeError("parse failed")

    result = TreeSitterExtractor("python", parser=BrokenParser()).extract("x = 1", "x.py")

    assert result.ok
    assert result.metadata["fallback"] == "file_only"
    assert result.diagnostics[0].code == "tree_sitter_parse_failed"


def test_artifact_extractor_splits_markdown_hashes_and_deduplicates(tmp_path) -> None:
    first = tmp_path / "docs" / "guide.md"
    first.parent.mkdir()
    first.write_text("# Intro\nHello\n## Next\nWorld\n", encoding="utf-8")
    duplicate = tmp_path / "copy.md"
    duplicate.write_bytes(first.read_bytes())
    extractor = ArtifactExtractor(tmp_path)

    result = extractor.extract(first)
    duplicate_result = extractor.extract(duplicate)

    assert result.content_hash
    assert [section.metadata["heading"] for section in result.sections] == ["Intro", "Next"]
    assert duplicate_result.sections == []
    assert duplicate_result.diagnostics[0].code == "artifact_duplicate"
    assert duplicate_result.metadata["duplicate_of"] == "docs/guide.md"


def test_artifact_extractor_rejects_outside_root_disallowed_and_large_files(tmp_path) -> None:
    outside = tmp_path.parent / "outside-mcum-test.txt"
    outside.write_text("outside", encoding="utf-8")
    blocked = tmp_path / "payload.exe"
    blocked.write_bytes(b"MZ")
    large = tmp_path / "large.txt"
    large.write_text("12345", encoding="utf-8")
    extractor = ArtifactExtractor(tmp_path, policy=ArtifactPolicy(max_bytes=4))
    try:
        outside_result = extractor.extract(outside)
        blocked_result = extractor.extract(blocked)
        large_result = extractor.extract(large)
    finally:
        outside.unlink(missing_ok=True)

    assert outside_result.diagnostics[0].code == "artifact_path_rejected"
    assert blocked_result.diagnostics[0].code == "artifact_type_not_allowed"
    assert large_result.diagnostics[0].code == "artifact_too_large"


def test_media_adapter_metadata_is_sanitized_and_ocr_is_off_by_default(tmp_path) -> None:
    image = tmp_path / "scan.png"
    image.write_bytes(b"\x89PNG\r\n")
    calls: list[dict[str, object]] = []

    def adapter(path: Path, **options: object) -> AdapterPayload:
        calls.append(options)
        return AdapterPayload(
            metadata={"width": 10, "raw": b"binary-data"},
            sections=[
                {"kind": "ocr", "text": "secret text"},
                {"kind": "preview", "text": b"binary-section"},
            ],
        )

    result = ArtifactExtractor(tmp_path, adapters={"image": adapter}).extract(image)

    assert calls[0]["enable_ocr"] is False
    assert result.sections[0].text == "[binary section omitted: 14 bytes]"
    assert result.metadata["adapter_metadata"]["raw"] == "[binary omitted: 11 bytes]"
    assert result.diagnostics[0].code == "ocr_disabled"
    assert b"binary-data" not in str(result.to_dict()).encode("utf-8")
    assert b"binary-section" not in str(result.to_dict()).encode("utf-8")


def test_ocr_requires_flag_and_budget_and_truncates_output(tmp_path) -> None:
    image = tmp_path / "scan.jpg"
    image.write_bytes(b"jpeg")
    policy = ArtifactPolicy(enable_ocr=True, budget=ArtifactBudget(ocr_chars=5))

    def adapter(path: Path, **options: object) -> dict[str, object]:
        assert options["enable_ocr"] is True
        return {"metadata": {"width": 20}, "sections": [{"kind": "ocr", "text": "abcdefgh"}]}

    result = ArtifactExtractor(tmp_path, policy=policy, adapters={"image": adapter}).extract(image)

    assert result.sections[0].text == "abcde"
    assert result.sections[0].confidence == 0.8
    assert any(item.code == "artifact_section_truncated" for item in result.diagnostics)


def test_pdf_without_adapter_returns_metadata_only(tmp_path) -> None:
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    result = ArtifactExtractor(tmp_path).extract(pdf)

    assert result.sections == []
    assert result.metadata["artifact_kind"] == "pdf"
    assert result.metadata["fallback"] == "metadata_only"
    assert result.diagnostics[0].code in {"artifact_adapter_unavailable", "artifact_adapter_failed"}


def test_video_transcription_requires_policy_flag_and_budget(tmp_path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    calls: list[dict[str, object]] = []
    policy = ArtifactPolicy(
        enable_transcription=True,
        budget=ArtifactBudget(transcription_chars=4),
    )

    def adapter(path: Path, **options: object) -> dict[str, object]:
        calls.append(options)
        return {
            "metadata": {"duration_seconds": 12},
            "sections": [{"kind": "transcript", "text": "abcdefgh"}],
        }

    result = ArtifactExtractor(tmp_path, policy=policy, adapters={"video": adapter}).extract(video)

    assert calls[0]["enable_transcription"] is True
    assert result.sections[0].text == "abcd"
    assert result.metadata["adapter_metadata"]["duration_seconds"] == 12
