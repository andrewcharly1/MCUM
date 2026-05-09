# PLAN DE MEJORA MCUM v2.0 — Alto Impacto

## Diagnostico Base (datos reales de la DB)

| Metrica actual | Valor | Problema |
|----------------|-------|----------|
| tokens_estimated registrados | 0/95 tareas | No se puede medir ahorro |
| session_duration_sec | Mide ciclo MCUM (~20s), no tarea real | Sin baseline de comparacion |
| SISL ciclos productivos | 1/34 (3%) | 97% desperdicio de compute |
| Skills evaluados por SISL | 2/11 | 9 skills invisibles |
| Skills realmente modificados | 0 | SISL no hace writeback |
| Experiencias con revalidacion | 22/27 (81%) | Memoria SI funciona |
| Retrieval hit rate | 97% (89/92) | Busqueda SI funciona |
| User feedback registrado | 0 | Sin validacion humana |
| Patterns promovidos | 0 | Trigger nunca se activa |

---

## FASE 1 — TRAZABILIDAD REAL (Medir para mejorar)
> Sin metricas reales, toda mejora es especulativa

### 1.1 Token Counter en project_logs

**Archivo:** `db/project_registry.py` → funcion `log_entry()`

**Cambio:** Agregar parametro `tokens_used: int = None` y popularlo.

```python
# En log_entry() agregar:
def log_entry(conn, project_id, log_type, title, ..., tokens_used=None):
    cur.execute("""
        INSERT INTO project_registry.project_logs
        (project_id, log_type, title, ..., tokens_estimated)
        VALUES (%s, %s, %s, ..., %s)
    """, [..., tokens_used])
```

**Quien lo popula:** El dispatcher MCUM en SKILL.md debe instruir al agente:
- Contar tokens del prompt de entrada (estimacion: len(text) / 4)
- Contar tokens de la respuesta generada
- Registrar en session_end

**Impacto:** Primera vez que tendras datos reales de consumo por tarea/proyecto/skill.

### 1.2 Timer de Tarea Real (wall-clock)

**Archivo:** `db/project_registry.py` → funciones `log_session_start()` y `log_session_end()`

**Cambio:** `log_session_start` ya registra timestamp. `log_session_end` ya calcula duracion.
El problema es que session_start/end se llaman en el ciclo MCUM (20s), no cubren la tarea real.

**Solucion:** Agregar `task_start_at` y `task_end_at` a project_logs:

```sql
ALTER TABLE project_registry.project_logs
    ADD COLUMN IF NOT EXISTS task_wall_clock_ms INTEGER,
    ADD COLUMN IF NOT EXISTS context_tokens_in INTEGER,
    ADD COLUMN IF NOT EXISTS context_tokens_out INTEGER,
    ADD COLUMN IF NOT EXISTS retrieval_latency_ms INTEGER;
```

**Impacto:** Permite calcular tiempo real por tarea, tokens in/out, y latencia de retrieval.

### 1.3 Vista Materializada de Metricas

**Archivo:** `db/schema.sql`

```sql
CREATE MATERIALIZED VIEW IF NOT EXISTS project_registry.mv_daily_metrics AS
SELECT
    pl.created_at::date AS day,
    p.project_name,
    COUNT(*) FILTER (WHERE pl.log_type = 'task') AS tasks,
    COUNT(*) FILTER (WHERE pl.outcome = 'success') AS successes,
    COUNT(*) FILTER (WHERE pl.outcome = 'failure') AS failures,
    AVG(pl.confidence_score) FILTER (WHERE pl.confidence_score IS NOT NULL) AS avg_confidence,
    SUM(pl.context_tokens_in) AS tokens_in,
    SUM(pl.context_tokens_out) AS tokens_out,
    AVG(pl.task_wall_clock_ms) AS avg_task_ms,
    AVG(pl.retrieval_latency_ms) AS avg_retrieval_ms,
    COUNT(*) FILTER (WHERE pl.log_type = 'improvement') AS sisl_cycles
FROM project_registry.project_logs pl
JOIN project_registry.projects p ON p.id = pl.project_id
GROUP BY pl.created_at::date, p.project_name
WITH DATA;

CREATE UNIQUE INDEX ON project_registry.mv_daily_metrics (day, project_name);
```

**Impacto:** Un solo query te da el dashboard completo de productividad por dia.

---

## FASE 2 — SISL QUE REALMENTE MEJORA SKILLS (Robustez)
> El loop actual corre 34 veces y no cambia nada. Hay que arreglarlo.

### 2.1 Tests Adversariales Reales (no auto-aprobados)

**Problema:** Los tests actuales son 10/10 triviales. CKL siempre es 1.0.
**Solucion:** Generar tests desde experiencias de FAILURE.

**Archivo nuevo:** `sisl/adversarial_test_generator.py`

```python
def generate_adversarial_tests(skill_name: str, conn) -> list[dict]:
    """Genera tests basados en failures reales, no triviales."""
    with get_cursor(conn) as cur:
        # 1. Buscar experiencias de tipo failure_pattern para este skill
        cur.execute("""
            SELECT title, content, task_description
            FROM core_brain.experiences
            WHERE skill_name = %s AND category = 'failure_pattern'
              AND current_confidence >= 0.5
        """, [skill_name])
        failures = cur.fetchall()

        # 2. Buscar retrieval_runs con outcome='failure'
        cur.execute("""
            SELECT input_context, failure_reason
            FROM core_brain.retrieval_runs
            WHERE skill_name = %s AND outcome_status = 'failure'
        """, [skill_name])
        failed_runs = cur.fetchall()

        # 3. Buscar experiencias con confidence degradada
        cur.execute("""
            SELECT title, content, initial_score, current_confidence
            FROM core_brain.experiences
            WHERE skill_name = %s AND current_confidence < initial_score
        """, [skill_name])
        degraded = cur.fetchall()

        tests = []
        for f in failures:
            tests.append({
                'test_type': 'negative_case',
                'input_query': f['task_description'] or f['title'],
                'pass_condition': f'Must detect failure pattern: {f["title"][:80]}',
                'partition': 'adversarial',
                'difficulty': 3,
                'source': 'real_failure'
            })

        for fr in failed_runs:
            tests.append({
                'test_type': 'conflict_resolution',
                'input_query': fr['input_context'][:200],
                'pass_condition': f'Must not repeat failure: {fr["failure_reason"][:80]}',
                'partition': 'adversarial',
                'difficulty': 4,
                'source': 'failed_retrieval'
            })

        return tests
```

**Impacto:** SISL dejara de auto-aprobarse. Tests basados en fallas reales fuerzan mejoras reales.

### 2.2 Writeback: Aplicar Propuestas al SKILL.md

**Problema:** SISL genera propuestas en `diff_patch` JSON pero nadie las escribe al archivo.

**Archivo nuevo:** `sisl/skill_writer.py`

```python
import json
from pathlib import Path

SKILLS_ROOT = Path(__file__).resolve().parent.parent.parent  # .agent/skills/

def apply_sisl_proposal(skill_name: str, proposal: dict, conn) -> bool:
    """Aplica una propuesta SISL al archivo SKILL.md real."""
    skill_path = SKILLS_ROOT / skill_name / "SKILL.md"
    if not skill_path.exists():
        return False

    content = skill_path.read_text(encoding='utf-8')

    ptype = proposal.get('type')
    section = proposal.get('section', '')
    proposed_text = proposal.get('proposed', '')

    if ptype == 'add_failure_warning':
        # Insertar warning despues de la seccion indicada
        marker = f"## {section}" if section else "## REGLAS DETERMINISTAS"
        if marker in content:
            insert_point = content.index(marker) + len(marker)
            next_newline = content.index('\n', insert_point)
            content = (content[:next_newline + 1] +
                       f"\n> **RIESGO DOCUMENTADO (SISL):**\n> {proposed_text}\n" +
                       content[next_newline + 1:])

    elif ptype == 'add_edge_case':
        # Agregar a seccion de edge cases o golden dataset
        content += f"\n\n### Edge Case (SISL auto-detected)\n{proposed_text}\n"

    elif ptype == 'refine_rule':
        # Buscar y reemplazar regla
        old_text = proposal.get('old_text', '')
        if old_text and old_text in content:
            content = content.replace(old_text, proposed_text, 1)

    # Escribir con backup
    backup_path = skill_path.with_suffix('.md.bak')
    backup_path.write_text(skill_path.read_text(encoding='utf-8'), encoding='utf-8')
    skill_path.write_text(content, encoding='utf-8')

    # Registrar en DB
    with get_cursor(conn) as cur:
        cur.execute("""
            UPDATE core_brain.skill_versions
            SET status = 'active'
            WHERE skill_name = %s AND version_semver = %s
        """, [skill_name, proposal.get('target_version', '1.0.0')])

    return True
```

**Impacto:** Las propuestas dejan de ser JSON muerto en la DB y se convierten en cambios reales al skill.

### 2.3 Evaluacion Cross-Skill (los 11, no solo 2)

**Problema:** SISL solo evalua skills con experiencias. 9/11 skills nunca entran al ciclo.

**Archivo:** Modificar el dispatcher SISL para incluir cold-start evaluation.

```python
def get_skills_for_evaluation(conn) -> list[str]:
    """Retorna TODOS los skills del workspace, no solo los que tienen experiencias."""
    skills_dir = Path(__file__).resolve().parent.parent.parent
    all_skills = [d.name for d in skills_dir.iterdir()
                  if d.is_dir() and (d / 'SKILL.md').exists()]

    # Priorizar: skills con experiencias primero, luego cold-start
    with get_cursor(conn) as cur:
        cur.execute("""
            SELECT DISTINCT skill_name, COUNT(*) as exp_count
            FROM core_brain.experiences
            GROUP BY skill_name
        """)
        with_exp = {r['skill_name']: r['exp_count'] for r in cur.fetchall()}

    # Ordenar: con experiencias primero, sin experiencias despues
    return sorted(all_skills, key=lambda s: with_exp.get(s, 0), reverse=True)
```

**Impacto:** Los 11 skills entran al ciclo de evaluacion. Los que no tienen experiencias reciben tests de cold-start.

---

## FASE 3 — ESCALABILIDAD (Crecer sin romperse)

### 3.1 Experience Archival (TTL + Decay)

**Problema:** Las 27 experiencias crecen sin control. Experiencias con confidence < 0.3 nunca se limpian.

**Archivo:** `db/schema.sql` + nuevo job

```sql
-- Funcion de decay automatico
CREATE OR REPLACE FUNCTION core_brain.decay_stale_experiences()
RETURNS INTEGER AS $$
DECLARE affected INTEGER;
BEGIN
    -- Experiencias no validadas en 30+ dias pierden 10% confidence
    UPDATE core_brain.experiences
    SET current_confidence = GREATEST(current_confidence * 0.9, 0.1),
        last_validated_at = now()
    WHERE last_validated_at < now() - interval '30 days'
      AND current_confidence > 0.3
      AND superseded_by IS NULL;
    GET DIAGNOSTICS affected = ROW_COUNT;

    -- Archivar experiencias con confidence < 0.15
    UPDATE core_brain.experiences
    SET superseded_by = id  -- auto-supersede = archived
    WHERE current_confidence < 0.15
      AND superseded_by IS NULL;

    RETURN affected;
END;
$$ LANGUAGE plpgsql;
```

**Impacto:** La base se auto-limpia. Experiencias obsoletas decaen naturalmente.

### 3.2 Retrieval con Budget de Tokens

**Problema:** `max_token_budget: 4000` esta definido pero nunca se enforcea.

**Archivo:** `db/experience_store.py` → `retrieve_for_task()`

```python
def retrieve_for_task(conn, query, project_id=None, ...):
    ...
    # Despues de recuperar experiencias, enforcer budget
    budget = policy.get('max_token_budget', 4000)
    token_count = 0
    budgeted_results = []
    for exp in results:
        exp_tokens = len(json.dumps(exp.get('content', {}))) // 4
        if token_count + exp_tokens > budget:
            break
        budgeted_results.append(exp)
        token_count += exp_tokens

    return budgeted_results
```

**Impacto:** El retrieval nunca inyecta mas contexto del que el modelo puede procesar eficientemente.

### 3.3 Partitioned project_logs (para escala)

**Problema:** project_logs tiene 188 rows ahora. En 6 meses tendra 10K+. Sin particionamiento.

```sql
-- Crear tabla particionada por mes
CREATE TABLE IF NOT EXISTS project_registry.project_logs_partitioned (
    LIKE project_registry.project_logs INCLUDING ALL
) PARTITION BY RANGE (created_at);

-- Particion automatica por mes
CREATE TABLE project_registry.project_logs_2026_03
    PARTITION OF project_registry.project_logs_partitioned
    FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');

CREATE TABLE project_registry.project_logs_2026_04
    PARTITION OF project_registry.project_logs_partitioned
    FOR VALUES FROM ('2026-04-01') TO ('2026-05-01');
```

**Impacto:** Queries sobre logs recientes no escanean toda la tabla. Particiones viejas se pueden archivar.

---

## FASE 4 — ROBUSTEZ (No romperse bajo presion)

### 4.1 Fix los 3 criticos pendientes de experience_store.py

**4.1.1 — WHERE clause con alias desde el inicio (linea ~441)**

```python
# ANTES (fragil):
where_clause.replace('current_confidence', 'e.current_confidence')...

# DESPUES (robusto):
conditions = []
conditions.append("e.superseded_by IS NULL")
conditions.append("e.embedding IS NOT NULL")
if min_confidence > 0:
    conditions.append("e.current_confidence >= %s")
    params.append(min_confidence)
if category:
    conditions.append("e.category = %s")
    params.append(category)
if skill_name:
    conditions.append("e.skill_name = %s")
    params.append(skill_name)
if project_id:
    conditions.append("e.project_id = %s")
    params.append(str(project_id))
```

**4.1.2 — Validacion en _embedding_to_sql()**

```python
def _embedding_to_sql(embedding: list[float]) -> str | None:
    if embedding is None:
        return None
    if len(embedding) != EMBEDDING_DIM:
        raise ValueError(f"Embedding must be {EMBEDDING_DIM}d, got {len(embedding)}d")
    if any(math.isnan(x) or math.isinf(x) for x in embedding):
        raise ValueError("Embedding contains NaN or Inf")
    if _is_pgvector_enabled():
        return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"
    return json.dumps(embedding)
```

**4.1.3 — Cache TTL en _is_pgvector_enabled()**

```python
_PGVECTOR_CACHE_TTL_SEC = 300
_pgvector_cache = {"value": None, "checked_at": 0.0}

def _is_pgvector_enabled(force_refresh: bool = False) -> bool:
    now = time.time()
    if (not force_refresh
        and _pgvector_cache["value"] is not None
        and now - _pgvector_cache["checked_at"] < _PGVECTOR_CACHE_TTL_SEC):
        return _pgvector_cache["value"]

    try:
        with get_db() as conn:
            with get_cursor(conn) as cur:
                cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
                result = cur.fetchone() is not None
    except Exception:
        result = False

    _pgvector_cache["value"] = result
    _pgvector_cache["checked_at"] = now
    return result
```

### 4.2 Connection Retry con Backoff

**Archivo:** `db/connection.py`

```python
import time

def get_db_with_retry(max_retries=3, base_delay=0.5):
    """Context manager con retry exponencial para conexiones transitorias."""
    for attempt in range(max_retries):
        try:
            return get_db()
        except psycopg.OperationalError as e:
            if attempt == max_retries - 1:
                raise
            delay = base_delay * (2 ** attempt)
            time.sleep(delay)
```

### 4.3 Health Check con Query Performance

**Archivo:** `db/connection.py` → `health_check()`

```python
def health_check():
    ...
    # Agregar test de performance
    start = time.monotonic()
    cur.execute("SELECT COUNT(*) FROM core_brain.experiences WHERE embedding IS NOT NULL")
    query_ms = (time.monotonic() - start) * 1000
    info['query_latency_ms'] = round(query_ms, 2)
    info['performance_ok'] = query_ms < 500  # threshold 500ms
    ...
```

---

## FASE 5 — METRICAS COMPARATIVAS (el dashboard que te falta)

### 5.1 Funcion SQL de Reporte Completo

```sql
CREATE OR REPLACE FUNCTION project_registry.generate_metrics_report(
    p_from DATE DEFAULT (now() - interval '7 days')::date,
    p_to DATE DEFAULT now()::date
) RETURNS TABLE (
    day DATE,
    project TEXT,
    tasks_total INT,
    tasks_success INT,
    tasks_failure INT,
    success_rate NUMERIC,
    avg_confidence NUMERIC,
    tokens_in BIGINT,
    tokens_out BIGINT,
    avg_task_ms INT,
    retrieval_hits INT,
    retrieval_misses INT,
    hit_rate NUMERIC,
    experiences_revalidated INT,
    sisl_cycles INT,
    sisl_proposals INT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        pl.created_at::date,
        p.project_name,
        COUNT(*) FILTER (WHERE pl.log_type = 'task')::INT,
        COUNT(*) FILTER (WHERE pl.outcome = 'success')::INT,
        COUNT(*) FILTER (WHERE pl.outcome = 'failure')::INT,
        ROUND(COUNT(*) FILTER (WHERE pl.outcome = 'success')::NUMERIC /
              NULLIF(COUNT(*) FILTER (WHERE pl.log_type = 'task'), 0), 3),
        ROUND(AVG(pl.confidence_score) FILTER (WHERE pl.confidence_score IS NOT NULL)::NUMERIC, 3),
        COALESCE(SUM(pl.context_tokens_in), 0)::BIGINT,
        COALESCE(SUM(pl.context_tokens_out), 0)::BIGINT,
        COALESCE(AVG(pl.task_wall_clock_ms)::INT, 0),
        0::INT,  -- populated from retrieval_runs join
        0::INT,
        0::NUMERIC,
        0::INT,
        COUNT(*) FILTER (WHERE pl.log_type = 'improvement')::INT,
        0::INT
    FROM project_registry.project_logs pl
    JOIN project_registry.projects p ON p.id = pl.project_id
    WHERE pl.created_at::date BETWEEN p_from AND p_to
    GROUP BY pl.created_at::date, p.project_name
    ORDER BY pl.created_at::date, p.project_name;
END;
$$ LANGUAGE plpgsql;
```

### 5.2 Instruccion en SKILL.md para Obligar Registro

Agregar al SKILL.md del MCUM en la seccion de REGLAS DETERMINISTAS:

```markdown
## R7 — Registro Obligatorio de Metricas
- TODA session_end DEBE incluir:
  - `context_tokens_in`: tokens estimados del prompt (len(text) // 4)
  - `context_tokens_out`: tokens estimados de la respuesta
  - `task_wall_clock_ms`: tiempo real de la tarea en ms
  - `retrieval_latency_ms`: tiempo del semantic_search en ms
- Si alguno falta, el session_end se marca como `incomplete_metrics`
```

---

## ORDEN DE IMPLEMENTACION

| Paso | Fase | Que | Archivos | Impacto | Esfuerzo |
|:----:|:----:|-----|----------|:-------:|:--------:|
| 1 | F4.1 | Fix 3 criticos experience_store | experience_store.py | CRITICO | 30 min |
| 2 | F1.1 | Columnas de metricas en project_logs | schema.sql + project_registry.py | ALTO | 20 min |
| 3 | F1.3 | Vista materializada mv_daily_metrics | schema.sql | ALTO | 10 min |
| 4 | F5.2 | Regla R7 en SKILL.md | SKILL.md | ALTO | 5 min |
| 5 | F2.1 | Tests adversariales desde failures | sisl/adversarial_test_generator.py (nuevo) | ALTO | 40 min |
| 6 | F2.2 | Writeback de propuestas SISL | sisl/skill_writer.py (nuevo) | ALTO | 30 min |
| 7 | F2.3 | Evaluacion cross-skill | sisl/ dispatcher | MEDIO | 20 min |
| 8 | F3.1 | Decay + archival de experiencias | schema.sql + job | MEDIO | 15 min |
| 9 | F3.2 | Token budget enforcement | experience_store.py | MEDIO | 10 min |
| 10 | F4.2 | Connection retry con backoff | connection.py | MEDIO | 10 min |
| 11 | F4.3 | Health check con query perf | connection.py | BAJO | 10 min |
| 12 | F5.1 | Funcion SQL de reporte | schema.sql | BAJO | 15 min |
| 13 | F3.3 | Partitioned logs (futuro) | schema.sql | BAJO | 20 min |

**Tiempo total estimado: ~4 horas**
**Archivos modificados: 5 existentes + 2 nuevos**
**Archivos nuevos: sisl/adversarial_test_generator.py, sisl/skill_writer.py**

---

## METRICAS OBJETIVO POST-MEJORA

| Metrica | Actual | Objetivo 30 dias | Objetivo 90 dias |
|---------|:------:|:-----------------:|:-----------------:|
| SISL ciclos productivos | 3% (1/34) | 30%+ | 60%+ |
| Skills evaluados por SISL | 2/11 | 11/11 | 11/11 |
| Skills con writeback real | 0 | 3+ | 8+ |
| tokens_estimated poblado | 0% | 100% | 100% |
| task_wall_clock_ms poblado | 0% | 100% | 100% |
| Experiencias con TTL/decay | 0 | activo | activo |
| Patterns promovidos | 0 | 2+ | 5+ |
| CKL score promedio (adversarial) | 1.0 (falso) | 0.75 (real) | 0.85 (real) |
| Retrieval hit rate | 97% | 97%+ | 98%+ |
| Avg confidence | 0.855 | 0.88+ | 0.90+ |
