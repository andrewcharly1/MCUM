-- MCUM minimal seed — safe for public release.
-- Registers a few EXAMPLE skills so a fresh install is not completely empty.
-- Everything else (experiences, playbooks, patterns, projects, memory) stays
-- EMPTY for the user to populate. Contains NO personal data and NO credentials.
-- Idempotent: safe to run repeatedly.

INSERT INTO project_registry.skill_catalog
    (skill_name, skill_dir_name, skill_path, source, status, description)
VALUES
    ('mcum-orchestrator', 'MCUM', '.agent/skills/MCUM', 'local', 'active',
     'Primary orchestration & PostgreSQL-backed operational memory. Wraps every task.'),
    ('html-dashboard-expert', 'html-dashboard-expert', '.agent/skills/html-dashboard-expert',
     'local', 'active', 'Executive HTML dashboards with KPIs and Chart.js.'),
    ('kaizen', 'kaizen', '.agent/skills/kaizen', 'local', 'active',
     'Continuous improvement, root-cause analysis, refactoring and technical-debt review.')
ON CONFLICT (skill_name) DO NOTHING;
