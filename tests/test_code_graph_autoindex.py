from __future__ import annotations

from MCUM.core.code_graph_autoindex import (
    DEFAULT_MAX_FIRST_BUILD_BYTES,
    DEFAULT_MAX_FIRST_BUILD_FILES,
    compute_source_fingerprint,
    decide_action,
    is_code_relevant,
)


def test_is_code_relevant_defaults_true_and_denylists_non_code() -> None:
    assert is_code_relevant(None) is True
    assert is_code_relevant("validar") is True
    assert is_code_relevant("mejorar") is True
    assert is_code_relevant("documentar") is False
    assert is_code_relevant("CONSULTAR") is False
    # force overrides the denylist
    assert is_code_relevant("documentar", force=True) is True


def test_fingerprint_is_deterministic_and_change_sensitive(tmp_path) -> None:
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "a.py").write_text("x = 1\n", encoding="utf-8")
    (src / "b.ts").write_text("export const y = 2;\n", encoding="utf-8")

    first = compute_source_fingerprint(tmp_path)
    second = compute_source_fingerprint(tmp_path)
    assert first["fingerprint"] == second["fingerprint"]
    assert first["file_count"] == 2

    # A new source file must change the fingerprint.
    (src / "c.go").write_text("package main\n", encoding="utf-8")
    third = compute_source_fingerprint(tmp_path)
    assert third["file_count"] == 3
    assert third["fingerprint"] != first["fingerprint"]


def test_fingerprint_excludes_vendored_dirs(tmp_path) -> None:
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    vendored = tmp_path / "node_modules" / "left-pad"
    vendored.mkdir(parents=True)
    (vendored / "index.js").write_text("module.exports = 1;\n", encoding="utf-8")

    info = compute_source_fingerprint(tmp_path)
    # Only app.py counts; node_modules is pruned.
    assert info["file_count"] == 1


def test_decide_action_no_code() -> None:
    decision = decide_action(None, {"file_count": 0, "total_bytes": 0, "fingerprint": "fp"})
    assert decision["action"] == "no_code"


def test_decide_action_missing_graph_indexes() -> None:
    fp = {"file_count": 5, "total_bytes": 100, "fingerprint": "fp-new"}
    decision = decide_action(None, fp)
    assert decision["action"] == "index"
    assert decision["incremental"] is False


def test_decide_action_fresh_when_fingerprint_matches() -> None:
    state = {
        "status": "active",
        "files_indexed": 5,
        "metadata": {"autoindex_fingerprint": "fp-match"},
    }
    fp = {"file_count": 5, "total_bytes": 100, "fingerprint": "fp-match"}
    decision = decide_action(state, fp)
    assert decision["action"] == "fresh"


def test_decide_action_stale_when_fingerprint_differs() -> None:
    state = {
        "status": "active",
        "files_indexed": 5,
        "metadata": {"autoindex_fingerprint": "fp-old"},
    }
    fp = {"file_count": 6, "total_bytes": 120, "fingerprint": "fp-new"}
    decision = decide_action(state, fp)
    assert decision["action"] == "index"
    assert decision["incremental"] is True


def test_decide_action_force_reindexes_even_when_fresh() -> None:
    state = {
        "status": "active",
        "files_indexed": 5,
        "metadata": {"autoindex_fingerprint": "fp-match"},
    }
    fp = {"file_count": 5, "total_bytes": 100, "fingerprint": "fp-match"}
    decision = decide_action(state, fp, force=True)
    assert decision["action"] == "index"


def test_decide_action_defers_large_first_build() -> None:
    fp = {
        "file_count": DEFAULT_MAX_FIRST_BUILD_FILES + 1,
        "total_bytes": 1000,
        "fingerprint": "fp-big",
    }
    decision = decide_action(None, fp)
    assert decision["action"] == "deferred"
    assert decision["recommended_tool"] == "mcum_code_graph_index"

    # allow_large overrides the deferral.
    allowed = decide_action(None, fp, allow_large=True)
    assert allowed["action"] == "index"


def test_decide_action_defers_large_first_build_by_bytes() -> None:
    fp = {
        "file_count": 10,
        "total_bytes": DEFAULT_MAX_FIRST_BUILD_BYTES + 1,
        "fingerprint": "fp-heavy",
    }
    decision = decide_action(None, fp)
    assert decision["action"] == "deferred"


def test_decide_action_falls_back_to_source_hash() -> None:
    # When no autoindex_fingerprint is stored, the legacy source_hash is used.
    state = {"status": "active", "files_indexed": 3, "source_hash": "fp-legacy", "metadata": {}}
    fp = {"file_count": 3, "total_bytes": 50, "fingerprint": "fp-legacy"}
    decision = decide_action(state, fp)
    assert decision["action"] == "fresh"
