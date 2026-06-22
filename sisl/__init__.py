"""
MCUM — Motor Cerebral Ultra Multiversal
sisl/__init__.py — Exportaciones del SISL (Self-Improving Skill Loop)
"""

from .test_generator import (
    generate_tests_for_skill,
    save_tests_to_db,
    generate_and_save,
    get_test_suite,
    list_skill_test_counts,
)
from .skill_bootstrap import (
    bootstrap_skill_from_doc,
    derive_bootstrap_payload,
)
from .test_runner import (
    run_evaluation,
    save_eval_to_db,
    SkillEvalResult,
    TestResult,
)
from .optimizer import (
    run_sisl_cycle,
    analyze_and_propose,
    save_optimization_report,
    apply_high_confidence_improvements,
    OptimizationReport,
    ImprovementProposal,
)
from .autonomous_loop import (
    AutonomousLoopConfig,
    get_skill_loop_stats,
    get_skills_for_evaluation,
    resolve_writeback_mode,
    run_autonomous_improvement,
    run_workspace_improvement_cycle,
)

__all__ = [
    # Test Generator
    "generate_tests_for_skill", "save_tests_to_db",
    "generate_and_save", "get_test_suite", "list_skill_test_counts",
    # Bootstrap
    "bootstrap_skill_from_doc", "derive_bootstrap_payload",
    # Test Runner
    "run_evaluation", "save_eval_to_db", "SkillEvalResult", "TestResult",
    # Optimizer
    "run_sisl_cycle", "analyze_and_propose", "save_optimization_report",
    "apply_high_confidence_improvements", "OptimizationReport", "ImprovementProposal",
    # Autonomous loop
    "AutonomousLoopConfig", "get_skill_loop_stats", "get_skills_for_evaluation",
    "resolve_writeback_mode",
    "run_autonomous_improvement", "run_workspace_improvement_cycle",
]
