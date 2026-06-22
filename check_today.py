from __future__ import annotations

import sys
from pathlib import Path


SKILLS_ROOT = Path(__file__).resolve().parent.parent
if str(SKILLS_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILLS_ROOT))

from MCUM.db.connection import get_db, get_cursor


def _format_outcome(outcome: str | None) -> str:
    normalized = str(outcome or "").strip().lower()
    if normalized == "success":
        return "EXITO"
    if normalized == "partial":
        return "PARCIAL"
    if normalized == "failure":
        return "FALLA"
    return "SIN RESULTADO"


def fetch_today_task_logs() -> list[dict]:
    with get_db() as conn:
        with get_cursor(conn) as cur:
            cur.execute(
                """
                SELECT
                    pl.title,
                    pl.skill_used,
                    pl.outcome,
                    pl.task_wall_clock_ms,
                    p.project_name,
                    pl.created_at
                FROM project_registry.project_logs pl
                LEFT JOIN project_registry.projects p ON pl.project_id = p.id
                WHERE pl.created_at >= CURRENT_DATE
                  AND pl.log_type = 'task'
                ORDER BY pl.created_at DESC
                """
            )
            return cur.fetchall()


def check_today() -> list[dict]:
    rows = fetch_today_task_logs()

    print("--- INTERACCIONES DEL MCUM DE HOY ---")
    print(f"Total de tareas ejecutadas hoy: {len(rows)}\n")

    for index, row in enumerate(rows, 1):
        elapsed_ms = row.get("task_wall_clock_ms")
        elapsed_render = f"{elapsed_ms} ms" if elapsed_ms is not None else "N/A"
        print(f"{index}. Proyecto: {row.get('project_name') or 'N/A'}")
        print(f"   Hora: {row.get('created_at')}")
        print(f"   Tarea: {row.get('title')}")
        print(f"   Agente Delegado (Skill): {row.get('skill_used') or 'N/A'}")
        print(f"   Resultado: {_format_outcome(row.get('outcome'))} (Tiempo: {elapsed_render})")
        print("-" * 50)

    return rows


if __name__ == "__main__":
    check_today()
