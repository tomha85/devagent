from __future__ import annotations

from devagent.controls.rules import evaluate_controls_rules, rules_pass
from devagent.controls.schema import parse_control_system_payload


def _payload():
    return {
        "schema": "devagent-controls-spec-v2",
        "project_id": "PACK01",
        "controllers": [
            {"id": "PLC1", "vendor": "ROCKWELL", "platform": "CONTROLLOGIX"}
        ],
        "equipment": [
            {
                "id": "CONV_101",
                "type": "CONVEYOR",
                "standard": "conveyor-v1",
                "controller": "PLC1",
                "signals": ["SAFE", "DRIVE_FAULT"],
                "commands": {"START": True, "STOP": True, "RESET": True},
                "status": ["READY", "RUNNING", "FAULTED"],
                "permissives": ["SAFE"],
                "interlocks": ["DRIVE_FAULT"],
                "faults": ["DRIVE_FAULT"],
                "alarms": [
                    {
                        "id": "ALM_DRIVE",
                        "priority": "HIGH",
                        "operator_response": "Inspect drive.",
                        "source_signal": "DRIVE_FAULT",
                    }
                ],
                "hmi": {"faceplate": "conveyor-v1", "historian": True},
                "requirements": [],
            }
        ],
    }


def test_complete_standard_contract_passes() -> None:
    results = evaluate_controls_rules(parse_control_system_payload(_payload()))
    assert rules_pass(results)
    assert not [item for item in results if item.status == "FAIL"]


def test_missing_required_reset_fails_company_standard() -> None:
    payload = _payload()
    payload["equipment"][0]["commands"]["RESET"] = False
    results = evaluate_controls_rules(parse_control_system_payload(payload))
    failures = [item for item in results if item.status == "FAIL"]
    assert any(item.id == "CTRL-E100" and "RESET" in item.summary for item in failures)


def test_any_generated_alarm_without_source_fails_company_standard() -> None:
    payload = _payload()
    payload["equipment"][0]["alarms"][0]["priority"] = "MEDIUM"
    del payload["equipment"][0]["alarms"][0]["source_signal"]
    results = evaluate_controls_rules(parse_control_system_payload(payload))
    failures = [item for item in results if item.status == "FAIL"]
    assert any(item.id == "CTRL-E500" for item in failures)


def test_missing_faceplate_fails_generated_hmi_contract() -> None:
    payload = _payload()
    payload["equipment"][0]["hmi"]["faceplate"] = None
    results = evaluate_controls_rules(parse_control_system_payload(payload))
    failures = [item for item in results if item.status == "FAIL"]
    assert any(item.id == "CTRL-E410" for item in failures)


def test_faceplate_drift_fails_company_standard() -> None:
    payload = _payload()
    payload["equipment"][0]["hmi"]["faceplate"] = "custom-screen-v1"
    results = evaluate_controls_rules(parse_control_system_payload(payload))
    failures = [item for item in results if item.status == "FAIL"]
    assert any(item.id == "CTRL-E410" for item in failures)


def test_faulted_standard_without_interlock_source_fails_closed() -> None:
    payload = _payload()
    payload["equipment"][0]["interlocks"] = []
    results = evaluate_controls_rules(parse_control_system_payload(payload))
    failures = [item for item in results if item.status == "FAIL"]
    assert any(item.id == "CTRL-E320" for item in failures)


def test_missing_permissive_fails_explicit_company_rule() -> None:
    payload = _payload()
    payload["equipment"][0]["permissives"] = []
    results = evaluate_controls_rules(parse_control_system_payload(payload))
    failures = [item for item in results if item.status == "FAIL"]
    assert any(item.id == "CTRL-E310" for item in failures)


def test_high_requirement_without_structured_assertion_fails() -> None:
    payload = _payload()
    payload["equipment"][0]["requirements"] = [
        {
            "id": "REQ_GUARD",
            "text": "Conveyor must not run with the fault active.",
            "criticality": "HIGH",
        }
    ]
    results = evaluate_controls_rules(parse_control_system_payload(payload))
    failures = [item for item in results if item.status == "FAIL"]
    assert any(item.id == "CTRL-E600" for item in failures)


def test_high_requirement_with_structured_assertion_passes_requirement_rule() -> None:
    payload = _payload()
    payload["equipment"][0]["requirements"] = [
        {
            "id": "REQ_FAULT",
            "text": "Conveyor must not run with drive fault active.",
            "criticality": "HIGH",
            "assertion": {
                "conditions": {
                    "COMMAND.START": True,
                    "SIGNAL.DRIVE_FAULT": True,
                },
                "expect": {"OUTPUT.RUN": False},
            },
        }
    ]
    results = evaluate_controls_rules(parse_control_system_payload(payload))
    assert not [
        item for item in results
        if item.id == "CTRL-E600" and item.status == "FAIL"
    ]


def test_missing_explicit_fault_fails_company_standard() -> None:
    payload = _payload()
    payload["equipment"][0]["faults"] = []
    results = evaluate_controls_rules(parse_control_system_payload(payload))
    failures = [item for item in results if item.status == "FAIL"]
    assert any(item.id == "CTRL-E325" for item in failures)


def test_declared_fault_requires_alarm_coverage() -> None:
    payload = _payload()
    payload["equipment"][0]["alarms"][0]["source_signal"] = "SAFE"
    results = evaluate_controls_rules(parse_control_system_payload(payload))
    failures = [item for item in results if item.status == "FAIL"]
    assert any(item.id == "CTRL-E331" for item in failures)
