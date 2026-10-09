from __future__ import annotations

import copy

from devagent.controls.fat import (
    evaluate_standard_state,
    generate_controls_fat,
    run_model_simulation,
)
from devagent.controls.ignition import (
    generate_ignition_payloads,
    ignition_binding_check,
    ignition_semantic_projection,
    normalize_ignition_projection,
)
from devagent.controls.ir import build_controls_ir
from devagent.controls.schema import parse_control_system_payload


def _ir():
    spec = parse_control_system_payload(
        {
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
                    "signals": ["SAFE", "GUARD_OPEN", "DRIVE_FAULT", "RUN_FB"],
                    "commands": {"START": True, "STOP": True, "RESET": True},
                    "status": ["READY", "RUNNING", "FAULTED"],
                    "permissives": ["SAFE"],
                    "interlocks": ["GUARD_OPEN", "DRIVE_FAULT"],
                    "faults": ["DRIVE_FAULT"],
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
                            "assertion": {
                                "conditions": {
                                    "COMMAND.START": True,
                                    "SIGNAL.GUARD_OPEN": True,
                                },
                                "expect": {"OUTPUT.RUN": False},
                            },
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
    assert instance["interlocks"] == ["DRIVE_FAULT", "GUARD_OPEN"]
    assert instance["faults"] == ["DRIVE_FAULT"]
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
    requirement_cases = [
        case for case in cases if "REQ_GUARD" in case.requirement_ids
    ]
    assert len(requirement_cases) == 1
    assert requirement_cases[0].method == "STRUCTURED_REQUIREMENT"
    assert any(case.expected_output == "ALARM.ALM_CONV101_DRIVE" for case in cases)
    assert any(
        case.expected_output == "STATUS.FAULTED"
        and dict(case.inputs).get("SIGNAL.DRIVE_FAULT") is True
        for case in cases
    )
    assert simulation["status"] == "PASS"
    assert simulation["vendor_runtime_execution"] == "NOT_RUN"


def test_ignition_semantic_projection_is_order_independent() -> None:
    payloads = generate_ignition_payloads(_ir())
    expected = ignition_semantic_projection(payloads)
    reordered = copy.deepcopy(expected)
    for name in (
        "udt_definitions",
        "instances",
        "alarms",
        "history",
        "views",
        "navigation",
    ):
        reordered[name].reverse()

    assert normalize_ignition_projection(reordered) == expected


def test_nonfault_interlock_inhibits_run_without_asserting_faulted() -> None:
    ir = _ir()
    item = ir.equipment[0]
    inputs = {
        "COMMAND.START": True,
        "COMMAND.STOP": False,
        "COMMAND.RESET": False,
        "SIGNAL.SAFE": True,
        "SIGNAL.GUARD_OPEN": True,
        "SIGNAL.DRIVE_FAULT": False,
    }

    state = evaluate_standard_state(item, inputs)

    assert state["OUTPUT.RUN"] is False
    assert state["STATUS.FAULTED"] is False


def test_explicit_fault_inhibits_run_and_asserts_faulted() -> None:
    ir = _ir()
    item = ir.equipment[0]
    inputs = {
        "COMMAND.START": True,
        "COMMAND.STOP": False,
        "COMMAND.RESET": False,
        "SIGNAL.SAFE": True,
        "SIGNAL.GUARD_OPEN": False,
        "SIGNAL.DRIVE_FAULT": True,
    }

    state = evaluate_standard_state(item, inputs)

    assert state["OUTPUT.RUN"] is False
    assert state["STATUS.FAULTED"] is True


def test_model_seal_in_holds_until_stop_or_interlock() -> None:
    ir = _ir()
    item = ir.equipment[0]
    safe = {
        "COMMAND.START": False,
        "COMMAND.STOP": False,
        "COMMAND.RESET": False,
        "SIGNAL.SAFE": True,
        "SIGNAL.GUARD_OPEN": False,
        "SIGNAL.DRIVE_FAULT": False,
        "SIGNAL.RUN_FB": False,
    }
    held = evaluate_standard_state(
        item,
        safe,
        prior_state={"OUTPUT.RUN": True},
    )
    assert held["OUTPUT.RUN"] is True

    stopped_inputs = dict(safe)
    stopped_inputs["COMMAND.STOP"] = True
    stopped = evaluate_standard_state(
        item,
        stopped_inputs,
        prior_state={"OUTPUT.RUN": True},
    )
    assert stopped["OUTPUT.RUN"] is False

    interlocked_inputs = dict(safe)
    interlocked_inputs["SIGNAL.GUARD_OPEN"] = True
    interlocked = evaluate_standard_state(
        item,
        interlocked_inputs,
        prior_state={"OUTPUT.RUN": True},
    )
    assert interlocked["OUTPUT.RUN"] is False


def test_running_status_comes_from_feedback_not_command_output() -> None:
    ir = _ir()
    item = ir.equipment[0]
    inputs = {
        "COMMAND.START": True,
        "COMMAND.STOP": False,
        "COMMAND.RESET": False,
        "SIGNAL.SAFE": True,
        "SIGNAL.GUARD_OPEN": False,
        "SIGNAL.DRIVE_FAULT": False,
        "SIGNAL.RUN_FB": False,
    }
    state = evaluate_standard_state(item, inputs)
    assert state["OUTPUT.RUN"] is True
    assert state["STATUS.RUNNING"] is False

    inputs["SIGNAL.RUN_FB"] = True
    state = evaluate_standard_state(item, inputs)
    assert state["STATUS.RUNNING"] is True
