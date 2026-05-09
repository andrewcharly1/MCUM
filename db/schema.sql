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
    reuse_count             INT DEFAULT 0,
    last_reused_at          TIMESTAMPTZ,
    embedding               JSONB,
    created_at              TIMESTAMPTZ DEFAULT NOW(),
    updated_at              TIMESTAMPTZ DEFAULT NOW()
);

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

CREATE INDEX IF NOT EXISTS idx_playbooks_project_skill
    ON core_brain.session_playbooks (project_id, skill_name, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_playbooks_reuse
    ON core_brain.session_playbooks (reuse_count DESC, last_reused_at DESC);

CREATE INDEX IF NOT EXISTS idx_skill_catalog_status
    ON project_registry.skill_catalog (status, last_used_at DESC);

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

CREATE OR REPLACE FUNCTION core_brain.check_pattern_promotion()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE core_brain.patterns p
    SET
        experience_count = (
            SELECT COUNT(*) FROM core_brain.pattern_evidence pe
            WHERE pe.pattern_id = NEW.pattern_id
        ),
        avg_score = (
            SELECT AVG(e.current_confidence)
            FROM core_brain.pattern_evidence pe
            JOIN core_brain.experiences e ON e.id = pe.experience_id
            WHERE pe.pattern_id = NEW.pattern_id
        ),
        updated_at = NOW()
    WHERE p.id = NEW.pattern_id;

    UPDATE core_brain.patterns
    SET status = 'active', promotion_criteria_met = TRUE
    WHERE id = NEW.pattern_id
      AND experience_count >= 3
      AND avg_score > 0.75
      AND status = 'draft';
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_pattern_promotion ON core_brain.pattern_evidence;
CREATE TRIGGER trg_pattern_promotion
    AFTER INSERT ON core_brain.pattern_evidence
    FOR EACH ROW EXECUTE FUNCTION core_brain.check_pattern_promotion();

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
    '3.0.0',
    'schema-baseline-3.0.0',
    'Performance-governed rollout release: MCUM now reviews testing skill versions with lifecycle scoring, project affinity, activation, and rollback gates.',
    'user',
    'active'
WHERE NOT EXISTS (
    SELECT 1
    FROM core_brain.skill_versions
    WHERE skill_name = 'mcum-orchestrator'
      AND version_semver = '3.0.0'
      AND COALESCE(skill_content_hash, '') = 'schema-baseline-3.0.0'
);

SELECT 'MCUM Schema v3.0 instalado OK' AS resultado;
SELECT 'core_brain: experiences, patterns, pattern_evidence, skill_versions, test_suite, retrieval_runs' AS tablas_core;
SELECT 'project_registry: projects, project_logs, project_kpis' AS tablas_registry;
