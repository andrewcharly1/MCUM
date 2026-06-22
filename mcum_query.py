"""
MCUM - mcum_query.py
Demo interactivo: busca en la Experience Store por lenguaje natural.
Uso: python mcum_query.py "como conectar postgresql"
"""

import sys
from pathlib import Path

# Poka-Yoke: Asegurar que tanto el directorio MCUM como su padre estén en sys.path
# Esto resuelve inconsistencias de importación entre scripts individuales y ejecución como módulos
_mcum_dir = Path(__file__).parent.resolve()
if str(_mcum_dir) not in sys.path:
    sys.path.insert(0, str(_mcum_dir))
_parent_dir = _mcum_dir.parent.resolve()
if str(_parent_dir) not in sys.path:
    sys.path.insert(0, str(_parent_dir))

from dotenv import load_dotenv

load_dotenv(_mcum_dir / ".env")

from MCUM.db.experience_store import retrieve_for_task  # noqa: E402
from MCUM.db.project_registry import list_projects  # noqa: E402


def demo_query(query: str) -> None:
    print("\nMCUM Semantic Search")
    print("-" * 50)
    print(f"Query: '{query}'")

    context = retrieve_for_task(query)
    mode = context["retrieval_mode"]
    total = context["total_retrieved"]

    print(f"Modo: {mode} | Total: {total} resultado(s)\n")

    if context["experiences"]:
        print("Experiences relevantes:")
        for exp in context["experiences"]:
            sim = exp.get("_similarity", exp.get("_combined_score", 0))
            conf = exp.get("current_confidence", 0)
            print(f"  [{exp['category'].upper()[:12]}] {exp['title'][:55]}")
            print(f"   score={sim:.3f}  confianza={conf:.2f}  skill={exp.get('skill_name', '?')}")

            content = exp.get("content")
            if content and isinstance(content, dict):
                conclusion = content.get("conclusion", "")
                if conclusion:
                    print(f"   -> {conclusion[:100]}")
            print()

    if context["failure_patterns"]:
        print("Failure Patterns (riesgos a considerar):")
        for fp in context["failure_patterns"]:
            print(f"  {fp['title'][:60]}")

    if total == 0:
        print("Cold start: sin experiences previas para esta query.")
        print("El sistema aprendera con el uso.")


def show_projects() -> None:
    print("\nProyectos en el catalogo MCUM:")
    projects = list_projects(status="all")
    for project in projects:
        print(f"  [{project.get('status', '?').upper()}] {project['project_name']}")
        print(f"   {project.get('project_path', '')}")
        print(
            f"   sesiones={project.get('total_sessions', 0)} "
            f"tareas={project.get('total_tasks_completed', 0)}"
        )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        demo_query(" ".join(sys.argv[1:]))
    else:
        print("MCUM - Motor Cerebral Ultra Multiversal")
        print("Uso: python mcum_query.py 'tu consulta aqui'")
        print("Demo con query de ejemplo:")
        demo_query("conectar postgresql python windows")
        show_projects()
