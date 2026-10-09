from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from devagent.controls.catalog import get_standard
from devagent.controls.ir import ControlsIR
from devagent.controls.models import EquipmentSpec, RequirementSpec

FAT_GENERATOR_VERSION = "2.0.0"


@dataclass(frozen=True)
class ControlsFATCase:
    id: str
    equipment_id: str
    title: str
    action: str
    inputs: tuple[tuple[str, bool], ...]
    expected_output: str
    expected_value: bool
    requirement_ids: tuple[str, ...]
    execution_status: str = "NOT_RUN"
    method: str = "DETERMINISTIC_STANDARD_MODEL"


def _safe_baseline_inputs(item: EquipmentSpec) -> dict[str, bool]:
    result: dict[str, bool] = {
        f"COMMAND.{name}": False for name, _enabled in item.commands
    }
    for name in item.permissives:
        result[f"SIGNAL.{name}"] = True
    for name in item.interlocks:
        result[f"SIGNAL.{name}"] = False
    for name in item.signals:
        result.setdefault(f"SIGNAL.{name}", False)
    return result


def _base_inputs(item: EquipmentSpec, action: str) -> dict[str, bool]:
    result = _safe_baseline_inputs(item)
    result[f"COMMAND.{action}"] = True
    standard = get_standard(item.standard)
    if standard.stop_action is not None and standard.stop_action != action:
        result[f"COMMAND.{standard.stop_action}"] = False
    return result


def _case(
    item: EquipmentSpec,
    *,
    suffix: str,
    title: str,
    action: str,
    inputs: dict[str, bool],
    expected_output: str,
    expected: bool,
    requirement_ids: tuple[str, ...] = (),
    method: str = "DETERMINISTIC_STANDARD_MODEL",
) -> ControlsFATCase:
    return ControlsFATCase(
        id=f"FAT-{item.id}-{suffix}",
        equipment_id=item.id,
        title=title,
        action=action,
        inputs=tuple(sorted(inputs.items())),
        expected_output=expected_output,
        expected_value=expected,
        requirement_ids=tuple(sorted(requirement_ids)),
        method=method,
    )


def _structured_requirement_case(
    item: EquipmentSpec,
    requirement: RequirementSpec,
) -> ControlsFATCase | None:
    assertion = requirement.assertion
    if assertion is None:
        return None
    inputs = _safe_baseline_inputs(item)
    inputs.update(dict(assertion.conditions))
    true_commands = sorted(
        ref.split(".", 1)[1]
        for ref, value in assertion.conditions
        if ref.startswith("COMMAND.") and value
    )
    action = true_commands[0] if true_commands else "ASSERT"
    return _case(
        item,
        suffix=f"REQ-{requirement.id}",
        title=f"{item.id} requirement {requirement.id}",
        action=action,
        inputs=inputs,
        expected_output=assertion.expected_ref,
        expected=assertion.expected_value,
        requirement_ids=(requirement.id,),
        method="STRUCTURED_REQUIREMENT",
    )


def generate_equipment_fat(item: EquipmentSpec) -> tuple[ControlsFATCase, ...]:
    standard = get_standard(item.standard)
    cases: list[ControlsFATCase] = []

    if standard.equipment_type in {"MOTOR", "VFD", "CONVEYOR"}:
        action = standard.primary_action
        base = _base_inputs(item, action)
        cases.append(
            _case(
                item,
                suffix="PRIMARY-POSITIVE",
                title=f"{item.id} primary run path",
                action=action,
                inputs=base,
                expected_output="OUTPUT.RUN",
                expected=True,
            )
        )
        if standard.stop_action is not None:
            stopped = dict(base)
            stopped[f"COMMAND.{standard.stop_action}"] = True
            cases.append(
                _case(
                    item,
                    suffix="STOP-INHIBIT",
                    title=f"{item.id} stop command inhibits run output",
                    action=action,
                    inputs=stopped,
                    expected_output="OUTPUT.RUN",
                    expected=False,
                )
            )
        for name in item.permissives:
            blocked = dict(base)
            blocked[f"SIGNAL.{name}"] = False
            cases.append(
                _case(
                    item,
                    suffix=f"PERMISSIVE-{name}-FALSE",
                    title=f"{item.id} requires permissive {name}",
                    action=action,
                    inputs=blocked,
                    expected_output="OUTPUT.RUN",
                    expected=False,
                )
            )
        for name in item.interlocks:
            blocked = dict(base)
            blocked[f"SIGNAL.{name}"] = True
            cases.append(
                _case(
                    item,
                    suffix=f"INTERLOCK-{name}-TRUE",
                    title=f"{item.id} is inhibited by interlock {name}",
                    action=action,
                    inputs=blocked,
                    expected_output="OUTPUT.RUN",
                    expected=False,
                )
            )
        if standard.fault_status_member is not None:
            for name in item.faults:
                faulted = _safe_baseline_inputs(item)
                faulted[f"SIGNAL.{name}"] = True
                cases.append(
                    _case(
                        item,
                        suffix=f"FAULT-{name}-STATUS",
                        title=f"{item.id} fault {name} drives fault status",
                        action="FAULT_ASSERT",
                        inputs=faulted,
                        expected_output=f"STATUS.{standard.fault_status_member}",
                        expected=True,
                    )
                )
        if "RESET" in standard.generated_outputs:
            reset_inputs = _safe_baseline_inputs(item)
            reset_inputs["COMMAND.RESET"] = True
            cases.append(
                _case(
                    item,
                    suffix="RESET",
                    title=f"{item.id} reset output follows reset command",
                    action="RESET",
                    inputs=reset_inputs,
                    expected_output="OUTPUT.RESET",
                    expected=True,
                )
            )

    elif standard.equipment_type == "VALVE":
        for action, inverse in (("OPEN", "CLOSE"), ("CLOSE", "OPEN")):
            base = _base_inputs(item, action)
            base[f"COMMAND.{inverse}"] = False
            cases.append(
                _case(
                    item,
                    suffix=f"{action}-POSITIVE",
                    title=f"{item.id} {action.lower()} path",
                    action=action,
                    inputs=base,
                    expected_output=f"OUTPUT.{action}",
                    expected=True,
                )
            )
            conflict = dict(base)
            conflict[f"COMMAND.{inverse}"] = True
            cases.append(
                _case(
                    item,
                    suffix=f"{action}-CONFLICT",
                    title=f"{item.id} opposite command inhibits {action.lower()} output",
                    action=action,
                    inputs=conflict,
                    expected_output=f"OUTPUT.{action}",
                    expected=False,
                )
            )
            for name in item.interlocks:
                blocked = dict(base)
                blocked[f"SIGNAL.{name}"] = True
                cases.append(
                    _case(
                        item,
                        suffix=f"{action}-INTERLOCK-{name}",
                        title=f"{item.id} {action.lower()} is inhibited by {name}",
                        action=action,
                        inputs=blocked,
                        expected_output=f"OUTPUT.{action}",
                        expected=False,
                    )
                )

    for alarm in item.alarms:
        if alarm.source_signal is None:
            continue
        alarm_inputs = _safe_baseline_inputs(item)
        alarm_inputs[f"SIGNAL.{alarm.source_signal}"] = True
        cases.append(
            _case(
                item,
                suffix=f"ALARM-{alarm.id}",
                title=f"{item.id} alarm {alarm.id} follows {alarm.source_signal}",
                action="ALARM_ASSERT",
                inputs=alarm_inputs,
                expected_output=f"ALARM.{alarm.id}",
                expected=True,
                method="PLC_HMI_BINDING_MODEL",
            )
        )

    for requirement in item.requirements:
        generated = _structured_requirement_case(item, requirement)
        if generated is not None:
            cases.append(generated)

    return tuple(cases)


def generate_controls_fat(ir: ControlsIR) -> tuple[ControlsFATCase, ...]:
    return tuple(case for item in ir.equipment for case in generate_equipment_fat(item))


def evaluate_standard_state(
    item: EquipmentSpec,
    inputs: dict[str, bool],
) -> dict[str, bool]:
    standard = get_standard(item.standard)
    result: dict[str, bool] = {}

    commands = {
        name: bool(inputs.get(f"COMMAND.{name}", False))
        for name, _enabled in item.commands
    }
    signals = {
        name: bool(inputs.get(f"SIGNAL.{name}", False))
        for name in item.signals
    }
    permissives_ok = all(signals.get(name, False) for name in item.permissives)
    interlocks_clear = all(not signals.get(name, False) for name in item.interlocks)
    fault_active = any(signals.get(name, False) for name in item.faults)

    if standard.equipment_type in {"MOTOR", "VFD", "CONVEYOR"}:
        primary = commands.get(standard.primary_action, False)
        stop_clear = (
            True
            if standard.stop_action is None
            else not commands.get(standard.stop_action, False)
        )
        run = primary and stop_clear and permissives_ok and interlocks_clear
        result["OUTPUT.RUN"] = run
        if "RESET" in standard.generated_outputs:
            result["OUTPUT.RESET"] = commands.get("RESET", False)
        result["STATUS.READY"] = permissives_ok and interlocks_clear
        result["STATUS.RUNNING"] = run
        if standard.fault_status_member is not None:
            result[f"STATUS.{standard.fault_status_member}"] = fault_active

    elif standard.equipment_type == "VALVE":
        open_output = (
            commands.get("OPEN", False)
            and not commands.get("CLOSE", False)
            and permissives_ok
            and interlocks_clear
        )
        close_output = (
            commands.get("CLOSE", False)
            and not commands.get("OPEN", False)
            and permissives_ok
            and interlocks_clear
        )
        result["OUTPUT.OPEN"] = open_output
        result["OUTPUT.CLOSE"] = close_output
        result["STATUS.OPEN"] = open_output
        result["STATUS.CLOSED"] = close_output

    for alarm in item.alarms:
        result[f"ALARM.{alarm.id}"] = (
            False
            if alarm.source_signal is None
            else signals.get(alarm.source_signal, False)
        )

    return result


def evaluate_standard_model(
    item: EquipmentSpec,
    action: str,
    inputs: dict[str, bool],
) -> dict[str, bool]:
    """Compatibility view exposing generated OUTPUT members only."""

    state = evaluate_standard_state(item, inputs)
    return {
        ref.split(".", 1)[1]: value
        for ref, value in state.items()
        if ref.startswith("OUTPUT.")
    }


def run_model_simulation(
    ir: ControlsIR,
    cases: tuple[ControlsFATCase, ...],
) -> dict[str, Any]:
    equipment = {item.id: item for item in ir.equipment}
    results: list[dict[str, Any]] = []
    for case in cases:
        item = equipment[case.equipment_id]
        state = evaluate_standard_state(item, dict(case.inputs))
        present = case.expected_output in state
        actual = state.get(case.expected_output)
        passed = present and bool(actual) == case.expected_value
        results.append(
            {
                "test_id": case.id,
                "status": "PASS" if passed else "FAIL",
                "expected_ref": case.expected_output,
                "expected": case.expected_value,
                "observed": actual if present else None,
                "modeled_reference": present,
                "model_only": True,
            }
        )
    passed = all(item["status"] == "PASS" for item in results)
    return {
        "status": "PASS" if passed else "FAIL",
        "model_only": True,
        "vendor_runtime_execution": "NOT_RUN",
        "results": results,
    }


def fat_payload(cases: tuple[ControlsFATCase, ...]) -> dict[str, Any]:
    return {
        "schema": "devagent-controls-fat-plan-v2",
        "execution_status": "NOT_RUN",
        "execution_owner": "CONTROLS_ENGINEER",
        "cases": [
            {
                "id": case.id,
                "equipment_id": case.equipment_id,
                "title": case.title,
                "action": case.action,
                "inputs": dict(case.inputs),
                "expected_output": case.expected_output,
                "expected_value": case.expected_value,
                "requirement_ids": list(case.requirement_ids),
                "execution_status": case.execution_status,
                "method": case.method,
            }
            for case in cases
        ],
    }
