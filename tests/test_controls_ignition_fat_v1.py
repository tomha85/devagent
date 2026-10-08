from __future__ import annotations

from devagent.controls.fat import generate_controls_fat, run_model_simulation
from devagent.controls.ignition import generate_ignition_payloads, ignition_binding_check
from devagent.controls.ir import build_controls_ir
from devagent.controls.schema import parse_control_system_payload


def _ir():
    spec = parse_control_system_payload(
        {
            "schema": "devagent-controls-spec-v1",
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
                    "signals": ["SAFE", "GUARD_OPEN", "DRIVE_FAULT"],
                    "commands": {"START": True, "STOP": True, "RESET": True},
                    "status": ["READY", "RUNNING", "FAULTED"],
                    "permissives": ["SAFE"],
                    "interlocks": ["GUARD_OPEN", "DRIVE_FAULT"],
                    "alarms": [
                        {
                            "id": "ALM_CONV101_DRIVE",
                            "priority": "HIGH",
                            "operator_response": "Inspect drive.",
                            "source_signal": "DRIVE_FAULT",
                        }
                    ],
                    "hmi": {"faceplate": "conveyor-v1", "historian": True},
                    "requirements": [
                        {
                            "id": "REQ_GUARD",
                            "text": "CONV_101 must not run with guard open.",
                            "criticality": "HIGH",
                        }
                    ],
                }
            ],
        }
    )
    return build_controls_ir(spec)


def test_ignition_staging_uses_same_equipment_identity_and_bound_alarm() -> None:
    ir = _ir()
    payloads = generate_ignition_payloads(ir)
    instance = payloads["equipment.json"]["instances"][0]
    alarm = payloads["alarms.json"]["alarms"][0]

    assert instance["id"] == "CONV_101"
    assert instance["path"] == "Equipment/CONV_101"
    assert alarm["equipment_id"] == "CONV_101"
    assert alarm["binding_status"] == "BOUND"
    assert alarm["plc_tag"] == instance["plc_tags"]["signals"]["DRIVE_FAULT"]
    assert ignition_binding_check(ir, payloads)["status"] == "PASS"


def test_generated_fat_remains_not_run_but_model_cases_are_self_consistent() -> None:
    ir = _ir()
    cases = generate_controls_fat(ir)
    simulation = run_model_simulation(ir, cases)

    assert cases
    assert all(case.execution_status == "NOT_RUN" for case in cases)
    assert all("REQ_GUARD" in case.requirement_ids for case in cases)
    assert simulation["status"] == "PASS"
    assert simulation["vendor_runtime_execution"] == "NOT_RUN"
