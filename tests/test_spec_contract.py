from __future__ import annotations

from MCUM.core.spec_contract import build_spec_contract, normalize_spec_policy


def test_spec_contract_full_mode_for_create_task() -> None:
    brief = {
        "project_path": "C:/tmp/project",
        "task": "Crear una landing con autenticacion",
        "task_type": "crear",
        "objective": "Crear una landing funcional",
        "expected_deliverable": "HTML y CSS validados",
        "success_criteria": "La landing renderiza sin errores",
        "execution_mode": "ejecutar",
        "risk_level": "medio",
        "validation_required": "Abrir en navegador y revisar estructura",
        "sources_to_review": ["index.html"],
        "constraints": ["No tocar backend"],
    }

    contract = build_spec_contract(brief, task_id="task-1")

    assert contract["enabled"] is True
    assert contract["mode"] == "full"
    assert contract["status"] == "auto_generated"
    assert any(item["code"] == "AC-003" for item in contract["acceptance_criteria"])
    assert any(item["kind"] == "anti_loop" for item in contract["scenarios"])
    assert contract["summary"]["clarification_count"] == 0
    assert contract["confidence_score"] >= 0.75


def test_spec_contract_lite_mode_for_analysis_task() -> None:
    brief = {
        "project_path": "C:/tmp/project",
        "task": "Analizar deuda tecnica",
        "task_type": "analizar",
        "objective": "Analizar deuda tecnica del modulo",
        "expected_deliverable": "Reporte breve",
        "success_criteria": "Reporte con hallazgos priorizados",
        "execution_mode": "analizar",
        "risk_level": "bajo",
        "validation_required": "Referencias a archivos revisados",
    }

    contract = build_spec_contract(brief, task_id="task-2")

    assert contract["mode"] == "lite"
    assert not any(item["code"] == "AC-003" for item in contract["acceptance_criteria"])
    assert any(item["code"] == "A-003" for item in contract["assumptions"])
    assert any(item["code"] == "Q-004" for item in contract["clarification_questions"])


def test_normalize_spec_policy_can_disable_persistence() -> None:
    policy = normalize_spec_policy({"spec_contract": {"persist": False, "min_score": 0.7}})

    assert policy["enabled"] is True
    assert policy["persist"] is False
    assert policy["min_score"] == 0.7
