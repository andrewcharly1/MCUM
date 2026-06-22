"""
MCUM - Motor Cerebral Ultra Multiversal
core/dispatcher.py - Motor de Seleccion de Skills

Selecciona el skill correcto para cada tarea usando:
1. Coincidencia semantica de la tarea con el perfil de cada skill
2. Reglas deterministas de keywords (triggers/anti-triggers)
3. Historial de confianza del skill en el proyecto

Arquitectura del Dispatcher:
  tarea_desc -> [skill_registry] -> ranking semantico -> skill_seleccionado
                                 -> reglas trigger    -> override si hay match exacto
"""

from __future__ import annotations
import re
import unicodedata
from dataclasses import dataclass, field

from ..db.embedder import embed, cosine_similarity
from ..db.skill_catalog import discover_local_skills
from .skill_factory import filter_dispatchable_skills

# ─────────────────────────────────────────
# REGISTRO DE SKILLS (fuente de verdad)
# ─────────────────────────────────────────
# Cada entry define el perfil semantico del skill:
#   triggers:  palabras clave de activacion determinista
#   anti:      palabras clave que impiden la activacion
#   profile:   texto descriptivo para matching semantico
#   priority:  0 = mas bajo, 10 = mas alto (para desempates)

LEGACY_ROUTING_DEFAULTS: list[dict] = [
    {
        "name"    : "go-industrial-expert",
        "file"    : "go-industrial-expert",
        "triggers": ["go", "golang", "gorilla", "gin", "fiber", "grpc", "microservicio", "concurrencia",
                     "goroutine", "channel", "api rest go", "backend go"],
        "anti"    : ["flutter", "dart", "react", "html", "dashboard", "frontend"],
        "profile" : "Desarrollo backend en Go: APIs REST, microservicios, goroutines, concurrencia, gin, fiber, grpc, tests unitarios Go",
        "priority": 9,
    },
    {
        "name"    : "flutter-premium-expert",
        "file"    : "flutter-premium-expert",
        "triggers": ["flutter", "dart", "widget", "statefulwidget", "statelesswidget", "riverpod",
                     "bloc", "getx", "app movil", "mobile app", "android", "ios"],
        "anti"    : ["backend", "postgresql", "api", "go", "html", "react"],
        "profile" : "Desarrollo de apps moviles con Flutter y Dart: widgets, estado, Riverpod, Bloc, animaciones, Material 3, iOS, Android",
        "priority": 9,
    },
    {
        "name"    : "html-dashboard-expert",
        "file"    : "html-dashboard-expert",
        "triggers": ["dashboard", "html", "kpi", "chart", "grafico", "presentacion", "gerencia",
                     "bhp", "mintral", "ejecutivo", "informe html"],
        "anti"    : ["react", "nextjs", "vue", "svelte", "flutter", "go", "backend"],
        "profile" : "Dashboards ejecutivos HTML puro con KPIs, charts Chart.js, paleta MINTRAL (Gold/Slate/Dark), calculos automaticos embebidos",
        "priority": 8,
    },
    {
        "name"    : "nextjs-supabase-auth",
        "file"    : "nextjs-supabase-auth",
        "triggers": ["next.js", "nextjs", "supabase auth", "autenticacion", "login supabase",
                     "middleware next", "ruta protegida", "jwt supabase", "auth next"],
        "anti"    : ["flutter", "go", "html puro", "dashboard ejecutivo"],
        "profile" : "Integracion de autenticacion Supabase con Next.js App Router: login, signup, middleware, rutas protegidas, JWT, RLS",
        "priority": 8,
    },
    {
        "name"    : "ui-ux-pro-max",
        "file"    : "ui-ux-pro-max",
        "triggers": ["ui", "ux", "glassmorphism", "animacion css", "landing page",
                     "portfolio", "ecommerce", "sass", "tailwind", "figma", "dark mode ui"],
        "anti"    : ["go", "flutter", "backend", "postgresql", "api rest", "nuevo skill", "crear skill"],
        "profile" : "Diseno UI/UX premium: glassmorphism, neumorphism, dark mode, animaciones CSS, componentes React/Next.js/Vue/Svelte, accesibilidad",
        "priority": 7,
    },
    {
        "name"    : "backend-analyzer-coder",
        "file"    : "backend-analyzer-coder",
        "triggers": ["arquitectura", "acid", "teorema cap", "cap theorem", "idempotencia",
                     "balanceador de carga", "cache", "cola de mensajes", "rabbitmq", "kafka",
                     "disenar api", "analizar backend"],
        "anti"    : ["flutter", "html", "dashboard", "css", "diseño"],
        "profile" : "Analisis y disenio de arquitecturas backend: ACID, CAP, idempotencia, observabilidad, balanceo de carga, patrones de microservicios",
        "priority": 7,
    },
    {
        "name"    : "use-skill-creator",
        "file"    : "creador-de-habilidades",
        "triggers": ["crear skill", "nueva skill", "skill nuevo", "encapsular", "skill creator",
                     "crear habilidad", "quiero un skill", "disenar skill", "nuevo skill",
                     "quiero crear", "hazme una skill", "construye un skill"],
        "anti"    : [],
        "profile" : "Creacion de nuevos skills MCUM usando el motor USE 1.0: analisis, entrevista, construccion del SKILL.md, auditoria adversarial, certificacion",
        "priority": 10,
    },
    {
        "name"    : "kaizen",
        "file"    : "kaizen",
        "triggers": ["refactorizar", "mejorar codigo", "optimizar", "causa raiz", "root cause",
                     "poka-yoke", "mejora continua", "deuda tecnica", "sprint", "pdca",
                     "analisis a3", "kaizen"],
        "anti"    : [],
        "profile" : "Analisis de causa raiz, mejora continua Kaizen, Poka-Yoke, PDCA, refactorizacion, deuda tecnica, escalabilidad, auditoria de codigo",
        "priority": 5,  # Default general, menor priority que skills especificos
    },
    {
        "name"    : "remucl-kaizen-pokayoke",
        "file"    : "remucl-kaizen-pokayoke",
        "triggers": ["remuneracion", "liquidacion", "sueldo", "afp", "isapre", "finiquito",
                     "contabilidad", "libro remuneraciones", "prevision social", "chile laboral"],
        "anti"    : [],
        "profile" : "Remuneraciones Chile, liquidaciones de sueldo, AFP, Isapre, finiquito, libro de remuneraciones, normativa laboral chilena",
        "priority": 9,
    },
]

# Backward-compatible export only. Runtime routing is loaded from local skills metadata.
SKILL_REGISTRY = LEGACY_ROUTING_DEFAULTS


def _normalize_match_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()


def _contains_trigger(task_text: str, trigger: str) -> bool:
    normalized_task = _normalize_match_text(task_text)
    normalized_trigger = _normalize_match_text(trigger)
    if not normalized_task or not normalized_trigger:
        return False
    if " " in normalized_trigger:
        return normalized_trigger in normalized_task
    task_tokens = set(normalized_task.split())
    return normalized_trigger in task_tokens


def _load_dynamic_skills() -> list[dict]:
    loaded: list[dict] = []
    try:
        discovered = discover_local_skills()
    except Exception:
        discovered = []

    if not discovered:
        return []

    for entry in discovered:
        name = str(entry.get("skill_name") or "").strip()
        if not name:
            continue
        metadata = dict(entry.get("metadata") or {})
        routing = dict(metadata.get("routing") or {})
        if routing.get("enabled") is False:
            continue
        description = str(entry.get("description") or "").strip()
        routing_source = str(routing.get("source") or "").strip()
        if not routing_source:
            if routing.get("has_explicit_routing"):
                routing_source = "frontmatter"
            else:
                routing_source = "local"
        loaded.append(
            {
                "name": name,
                "file": entry.get("skill_dir_name") or name,
                "triggers": list(routing.get("triggers") or []),
                "anti": list(routing.get("anti") or []),
                "profile": (
                    str(routing.get("profile") or "").strip()
                    or description
                    or f"Local skill loaded from {entry.get('skill_dir_name') or name}"
                ),
                "priority": int(routing.get("priority") or 6),
                "routing_source": routing_source,
                "metadata": metadata,
            }
        )

    return loaded


def _get_skill_registry(
    *,
    include_candidates: bool = False,
    project_context: dict | None = None,
) -> list[dict]:
    registry = _load_dynamic_skills()
    if project_context is None:
        return filter_dispatchable_skills(registry, include_candidates=include_candidates)
    return filter_dispatchable_skills(
        registry,
        include_candidates=include_candidates,
        project_context=project_context,
    )


def _get_dispatch_learning_adjustment(skill_name: str, dispatch_learning_profile: dict | None) -> dict:
    if not isinstance(dispatch_learning_profile, dict) or not dispatch_learning_profile.get("active"):
        return {"priority_delta": 0.0, "score_delta": 0.0}
    priority_delta = float(
        (dispatch_learning_profile.get("priority_adjustments") or {}).get(skill_name) or 0.0
    )
    score_delta = float(
        (dispatch_learning_profile.get("score_adjustments") or {}).get(skill_name) or 0.0
    )
    return {
        "priority_delta": priority_delta,
        "score_delta": score_delta,
    }


def _format_semantic_alternative(candidate: dict) -> dict:
    return {
        "name": candidate["name"],
        "score": round(float(candidate.get("semantic_score", candidate.get("score", 0.0))), 4),
        "priority": candidate.get("base_priority", candidate.get("priority", 5)),
    }


def _dispatch_learning_warning(candidate: dict) -> str | None:
    priority_delta = float(candidate.get("priority_delta") or 0.0)
    score_delta = float(candidate.get("score_adjustment") or 0.0)
    if abs(priority_delta) < 0.12 and abs(score_delta) < 0.01:
        return None
    return (
        f"Dispatch learning adjusted {candidate['name']} "
        f"(priority_delta={priority_delta:+.2f}, score_delta={score_delta:+.02f})."
    )


def _normalize_dispatch_hints(dispatch_hints: dict | None) -> dict[str, object]:
    hints = dict(dispatch_hints or {})
    preferred = [
        str(item).strip()
        for item in (hints.get("preferred_skills") or [])
        if str(item).strip()
    ]
    avoid = [
        str(item).strip()
        for item in (hints.get("avoid_skills") or [])
        if str(item).strip()
    ]
    loop_risk = float(hints.get("loop_risk") or 0.0)
    medium_threshold = float(hints.get("warning_risk_threshold", 0.35) or 0.35)
    recommendation = str(hints.get("recommendation") or "").strip()
    active = bool(preferred or avoid) and (
        loop_risk >= medium_threshold
        or recommendation in {
            "reuse_prior_success",
            "consider_alternate_successful_skill",
            "switch_strategy_before_retry",
            "increase_validation_and_diverge",
        }
    )
    return {
        "enabled": bool(hints.get("enabled", True)),
        "active": active,
        "preferred_skills": preferred,
        "avoid_skills": avoid,
        "loop_risk": loop_risk,
        "recommendation": recommendation,
        "warning_risk_threshold": medium_threshold,
        "preferred_score_boost": float(hints.get("preferred_score_boost", 0.08) or 0.08),
        "preferred_priority_boost": float(hints.get("preferred_priority_boost", 0.5) or 0.5),
        "avoid_score_penalty": float(hints.get("avoid_score_penalty", 0.06) or 0.06),
        "avoid_priority_penalty": float(hints.get("avoid_priority_penalty", 0.3) or 0.3),
    }


def _apply_dispatch_hints_to_ranking(
    ranked: list[dict],
    dispatch_hints: dict | None,
) -> tuple[list[dict], list[str]]:
    normalized = _normalize_dispatch_hints(dispatch_hints)
    if not ranked or not normalized.get("enabled") or not normalized.get("active"):
        return ranked, []

    preferred_positions = {
        skill_name: index
        for index, skill_name in enumerate(normalized.get("preferred_skills") or [])
    }
    avoid_skills = set(normalized.get("avoid_skills") or [])
    preferred_score_boost = float(normalized.get("preferred_score_boost") or 0.08)
    preferred_priority_boost = float(normalized.get("preferred_priority_boost") or 0.5)
    avoid_score_penalty = float(normalized.get("avoid_score_penalty") or 0.06)
    avoid_priority_penalty = float(normalized.get("avoid_priority_penalty") or 0.3)

    adjusted: list[dict] = []
    for candidate in ranked:
        updated = dict(candidate)
        name = str(candidate.get("name") or "").strip()
        score_delta = 0.0
        priority_delta = 0.0

        if name in preferred_positions:
            weight = max(0.5, 1.0 - (preferred_positions[name] * 0.2))
            score_delta += preferred_score_boost * weight
            priority_delta += preferred_priority_boost * weight
        if name in avoid_skills:
            score_delta -= avoid_score_penalty
            priority_delta -= avoid_priority_penalty

        updated["score"] = round(float(candidate.get("score") or 0.0) + score_delta, 4)
        updated["priority"] = round(float(candidate.get("priority") or 0.0) + priority_delta, 4)
        updated["dispatch_hint_score_delta"] = round(score_delta, 4)
        updated["dispatch_hint_priority_delta"] = round(priority_delta, 4)
        adjusted.append(updated)

    adjusted.sort(key=lambda item: (item["score"], item["priority"]), reverse=True)
    warnings: list[str] = []
    if preferred_positions:
        warnings.append(
            "Anti-loop dispatch bias active: preferred "
            f"{', '.join(list(preferred_positions.keys())[:3])}."
        )
    if avoid_skills:
        warnings.append(
            "Anti-loop dispatch bias active: avoid "
            f"{', '.join(list(avoid_skills)[:3])}."
        )
    return adjusted, warnings


def _dispatch_hint_warning(candidate: dict) -> str | None:
    priority_delta = float(candidate.get("dispatch_hint_priority_delta") or 0.0)
    score_delta = float(candidate.get("dispatch_hint_score_delta") or 0.0)
    if abs(priority_delta) < 0.08 and abs(score_delta) < 0.01:
        return None
    return (
        f"Anti-loop dispatch bias adjusted {candidate['name']} "
        f"(priority_delta={priority_delta:+.2f}, score_delta={score_delta:+.02f})."
    )


# Skill por defecto cuando ningun match supera el threshold
DEFAULT_SKILL = "kaizen"
SEMANTIC_THRESHOLD = 0.25   # Similitud minima para match semantico
TRIGGER_CONFIDENCE = 0.95   # Confianza cuando hay match de trigger exacto


@dataclass
class DispatchResult:
    """Resultado de la seleccion del dispatcher."""
    skill_name      : str
    confidence      : float
    match_method    : str          # "trigger_exact" | "semantic" | "default"
    alternatives    : list[dict]   # Otros skills candidatos
    triggered_by    : str          # Keyword o frase que disparo el match
    semantic_score  : float = 0.0
    warnings        : list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "skill_name"   : self.skill_name,
            "confidence"   : self.confidence,
            "match_method" : self.match_method,
            "triggered_by" : self.triggered_by,
            "semantic_score": self.semantic_score,
            "alternatives" : self.alternatives,
            "warnings"     : self.warnings,
        }


# ─────────────────────────────────────────
# MATCHER DE TRIGGERS
# ─────────────────────────────────────────

def _match_triggers(task_lower: str, skill: dict) -> tuple[bool, str]:
    """
    Verifica si la tarea coincide con los triggers del skill.
    Es determinista: si hay trigger match, gana sobre semantica.
    
    Returns:
        (matched: bool, trigger_found: str)
    """
    # Verificar anti-triggers primero (Poka-Yoke)
    for anti in skill.get("anti", []):
        if _contains_trigger(task_lower, anti):
            return False, ""

    # Verificar triggers
    for trigger in skill.get("triggers", []):
        if _contains_trigger(task_lower, trigger):
            return True, trigger

    return False, ""


def _rank_by_semantics(
    task_text: str,
    exclude_anti: bool = True,
    project_context: dict | None = None,
    dispatch_learning_profile: dict | None = None,
) -> list[dict]:
    """
    Rankea skills por similitud semantica entre la tarea y el profile del skill.
    
    Returns:
        Lista de dicts con {name, score, method} ordenados por score DESC.
    """
    task_lower = task_text.lower()
    task_embedding = embed(task_text)
    registry = _get_skill_registry(project_context=project_context)

    ranked = []
    for skill in registry:
        # Respetar anti-triggers incluso en semantica
        if exclude_anti:
            blocked = any(_contains_trigger(task_lower, anti) for anti in skill.get("anti", []))
            if blocked:
                continue

        profile_embedding = embed(skill["profile"])
        raw_score = cosine_similarity(task_embedding, profile_embedding)
        learning_adjustment = _get_dispatch_learning_adjustment(
            skill["name"],
            dispatch_learning_profile,
        )
        adjusted_score = raw_score + learning_adjustment["score_delta"]
        adjusted_priority = skill.get("priority", 5) + learning_adjustment["priority_delta"]
        ranked.append({
            "name"            : skill["name"],
            "score"           : round(adjusted_score, 4),
            "semantic_score"  : round(raw_score, 4),
            "priority"        : round(adjusted_priority, 4),
            "base_priority"   : skill.get("priority", 5),
            "priority_delta"  : round(learning_adjustment["priority_delta"], 4),
            "score_adjustment": round(learning_adjustment["score_delta"], 4),
        })

    # Ordenar: primero por score, luego por priority en empates
    ranked.sort(key=lambda x: (x["score"], x["priority"]), reverse=True)
    return ranked


# ─────────────────────────────────────────
# DISPATCHER PRINCIPAL
# ─────────────────────────────────────────

def dispatch(
    task_description : str,
    project_context  : dict | None = None,
    force_skill      : str | None = None,
    dispatch_learning_profile: dict | None = None,
    dispatch_hints: dict | None = None,
) -> DispatchResult:
    """
    Selecciona el skill correcto para la tarea.
    
    Prioridad de seleccion:
    1. force_skill: el usuario lo especifico explicitamente
    2. Trigger exacto: keyword determinista encontrado en la tarea
    3. Semantica: el profile del skill mejor matchea la tarea
    4. Default: kaizen (siempre funciona como safety net)
    
    Args:
        task_description: Descripcion completa de la tarea.
        project_context:  Dict con metadata del proyecto {tech_stack, primary_language, ...}
        force_skill:      Si se provee, usar este skill directamente.
    
    Returns:
        DispatchResult con skill seleccionado y metadata del match.
    """
    warnings = []
    task_lower = task_description.lower()
    registry = _get_skill_registry(
        include_candidates=bool(force_skill),
        project_context=project_context,
    )

    # Validacion de enums de task_type y execution_mode (P2-A)
    # No bloquea, solo emite warnings para transparencia de governance
    if project_context and isinstance(project_context, dict):
        task_brief = project_context.get("_task_brief") or project_context.get("task_brief") or {}
        allowed_task_types = {"analizar", "crear", "corregir", "mejorar", "planificar", "validar", "automatizar"}
        allowed_execution_modes = {"analizar", "proponer", "ejecutar"}
        task_type = str(task_brief.get("task_type") or "").strip()
        execution_mode = str(task_brief.get("execution_mode") or "").strip()
        if task_type and task_type not in allowed_task_types:
            warnings.append(
                f"dispatcher: task_type '{task_type}' no reconocido. "
                f"Usar uno de: {sorted(allowed_task_types)}"
            )
        if execution_mode and execution_mode not in allowed_execution_modes:
            warnings.append(
                f"dispatcher: execution_mode '{execution_mode}' no reconocido. "
                f"Usar uno de: {sorted(allowed_execution_modes)}"
            )

    # ── 1. FORCE (el usuario lo pide explicitamente)
    if force_skill:
        if any(s["name"] == force_skill for s in registry):
            return DispatchResult(
                skill_name   = force_skill,
                confidence   = 1.0,
                match_method = "forced_by_user",
                alternatives = [],
                triggered_by = "user_override",
            )
        else:
            warnings.append(f"Skill '{force_skill}' no encontrado en el registry. Usando seleccion automatica.")

    # ── 2. TRIGGER EXACTO (determinista, maxima prioridad)
    trigger_matches = []
    for skill in registry:
        matched, trigger = _match_triggers(task_lower, skill)
        if matched:
            trigger_matches.append({
                "name"    : skill["name"],
                "trigger" : trigger,
                "priority": skill.get("priority", 5),
            })

    if len(trigger_matches) == 1:
        winner = trigger_matches[0]
        return DispatchResult(
            skill_name   = winner["name"],
            confidence   = TRIGGER_CONFIDENCE,
            match_method = "trigger_exact",
            alternatives = [],
            triggered_by = winner["trigger"],
            warnings     = warnings,
        )

    if len(trigger_matches) > 1:
        # Multiples triggers: usar semantica para desempatar entre ellos
        trigger_names = {m["name"] for m in trigger_matches}
        semantic_ranking = _rank_by_semantics(
            task_description,
            project_context=project_context,
            dispatch_learning_profile=dispatch_learning_profile,
        )
        semantic_ranking, hint_warnings = _apply_dispatch_hints_to_ranking(
            semantic_ranking,
            dispatch_hints,
        )
        warnings.extend(hint_warnings)
        for candidate in semantic_ranking:
            if candidate["name"] in trigger_names:
                alts = [
                    {"name": m["name"], "trigger": m.get("trigger", "?")}
                    for m in trigger_matches if m["name"] != candidate["name"]
                ]
                learning_warning = _dispatch_learning_warning(candidate)
                dispatch_hint_warning = _dispatch_hint_warning(candidate)
                triggered_by = next(
                    (m["trigger"] for m in trigger_matches if m["name"] == candidate["name"]),
                    "multi-trigger"
                )
                return DispatchResult(
                    skill_name    = candidate["name"],
                    confidence    = TRIGGER_CONFIDENCE,
                    match_method  = "trigger_multi_semantic_tiebreak",
                    alternatives  = alts,
                    triggered_by  = triggered_by,
                    semantic_score= candidate.get("semantic_score", candidate["score"]),
                    warnings      = warnings
                    + [f"Multi-trigger: {[m['name'] for m in trigger_matches]}"]
                    + ([learning_warning] if learning_warning else [])
                    + ([dispatch_hint_warning] if dispatch_hint_warning else []),
                )

    # ── 3. MATCHING SEMANTICO
    semantic_ranking = _rank_by_semantics(
        task_description,
        project_context=project_context,
        dispatch_learning_profile=dispatch_learning_profile,
    )
    semantic_ranking, hint_warnings = _apply_dispatch_hints_to_ranking(
        semantic_ranking,
        dispatch_hints,
    )
    warnings.extend(hint_warnings)

    if semantic_ranking:
        eligible = [
            candidate
            for candidate in semantic_ranking
            if float(candidate.get("semantic_score", candidate.get("score", 0.0))) >= SEMANTIC_THRESHOLD
        ]
        top = eligible[0] if eligible else semantic_ranking[0]
        alts = [
            _format_semantic_alternative(candidate)
            for candidate in semantic_ranking
            if candidate["name"] != top["name"]
        ][:3]
        top_semantic_score = float(top.get("semantic_score", top["score"]))
        learning_warning = _dispatch_learning_warning(top)
        dispatch_hint_warning = _dispatch_hint_warning(top)

        if top_semantic_score >= SEMANTIC_THRESHOLD:
            return DispatchResult(
                skill_name    = top["name"],
                confidence    = 0.60 + (top_semantic_score * 0.35),  # Escalado: 0.60 - 0.95
                match_method  = "semantic",
                alternatives  = alts,
                triggered_by  = f"semantic_score={top_semantic_score}",
                semantic_score= top_semantic_score,
                warnings      = warnings
                + ([learning_warning] if learning_warning else [])
                + ([dispatch_hint_warning] if dispatch_hint_warning else []),
            )
        else:
            warnings.append(
                f"Similitud maxima ({top_semantic_score:.2f}) por debajo del threshold ({SEMANTIC_THRESHOLD}). "
                f"Usando skill default '{DEFAULT_SKILL}'."
            )

    # ── 4. DEFAULT (safety net)
    return DispatchResult(
        skill_name   = DEFAULT_SKILL,
        confidence   = 0.40,
        match_method = "default",
        alternatives = (
            [_format_semantic_alternative(candidate) for candidate in semantic_ranking[:3]]
            if semantic_ranking else []
        ),
        triggered_by = "no_match_found",
        warnings     = warnings,
    )


def get_skill_profile(skill_name: str) -> dict | None:
    """Retorna el perfil completo de un skill del registry."""
    for skill in _get_skill_registry(include_candidates=True):
        if skill["name"] == skill_name:
            return dict(skill)
    return None


def list_available_skills() -> list[dict]:
    """Lista todos los skills disponibles con sus perfiles."""
    return [
        {
            "name"    : s["name"],
            "profile" : s["profile"][:80] + "...",
            "priority": s["priority"],
            "status"  : s.get("status", "unknown"),
            "triggers": s["triggers"][:3],  # Solo primeros 3 para brevedad
        }
        for s in _get_skill_registry(include_candidates=True)
    ]


# ─────────────────────────────────────────
# CLI DE DIAGNOSTICO
# ─────────────────────────────────────────
if __name__ == "__main__":
    print("🎯 MCUM Dispatcher — Test de Seleccion de Skills")
    print("─" * 55)

    test_tasks = [
        "Crear un endpoint REST en Go para gestionar contratos",
        "disenar un dashboard HTML con KPIs de logistica para gerencia",
        "agregar autenticacion con Supabase Auth a mi app Next.js",
        "refactorizar este codigo python para mejorar la arquitectura",
        "crear una app Flutter con estado usando Riverpod",
        "quiero crear un nuevo skill para analisis de contratos",
        "optimizar la consulta a la base de datos postgresql",
        "liquidacion de sueldo con AFP e isapre para empleado en Chile",
    ]

    print("  Modelo semantico cargando...\n")

    results_summary = []
    for task in test_tasks:
        result = dispatch(task)
        bar_len = int(result.confidence * 20)
        bar = "█" * bar_len + "░" * (20 - bar_len)
        print(f"  TASK: {task[:55]}")
        print(f"    → {result.skill_name:<30} [{bar}] {result.confidence:.2f}  ({result.match_method})")
        if result.warnings:
            print(f"    ⚠ {result.warnings[0]}")
        print()
        results_summary.append({
            "task"  : task[:40],
            "skill" : result.skill_name,
            "method": result.match_method,
            "conf"  : result.confidence,
        })

    # Verificar resultados esperados
    expected = {
        "Crear un endpoint REST en Go": "go-industrial-expert",
        "disenar un dashboard HTML": "html-dashboard-expert",
        "agregar autenticacion con Supabase Auth": "nextjs-supabase-auth",
        "crear una app Flutter": "flutter-premium-expert",
        "quiero crear un nuevo skill": "use-skill-creator",
    }

    print("\n" + "─" * 55)
    print("  Validacion de resultados esperados:")
    all_ok = True
    for expected_task, expected_skill in expected.items():
        matched = next(
            (r for r in results_summary if expected_task.lower() in r["task"].lower()),
            None
        )
        if matched:
            ok = matched["skill"] == expected_skill
            icon = "✅" if ok else "❌"
            print(f"  {icon} '{expected_task[:35]}'")
            print(f"     esperado: {expected_skill}")
            if not ok:
                print(f"     obtenido: {matched['skill']}")
                all_ok = False
        else:
            print(f"  ❓ '{expected_task}' no encontrado en resultados")

    print()
    if all_ok:
        print("  ✅ Dispatcher funcionando correctamente")
    else:
        print("  ⚠️  Algunos resultados difieren de lo esperado (ajustar triggers/perfiles)")
