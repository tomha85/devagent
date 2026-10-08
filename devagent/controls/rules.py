from __future__ import annotations

from dataclasses import dataclass

from devagent.controls.catalog import get_standard
from devagent.controls.models import ControlSystemSpec, EquipmentSpec


@dataclass(frozen=True)
class ControlsRuleResult:
    id: str
    status: str
    subject: str
    summary: str


def _equipment_rules(item: EquipmentSpec) -> list[ControlsRuleResult]:
    standard = get_standard(item.standard)
    commands = {name: enabled for name, enabled in item.commands}
    status = set(item.status)
    results: list[ControlsRuleResult] = []

    missing_commands = [
        name for name in standard.required_commands if not commands.get(name, False)
    ]
    results.append(
        ControlsRuleResult(
            id="CTRL-E100",
            status="FAIL" if missing_commands else "PASS",
            subject=item.id,
            summary=(
                "Missing or disabled required command(s): " + ", ".join(missing_commands)
                if missing_commands
                else f"Required command contract satisfied for {standard.id}."
            ),
        )
    )

    missing_status = [name for name in standard.required_status if name not in status]
    results.append(
        ControlsRuleResult(
            id="CTRL-E110",
            status="FAIL" if missing_status else "PASS",
            subject=item.id,
            summary=(
                "Missing required status member(s): " + ", ".join(missing_status)
                if missing_status
                else f"Required status contract satisfied for {standard.id}."
            ),
        )
    )

    if "FAULTED" in standard.required_status:
        results.append(
            ControlsRuleResult(
                id="CTRL-E320",
                status="PASS" if item.interlocks else "FAIL",
                subject=item.id,
                summary=(
                    "At least one explicit interlock/fault source is available for "
                    "the generated FAULTED status."
                    if item.interlocks
                    else "Standard requires FAULTED status but no interlock/fault "
                    "source was declared."
                ),
            )
        )

    overlap = sorted(set(item.permissives) & set(item.interlocks))
    results.append(
        ControlsRuleResult(
            id="CTRL-E310",
            status="FAIL" if overlap else "PASS",
            subject=item.id,
            summary=(
                "Signal(s) cannot be both permissive and interlock: " + ", ".join(overlap)
                if overlap
                else "Permissive/interlock ownership is unambiguous."
            ),
        )
    )

    if item.hmi.faceplate is None:
        faceplate_status = "FAIL"
        faceplate_summary = (
            f"V1 generated HMI requires explicit qualified faceplate "
            f"{standard.default_faceplate!r}; no faceplate was selected."
        )
    elif item.hmi.faceplate != standard.default_faceplate:
        faceplate_status = "FAIL"
        faceplate_summary = (
            f"Faceplate {item.hmi.faceplate!r} does not match qualified standard "
            f"{standard.default_faceplate!r}."
        )
    else:
        faceplate_status = "PASS"
        faceplate_summary = f"Qualified faceplate {item.hmi.faceplate} selected."
    results.append(
        ControlsRuleResult(
            id="CTRL-E410",
            status=faceplate_status,
            subject=item.id,
            summary=faceplate_summary,
        )
    )

    for alarm in item.alarms:
        if alarm.source_signal is None:
            alarm_status = "FAIL"
            alarm_summary = (
                f"Alarm {alarm.id} requires an explicit source_signal for deterministic "
                "PLC/HMI binding."
            )
        else:
            alarm_status = "PASS"
            alarm_summary = (
                f"Alarm {alarm.id} is explicitly bound to signal {alarm.source_signal}."
            )
        results.append(
            ControlsRuleResult(
                id="CTRL-E500",
                status=alarm_status,
                subject=f"{item.id}:{alarm.id}",
                summary=alarm_summary,
            )
        )

    return results


def evaluate_controls_rules(spec: ControlSystemSpec) -> tuple[ControlsRuleResult, ...]:
    results: list[ControlsRuleResult] = []
    assigned = {item.controller for item in spec.equipment}
    for controller in spec.controllers:
        results.append(
            ControlsRuleResult(
                id="CTRL-E020",
                status="PASS" if controller.id in assigned else "FAIL",
                subject=controller.id,
                summary=(
                    "Controller has at least one assigned equipment object."
                    if controller.id in assigned
                    else "Controller has no assigned equipment; empty generated projects are prohibited."
                ),
            )
        )
    for item in spec.equipment:
        results.extend(_equipment_rules(item))
    return tuple(results)


def rules_pass(results: tuple[ControlsRuleResult, ...]) -> bool:
    return all(item.status != "FAIL" for item in results)
