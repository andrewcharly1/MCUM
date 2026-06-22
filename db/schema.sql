-- ============================================================
-- MOTOR CEREBRAL ULTRA MULTIVERSAL (MCUM) v2.3
-- Schema PostgreSQL - Fundacion del Sistema Cognitivo
-- ASCII-safe para compatibilidad Windows psql encoding
-- ============================================================
-- Uso: psql -U postgres -d postgres -f db/schema_install.sql
-- ============================================================

-- Extensiones
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- pgvector (optional — graceful fallback if not available)
DO $$ BEGIN
    CREATE EXTENSION IF NOT EXISTS vector;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pgvector extension not available — using JSONB fallback for embeddings';
END $$;

-- Schemas
CREATE SCHEMA IF NOT EXISTS core_brain;
CREATE SCHEMA IF NOT EXISTS project_registry;
CREATE SCHEMA IF NOT EXISTS knowledge_library;
CREATE SCHEMA IF NOT EXISTS code_graph;
CREATE SCHEMA IF NOT EXISTS mcum_graph;

-- ─────────────────────────────────────────
-- ENUMS core_brain
-- ─────────────────────────────────────────
DO $$ BEGIN
    CREATE TYPE core_brain.knowledge_category AS ENUM (
        'stack_decision',
        'architecture_pattern',
        'implementation_recipe',
        'testing_strategy',
        'prompting_heuristic',
        'failure_pattern',
        'regulatory_rule',
        'evaluation_policy'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE core_brain.pattern_status AS ENUM (
        'draft',
        'active',
        'deprecated'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE core_brain.test_type AS ENUM (
        'factual_retrieval',
        'negative_case',
        'conflict_resolution',
        'multi_hop',
        'precision_citation'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ─────────────────────────────────────────
-- TABLA: test_suite (creada primero por FK circular)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS core_brain.test_suite (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    skill_name          TEXT NOT NULL,
    test_type           core_brain.test_type NOT NULL,
    input_query         TEXT NOT NULL,
    expected_result     TEXT,
    expected_source     TEXT,
    expected_steps      JSONB,
    pass_condition      TEXT NOT NULL,
    partition           TEXT DEFAULT 'dev'
        CHECK (partition IN ('dev','val','adversarial')),
    difficulty          INT DEFAULT 1 CHECK (difficulty BETWEEN 1 AND 5),
    generated_by        TEXT DEFAULT 'agent'
        CHECK (generated_by IN ('agent','human','sisl')),
    source_experience_id UUID,
    is_active           BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────
-- TABLA: experiences (Core del COS)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS core_brain.experiences (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    category                core_brain.knowledge_category NOT NULL,
    title                   TEXT NOT NULL,
    content                 JSONB NOT NULL,
    applicability           JSONB,
    not_applicable_cases    JSONB,
    conditions              JSONB,
    initial_score           FLOAT DEFAULT 1.0 CHECK (initial_score BETWEEN 0 AND 1),
    current_confidence      FLOAT DEFAULT 1.0 CHECK (current_confidence BETWEEN 0 AND 1),
    revalidation_count      INT DEFAULT 0 CHECK (revalidation_count >= 0),
    unique_context_count    INT DEFAULT 0,
    contradiction_penalty   FLOAT DEFAULT 0.0,
    evaluator_independence  FLOAT DEFAULT 0.5,
    evidence_refs           JSONB,
    benchmark_run_id        UUID,
    source_artifacts        JSONB,
    review_notes            TEXT,
    is_synthetic            BOOLEAN DEFAULT FALSE,
    tested_by               TEXT DEFAULT 'agent',
    conflict_refs           UUID[],
    superseded_by           UUID REFERENCES core_brain.experiences(id),
    project_id              UUID,
    skill_name              TEXT NOT NULL,
    skill_version           TEXT,
    task_description        TEXT,
    embedding               JSONB,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    last_validated_at       TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE core_brain.experiences
    ADD COLUMN IF NOT EXISTS embedding JSONB;

-- FK diferida test_suite -> experiences
DO $$ BEGIN
    ALTER TABLE core_brain.test_suite
        ADD CONSTRAINT fk_test_source_exp
        FOREIGN KEY (source_experience_id)
        REFERENCES core_brain.experiences(id)
        DEFERRABLE INITIALLY DEFERRED;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ─────────────────────────────────────────
-- TABLA: patterns
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS core_brain.patterns (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name                    TEXT UNIQUE NOT NULL,
    description             TEXT NOT NULL,
    category                core_brain.knowledge_category NOT NULL,
    status                  core_brain.pattern_status DEFAULT 'draft',
    promotion_criteria_met  BOOLEAN DEFAULT FALSE,
    experience_count        INT DEFAULT 0,
    avg_score               FLOAT DEFAULT 0.0,
    context_diversity       INT DEFAULT 0,
    deprecated_at           TIMESTAMPTZ,
    deprecated_reason       TEXT,
    replacement_pattern_id  UUID REFERENCES core_brain.patterns(id),
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS core_brain.pattern_evidence (
    pattern_id              UUID REFERENCES core_brain.patterns(id) ON DELETE CASCADE,
    experience_id           UUID REFERENCES core_brain.experiences(id) ON DELETE CASCADE,
    added_at                TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (pattern_id, experience_id)
);

-- Pattern Intelligence V2: governed staging, evidence quality and runtime utility.
ALTER TABLE core_brain.patterns
    ADD COLUMN IF NOT EXISTS pattern_key TEXT,
    ADD COLUMN IF NOT EXISTS scope_type TEXT NOT NULL DEFAULT 'skill',
    ADD COLUMN IF NOT EXISTS scope_project_id UUID,
    ADD COLUMN IF NOT EXISTS scope_skill_name TEXT,
    ADD COLUMN IF NOT EXISTS cohesion_score FLOAT NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS support_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS contradiction_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS utility_score FLOAT NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS usage_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS success_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS failure_count INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS health_state TEXT NOT NULL DEFAULT 'observing',
    ADD COLUMN IF NOT EXISTS last_evidence_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE core_brain.pattern_evidence
    ADD COLUMN IF NOT EXISTS evidence_role TEXT NOT NULL DEFAULT 'support',
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual',
    ADD COLUMN IF NOT EXISTS similarity FLOAT,
    ADD COLUMN IF NOT EXISTS weight FLOAT NOT NULL DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS resolution TEXT,
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE UNIQUE INDEX IF NOT EXISTS idx_patterns_pattern_key
    ON core_brain.patterns (pattern_key)
    WHERE pattern_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_patterns_runtime_health
    ON core_brain.patterns (status, health_state, scope_skill_name, scope_project_id);

CREATE TABLE IF NOT EXISTS core_brain.pattern_discovery_runs (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    scope_type              TEXT NOT NULL DEFAULT 'global',
    project_id              UUID,
    status                  TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','success','partial','blocked','failure')),
    mode                    TEXT NOT NULL DEFAULT 'shadow',
    policy_version          TEXT,
    algorithm_version       TEXT,
    embedding_model         TEXT,
    experiences_scanned     INT NOT NULL DEFAULT 0,
    embeddings_generated    INT NOT NULL DEFAULT 0,
    embeddings_reused       INT NOT NULL DEFAULT 0,
    groups_analyzed         INT NOT NULL DEFAULT 0,
    candidates_observed     INT NOT NULL DEFAULT 0,
    candidates_review_ready INT NOT NULL DEFAULT 0,
    findings                JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message           TEXT,
    started_at              TIMESTAMPTZ DEFAULT NOW(),
    finished_at             TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS core_brain.pattern_candidates (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    candidate_key           TEXT UNIQUE NOT NULL,
    category                core_brain.knowledge_category NOT NULL,
    skill_name              TEXT NOT NULL,
    scope_type              TEXT NOT NULL DEFAULT 'skill',
    scope_project_id        UUID,
    label                   TEXT NOT NULL,
    summary                 TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'shadow'
        CHECK (status IN ('shadow','review','accepted','rejected','expired')),
    support_count           INT NOT NULL DEFAULT 0,
    distinct_project_count  INT NOT NULL DEFAULT 0,
    context_diversity       INT NOT NULL DEFAULT 0,
    cohesion_score          FLOAT NOT NULL DEFAULT 0.0,
    contradiction_count     INT NOT NULL DEFAULT 0,
    avg_confidence          FLOAT NOT NULL DEFAULT 0.0,
    quality_score           FLOAT NOT NULL DEFAULT 0.0,
    quality_ready           BOOLEAN NOT NULL DEFAULT FALSE,
    seed_experience_id      UUID REFERENCES core_brain.experiences(id) ON DELETE SET NULL,
    discovery_run_id        UUID REFERENCES core_brain.pattern_discovery_runs(id) ON DELETE SET NULL,
    embedding_model         TEXT,
    algorithm_version       TEXT,
    first_seen_at           TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at            TIMESTAMPTZ DEFAULT NOW(),
    reviewed_at             TIMESTAMPTZ,
    reviewed_by             TEXT,
    review_notes            TEXT,
    materialized_pattern_id UUID REFERENCES core_brain.patterns(id) ON DELETE SET NULL,
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS core_brain.pattern_candidate_evidence (
    candidate_id            UUID REFERENCES core_brain.pattern_candidates(id) ON DELETE CASCADE,
    experience_id           UUID REFERENCES core_brain.experiences(id) ON DELETE CASCADE,
    evidence_role           TEXT NOT NULL DEFAULT 'support'
        CHECK (evidence_role IN ('support','contradict','neutral')),
    similarity              FLOAT,
    weight                  FLOAT NOT NULL DEFAULT 1.0,
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    added_at                TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (candidate_id, experience_id)
);

CREATE TABLE IF NOT EXISTS core_brain.pattern_experience_embeddings (
    experience_id           UUID REFERENCES core_brain.experiences(id) ON DELETE CASCADE,
    model_name              TEXT NOT NULL,
    source_hash             TEXT NOT NULL,
    embedding               JSONB NOT NULL,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (experience_id, model_name)
);

CREATE TABLE IF NOT EXISTS core_brain.pattern_embeddings (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    pattern_id              UUID REFERENCES core_brain.patterns(id) ON DELETE CASCADE,
    candidate_id            UUID REFERENCES core_brain.pattern_candidates(id) ON DELETE CASCADE,
    model_name              TEXT NOT NULL,
    source_hash             TEXT,
    embedding               JSONB NOT NULL,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    CHECK ((pattern_id IS NOT NULL) <> (candidate_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS core_brain.pattern_usage_events (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    pattern_id              UUID NOT NULL REFERENCES core_brain.patterns(id) ON DELETE CASCADE,
    project_id              UUID,
    session_id              TEXT,
    log_id                  UUID,
    outcome                 TEXT,
    user_feedback           INT,
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pattern_candidates_status_quality
    ON core_brain.pattern_candidates (status, quality_ready, quality_score DESC);
CREATE INDEX IF NOT EXISTS idx_pattern_candidate_evidence_experience
    ON core_brain.pattern_candidate_evidence (experience_id, candidate_id);
CREATE INDEX IF NOT EXISTS idx_pattern_discovery_runs_recent
    ON core_brain.pattern_discovery_runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_pattern_usage_events_pattern
    ON core_brain.pattern_usage_events (pattern_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pattern_embeddings_pattern_model
    ON core_brain.pattern_embeddings (pattern_id, model_name)
    WHERE pattern_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_pattern_embeddings_candidate_model
    ON core_brain.pattern_embeddings (candidate_id, model_name)
    WHERE candidate_id IS NOT NULL;

-- ─────────────────────────────────────────
-- TABLA: skill_versions
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS core_brain.skill_versions (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    skill_name              TEXT NOT NULL,
    version_semver          TEXT NOT NULL,
    git_commit_hash         TEXT,
    skill_content_hash      TEXT,
    ckl_score               FLOAT,
    test_pass_count         INT DEFAULT 0,
    test_total_count        INT DEFAULT 0,
    status                  TEXT DEFAULT 'active'
        CHECK (status IN ('active','deprecated','testing')),
    changes_description     TEXT NOT NULL,
    diff_patch              TEXT,
    improvement_source      TEXT DEFAULT 'user'
        CHECK (improvement_source IN ('sisl_loop','user','kaizen_analysis','test_failure')),
    trigger_test_id         UUID REFERENCES core_brain.test_suite(id),
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────
-- TABLA: retrieval_runs
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS core_brain.retrieval_runs (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id              TEXT,
    skill_name              TEXT,
    input_context           TEXT,
    experiences_retrieved   UUID[],
    patterns_retrieved      UUID[],
    retrieval_scores        JSONB,
    retrieval_policy_used   JSONB,
    final_confidence        FLOAT,
    decision_taken          TEXT,
    outcome_status          TEXT CHECK (outcome_status IN ('success','partial','failure','review')),
    outcome_description     TEXT,
    user_feedback           INT CHECK (user_feedback IN (-1,0,1)),
    failure_reason          TEXT,
    project_id              UUID,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

-- ─────────────────────────────────────────
-- SCHEMA project_registry
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS project_registry.projects (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_name            TEXT NOT NULL,
    project_path            TEXT UNIQUE NOT NULL,
    description             TEXT,
    tech_stack              JSONB,
    primary_language        TEXT,
    frameworks              TEXT[],
    client_or_context       TEXT,
    status                  TEXT DEFAULT 'active'
        CHECK (status IN ('active','paused','completed','archived')),
    phase                   TEXT,
    total_sessions          INT DEFAULT 0,
    total_tasks_completed   INT DEFAULT 0,
    total_improvements      INT DEFAULT 0,
    avg_confidence_score    FLOAT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    last_activity_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS project_registry.project_logs (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id              UUID NOT NULL REFERENCES project_registry.projects(id),
    log_type                TEXT NOT NULL
        CHECK (log_type IN ('task','decision','improvement','error','milestone','session_start','session_end')),
    title                   TEXT NOT NULL,
    description             TEXT,
    skill_used              TEXT,
    skills_orchestrated     TEXT[],
    outcome                 TEXT CHECK (outcome IN ('success','partial','failure','pending')),
    outcome_details         TEXT,
    artifacts_generated     JSONB,
    experience_ids          UUID[],
    pattern_ids_used        UUID[],
    retrieval_run_id        UUID REFERENCES core_brain.retrieval_runs(id),
    session_duration_sec    INT,
    confidence_score        FLOAT,
    tokens_estimated        INT,
    context_tokens_in       INT,
    context_tokens_out      INT,
    task_wall_clock_ms      INT,
    retrieval_latency_ms    INT,
    git_commit              TEXT,
    log_metadata            JSONB,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE project_registry.project_logs
    ADD COLUMN IF NOT EXISTS context_tokens_in INT;

ALTER TABLE project_registry.project_logs
    ADD COLUMN IF NOT EXISTS context_tokens_out INT;

ALTER TABLE project_registry.project_logs
    ADD COLUMN IF NOT EXISTS task_wall_clock_ms INT;

ALTER TABLE project_registry.project_logs
    ADD COLUMN IF NOT EXISTS retrieval_latency_ms INT;

CREATE TABLE IF NOT EXISTS project_registry.agent_invocations (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id              UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    session_id              TEXT,
    task_log_id             UUID REFERENCES project_registry.project_logs(id) ON DELETE SET NULL,
    task_id                 TEXT,
    agent_role              TEXT NOT NULL,
    runner                  TEXT NOT NULL,
    provider                TEXT,
    model                   TEXT,
    protocol                TEXT,
    credential_source       TEXT,
    outcome                 TEXT,
    exit_code               INT,
    input_tokens            INT,
    output_tokens           INT,
    total_tokens            INT,
    prompt_tokens_estimate  INT,
    cost_usd                NUMERIC(12,6),
    wall_clock_ms           INT,
    started_at              TIMESTAMPTZ,
    finished_at             TIMESTAMPTZ,
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS code_graph.graphs (
    id                          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id                  UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    project_path                TEXT NOT NULL,
    project_name                TEXT,
    graph_version               INT NOT NULL DEFAULT 1,
    extractor_version           TEXT NOT NULL DEFAULT 'mcum-code-graph-v1',
    status                      TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','stale','building','failed')),
    mode                        TEXT NOT NULL DEFAULT 'incremental'
        CHECK (mode IN ('full','incremental','imported')),
    source_hash                 TEXT,
    files_total                 INT NOT NULL DEFAULT 0 CHECK (files_total >= 0),
    files_indexed              INT NOT NULL DEFAULT 0 CHECK (files_indexed >= 0),
    files_skipped              INT NOT NULL DEFAULT 0 CHECK (files_skipped >= 0),
    nodes_total                 INT NOT NULL DEFAULT 0 CHECK (nodes_total >= 0),
    edges_total                 INT NOT NULL DEFAULT 0 CHECK (edges_total >= 0),
    tokens_indexed_estimate     BIGINT NOT NULL DEFAULT 0 CHECK (tokens_indexed_estimate >= 0),
    tokens_context_saved_estimate BIGINT NOT NULL DEFAULT 0 CHECK (tokens_context_saved_estimate >= 0),
    error_message               TEXT,
    metadata                    JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at                  TIMESTAMPTZ,
    finished_at                 TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (project_id)
);

CREATE TABLE IF NOT EXISTS code_graph.files (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    graph_id                UUID NOT NULL REFERENCES code_graph.graphs(id) ON DELETE CASCADE,
    project_id              UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    relative_path           TEXT NOT NULL,
    absolute_path           TEXT,
    language                TEXT NOT NULL DEFAULT 'text',
    file_hash               TEXT NOT NULL,
    bytes_size              BIGINT NOT NULL DEFAULT 0 CHECK (bytes_size >= 0),
    line_count              INT NOT NULL DEFAULT 0 CHECK (line_count >= 0),
    token_estimate          INT NOT NULL DEFAULT 0 CHECK (token_estimate >= 0),
    status                  TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','deleted','skipped','parse_error')),
    indexed_at              TIMESTAMPTZ DEFAULT NOW(),
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (graph_id, relative_path)
);

CREATE TABLE IF NOT EXISTS code_graph.nodes (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    graph_id                UUID NOT NULL REFERENCES code_graph.graphs(id) ON DELETE CASCADE,
    file_id                 UUID REFERENCES code_graph.files(id) ON DELETE CASCADE,
    project_id              UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    node_kind               TEXT NOT NULL,
    name                    TEXT NOT NULL,
    qualified_name          TEXT NOT NULL,
    signature               TEXT,
    line_start              INT CHECK (line_start IS NULL OR line_start >= 1),
    line_end                INT CHECK (line_end IS NULL OR line_end >= 1),
    doc_excerpt             TEXT,
    search_text             TEXT NOT NULL DEFAULT '',
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (graph_id, qualified_name, file_id, line_start)
);

CREATE TABLE IF NOT EXISTS code_graph.edges (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    graph_id                UUID NOT NULL REFERENCES code_graph.graphs(id) ON DELETE CASCADE,
    project_id              UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    source_node_id          UUID REFERENCES code_graph.nodes(id) ON DELETE CASCADE,
    target_node_id          UUID REFERENCES code_graph.nodes(id) ON DELETE SET NULL,
    source_ref              TEXT,
    target_ref              TEXT NOT NULL,
    edge_kind               TEXT NOT NULL,
    confidence              FLOAT NOT NULL DEFAULT 0.70 CHECK (confidence BETWEEN 0 AND 1),
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS code_graph.index_runs (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    graph_id                UUID REFERENCES code_graph.graphs(id) ON DELETE SET NULL,
    project_id              UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    mode                    TEXT NOT NULL DEFAULT 'incremental'
        CHECK (mode IN ('full','incremental','imported')),
    status                  TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','success','partial','failure')),
    files_scanned           INT NOT NULL DEFAULT 0 CHECK (files_scanned >= 0),
    files_indexed           INT NOT NULL DEFAULT 0 CHECK (files_indexed >= 0),
    files_skipped           INT NOT NULL DEFAULT 0 CHECK (files_skipped >= 0),
    nodes_indexed           INT NOT NULL DEFAULT 0 CHECK (nodes_indexed >= 0),
    edges_indexed           INT NOT NULL DEFAULT 0 CHECK (edges_indexed >= 0),
    tokens_indexed_estimate BIGINT NOT NULL DEFAULT 0 CHECK (tokens_indexed_estimate >= 0),
    tokens_saved_estimate   BIGINT NOT NULL DEFAULT 0 CHECK (tokens_saved_estimate >= 0),
    duration_ms             INT,
    error_message           TEXT,
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at              TIMESTAMPTZ DEFAULT NOW(),
    finished_at             TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS code_graph.experience_links (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id              UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    graph_id                UUID NOT NULL REFERENCES code_graph.graphs(id) ON DELETE CASCADE,
    experience_id           UUID NOT NULL REFERENCES core_brain.experiences(id) ON DELETE CASCADE,
    file_id                 UUID REFERENCES code_graph.files(id) ON DELETE SET NULL,
    node_id                 UUID REFERENCES code_graph.nodes(id) ON DELETE SET NULL,
    relative_path           TEXT NOT NULL,
    qualified_name          TEXT NOT NULL DEFAULT '',
    link_kind               TEXT NOT NULL DEFAULT 'applies_to',
    confidence              FLOAT NOT NULL DEFAULT 0.80 CHECK (confidence BETWEEN 0 AND 1),
    file_hash               TEXT,
    graph_version           INT,
    metadata                JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (experience_id, relative_path, qualified_name, link_kind)
);

CREATE TABLE IF NOT EXISTS mcum_graph.entities (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id          UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    entity_type         TEXT NOT NULL,
    canonical_key       TEXT NOT NULL,
    source_schema       TEXT NOT NULL,
    source_table        TEXT NOT NULL,
    source_id           TEXT NOT NULL,
    title               TEXT NOT NULL,
    summary             TEXT,
    content_hash        TEXT,
    confidence          FLOAT NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
    provenance_kind     TEXT NOT NULL DEFAULT 'extracted',
    health_state        TEXT NOT NULL DEFAULT 'active',
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    valid_from          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_to            TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, source_schema, source_table, source_id)
);

CREATE TABLE IF NOT EXISTS mcum_graph.relations (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id          UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    source_entity_id    UUID NOT NULL REFERENCES mcum_graph.entities(id) ON DELETE CASCADE,
    target_entity_id    UUID NOT NULL REFERENCES mcum_graph.entities(id) ON DELETE CASCADE,
    relation_type       TEXT NOT NULL,
    weight              FLOAT NOT NULL DEFAULT 1.0,
    confidence          FLOAT NOT NULL DEFAULT 1.0 CHECK (confidence BETWEEN 0 AND 1),
    provenance_kind     TEXT NOT NULL DEFAULT 'extracted',
    evidence_ref        JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    valid_from          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_to            TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, source_entity_id, target_entity_id, relation_type)
);

CREATE TABLE IF NOT EXISTS mcum_graph.snapshots (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id          UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    trigger             TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'active',
    code_graph_version  INT,
    entity_count        INT NOT NULL DEFAULT 0,
    relation_count      INT NOT NULL DEFAULT 0,
    source_hash         TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mcum_graph.context_packs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id          UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    snapshot_id         UUID REFERENCES mcum_graph.snapshots(id) ON DELETE SET NULL,
    session_id          TEXT,
    agent_role          TEXT NOT NULL DEFAULT 'coordinator',
    task_query          TEXT NOT NULL,
    token_budget        INT NOT NULL DEFAULT 0,
    token_estimate      INT NOT NULL DEFAULT 0,
    envelope            JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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
);

CREATE TABLE IF NOT EXISTS project_registry.spec_assumptions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    spec_contract_id    UUID NOT NULL REFERENCES project_registry.spec_contracts(id) ON DELETE CASCADE,
    assumption_code     TEXT NOT NULL,
    assumption_text     TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'inferred',
    risk_level          TEXT DEFAULT 'medium',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (spec_contract_id, assumption_code)
);

CREATE TABLE IF NOT EXISTS project_registry.spec_scenarios (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    spec_contract_id    UUID NOT NULL REFERENCES project_registry.spec_contracts(id) ON DELETE CASCADE,
    scenario_kind       TEXT NOT NULL,
    title               TEXT NOT NULL,
    given_text          TEXT,
    when_text           TEXT,
    then_text           TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS project_registry.spec_acceptance_criteria (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    spec_contract_id    UUID NOT NULL REFERENCES project_registry.spec_contracts(id) ON DELETE CASCADE,
    criteria_code       TEXT NOT NULL,
    criteria_text       TEXT NOT NULL,
    verification        TEXT,
    required            BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (spec_contract_id, criteria_code)
);

CREATE TABLE IF NOT EXISTS project_registry.spec_trace_links (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    spec_contract_id    UUID NOT NULL REFERENCES project_registry.spec_contracts(id) ON DELETE CASCADE,
    link_kind           TEXT NOT NULL,
    target_ref          TEXT NOT NULL,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS project_registry.design_system_profiles (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id              UUID NOT NULL UNIQUE REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    product_name            TEXT NOT NULL,
    audience                TEXT,
    platform_targets        TEXT[] DEFAULT '{}',
    design_maturity         TEXT DEFAULT 'draft'
        CHECK (design_maturity IN ('draft','proposed','approved','deprecated')),
    source_summary          TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS project_registry.design_system_versions (
    id                          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    profile_id                  UUID NOT NULL REFERENCES project_registry.design_system_profiles(id) ON DELETE CASCADE,
    version_number              INT NOT NULL CHECK (version_number > 0),
    status                      TEXT DEFAULT 'proposed'
        CHECK (status IN ('proposed','approved','deprecated','rejected')),
    source_kind                 TEXT DEFAULT 'manual'
        CHECK (source_kind IN ('manual','reference_image','screenshot','existing_product','mixed')),
    design_brief                JSONB NOT NULL DEFAULT '{}'::jsonb,
    design_tokens               JSONB NOT NULL DEFAULT '{}'::jsonb,
    layout_system               JSONB NOT NULL DEFAULT '{}'::jsonb,
    component_guidelines        JSONB NOT NULL DEFAULT '{}'::jsonb,
    interaction_guidelines      JSONB NOT NULL DEFAULT '{}'::jsonb,
    accessibility_guidelines    JSONB NOT NULL DEFAULT '{}'::jsonb,
    content_voice               JSONB NOT NULL DEFAULT '{}'::jsonb,
    reference_artifacts         JSONB NOT NULL DEFAULT '[]'::jsonb,
    approval_metadata           JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by_skill            TEXT DEFAULT 'design-system-orchestrator',
    source_task_log_id          UUID REFERENCES project_registry.project_logs(id),
    created_at                  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (profile_id, version_number)
);

CREATE TABLE IF NOT EXISTS core_brain.session_playbooks (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id              UUID NOT NULL REFERENCES project_registry.projects(id),
    skill_name              TEXT NOT NULL,
    title                   TEXT NOT NULL,
    task_description        TEXT NOT NULL,
    objective               TEXT,
    output_summary          TEXT,
    validation_summary      TEXT,
    commands                JSONB,
    files_touched           JSONB,
    artifacts               JSONB,
    issues_avoided          JSONB,
    reusable_when           TEXT,
    outcome                 TEXT
        CHECK (outcome IN ('success','partial','failure')),
    confidence_score        FLOAT,
    source_session_id       TEXT,
    source_task_log_id      UUID REFERENCES project_registry.project_logs(id),
    pattern_ids             UUID[] NOT NULL DEFAULT ARRAY[]::uuid[],
    pattern_alignment_score REAL,
    reuse_count             INT DEFAULT 0,
    last_reused_at          TIMESTAMPTZ,
    embedding               JSONB,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

-- Fase 3 - Bridge pattern <-> playbook: idempotent migrations for existing installs
ALTER TABLE core_brain.session_playbooks
    ADD COLUMN IF NOT EXISTS pattern_ids UUID[] NOT NULL DEFAULT ARRAY[]::uuid[];
ALTER TABLE core_brain.session_playbooks
    ADD COLUMN IF NOT EXISTS pattern_alignment_score REAL;

CREATE TABLE IF NOT EXISTS project_registry.skill_catalog (
    skill_name              TEXT PRIMARY KEY,
    skill_dir_name          TEXT NOT NULL,
    skill_path              TEXT NOT NULL,
    source                  TEXT DEFAULT 'local'
        CHECK (source IN ('local','global','generated')),
    status                  TEXT DEFAULT 'active'
        CHECK (status IN ('candidate','active','degraded','deprecated','blocked')),
    description             TEXT,
    metadata                JSONB,
    discovered_at           TIMESTAMPTZ DEFAULT NOW(),
    last_synced_at          TIMESTAMPTZ DEFAULT NOW(),
    last_used_at            TIMESTAMPTZ,
    last_improved_at        TIMESTAMPTZ,
    experience_count        INT DEFAULT 0,
    active_test_count       INT DEFAULT 0,
    avg_confidence          FLOAT,
    project_count           INT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS project_registry.project_kpis (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id              UUID NOT NULL REFERENCES project_registry.projects(id),
    snapshot_date           DATE DEFAULT CURRENT_DATE,
    tasks_this_period       INT DEFAULT 0,
    success_rate            FLOAT,
    avg_confidence          FLOAT,
    skills_improved         INT DEFAULT 0,
    total_experiences_added INT DEFAULT 0,
    notes                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS project_registry.maintenance_runs (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id              UUID REFERENCES project_registry.projects(id),
    maintenance_name        TEXT NOT NULL DEFAULT 'daily_guard',
    scope                   TEXT DEFAULT 'project'
        CHECK (scope IN ('project','global')),
    status                  TEXT DEFAULT 'success'
        CHECK (status IN ('queued','running','success','skipped','partial','failure')),
    trigger_reason          TEXT,
    started_at              TIMESTAMPTZ DEFAULT NOW(),
    finished_at             TIMESTAMPTZ,
    last_seen_activity_at   TIMESTAMPTZ,
    metrics_snapshot        JSONB,
    findings                JSONB,
    actions_applied         JSONB,
    tokens_estimated        INT DEFAULT 0,
    notes                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW()
);

DO $$ BEGIN
    ALTER TABLE project_registry.maintenance_runs
        DROP CONSTRAINT IF EXISTS maintenance_runs_status_check;
    ALTER TABLE project_registry.maintenance_runs
        ADD CONSTRAINT maintenance_runs_status_check
        CHECK (status IN ('queued','running','success','skipped','partial','failure'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- FK project en experiences
DO $$ BEGIN
    ALTER TABLE core_brain.experiences
        ADD CONSTRAINT fk_exp_project
        FOREIGN KEY (project_id)
        REFERENCES project_registry.projects(id)
        DEFERRABLE INITIALLY DEFERRED;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE core_brain.retrieval_runs
        ADD CONSTRAINT fk_retrieval_project
        FOREIGN KEY (project_id)
        REFERENCES project_registry.projects(id)
        DEFERRABLE INITIALLY DEFERRED;
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ─────────────────────────────────────────
-- INDICES
-- ─────────────────────────────────────────
CREATE TEMP TABLE tmp_mcum_experience_dedupe_map AS
WITH ranked AS (
    SELECT
        id,
        FIRST_VALUE(id) OVER (
            PARTITION BY
                category,
                skill_name,
                title,
                COALESCE(task_description, ''),
                COALESCE(content->>'conclusion', '')
            ORDER BY created_at, id
        ) AS keeper_id
    FROM core_brain.experiences
)
SELECT
    id AS duplicate_id,
    keeper_id
FROM ranked
WHERE id <> keeper_id;

UPDATE core_brain.test_suite ts
SET source_experience_id = map.keeper_id
FROM tmp_mcum_experience_dedupe_map map
WHERE ts.source_experience_id = map.duplicate_id;

UPDATE core_brain.pattern_evidence pe
SET experience_id = map.keeper_id
FROM tmp_mcum_experience_dedupe_map map
WHERE pe.experience_id = map.duplicate_id
  AND NOT EXISTS (
      SELECT 1
      FROM core_brain.pattern_evidence existing
      WHERE existing.pattern_id = pe.pattern_id
        AND existing.experience_id = map.keeper_id
  );

DELETE FROM core_brain.pattern_evidence pe
USING tmp_mcum_experience_dedupe_map map
WHERE pe.experience_id = map.duplicate_id;

UPDATE core_brain.experiences exp
SET superseded_by = map.keeper_id
FROM tmp_mcum_experience_dedupe_map map
WHERE exp.superseded_by = map.duplicate_id;

UPDATE core_brain.experiences exp
SET conflict_refs = (
    SELECT ARRAY_AGG(DISTINCT COALESCE(map.keeper_id, ref_id))
    FROM UNNEST(exp.conflict_refs) AS refs(ref_id)
    LEFT JOIN tmp_mcum_experience_dedupe_map map
        ON map.duplicate_id = refs.ref_id
)
WHERE exp.conflict_refs IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM UNNEST(exp.conflict_refs) AS refs(ref_id)
      JOIN tmp_mcum_experience_dedupe_map map
          ON map.duplicate_id = refs.ref_id
  );

UPDATE project_registry.project_logs log
SET experience_ids = (
    SELECT ARRAY_AGG(DISTINCT COALESCE(map.keeper_id, ref_id))
    FROM UNNEST(log.experience_ids) AS refs(ref_id)
    LEFT JOIN tmp_mcum_experience_dedupe_map map
        ON map.duplicate_id = refs.ref_id
)
WHERE log.experience_ids IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM UNNEST(log.experience_ids) AS refs(ref_id)
      JOIN tmp_mcum_experience_dedupe_map map
          ON map.duplicate_id = refs.ref_id
  );

UPDATE core_brain.retrieval_runs run
SET experiences_retrieved = (
    SELECT ARRAY_AGG(DISTINCT COALESCE(map.keeper_id, ref_id))
    FROM UNNEST(run.experiences_retrieved) AS refs(ref_id)
    LEFT JOIN tmp_mcum_experience_dedupe_map map
        ON map.duplicate_id = refs.ref_id
)
WHERE run.experiences_retrieved IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM UNNEST(run.experiences_retrieved) AS refs(ref_id)
      JOIN tmp_mcum_experience_dedupe_map map
          ON map.duplicate_id = refs.ref_id
  );

DELETE FROM core_brain.experiences exp
USING tmp_mcum_experience_dedupe_map map
WHERE exp.id = map.duplicate_id;

DROP TABLE tmp_mcum_experience_dedupe_map;

UPDATE core_brain.experiences
SET skill_version = '1.0.0'
WHERE skill_name = 'mcum-orchestrator'
  AND skill_version IS NULL;

UPDATE core_brain.experiences exp
SET project_id = singleton.id
FROM (
    SELECT id
    FROM project_registry.projects
    ORDER BY created_at
    LIMIT 1
) singleton
WHERE exp.project_id IS NULL
  AND exp.skill_name = 'mcum-orchestrator'
  AND (SELECT COUNT(*) FROM project_registry.projects) = 1;

UPDATE core_brain.retrieval_runs run
SET project_id = singleton.id
FROM (
    SELECT id
    FROM project_registry.projects
    ORDER BY created_at
    LIMIT 1
) singleton
WHERE run.project_id IS NULL
  AND run.skill_name = 'mcum-orchestrator'
  AND (SELECT COUNT(*) FROM project_registry.projects) = 1;

UPDATE project_registry.projects p
SET total_sessions = stats.total_sessions,
    total_tasks_completed = stats.total_tasks_completed,
    total_improvements = stats.total_improvements,
    avg_confidence_score = stats.avg_confidence_score,
    last_activity_at = stats.last_activity_at,
    updated_at = NOW()
FROM (
    SELECT
        project_id,
        COUNT(*) FILTER (WHERE log_type = 'session_start') AS total_sessions,
        COUNT(*) FILTER (WHERE log_type = 'task' AND outcome = 'success') AS total_tasks_completed,
        COUNT(*) FILTER (WHERE log_type = 'improvement') AS total_improvements,
        AVG(confidence_score) FILTER (
            WHERE log_type = 'task'
              AND confidence_score IS NOT NULL
        ) AS avg_confidence_score,
        MAX(created_at) AS last_activity_at
    FROM project_registry.project_logs
    GROUP BY project_id
) stats
WHERE p.id = stats.project_id;

CREATE INDEX IF NOT EXISTS idx_exp_category_conf
    ON core_brain.experiences (category, current_confidence DESC);

CREATE INDEX IF NOT EXISTS idx_exp_project
    ON core_brain.experiences (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_exp_skill
    ON core_brain.experiences (skill_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_exp_confidence
    ON core_brain.experiences (current_confidence)
    WHERE current_confidence > 0.30;

-- -----------------------------------------
-- PGVECTOR MIGRATION: JSONB -> vector(384)
-- -----------------------------------------
-- Idempotent: only runs if pgvector is installed and column is still JSONB.
DO $$
DECLARE
    col_type TEXT;
    has_pgvector BOOLEAN;
BEGIN
    SELECT EXISTS (
        SELECT 1 FROM pg_extension WHERE extname = 'vector'
    ) INTO has_pgvector;

    IF NOT has_pgvector THEN
        RAISE NOTICE 'pgvector not installed — keeping JSONB embedding column';
        RETURN;
    END IF;

    SELECT data_type INTO col_type
    FROM information_schema.columns
    WHERE table_schema = 'core_brain'
      AND table_name = 'experiences'
      AND column_name = 'embedding';

    IF col_type = 'jsonb' THEN
        -- Drop dependent view before column swap
        DROP VIEW IF EXISTS core_brain.v_active_experiences;

        -- Nullify malformed embeddings (wrong dimension) so backfill recalculates them
        UPDATE core_brain.experiences
        SET embedding = NULL
        WHERE embedding IS NOT NULL
          AND jsonb_array_length(embedding) != 384;

        -- Add temporary vector column
        ALTER TABLE core_brain.experiences ADD COLUMN embedding_vec vector(384);

        -- Migrate existing JSONB embeddings to vector format
        UPDATE core_brain.experiences
        SET embedding_vec = embedding::text::vector(384)
        WHERE embedding IS NOT NULL;

        -- Swap columns
        ALTER TABLE core_brain.experiences DROP COLUMN embedding;
        ALTER TABLE core_brain.experiences RENAME COLUMN embedding_vec TO embedding;

        RAISE NOTICE 'Migrated embedding column from JSONB to vector(384)';
    ELSIF col_type = 'USER-DEFINED' THEN
        RAISE NOTICE 'embedding column is already vector type — no migration needed';
    END IF;
END $$;

-- Embedding index: HNSW if pgvector, boolean partial otherwise
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector') THEN
        DROP INDEX IF EXISTS core_brain.idx_exp_embedding_present;
        CREATE INDEX IF NOT EXISTS idx_exp_embedding_hnsw
            ON core_brain.experiences
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64);
        RAISE NOTICE 'Created HNSW index on embedding column';
    ELSE
        CREATE INDEX IF NOT EXISTS idx_exp_embedding_present
            ON core_brain.experiences ((embedding IS NOT NULL));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_test_skill_partition
    ON core_brain.test_suite (skill_name, partition, is_active);

CREATE INDEX IF NOT EXISTS idx_proj_path
    ON project_registry.projects (project_path);

CREATE INDEX IF NOT EXISTS idx_proj_status
    ON project_registry.projects (status, last_activity_at DESC);

CREATE INDEX IF NOT EXISTS idx_logs_project_type
    ON project_registry.project_logs (project_id, log_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_logs_recent
    ON project_registry.project_logs (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_invocations_project_created
    ON project_registry.agent_invocations (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_invocations_task_log
    ON project_registry.agent_invocations (task_log_id);

CREATE INDEX IF NOT EXISTS idx_code_graph_graphs_project
    ON code_graph.graphs (project_id, status);

CREATE INDEX IF NOT EXISTS idx_code_graph_files_graph_path
    ON code_graph.files (graph_id, relative_path);

CREATE INDEX IF NOT EXISTS idx_code_graph_files_hash
    ON code_graph.files (graph_id, file_hash);

CREATE INDEX IF NOT EXISTS idx_code_graph_nodes_graph_kind
    ON code_graph.nodes (graph_id, node_kind, qualified_name);

CREATE INDEX IF NOT EXISTS idx_code_graph_nodes_search
    ON code_graph.nodes USING GIN (to_tsvector('simple', search_text));

CREATE INDEX IF NOT EXISTS idx_code_graph_edges_graph_kind
    ON code_graph.edges (graph_id, edge_kind);

CREATE INDEX IF NOT EXISTS idx_code_graph_edges_target_ref
    ON code_graph.edges (graph_id, target_ref);

CREATE INDEX IF NOT EXISTS idx_code_graph_experience_links_project
    ON code_graph.experience_links (project_id, relative_path);

CREATE INDEX IF NOT EXISTS idx_code_graph_experience_links_node
    ON code_graph.experience_links (node_id);

CREATE INDEX IF NOT EXISTS idx_mcum_graph_entities_project_type
    ON mcum_graph.entities (project_id, entity_type, health_state);

CREATE UNIQUE INDEX IF NOT EXISTS idx_mcum_graph_entities_active_canonical
    ON mcum_graph.entities (project_id, canonical_key)
    WHERE valid_to IS NULL;

CREATE INDEX IF NOT EXISTS idx_mcum_graph_entities_search
    ON mcum_graph.entities USING GIN (to_tsvector('simple', title || ' ' || COALESCE(summary, '')));

CREATE INDEX IF NOT EXISTS idx_mcum_graph_relations_source
    ON mcum_graph.relations (project_id, source_entity_id, relation_type);

CREATE INDEX IF NOT EXISTS idx_mcum_graph_relations_target
    ON mcum_graph.relations (project_id, target_entity_id, relation_type);

CREATE INDEX IF NOT EXISTS idx_mcum_graph_snapshots_project
    ON mcum_graph.snapshots (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_design_system_profiles_project
    ON project_registry.design_system_profiles (project_id);

CREATE INDEX IF NOT EXISTS idx_design_system_versions_profile_status
    ON project_registry.design_system_versions (profile_id, status, version_number DESC);

CREATE INDEX IF NOT EXISTS idx_spec_contracts_project_status
    ON project_registry.spec_contracts (project_id, status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_spec_contracts_task_mode
    ON project_registry.spec_contracts (task_type, execution_mode, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_spec_trace_links_contract
    ON project_registry.spec_trace_links (spec_contract_id, link_kind);

CREATE INDEX IF NOT EXISTS idx_playbooks_project_skill
    ON core_brain.session_playbooks (project_id, skill_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_playbooks_reuse
    ON core_brain.session_playbooks (reuse_count DESC, last_reused_at DESC);

CREATE INDEX IF NOT EXISTS idx_playbooks_pattern_ids_gin
    ON core_brain.session_playbooks USING gin (pattern_ids);

CREATE INDEX IF NOT EXISTS idx_skill_catalog_status
    ON project_registry.skill_catalog (status, last_used_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_project_kpis_project_date_unique
    ON project_registry.project_kpis (project_id, snapshot_date);

CREATE INDEX IF NOT EXISTS idx_maintenance_runs_project
    ON project_registry.maintenance_runs (project_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_maintenance_runs_name
    ON project_registry.maintenance_runs (maintenance_name, started_at DESC);

-- ─────────────────────────────────────────
-- TRIGGERS
-- ─────────────────────────────────────────
CREATE OR REPLACE FUNCTION project_registry.update_project_on_log()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE project_registry.projects
    SET updated_at = NOW(),
        last_activity_at = NOW()
    WHERE id = NEW.project_id;

    IF NEW.log_type = 'session_start' THEN
        UPDATE project_registry.projects
        SET total_sessions = total_sessions + 1
        WHERE id = NEW.project_id;
    END IF;

    IF NEW.log_type = 'task' AND NEW.outcome = 'success' THEN
        UPDATE project_registry.projects
        SET total_tasks_completed = total_tasks_completed + 1
        WHERE id = NEW.project_id;
    END IF;

    IF NEW.log_type = 'improvement' THEN
        UPDATE project_registry.projects
        SET total_improvements = total_improvements + 1
        WHERE id = NEW.project_id;
    END IF;

    IF NEW.log_type = 'task' AND NEW.confidence_score IS NOT NULL THEN
        UPDATE project_registry.projects
        SET avg_confidence_score = (
            SELECT AVG(confidence_score)
            FROM project_registry.project_logs
            WHERE project_id = NEW.project_id
              AND log_type = 'task'
              AND confidence_score IS NOT NULL
        )
        WHERE id = NEW.project_id;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_update_project_on_log ON project_registry.project_logs;
CREATE TRIGGER trg_update_project_on_log
    AFTER INSERT ON project_registry.project_logs
    FOR EACH ROW EXECUTE FUNCTION project_registry.update_project_on_log();

CREATE OR REPLACE FUNCTION core_brain.refresh_pattern_metrics()
RETURNS TRIGGER AS $$
DECLARE
    target_pattern_id UUID;
BEGIN
    IF TG_OP = 'DELETE' THEN
        target_pattern_id := OLD.pattern_id;
    ELSE
        target_pattern_id := NEW.pattern_id;
    END IF;

    UPDATE core_brain.patterns p
    SET
        experience_count = (
            SELECT COUNT(*) FROM core_brain.pattern_evidence pe
            WHERE pe.pattern_id = target_pattern_id
              AND pe.evidence_role = 'support'
        ),
        support_count = (
            SELECT COUNT(*) FROM core_brain.pattern_evidence pe
            WHERE pe.pattern_id = target_pattern_id
              AND pe.evidence_role = 'support'
        ),
        contradiction_count = (
            SELECT COUNT(*) FROM core_brain.pattern_evidence pe
            WHERE pe.pattern_id = target_pattern_id
              AND pe.evidence_role = 'contradict'
              AND COALESCE(pe.resolution, '') = ''
        ),
        avg_score = (
            SELECT AVG(e.current_confidence)
            FROM core_brain.pattern_evidence pe
            JOIN core_brain.experiences e ON e.id = pe.experience_id
            WHERE pe.pattern_id = target_pattern_id
              AND pe.evidence_role = 'support'
        ),
        context_diversity = (
            SELECT COUNT(DISTINCT COALESCE(e.project_id::text, '') || ':' || COALESCE(e.task_description, ''))
            FROM core_brain.pattern_evidence pe
            JOIN core_brain.experiences e ON e.id = pe.experience_id
            WHERE pe.pattern_id = target_pattern_id
              AND pe.evidence_role = 'support'
        ),
        last_evidence_at = (
            SELECT MAX(pe.added_at)
            FROM core_brain.pattern_evidence pe
            WHERE pe.pattern_id = target_pattern_id
        ),
        updated_at = NOW()
    WHERE p.id = target_pattern_id;

    -- Promotion is deliberately not performed in SQL. Pattern Intelligence V2
    -- requires explicit policy evaluation and human acceptance.
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_pattern_promotion ON core_brain.pattern_evidence;
DROP TRIGGER IF EXISTS trg_pattern_metrics ON core_brain.pattern_evidence;
CREATE TRIGGER trg_pattern_metrics
    AFTER INSERT OR UPDATE OR DELETE ON core_brain.pattern_evidence
    FOR EACH ROW EXECUTE FUNCTION core_brain.refresh_pattern_metrics();

DROP FUNCTION IF EXISTS core_brain.check_pattern_promotion();

-- ─────────────────────────────────────────
-- VISTAS
-- ─────────────────────────────────────────
DROP VIEW IF EXISTS core_brain.v_active_experiences;
CREATE VIEW core_brain.v_active_experiences AS
SELECT
    id, category, title, content, applicability, not_applicable_cases,
    current_confidence, revalidation_count, unique_context_count,
    tested_by, skill_name, skill_version, project_id, task_description,
    conflict_refs, embedding, created_at, last_validated_at
FROM core_brain.experiences
WHERE current_confidence > 0.30
  AND superseded_by IS NULL
ORDER BY current_confidence DESC;

CREATE OR REPLACE VIEW core_brain.v_pattern_health AS
SELECT
    p.id, p.pattern_key, p.name, p.category, p.status, p.health_state,
    p.scope_type, p.scope_project_id, p.scope_skill_name,
    p.experience_count, p.support_count, p.contradiction_count,
    p.context_diversity, p.avg_score, p.cohesion_score, p.utility_score,
    p.usage_count, p.success_count, p.failure_count,
    p.last_evidence_at, p.last_used_at, p.updated_at
FROM core_brain.patterns p;

CREATE OR REPLACE VIEW core_brain.v_pattern_candidate_health AS
SELECT
    c.id, c.candidate_key, c.category, c.skill_name,
    c.scope_type, c.scope_project_id, c.label, c.status,
    c.quality_ready, c.quality_score, c.support_count,
    c.distinct_project_count, c.context_diversity, c.cohesion_score,
    c.contradiction_count, c.avg_confidence, c.embedding_model,
    c.algorithm_version, c.last_seen_at, c.reviewed_at, c.reviewed_by,
    c.materialized_pattern_id
FROM core_brain.pattern_candidates c;

CREATE OR REPLACE VIEW core_brain.v_pattern_activation_backlog AS
SELECT
    c.id, c.candidate_key, c.label, c.skill_name, c.scope_type,
    c.scope_project_id, c.quality_score, c.support_count,
    c.context_diversity, c.distinct_project_count, c.cohesion_score,
    c.contradiction_count, c.avg_confidence, c.last_seen_at,
    EXTRACT(DAY FROM (NOW() - c.last_seen_at))::int AS age_days
FROM core_brain.pattern_candidates c
WHERE c.status = 'review'
  AND c.quality_ready = TRUE
  AND c.materialized_pattern_id IS NULL;

CREATE OR REPLACE VIEW project_registry.v_project_summary AS
SELECT
    p.id, p.project_name, p.project_path, p.status, p.phase,
    p.tech_stack, p.total_sessions, p.total_tasks_completed,
    p.total_improvements, p.last_activity_at, p.client_or_context,
    COUNT(DISTINCT pl.id) AS total_log_entries,
    MAX(pl.created_at)    AS last_log_entry
FROM project_registry.projects p
LEFT JOIN project_registry.project_logs pl ON pl.project_id = p.id
GROUP BY p.id
ORDER BY p.last_activity_at DESC;

CREATE OR REPLACE VIEW project_registry.v_recent_logs AS
SELECT
    pl.id, p.project_name, p.project_path,
    pl.log_type, pl.title, pl.skill_used, pl.outcome,
    pl.confidence_score, pl.created_at
FROM project_registry.project_logs pl
JOIN project_registry.projects p ON p.id = pl.project_id
ORDER BY pl.created_at DESC;

DROP MATERIALIZED VIEW IF EXISTS project_registry.mv_daily_metrics;
CREATE MATERIALIZED VIEW project_registry.mv_daily_metrics AS
SELECT
    pl.created_at::date AS day,
    p.id AS project_id,
    p.project_name,
    COUNT(*) FILTER (WHERE pl.log_type = 'task') AS tasks,
    COUNT(*) FILTER (WHERE pl.log_type = 'task' AND pl.outcome = 'success') AS successes,
    COUNT(*) FILTER (WHERE pl.log_type = 'task' AND pl.outcome = 'failure') AS failures,
    AVG(pl.confidence_score) FILTER (WHERE pl.log_type = 'task' AND pl.confidence_score IS NOT NULL) AS avg_confidence,
    SUM(pl.context_tokens_in) FILTER (WHERE pl.context_tokens_in IS NOT NULL) AS tokens_in,
    SUM(pl.context_tokens_out) FILTER (WHERE pl.context_tokens_out IS NOT NULL) AS tokens_out,
    AVG(pl.task_wall_clock_ms) FILTER (WHERE pl.task_wall_clock_ms IS NOT NULL) AS avg_task_ms,
    AVG(pl.retrieval_latency_ms) FILTER (WHERE pl.retrieval_latency_ms IS NOT NULL) AS avg_retrieval_ms,
    COUNT(*) FILTER (WHERE pl.log_type = 'improvement') AS sisl_cycles
FROM project_registry.project_logs pl
JOIN project_registry.projects p ON p.id = pl.project_id
GROUP BY pl.created_at::date, p.id, p.project_name
WITH DATA;

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_daily_metrics_day_project
    ON project_registry.mv_daily_metrics (day, project_id);

CREATE OR REPLACE FUNCTION project_registry.generate_metrics_report(
    from_date DATE,
    to_date DATE
)
RETURNS TABLE (
    day DATE,
    project_id UUID,
    project_name TEXT,
    tasks BIGINT,
    successes BIGINT,
    failures BIGINT,
    avg_confidence DOUBLE PRECISION,
    tokens_in BIGINT,
    tokens_out BIGINT,
    avg_task_ms DOUBLE PRECISION,
    avg_retrieval_ms DOUBLE PRECISION,
    sisl_cycles BIGINT
) AS $$
    SELECT
        m.day,
        m.project_id,
        m.project_name,
        m.tasks,
        m.successes,
        m.failures,
        m.avg_confidence,
        m.tokens_in,
        m.tokens_out,
        m.avg_task_ms,
        m.avg_retrieval_ms,
        m.sisl_cycles
    FROM project_registry.mv_daily_metrics m
    WHERE m.day BETWEEN from_date AND to_date
    ORDER BY m.day DESC, m.project_name;
$$ LANGUAGE SQL STABLE;

-- ─────────────────────────────────────────
-- SCHEMA knowledge_library
-- Biblioteca gobernada aislada: permanece desactivada hasta que el orquestador la habilite.
-- ─────────────────────────────────────────
DO $$ BEGIN
    CREATE TYPE knowledge_library.source_kind AS ENUM (
        'pdf',
        'markdown',
        'repository',
        'article',
        'book',
        'note',
        'webpage',
        'transcript'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE knowledge_library.document_status AS ENUM (
        'draft',
        'queued',
        'ingesting',
        'indexed',
        'needs_review',
        'deprecated',
        'disabled'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE knowledge_library.ingestion_status AS ENUM (
        'queued',
        'extracting',
        'chunking',
        'summarizing',
        'indexing',
        'completed',
        'failed',
        'disabled'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE knowledge_library.extraction_mode AS ENUM (
        'native',
        'ocr',
        'hybrid',
        'manual'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE knowledge_library.summary_level AS ENUM (
        'document',
        'section',
        'chunk'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE knowledge_library.summary_style AS ENUM (
        'extractive',
        'abstractive',
        'hybrid'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE knowledge_library.citation_kind AS ENUM (
        'page',
        'section',
        'paragraph',
        'figure',
        'table',
        'quote'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE knowledge_library.concept_type AS ENUM (
        'methodology',
        'principle',
        'pattern',
        'tool',
        'role',
        'artifact',
        'domain',
        'metric',
        'topic'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE knowledge_library.authority_tier AS ENUM (
        'canonical',
        'primary',
        'secondary',
        'community',
        'internal'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE knowledge_library.access_mode AS ENUM (
        'local_file',
        'git_repo',
        'web_link',
        'manual_import'
    );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS knowledge_library.library_settings (
    setting_key             TEXT PRIMARY KEY DEFAULT 'global',
    enabled                 BOOLEAN NOT NULL DEFAULT FALSE,
    default_retrieval_mode  TEXT NOT NULL DEFAULT 'summary_first'
        CHECK (default_retrieval_mode IN ('summary_first','chunk_first','document_first')),
    allow_full_text         BOOLEAN NOT NULL DEFAULT FALSE,
    allow_ocr_fallback      BOOLEAN NOT NULL DEFAULT TRUE,
    max_summary_depth       INT NOT NULL DEFAULT 3 CHECK (max_summary_depth BETWEEN 1 AND 5),
    max_chunks_per_query    INT NOT NULL DEFAULT 8 CHECK (max_chunks_per_query BETWEEN 1 AND 50),
    notes                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO knowledge_library.library_settings (
    setting_key, enabled, default_retrieval_mode, allow_full_text,
    allow_ocr_fallback, max_summary_depth, max_chunks_per_query, notes
)
SELECT
    'global',
    FALSE,
    'summary_first',
    FALSE,
    TRUE,
    3,
    8,
    'Dormant library defaults for governed knowledge retrieval.'
WHERE NOT EXISTS (
    SELECT 1
    FROM knowledge_library.library_settings
    WHERE setting_key = 'global'
);

CREATE TABLE IF NOT EXISTS knowledge_library.documents (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_slug           TEXT UNIQUE NOT NULL,
    title                   TEXT NOT NULL,
    subtitle                TEXT,
    source_kind             knowledge_library.source_kind NOT NULL,
    access_mode             knowledge_library.access_mode NOT NULL DEFAULT 'manual_import',
    source_uri              TEXT,
    source_path             TEXT,
    source_repository       TEXT,
    source_branch           TEXT,
    source_commit           TEXT,
    author                  TEXT,
    publisher               TEXT,
    edition                 TEXT,
    version_label           TEXT,
    language_code           TEXT DEFAULT 'en',
    authority_tier          knowledge_library.authority_tier NOT NULL DEFAULT 'internal',
    status                  knowledge_library.document_status NOT NULL DEFAULT 'draft',
    description             TEXT,
    license_name            TEXT,
    license_url             TEXT,
    publication_year        INT CHECK (publication_year BETWEEN 1500 AND 2100),
    canonical_citation      TEXT,
    notes                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS knowledge_library.document_versions (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id             UUID NOT NULL REFERENCES knowledge_library.documents(id) ON DELETE CASCADE,
    version_label           TEXT NOT NULL,
    source_hash             TEXT,
    checksum                TEXT,
    extraction_mode         knowledge_library.extraction_mode NOT NULL DEFAULT 'manual',
    ingestion_status        knowledge_library.ingestion_status NOT NULL DEFAULT 'queued',
    source_page_count       INT DEFAULT 0 CHECK (source_page_count >= 0),
    text_page_count         INT DEFAULT 0 CHECK (text_page_count >= 0),
    ocr_page_count          INT DEFAULT 0 CHECK (ocr_page_count >= 0),
    chunks_total            INT DEFAULT 0 CHECK (chunks_total >= 0),
    summaries_total         INT DEFAULT 0 CHECK (summaries_total >= 0),
    citations_total         INT DEFAULT 0 CHECK (citations_total >= 0),
    extracted_text_path     TEXT,
    normalized_text_path    TEXT,
    normalized_markdown_path TEXT,
    ocr_text_path           TEXT,
    artifacts_path          TEXT,
    extraction_confidence   FLOAT CHECK (extraction_confidence BETWEEN 0 AND 1),
    ocr_engine              TEXT,
    ocr_language            TEXT,
    token_count             BIGINT DEFAULT 0 CHECK (token_count >= 0),
    error_message           TEXT,
    started_at              TIMESTAMPTZ,
    finished_at             TIMESTAMPTZ,
    ingested_at             TIMESTAMPTZ,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (document_id, version_label)
);


CREATE TABLE IF NOT EXISTS knowledge_library.sections (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_version_id     UUID NOT NULL REFERENCES knowledge_library.document_versions(id) ON DELETE CASCADE,
    parent_section_id       UUID REFERENCES knowledge_library.sections(id) ON DELETE CASCADE,
    section_order           INT NOT NULL DEFAULT 1,
    section_level           INT NOT NULL DEFAULT 1 CHECK (section_level >= 1),
    section_type            TEXT NOT NULL DEFAULT 'heading',
    heading                 TEXT NOT NULL,
    section_slug            TEXT NOT NULL,
    section_path            TEXT NOT NULL,
    page_start              INT,
    page_end                INT,
    char_start              BIGINT,
    char_end                BIGINT,
    token_start             BIGINT,
    token_end               BIGINT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (document_version_id, section_path)
);

CREATE TABLE IF NOT EXISTS knowledge_library.chunks (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_version_id     UUID NOT NULL REFERENCES knowledge_library.document_versions(id) ON DELETE CASCADE,
    section_id              UUID REFERENCES knowledge_library.sections(id) ON DELETE CASCADE,
    chunk_order             INT NOT NULL DEFAULT 1,
    chunk_type              TEXT NOT NULL DEFAULT 'text',
    page_start              INT,
    page_end                INT,
    char_start              BIGINT,
    char_end                BIGINT,
    token_start             BIGINT,
    token_end               BIGINT,
    content                 TEXT NOT NULL,
    content_hash            TEXT,
    summary_excerpt         TEXT,
    embedding               JSONB,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (section_id, chunk_order)
);

CREATE TABLE IF NOT EXISTS knowledge_library.summaries (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_version_id     UUID NOT NULL REFERENCES knowledge_library.document_versions(id) ON DELETE CASCADE,
    section_id              UUID REFERENCES knowledge_library.sections(id) ON DELETE CASCADE,
    chunk_id                UUID REFERENCES knowledge_library.chunks(id) ON DELETE CASCADE,
    summary_level           knowledge_library.summary_level NOT NULL,
    summary_style           knowledge_library.summary_style NOT NULL DEFAULT 'hybrid',
    summary_title           TEXT,
    summary_text            TEXT NOT NULL,
    summary_json            JSONB,
    source_section_ids      UUID[],
    source_chunk_ids        UUID[],
    token_count             INT DEFAULT 0 CHECK (token_count >= 0),
    model_name              TEXT,
    embedding               JSONB,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW(),
    CHECK (
        (
            summary_level = 'document'
            AND section_id IS NULL
            AND chunk_id IS NULL
        )
        OR (
            summary_level = 'section'
            AND section_id IS NOT NULL
            AND chunk_id IS NULL
        )
        OR (
            summary_level = 'chunk'
            AND chunk_id IS NOT NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS knowledge_library.citations (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_version_id     UUID NOT NULL REFERENCES knowledge_library.document_versions(id) ON DELETE CASCADE,
    section_id              UUID REFERENCES knowledge_library.sections(id) ON DELETE CASCADE,
    chunk_id                UUID REFERENCES knowledge_library.chunks(id) ON DELETE CASCADE,
    citation_kind           knowledge_library.citation_kind NOT NULL DEFAULT 'section',
    page_number             INT,
    line_start              INT,
    line_end                INT,
    character_start         BIGINT,
    character_end           BIGINT,
    locator                 JSONB,
    quote_text              TEXT,
    quote_hash              TEXT,
    source_reference        TEXT,
    citation_note           TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS knowledge_library.methodologies (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    methodology_slug        TEXT UNIQUE NOT NULL,
    name                    TEXT NOT NULL,
    description             TEXT,
    authority_tier          knowledge_library.authority_tier NOT NULL DEFAULT 'primary',
    origin_document_id      UUID REFERENCES knowledge_library.documents(id) ON DELETE SET NULL,
    origin_section_id       UUID REFERENCES knowledge_library.sections(id) ON DELETE SET NULL,
    notes                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS knowledge_library.document_methodologies (
    document_id             UUID NOT NULL REFERENCES knowledge_library.documents(id) ON DELETE CASCADE,
    methodology_id          UUID NOT NULL REFERENCES knowledge_library.methodologies(id) ON DELETE CASCADE,
    relevance_score         FLOAT DEFAULT 1.0 CHECK (relevance_score BETWEEN 0 AND 1),
    notes                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (document_id, methodology_id)
);

CREATE TABLE IF NOT EXISTS knowledge_library.concepts (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    concept_slug            TEXT UNIQUE NOT NULL,
    concept_name            TEXT NOT NULL,
    concept_type            knowledge_library.concept_type NOT NULL,
    description             TEXT,
    parent_concept_id       UUID REFERENCES knowledge_library.concepts(id) ON DELETE SET NULL,
    aliases                 JSONB,
    authority_tier          knowledge_library.authority_tier NOT NULL DEFAULT 'internal',
    notes                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS knowledge_library.document_concepts (
    document_id             UUID NOT NULL REFERENCES knowledge_library.documents(id) ON DELETE CASCADE,
    concept_id              UUID NOT NULL REFERENCES knowledge_library.concepts(id) ON DELETE CASCADE,
    relevance_score         FLOAT DEFAULT 1.0 CHECK (relevance_score BETWEEN 0 AND 1),
    notes                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (document_id, concept_id)
);

CREATE TABLE IF NOT EXISTS knowledge_library.section_concepts (
    section_id              UUID NOT NULL REFERENCES knowledge_library.sections(id) ON DELETE CASCADE,
    concept_id              UUID NOT NULL REFERENCES knowledge_library.concepts(id) ON DELETE CASCADE,
    relevance_score         FLOAT DEFAULT 1.0 CHECK (relevance_score BETWEEN 0 AND 1),
    notes                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (section_id, concept_id)
);

CREATE TABLE IF NOT EXISTS knowledge_library.chunk_concepts (
    chunk_id                UUID NOT NULL REFERENCES knowledge_library.chunks(id) ON DELETE CASCADE,
    concept_id              UUID NOT NULL REFERENCES knowledge_library.concepts(id) ON DELETE CASCADE,
    relevance_score         FLOAT DEFAULT 1.0 CHECK (relevance_score BETWEEN 0 AND 1),
    notes                   TEXT,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (chunk_id, concept_id)
);

DO $$
BEGIN
    IF to_regclass('knowledge_library.concept_embeddings') IS NULL THEN
        EXECUTE '
            CREATE TABLE knowledge_library.concept_embeddings (
                concept_id UUID PRIMARY KEY REFERENCES knowledge_library.concepts(id) ON DELETE CASCADE,
                model_name TEXT NOT NULL,
                text_repr TEXT NOT NULL,
                embedding JSONB NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        ';
    END IF;
END $$;


-- ─────────────────────────────────────────
-- SEMILLA: version del orquestador
-- ─────────────────────────────────────────
DELETE FROM core_brain.skill_versions a
USING core_brain.skill_versions b
WHERE a.id < b.id
  AND a.skill_name = b.skill_name
  AND a.version_semver = b.version_semver
  AND COALESCE(a.skill_content_hash, '') = COALESCE(b.skill_content_hash, '');

INSERT INTO core_brain.skill_versions (
    skill_name, version_semver, skill_content_hash,
    changes_description, improvement_source, status
)
SELECT
    'mcum-orchestrator',
'3.16.0',
'schema-baseline-3.16.0',
    'Pattern Intelligence V2: governed shadow discovery, evidence quality gates, semantic embedding cache, runtime utility and manual draft materialization.',
    'user',
    'active'
WHERE NOT EXISTS (
    SELECT 1
    FROM core_brain.skill_versions
    WHERE skill_name = 'mcum-orchestrator'
AND version_semver = '3.16.0'
AND COALESCE(skill_content_hash, '') = 'schema-baseline-3.16.0'
);

CREATE TABLE IF NOT EXISTS knowledge_library.ingestion_jobs (
    id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    document_id             UUID REFERENCES knowledge_library.documents(id) ON DELETE CASCADE,
    document_version_id     UUID REFERENCES knowledge_library.document_versions(id) ON DELETE SET NULL,
    source_path             TEXT NOT NULL,
    requested_mode          knowledge_library.extraction_mode NOT NULL DEFAULT 'native',
    status                  knowledge_library.ingestion_status NOT NULL DEFAULT 'queued',
    stage                   TEXT NOT NULL DEFAULT 'queued',
    native_text_ratio       FLOAT CHECK (native_text_ratio BETWEEN 0 AND 1),
    pages_total             INT DEFAULT 0 CHECK (pages_total >= 0),
    pages_with_text         INT DEFAULT 0 CHECK (pages_with_text >= 0),
    ocr_requested           BOOLEAN NOT NULL DEFAULT FALSE,
    ocr_executed            BOOLEAN NOT NULL DEFAULT FALSE,
    artifacts_path          TEXT,
    notes                   JSONB,
    error_message           TEXT,
    started_at              TIMESTAMPTZ,
    finished_at             TIMESTAMPTZ,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kl_documents_status
    ON knowledge_library.documents (status, authority_tier);

CREATE INDEX IF NOT EXISTS idx_kl_documents_source_path
    ON knowledge_library.documents (source_path);

CREATE INDEX IF NOT EXISTS idx_kl_document_versions_status
    ON knowledge_library.document_versions (document_id, ingestion_status);

CREATE INDEX IF NOT EXISTS idx_kl_sections_version_order
    ON knowledge_library.sections (document_version_id, section_order);

CREATE INDEX IF NOT EXISTS idx_kl_chunks_version_section
    ON knowledge_library.chunks (document_version_id, section_id, chunk_order);

CREATE INDEX IF NOT EXISTS idx_kl_chunks_fts
    ON knowledge_library.chunks
    USING GIN (to_tsvector('simple', COALESCE(content, '')));

CREATE INDEX IF NOT EXISTS idx_kl_summaries_level
    ON knowledge_library.summaries (document_version_id, summary_level);

CREATE INDEX IF NOT EXISTS idx_kl_summaries_fts
    ON knowledge_library.summaries
    USING GIN (to_tsvector('simple', COALESCE(summary_text, '')));

CREATE INDEX IF NOT EXISTS idx_kl_citations_locator
    ON knowledge_library.citations (document_version_id, page_number, citation_kind);

CREATE INDEX IF NOT EXISTS idx_kl_ingestion_jobs_status
    ON knowledge_library.ingestion_jobs (status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_kl_concept_embeddings_model
    ON knowledge_library.concept_embeddings (model_name, updated_at DESC);

CREATE OR REPLACE VIEW knowledge_library.chunk_summaries AS
SELECT
    id,
    document_version_id,
    section_id,
    chunk_id,
    summary_level,
    summary_style,
    summary_title,
    summary_text,
    summary_json,
    source_section_ids,
    source_chunk_ids,
    token_count,
    model_name,
    embedding,
    created_at,
    updated_at
FROM knowledge_library.summaries
WHERE summary_level = 'chunk';

CREATE OR REPLACE VIEW knowledge_library.concept_index AS
SELECT
    c.id,
    c.concept_slug,
    c.concept_name,
    c.concept_type,
    c.description,
    c.authority_tier,
    c.aliases,
    c.notes,
    c.created_at,
    c.updated_at
FROM knowledge_library.concepts c;

CREATE OR REPLACE VIEW knowledge_library.methodology_tags AS
SELECT
    m.id,
    m.methodology_slug,
    m.name,
    m.description,
    m.authority_tier,
    m.origin_document_id,
    m.origin_section_id,
    m.notes,
    m.created_at,
    m.updated_at
FROM knowledge_library.methodologies m;

CREATE OR REPLACE VIEW code_graph.v_files AS
SELECT
    f.id,
    f.graph_id,
    f.project_id,
    g.project_name,
    f.relative_path,
    f.absolute_path,
    f.language,
    f.file_hash,
    f.bytes_size,
    f.line_count,
    f.token_estimate,
    f.status,
    f.indexed_at,
    f.metadata
FROM code_graph.files f
JOIN code_graph.graphs g ON g.id = f.graph_id
WHERE f.status = 'active';

CREATE OR REPLACE VIEW code_graph.v_nodes AS
SELECT
    n.id,
    n.graph_id,
    n.project_id,
    g.project_name,
    f.relative_path,
    f.language,
    n.node_kind,
    n.name,
    n.qualified_name,
    n.signature,
    n.line_start,
    n.line_end,
    n.doc_excerpt,
    n.metadata,
    n.created_at,
    n.updated_at
FROM code_graph.nodes n
JOIN code_graph.graphs g ON g.id = n.graph_id
LEFT JOIN code_graph.files f ON f.id = n.file_id;

CREATE OR REPLACE VIEW code_graph.v_edges AS
SELECT
    e.id,
    e.graph_id,
    e.project_id,
    g.project_name,
    e.edge_kind,
    e.source_ref,
    src.qualified_name AS source_qualified_name,
    e.target_ref,
    dst.qualified_name AS target_qualified_name,
    e.confidence,
    e.metadata,
    e.created_at
FROM code_graph.edges e
JOIN code_graph.graphs g ON g.id = e.graph_id
LEFT JOIN code_graph.nodes src ON src.id = e.source_node_id
LEFT JOIN code_graph.nodes dst ON dst.id = e.target_node_id;

CREATE OR REPLACE VIEW code_graph.v_experience_links AS
SELECT
    l.id,
    l.project_id,
    g.project_name,
    l.graph_id,
    l.experience_id,
    e.title AS experience_title,
    e.category,
    l.file_id,
    l.node_id,
    l.relative_path,
    l.qualified_name,
    l.link_kind,
    l.confidence,
    l.file_hash,
    l.graph_version,
    l.metadata,
    l.created_at,
    l.updated_at
FROM code_graph.experience_links l
JOIN code_graph.graphs g ON g.id = l.graph_id
JOIN core_brain.experiences e ON e.id = l.experience_id;

CREATE OR REPLACE VIEW mcum_graph.v_project_health AS
WITH entity_counts AS (
    SELECT project_id, COUNT(*) AS entity_count
    FROM mcum_graph.entities
    WHERE valid_to IS NULL
    GROUP BY project_id
),
relation_counts AS (
    SELECT project_id, COUNT(*) AS relation_count
    FROM mcum_graph.relations
    WHERE valid_to IS NULL
    GROUP BY project_id
),
latest_snapshots AS (
    SELECT project_id, MAX(created_at) AS latest_snapshot_at
    FROM mcum_graph.snapshots
    GROUP BY project_id
)
SELECT
    p.id AS project_id,
    p.project_name,
    p.project_path,
    COALESCE(e.entity_count, 0) AS entity_count,
    COALESCE(r.relation_count, 0) AS relation_count,
    s.latest_snapshot_at
FROM project_registry.projects p
LEFT JOIN entity_counts e ON e.project_id = p.id
LEFT JOIN relation_counts r ON r.project_id = p.id
LEFT JOIN latest_snapshots s ON s.project_id = p.id;

CREATE OR REPLACE FUNCTION code_graph.context_pack(
    p_project_id UUID,
    p_query TEXT,
    p_limit INT DEFAULT 12,
    p_depth INT DEFAULT 1
)
RETURNS TABLE (
    node_id UUID,
    relative_path TEXT,
    language TEXT,
    node_kind TEXT,
    qualified_name TEXT,
    signature TEXT,
    line_start INT,
    line_end INT,
    score FLOAT,
    inbound_edges INT,
    outbound_edges INT,
    context_summary TEXT
)
LANGUAGE sql
STABLE
AS $$
WITH active_graph AS (
    SELECT id
    FROM code_graph.graphs
    WHERE project_id = p_project_id
      AND status = 'active'
    ORDER BY updated_at DESC
    LIMIT 1
),
raw_query AS (
    SELECT LOWER(COALESCE(NULLIF(p_query, ''), 'code')) AS query_text
),
query_tokens AS (
    SELECT regexp_replace(token, '[^a-z0-9_]+', '', 'g') AS token
    FROM raw_query, regexp_split_to_table(query_text, '\s+') AS token
    WHERE length(regexp_replace(token, '[^a-z0-9_]+', '', 'g')) >= 2
    LIMIT 12
),
query_terms AS (
    SELECT
        (SELECT query_text FROM raw_query) AS query_text,
        COALESCE(
            to_tsquery('simple', NULLIF(string_agg(token || ':*', ' | '), '')),
            plainto_tsquery('simple', 'code')
        ) AS tsq,
        ARRAY(SELECT '%' || token || '%' FROM query_tokens) AS like_queries
    FROM query_tokens
),
candidate_nodes AS (
    SELECT
        n.id AS node_id,
        f.relative_path,
        f.language,
        n.node_kind,
        n.qualified_name,
        n.signature,
        n.line_start,
        n.line_end,
        n.search_text,
        ts_rank_cd(to_tsvector('simple', COALESCE(n.search_text, '')), q.tsq) AS lexical_score,
        (
            SELECT COUNT(*)::FLOAT
            FROM unnest(q.like_queries) AS token_match(pattern)
            WHERE LOWER(COALESCE(n.search_text, '')) LIKE token_match.pattern
        ) / GREATEST(1, cardinality(q.like_queries)) AS token_coverage,
        CASE
            WHEN LOWER(n.qualified_name) = q.query_text THEN 0.75
            WHEN LOWER(n.qualified_name) LIKE '%.' || q.query_text THEN 0.65
            WHEN LOWER(n.qualified_name) LIKE ANY(q.like_queries) THEN 0.35
            WHEN LOWER(COALESCE(f.relative_path, '')) LIKE ANY(q.like_queries) THEN 0.25
            ELSE 0
        END AS path_score
    FROM code_graph.nodes n
    JOIN active_graph ag ON ag.id = n.graph_id
    LEFT JOIN code_graph.files f ON f.id = n.file_id
    CROSS JOIN query_terms q
    WHERE to_tsvector('simple', COALESCE(n.search_text, '')) @@ q.tsq
       OR LOWER(n.qualified_name) LIKE ANY(q.like_queries)
       OR LOWER(COALESCE(f.relative_path, '')) LIKE ANY(q.like_queries)
),
inbound_counts AS (
    SELECT target_node_id AS node_id, COUNT(*)::INT AS inbound_edges
    FROM code_graph.edges
    WHERE target_node_id IS NOT NULL
    GROUP BY target_node_id
),
outbound_counts AS (
    SELECT source_node_id AS node_id, COUNT(*)::INT AS outbound_edges
    FROM code_graph.edges
    WHERE source_node_id IS NOT NULL
    GROUP BY source_node_id
)
SELECT
    cn.node_id,
    cn.relative_path,
    cn.language,
    cn.node_kind,
    cn.qualified_name,
    cn.signature,
    cn.line_start,
    cn.line_end,
    (
        LEAST(0.20, cn.lexical_score)
        + (cn.token_coverage * 0.60)
        + cn.path_score
        + LEAST(0.15, (COALESCE(ic.inbound_edges, 0) + COALESCE(oc.outbound_edges, 0)) * 0.01)
    )::FLOAT AS score,
    COALESCE(ic.inbound_edges, 0)::INT AS inbound_edges,
    COALESCE(oc.outbound_edges, 0)::INT AS outbound_edges,
    CONCAT(
        cn.node_kind, ' ', cn.qualified_name,
        CASE WHEN cn.relative_path IS NOT NULL THEN ' @ ' || cn.relative_path ELSE '' END,
        CASE WHEN cn.line_start IS NOT NULL THEN ':' || cn.line_start::TEXT ELSE '' END,
        CASE WHEN cn.signature IS NOT NULL AND cn.signature <> '' THEN ' | ' || cn.signature ELSE '' END
    ) AS context_summary
FROM candidate_nodes cn
LEFT JOIN inbound_counts ic ON ic.node_id = cn.node_id
LEFT JOIN outbound_counts oc ON oc.node_id = cn.node_id
ORDER BY score DESC, cn.relative_path ASC, cn.line_start ASC
LIMIT GREATEST(1, LEAST(COALESCE(p_limit, 12), 50));
$$;

CREATE OR REPLACE FUNCTION code_graph.context_pack_filtered(
    p_project_id UUID,
    p_query TEXT,
    p_limit INT DEFAULT 12,
    p_depth INT DEFAULT 1,
    p_languages TEXT[] DEFAULT NULL,
    p_exclude_languages TEXT[] DEFAULT NULL,
    p_path_prefix TEXT DEFAULT NULL,
    p_node_kinds TEXT[] DEFAULT NULL
)
RETURNS TABLE (
    node_id UUID,
    relative_path TEXT,
    language TEXT,
    node_kind TEXT,
    qualified_name TEXT,
    signature TEXT,
    line_start INT,
    line_end INT,
    score FLOAT,
    inbound_edges INT,
    outbound_edges INT,
    context_summary TEXT
)
LANGUAGE sql
STABLE
AS $$
SELECT cp.*
FROM code_graph.context_pack(p_project_id, p_query, 50, p_depth) cp
WHERE (
        p_languages IS NULL
        OR cardinality(p_languages) = 0
        OR LOWER(COALESCE(cp.language, '')) = ANY(p_languages)
    )
  AND (
        p_exclude_languages IS NULL
        OR cardinality(p_exclude_languages) = 0
        OR NOT (LOWER(COALESCE(cp.language, '')) = ANY(p_exclude_languages))
    )
  AND (
        NULLIF(TRIM(BOTH '/' FROM COALESCE(p_path_prefix, '')), '') IS NULL
        OR LOWER(COALESCE(cp.relative_path, '')) LIKE LOWER(TRIM(BOTH '/' FROM p_path_prefix)) || '%'
    )
  AND (
        p_node_kinds IS NULL
        OR cardinality(p_node_kinds) = 0
        OR LOWER(cp.node_kind) = ANY(p_node_kinds)
    )
ORDER BY cp.score DESC, cp.relative_path ASC, cp.line_start ASC
LIMIT GREATEST(1, LEAST(COALESCE(p_limit, 12), 50));
$$;

-- Graph intelligence analytics, impact, artifacts, comparisons and connector health.
-- These tables are additive projections; source code and binary artifacts remain on disk.
CREATE TABLE IF NOT EXISTS mcum_graph.analytics_runs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id          UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    snapshot_id         UUID REFERENCES mcum_graph.snapshots(id) ON DELETE SET NULL,
    analysis_type       TEXT NOT NULL,
    algorithm           TEXT NOT NULL,
    algorithm_version   TEXT NOT NULL DEFAULT '1.0',
    parameters          JSONB NOT NULL DEFAULT '{}'::jsonb,
    status              TEXT NOT NULL DEFAULT 'success',
    duration_ms         INT,
    source_hash         TEXT,
    metrics             JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mcum_graph.communities (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id          UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    snapshot_id         UUID REFERENCES mcum_graph.snapshots(id) ON DELETE SET NULL,
    run_id              UUID NOT NULL REFERENCES mcum_graph.analytics_runs(id) ON DELETE CASCADE,
    community_key       TEXT NOT NULL,
    label               TEXT NOT NULL,
    member_count        INT NOT NULL DEFAULT 0,
    modularity          FLOAT,
    conductance         FLOAT,
    cohesion            FLOAT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, community_key)
);

CREATE TABLE IF NOT EXISTS mcum_graph.entity_communities (
    community_id        UUID NOT NULL REFERENCES mcum_graph.communities(id) ON DELETE CASCADE,
    entity_id           UUID NOT NULL REFERENCES mcum_graph.entities(id) ON DELETE CASCADE,
    membership_strength FLOAT NOT NULL DEFAULT 1.0,
    is_representative   BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (community_id, entity_id)
);

CREATE TABLE IF NOT EXISTS mcum_graph.entity_metrics (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id          UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    snapshot_id         UUID REFERENCES mcum_graph.snapshots(id) ON DELETE SET NULL,
    entity_id           UUID NOT NULL REFERENCES mcum_graph.entities(id) ON DELETE CASCADE,
    degree_in           INT NOT NULL DEFAULT 0,
    degree_out          INT NOT NULL DEFAULT 0,
    pagerank            FLOAT NOT NULL DEFAULT 0,
    betweenness         FLOAT NOT NULL DEFAULT 0,
    k_core              INT NOT NULL DEFAULT 0,
    hub_score           FLOAT NOT NULL DEFAULT 0,
    god_node_score      FLOAT NOT NULL DEFAULT 0,
    surprise_score      FLOAT NOT NULL DEFAULT 0,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, snapshot_id, entity_id)
);

CREATE TABLE IF NOT EXISTS mcum_graph.surprising_connections (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id          UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    snapshot_id         UUID REFERENCES mcum_graph.snapshots(id) ON DELETE SET NULL,
    source_entity_id    UUID NOT NULL REFERENCES mcum_graph.entities(id) ON DELETE CASCADE,
    target_entity_id    UUID NOT NULL REFERENCES mcum_graph.entities(id) ON DELETE CASCADE,
    surprise_kind       TEXT NOT NULL,
    score               FLOAT NOT NULL DEFAULT 0,
    confidence          FLOAT NOT NULL DEFAULT 0,
    explanation         TEXT NOT NULL DEFAULT '',
    evidence            JSONB NOT NULL DEFAULT '{}'::jsonb,
    review_status       TEXT NOT NULL DEFAULT 'unreviewed',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, snapshot_id, source_entity_id, target_entity_id, surprise_kind)
);

CREATE TABLE IF NOT EXISTS mcum_graph.impact_runs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id          UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    snapshot_id         UUID REFERENCES mcum_graph.snapshots(id) ON DELETE SET NULL,
    changed_paths       JSONB NOT NULL DEFAULT '[]'::jsonb,
    changed_entities    JSONB NOT NULL DEFAULT '[]'::jsonb,
    status              TEXT NOT NULL DEFAULT 'success',
    max_depth           INT NOT NULL DEFAULT 3,
    metrics             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mcum_graph.impact_items (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    impact_run_id       UUID NOT NULL REFERENCES mcum_graph.impact_runs(id) ON DELETE CASCADE,
    entity_id           UUID REFERENCES mcum_graph.entities(id) ON DELETE SET NULL,
    impact_kind         TEXT NOT NULL,
    distance            INT NOT NULL DEFAULT 0,
    risk_score          FLOAT NOT NULL DEFAULT 0,
    reason              TEXT NOT NULL DEFAULT '',
    evidence            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS mcum_graph.test_selections (
    id                       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    impact_run_id            UUID NOT NULL REFERENCES mcum_graph.impact_runs(id) ON DELETE CASCADE,
    test_entity_id           UUID REFERENCES mcum_graph.entities(id) ON DELETE SET NULL,
    test_ref                 TEXT,
    selection_rank           INT NOT NULL DEFAULT 0,
    coverage_score           FLOAT NOT NULL DEFAULT 0,
    historical_failure_score FLOAT NOT NULL DEFAULT 0,
    required                 BOOLEAN NOT NULL DEFAULT FALSE,
    reason                   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS mcum_graph.comparisons (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    left_project_id     UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    right_project_id    UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    left_snapshot_id    UUID REFERENCES mcum_graph.snapshots(id) ON DELETE SET NULL,
    right_snapshot_id   UUID REFERENCES mcum_graph.snapshots(id) ON DELETE SET NULL,
    comparison_mode     TEXT NOT NULL DEFAULT 'explicit',
    status              TEXT NOT NULL DEFAULT 'success',
    metrics             JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS mcum_graph.comparison_items (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    comparison_id       UUID NOT NULL REFERENCES mcum_graph.comparisons(id) ON DELETE CASCADE,
    item_kind           TEXT NOT NULL,
    left_entity_id      UUID REFERENCES mcum_graph.entities(id) ON DELETE SET NULL,
    right_entity_id     UUID REFERENCES mcum_graph.entities(id) ON DELETE SET NULL,
    similarity          FLOAT NOT NULL DEFAULT 0,
    severity            TEXT NOT NULL DEFAULT 'info',
    summary             TEXT NOT NULL DEFAULT '',
    evidence            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS mcum_graph.source_artifacts (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    project_id          UUID NOT NULL REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    relative_path       TEXT NOT NULL,
    artifact_kind       TEXT NOT NULL,
    mime_type           TEXT,
    content_hash        TEXT NOT NULL,
    bytes_size          BIGINT NOT NULL DEFAULT 0,
    extractor_version   TEXT NOT NULL DEFAULT '1.0',
    status              TEXT NOT NULL DEFAULT 'active',
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    indexed_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, relative_path)
);

CREATE TABLE IF NOT EXISTS mcum_graph.artifact_sections (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    artifact_id         UUID NOT NULL REFERENCES mcum_graph.source_artifacts(id) ON DELETE CASCADE,
    section_kind        TEXT NOT NULL,
    locator             JSONB NOT NULL DEFAULT '{}'::jsonb,
    title               TEXT,
    summary             TEXT,
    text_excerpt        TEXT,
    confidence          FLOAT NOT NULL DEFAULT 1.0,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS project_registry.connector_registry (
    connector_key       TEXT PRIMARY KEY,
    connector_type      TEXT NOT NULL,
    display_name        TEXT NOT NULL,
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    health_mode         TEXT NOT NULL DEFAULT 'invocation',
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS project_registry.connector_health_events (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    connector_key       TEXT NOT NULL REFERENCES project_registry.connector_registry(connector_key) ON DELETE CASCADE,
    project_id          UUID REFERENCES project_registry.projects(id) ON DELETE CASCADE,
    status              TEXT NOT NULL,
    latency_ms          INT,
    message             TEXT,
    metadata            JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mcum_graph_analytics_runs_project
    ON mcum_graph.analytics_runs (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mcum_graph_entity_metrics_project
    ON mcum_graph.entity_metrics (project_id, snapshot_id, hub_score DESC);
CREATE INDEX IF NOT EXISTS idx_mcum_graph_impact_runs_project
    ON mcum_graph.impact_runs (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_mcum_graph_artifacts_project
    ON mcum_graph.source_artifacts (project_id, artifact_kind, indexed_at DESC);
CREATE INDEX IF NOT EXISTS idx_connector_health_events_recent
    ON project_registry.connector_health_events (connector_key, created_at DESC);

-- -----------------------------------------
-- PGVECTOR MIGRATION (phase 2): session playbooks
-- -----------------------------------------
-- Runs last so every target table already exists. Idempotent: migrates the
-- session_playbooks embedding column to vector(384) and builds an HNSW index so
-- playbook candidate selection is a DB-side nearest-neighbour search. Only runs
-- if pgvector is installed; otherwise the column stays JSONB and retrieval falls
-- back to Python cosine. Other embedding tables (pattern_embeddings,
-- pattern_experience_embeddings, concept_embeddings, knowledge_library chunks /
-- summaries) are intentionally left JSONB: the pattern caches are loaded whole
-- for clustering (no NN benefit) and the rest hold few or no embeddings. The
-- store code is dual-mode, so migrating them later activates the SQL path
-- automatically.
DO $$
DECLARE
    has_pgvector BOOLEAN;
    tgt RECORD;
    col_type TEXT;
BEGIN
    SELECT EXISTS(SELECT 1 FROM pg_extension WHERE extname = 'vector') INTO has_pgvector;
    IF NOT has_pgvector THEN
        RAISE NOTICE 'pgvector not installed — keeping JSONB embedding columns (phase 2)';
        RETURN;
    END IF;

    FOR tgt IN
        SELECT * FROM (VALUES
            ('core_brain', 'session_playbooks', true)
        ) AS t(sch, tbl, want_hnsw)
    LOOP
        IF to_regclass(tgt.sch || '.' || tgt.tbl) IS NULL THEN
            CONTINUE;
        END IF;

        SELECT data_type INTO col_type
        FROM information_schema.columns
        WHERE table_schema = tgt.sch AND table_name = tgt.tbl AND column_name = 'embedding';

        IF col_type IS NULL THEN
            -- Table has no embedding column on this install; nothing to migrate.
            CONTINUE;
        END IF;

        IF col_type = 'jsonb' THEN
            EXECUTE format(
                'UPDATE %I.%I SET embedding = NULL WHERE embedding IS NOT NULL AND jsonb_array_length(embedding) <> 384',
                tgt.sch, tgt.tbl);
            EXECUTE format('ALTER TABLE %I.%I ADD COLUMN embedding_vec vector(384)', tgt.sch, tgt.tbl);
            EXECUTE format(
                'UPDATE %I.%I SET embedding_vec = embedding::text::vector(384) WHERE embedding IS NOT NULL',
                tgt.sch, tgt.tbl);
            EXECUTE format('ALTER TABLE %I.%I DROP COLUMN embedding', tgt.sch, tgt.tbl);
            EXECUTE format('ALTER TABLE %I.%I RENAME COLUMN embedding_vec TO embedding', tgt.sch, tgt.tbl);
            RAISE NOTICE 'Migrated %.% embedding JSONB -> vector(384)', tgt.sch, tgt.tbl;
        END IF;

        IF tgt.want_hnsw THEN
            EXECUTE format(
                'CREATE INDEX IF NOT EXISTS %I ON %I.%I USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)',
                'idx_' || tgt.tbl || '_embedding_hnsw', tgt.sch, tgt.tbl);
        END IF;
    END LOOP;
END $$;

SELECT 'MCUM Schema v3.17 instalado OK' AS resultado;
SELECT 'core_brain: experiences, patterns, pattern_candidates, pattern_evidence, pattern_usage_events, skill_versions, test_suite, retrieval_runs' AS tablas_core;
SELECT 'project_registry: projects, project_logs, project_kpis, design_system_profiles, design_system_versions, spec_contracts, spec_assumptions, spec_scenarios, spec_acceptance_criteria, spec_trace_links' AS tablas_registry;
SELECT 'knowledge_library: documents, document_versions, sections, chunks, summaries, citations, methodologies, concepts, concept_embeddings, ingestion_jobs' AS tablas_library;
SELECT 'code_graph: graphs, files, nodes, edges, index_runs, experience_links, context_pack, context_pack_filtered' AS tablas_code_graph;
SELECT 'mcum_graph: entities, relations, snapshots, context_packs, analytics, impact, artifacts, comparisons, v_project_health' AS tablas_mcum_graph;
