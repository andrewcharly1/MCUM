from __future__ import annotations

from MCUM.core.code_graph_indexer import scan_project_code_graph


def test_scan_project_code_graph_extracts_python_symbols_and_edges(tmp_path) -> None:
    source = tmp_path / "src" / "auth.py"
    source.parent.mkdir()
    source.write_text(
        "\n".join(
            [
                "import os",
                "from services.user import load_user",
                "",
                "class AuthService:",
                "    def login(self, user_id):",
                "        return load_user(user_id)",
            ]
        ),
        encoding="utf-8",
    )

    result = scan_project_code_graph(str(tmp_path))

    qualified = {node["qualified_name"] for node in result["nodes"]}
    edge_targets = {edge["target_ref"] for edge in result["edges"]}
    assert "auth" in qualified
    assert "auth.AuthService" in qualified
    assert "auth.AuthService.login" in qualified
    assert "services.user.load_user" in edge_targets
    assert "load_user" in edge_targets
    assert result["stats"]["files_indexed"] == 1
    assert result["stats"]["tokens_indexed_estimate"] > 0


def test_scan_project_code_graph_excludes_node_modules(tmp_path) -> None:
    app = tmp_path / "app.py"
    app.write_text("def ok():\n    return 1\n", encoding="utf-8")
    vendored = tmp_path / "node_modules" / "pkg" / "index.js"
    vendored.parent.mkdir(parents=True)
    vendored.write_text("function noisy() {}", encoding="utf-8")

    result = scan_project_code_graph(str(tmp_path))

    paths = {item["relative_path"] for item in result["files"]}
    assert "app.py" in paths
    assert "node_modules/pkg/index.js" not in paths
    assert result["stats"]["directories_pruned"] == 1


def test_scan_project_code_graph_extracts_dart_symbols_and_excludes_generated_files(tmp_path) -> None:
    service = tmp_path / "apps" / "driver" / "lib" / "gps_service.dart"
    service.parent.mkdir(parents=True)
    service.write_text(
        "\n".join(
            [
                "import 'package:supabase_flutter/supabase_flutter.dart';",
                "",
                "class GpsService {",
                "  Future<void> syncPosition(String tripId) async {",
                "    return;",
                "  }",
                "}",
            ]
        ),
        encoding="utf-8",
    )
    generated = tmp_path / "apps" / "driver" / "ios" / "Flutter" / "ephemeral" / "flutter_lldb_helper.py"
    generated.parent.mkdir(parents=True)
    generated.write_text("def noisy():\n    return 1\n", encoding="utf-8")

    result = scan_project_code_graph(str(tmp_path))

    paths = {item["relative_path"] for item in result["files"]}
    qualified = {node["qualified_name"] for node in result["nodes"]}
    edge_targets = {edge["target_ref"] for edge in result["edges"]}
    assert "apps/driver/lib/gps_service.dart" in paths
    assert "apps/driver/ios/Flutter/ephemeral/flutter_lldb_helper.py" not in paths
    assert "apps.driver.lib.gps_service.GpsService" in qualified
    assert "apps.driver.lib.gps_service.syncPosition" in qualified
    assert "package:supabase_flutter/supabase_flutter.dart" in edge_targets


def test_scan_project_code_graph_compacts_multiline_signatures(tmp_path) -> None:
    source = tmp_path / "src" / "large.ts"
    source.parent.mkdir()
    params = ",\n".join(f"  value{index}: string" for index in range(80))
    source.write_text(f"function renderHtml(\n{params}\n) {{ return ''; }}", encoding="utf-8")

    result = scan_project_code_graph(str(tmp_path))

    function_node = next(node for node in result["nodes"] if node["node_kind"] == "function")
    assert "\n" not in function_node["signature"]
    assert len(function_node["signature"]) <= 280


def test_scan_project_code_graph_survives_null_bytes(tmp_path) -> None:
    # Regression: a single file with null bytes (UTF-16, binary mislabeled as
    # source, or corruption) used to raise ValueError "source code string
    # cannot contain null bytes" from ast.parse() and abort the entire scan.
    good = tmp_path / "good.py"
    good.write_text("def healthy():\n    return 1\n", encoding="utf-8")
    poisoned = tmp_path / "poisoned.py"
    poisoned.write_bytes(b"def broken():\x00\x00\n    return 2\n")
    utf16_sql = tmp_path / "report.sql"
    utf16_sql.write_bytes("SELECT 1 AS x;\n".encode("utf-16"))

    # Must not raise.
    result = scan_project_code_graph(str(tmp_path))

    paths = {item["relative_path"] for item in result["files"]}
    qualified = {node["qualified_name"] for node in result["nodes"]}
    assert "good.py" in paths
    assert "poisoned.py" in paths
    assert "report.sql" in paths
    # The healthy file is still fully parsed.
    assert "good.healthy" in qualified
    # The poisoned file degrades to a parse_error node instead of crashing,
    # OR parses cleanly now that null bytes are stripped at read time.
    assert result["stats"]["files_indexed"] == 3


def test_scan_project_code_graph_respects_file_budget(tmp_path) -> None:
    # Regression: an unbounded os.walk over a huge tree (e.g. an entire OneDrive
    # workspace) blocked session-begin for 60-128s. A file budget caps the scan.
    for index in range(30):
        (tmp_path / f"mod_{index}.py").write_text(
            f"def fn_{index}():\n    return {index}\n", encoding="utf-8"
        )

    capped = scan_project_code_graph(str(tmp_path), max_files=10)
    assert capped["stats"]["files_indexed"] == 10
    assert capped["stats"]["budget_exhausted"] is True

    full = scan_project_code_graph(str(tmp_path))
    assert full["stats"]["files_indexed"] == 30
    assert full["stats"]["budget_exhausted"] is False


def test_scan_project_code_graph_indexes_only_incremental_delta(tmp_path) -> None:
    stable = tmp_path / "stable.py"
    changed = tmp_path / "changed.py"
    deleted = tmp_path / "deleted.py"
    stable.write_text("def stable():\n    return 1\n", encoding="utf-8")
    changed.write_text("def before():\n    return 1\n", encoding="utf-8")
    deleted.write_text("def gone():\n    return 1\n", encoding="utf-8")

    full = scan_project_code_graph(str(tmp_path))
    manifest = {item["relative_path"]: item for item in full["files"]}

    changed.write_text("def after():\n    return 2\n", encoding="utf-8")
    deleted.unlink()
    added = tmp_path / "added.py"
    added.write_text("def added():\n    return 3\n", encoding="utf-8")

    delta = scan_project_code_graph(str(tmp_path), previous_manifest=manifest)

    assert {item["relative_path"] for item in delta["files"]} == {"added.py", "changed.py"}
    assert delta["delta"]["new_paths"] == ["added.py"]
    assert delta["delta"]["modified_paths"] == ["changed.py"]
    assert delta["delta"]["deleted_paths"] == ["deleted.py"]
    assert delta["delta"]["unchanged_paths"] == ["stable.py"]
    assert delta["stats"]["files_indexed"] == 2
    assert delta["stats"]["tokens_indexed_estimate"] < delta["stats"]["tokens_project_estimate"]
