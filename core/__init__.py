"""
MCUM — Motor Cerebral Ultra Multiversal
core/__init__.py — Exportaciones del modulo core
"""

from .dispatcher import (
    dispatch,
    DispatchResult,
    get_skill_profile,
    list_available_skills,
    SKILL_REGISTRY,
)
from .session_manager import (
    OrchestratorSession,
    TaskContext,
    TaskResult,
    quick_dispatch,
)
from .skill_factory import (
    apply_dispatch_hints,
    bootstrap_candidate_skill,
    collect_dispatch_hints,
    collect_skill_gap_signals,
    evaluate_candidate_promotion,
    run_skill_factory_cycle,
)

__all__ = [
    # Dispatcher
    "dispatch", "DispatchResult",
    "get_skill_profile", "list_available_skills", "SKILL_REGISTRY",
    # Session Manager
    "OrchestratorSession", "TaskContext", "TaskResult", "quick_dispatch",
    # Skill factory
    "apply_dispatch_hints", "bootstrap_candidate_skill", "collect_dispatch_hints",
    "collect_skill_gap_signals",
    "evaluate_candidate_promotion", "run_skill_factory_cycle",
]
