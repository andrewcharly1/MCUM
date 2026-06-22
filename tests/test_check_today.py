from __future__ import annotations

from datetime import datetime, timezone

from MCUM import check_today


class _CursorStub:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = [dict(row) for row in rows]
        self.executed: list[tuple[str, object]] = []

    def execute(self, query: str, params: object = None) -> None:
        self.executed.append((query, params))

    def fetchall(self) -> list[dict]:
        return [dict(row) for row in self._rows]


class _CursorManager:
    def __init__(self, cursor: _CursorStub) -> None:
        self._cursor = cursor

    def __enter__(self) -> _CursorStub:
        return self._cursor

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class _ConnManager:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_fetch_today_task_logs_uses_current_project_log_columns(monkeypatch) -> None:
    cursor = _CursorStub(
        [
            {
                "title": "Tarea de prueba",
                "skill_used": "mcum-orchestrator",
                "outcome": "success",
                "task_wall_clock_ms": 1234,
                "project_name": "MCUM",
                "created_at": datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc),
            }
        ]
    )
    monkeypatch.setattr(check_today, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(check_today, "get_cursor", lambda conn: _CursorManager(cursor))

    rows = check_today.fetch_today_task_logs()

    assert len(rows) == 1
    assert rows[0]["title"] == "Tarea de prueba"
    query, params = cursor.executed[0]
    assert "pl.title" in query
    assert "pl.skill_used" in query
    assert "pl.outcome" in query
    assert "pl.log_type = 'task'" in query
    assert params is None


def test_check_today_renders_current_outcomes(capsys, monkeypatch) -> None:
    cursor = _CursorStub(
        [
            {
                "title": "Tarea exitosa",
                "skill_used": "mcum-orchestrator",
                "outcome": "success",
                "task_wall_clock_ms": 1500,
                "project_name": "MCUM",
                "created_at": datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc),
            },
            {
                "title": "Tarea parcial",
                "skill_used": "validator-skill",
                "outcome": "partial",
                "task_wall_clock_ms": None,
                "project_name": "Proyecto X",
                "created_at": datetime(2026, 4, 2, 11, 30, tzinfo=timezone.utc),
            },
            {
                "title": "Tarea fallida",
                "skill_used": "validator-skill",
                "outcome": "failure",
                "task_wall_clock_ms": 2100,
                "project_name": None,
                "created_at": datetime(2026, 4, 2, 11, 0, tzinfo=timezone.utc),
            },
        ]
    )
    monkeypatch.setattr(check_today, "get_db", lambda: _ConnManager())
    monkeypatch.setattr(check_today, "get_cursor", lambda conn: _CursorManager(cursor))

    rows = check_today.check_today()
    captured = capsys.readouterr().out

    assert len(rows) == 3
    assert "Total de tareas ejecutadas hoy: 3" in captured
    assert "Resultado: EXITO (Tiempo: 1500 ms)" in captured
    assert "Resultado: PARCIAL (Tiempo: N/A)" in captured
    assert "Resultado: FALLA (Tiempo: 2100 ms)" in captured
    assert "Proyecto: N/A" in captured
