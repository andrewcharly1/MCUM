from __future__ import annotations

from pathlib import Path

from MCUM import install_schema


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_FILE = ROOT / "db" / "schema.sql"


def test_schema_source_of_truth_includes_governed_library_contract() -> None:
    schema = SCHEMA_FILE.read_text(encoding="utf-8", errors="replace")

    assert "CREATE SCHEMA IF NOT EXISTS knowledge_library;" in schema
    assert "CREATE TABLE IF NOT EXISTS knowledge_library.documents" in schema
    assert "knowledge_library.concept_embeddings" in schema
    assert "CREATE OR REPLACE VIEW knowledge_library.methodology_tags AS" in schema
    assert "CREATE TABLE IF NOT EXISTS project_registry.design_system_profiles" in schema
    assert "CREATE TABLE IF NOT EXISTS project_registry.design_system_versions" in schema
    assert "CREATE TABLE IF NOT EXISTS project_registry.spec_contracts" in schema
    assert "CREATE TABLE IF NOT EXISTS project_registry.spec_acceptance_criteria" in schema
    assert "CREATE SCHEMA IF NOT EXISTS code_graph;" in schema
    assert "CREATE TABLE IF NOT EXISTS code_graph.graphs" in schema
    assert "CREATE TABLE IF NOT EXISTS code_graph.experience_links" in schema
    assert "CREATE SCHEMA IF NOT EXISTS mcum_graph;" in schema
    assert "CREATE TABLE IF NOT EXISTS mcum_graph.entities" in schema
    assert "CREATE TABLE IF NOT EXISTS mcum_graph.relations" in schema
    assert "CREATE TABLE IF NOT EXISTS mcum_graph.snapshots" in schema
    assert "CREATE TABLE IF NOT EXISTS mcum_graph.context_packs" in schema
    assert "CREATE TABLE IF NOT EXISTS mcum_graph.analytics_runs" in schema
    assert "CREATE TABLE IF NOT EXISTS mcum_graph.communities" in schema
    assert "CREATE TABLE IF NOT EXISTS mcum_graph.entity_metrics" in schema
    assert "CREATE TABLE IF NOT EXISTS mcum_graph.impact_runs" in schema
    assert "CREATE TABLE IF NOT EXISTS mcum_graph.test_selections" in schema
    assert "CREATE TABLE IF NOT EXISTS mcum_graph.comparisons" in schema
    assert "CREATE TABLE IF NOT EXISTS mcum_graph.source_artifacts" in schema
    assert "CREATE TABLE IF NOT EXISTS project_registry.connector_registry" in schema
    assert "CREATE TABLE IF NOT EXISTS project_registry.connector_health_events" in schema
    assert "CREATE OR REPLACE VIEW mcum_graph.v_project_health AS" in schema
    assert "idx_mcum_graph_entities_active_canonical" in schema
    assert "CREATE TABLE IF NOT EXISTS core_brain.pattern_candidates" in schema
    assert "CREATE TABLE IF NOT EXISTS core_brain.pattern_discovery_runs" in schema
    assert "CREATE TABLE IF NOT EXISTS core_brain.pattern_usage_events" in schema
    assert "CREATE OR REPLACE VIEW core_brain.v_pattern_health AS" in schema
    assert "CREATE OR REPLACE VIEW core_brain.v_pattern_activation_backlog AS" in schema
    assert "CREATE OR REPLACE FUNCTION core_brain.refresh_pattern_metrics()" in schema
    assert "Promotion is deliberately not performed in SQL" in schema
    assert "CREATE OR REPLACE FUNCTION code_graph.context_pack" in schema
    assert "CREATE OR REPLACE FUNCTION code_graph.context_pack_filtered" in schema
    assert "CREATE OR REPLACE VIEW code_graph.v_experience_links AS" in schema
    assert "idx_spec_contracts_project_status" in schema
    assert "queued','running','success','skipped','partial','failure" in schema
    assert "MCUM Schema v3.17 instalado OK" in schema


def test_install_schema_reads_same_source_of_truth() -> None:
    direct = SCHEMA_FILE.read_text(encoding="utf-8", errors="replace")
    loaded = install_schema.read_schema()

    assert loaded == direct
