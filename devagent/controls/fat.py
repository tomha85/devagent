from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from devagent.controls.catalog import get_standard
from devagent.controls.ir import ControlsIR
from devagent.controls.models import EquipmentSpec


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


def _base_inputs(item: EquipmentSpec, action: str) -> dict[str, bool]:
    standard = get_standard(item.standard)
    result: dict[str, bool] = {
        f"COMMAND.{name}": False for name, _enabled in item.commands
    }
    result[f"COMMAND.{action}"] = True
    if standard.stop_action is not None and standard.stop_action != action:
        result[f"COMMAND.{standard.stop_action}"] = False
    for name in item.permissives:
        result[f"SIGNAL.{name}"] = True
    for name in item.interlocks:
        result[f"SIGNAL.{name}"] = False
    return result


def _case(
    item: EquipmentSpec,
    *,
    suffix: str,
    title: str,
    action: str,
    inputs: dict[str, bool],
    output: str,
    expected: bool,
) -> ControlsFATCase:
    return ControlsFATCase(
        id=f"FAT-{item.id}-{suffix}",
        equipment_id=item.id,
        title=title,
        action=action,
        inputs=tuple(sorted(inputs.items())),
        expected_output=f"OUTPUT.{output}",
        expected_value=expected,
        requirement_ids=tuple(sorted(requirement.id for requirement in item.requirements)),
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
                output="RUN",
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
                    output="RUN",
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
                    output="RUN",
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
                    output="RUN",
                    expected=False,
                )
            )
        if "RESET" in standard.generated_outputs:
            reset_inputs = {
                f"COMMAND.{name}": name == "RESET"
                for name, _enabled in item.commands
            }
            cases.append(
                _case(
                    item,
                    suffix="RESET",
                    title=f"{item.id} reset output follows reset command",
                    action="RESET",
                    inputs=reset_inputs,
                    output="RESET",
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
                    output=action,
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
                    output=action,
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
                        output=action,
                        expected=False,
                    )
                )
    return tuple(cases)


def generate_controls_fat(ir: ControlsIR) -> tuple[ControlsFATCase, ...]:
    return tuple(case for item in ir.equipment for case in generate_equipment_fat(item))


def evaluate_standard_model(item: EquipmentSpec, action: str, inputs: dict[str, bool]) -> dict[str, bool]:
    standard = get_standard(item.standard)
    outputs = {name: False for name in standard.generated_outputs}

    if action == "RESET" and "RESET" in outputs:
        outputs["RESET"] = bool(inputs.get("COMMAND.RESET", False))
        return outputs

    command_on = bool(inputs.get(f"COMMAND.{action}", False))
    stop_action: str | None
    if standard.equipment_type == "VALVE":
        stop_action = "CLOSE" if action == "OPEN" else "OPEN"
    else:
        stop_action = standard.stop_action
    stop_clear = True if stop_action is None else not bool(
        inputs.get(f"COMMAND.{stop_action}", False)
    )
    permissives_ok = all(
        bool(inputs.get(f"SIGNAL.{name}", False)) for name in item.permissives
    )
    interlocks_clear = all(
        not bool(inputs.get(f"SIGNAL.{name}", False)) for name in item.interlocks
    )

    output_name = "RUN" if standard.equipment_type != "VALVE" else action
    outputs[output_name] = command_on and stop_clear and permissives_ok and interlocks_clear
    return outputs


def run_model_simulation(ir: ControlsIR, cases: tuple[ControlsFATCase, ...]) -> dict[str, Any]:
    equipment = {item.id: item for item in ir.equipment}
    results: list[dict[str, Any]] = []
    for case in cases:
        item = equipment[case.equipment_id]
        observed = evaluate_standard_model(item, case.action, dict(case.inputs))
        output = case.expected_output.split(".", 1)[1]
        actual = bool(observed.get(output, False))
        results.append(
            {
                "test_id": case.id,
                "status": "PASS" if actual == case.expected_value else "FAIL",
                "expected": case.expected_value,
                "observed": actual,
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
        "schema": "devagent-controls-fat-plan-v1",
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
