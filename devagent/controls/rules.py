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


def _result(rule_id: str, passed: bool, subject: str, pass_text: str, fail_text: str) -> ControlsRuleResult:
    return ControlsRuleResult(
        id=rule_id,
        status="PASS" if passed else "FAIL",
        subject=subject,
        summary=pass_text if passed else fail_text,
    )


def required_release_io_refs(item: EquipmentSpec) -> tuple[str, ...]:
    """Logical I/O members that must be bound before production release handoff."""

    standard = get_standard(item.standard)
    refs = [
        *(f"SIGNAL.{name}" for name in standard.required_feedback_signals),
        *(f"OUTPUT.{name}" for name in standard.generated_outputs),
    ]
    return tuple(sorted(refs))


def release_io_mapping_check(spec: ControlSystemSpec) -> dict[str, object]:
    missing: dict[str, list[str]] = {}
    for item in spec.equipment:
        mapped = {mapping.member for mapping in item.io}
        absent = sorted(set(required_release_io_refs(item)) - mapped)
        if absent:
            missing[item.id] = absent
    return {
        "schema": "devagent-controls-release-io-coverage-v1",
        "status": "PASS" if not missing else "FAIL",
        "missing": missing,
    }


def _equipment_rules(item: EquipmentSpec) -> list[ControlsRuleResult]:
    standard = get_standard(item.standard)
    commands = {name: enabled for name, enabled in item.commands}
    status = set(item.status)
    results: list[ControlsRuleResult] = []

    missing_commands = [
        name for name in standard.required_commands if not commands.get(name, False)
    ]
    results.append(
        _result(
            "CTRL-E100",
            not missing_commands,
            item.id,
            f"Required command contract satisfied for {standard.id}.",
            "Missing or disabled required command(s): " + ", ".join(missing_commands),
        )
    )

    missing_status = [name for name in standard.required_status if name not in status]
    results.append(
        _result(
            "CTRL-E110",
            not missing_status,
            item.id,
            f"Required status contract satisfied for {standard.id}.",
            "Missing required status member(s): " + ", ".join(missing_status),
        )
    )

    signal_set = set(item.signals)
    missing_feedback = [
        name for name in standard.required_feedback_signals if name not in signal_set
    ]
    results.append(
        _result(
            "CTRL-E120",
            not missing_feedback,
            item.id,
            f"Required feedback contract satisfied for {standard.id}.",
            "Missing required feedback signal(s): " + ", ".join(missing_feedback),
        )
    )

    reset_required = "RESET" in standard.required_commands
    reset_ok = not reset_required or commands.get("RESET", False)
    results.append(
        _result(
            "CTRL-E417",
            reset_ok,
            item.id,
            "Reset contract is satisfied or not required by this standard.",
            f"{standard.id} requires an enabled RESET command.",
        )
    )

    results.append(
        _result(
            "CTRL-E310",
            len(item.permissives) >= standard.min_permissives,
            item.id,
            f"Permissive contract satisfies minimum {standard.min_permissives}.",
            (
                f"{standard.id} requires at least {standard.min_permissives} permissive(s); "
                f"found {len(item.permissives)}."
            ),
        )
    )
    results.append(
        _result(
            "CTRL-E320",
            len(item.interlocks) >= standard.min_interlocks,
            item.id,
            f"Interlock contract satisfies minimum {standard.min_interlocks}.",
            (
                f"{standard.id} requires at least {standard.min_interlocks} interlock(s); "
                f"found {len(item.interlocks)}."
            ),
        )
    )
    results.append(
        _result(
            "CTRL-E325",
            len(item.faults) >= standard.min_faults,
            item.id,
            f"Fault-source contract satisfies minimum {standard.min_faults}.",
            (
                f"{standard.id} requires at least {standard.min_faults} explicit fault "
                f"source(s); found {len(item.faults)}."
            ),
        )
    )
    results.append(
        _result(
            "CTRL-E330",
            len(item.alarms) >= standard.min_alarms,
            item.id,
            f"Alarm contract satisfies minimum {standard.min_alarms}.",
            (
                f"{standard.id} requires at least {standard.min_alarms} alarm(s); "
                f"found {len(item.alarms)}."
            ),
        )
    )

    overlap = sorted(set(item.permissives) & set(item.interlocks))
    results.append(
        _result(
            "CTRL-E311",
            not overlap,
            item.id,
            "Permissive/interlock ownership is unambiguous.",
            "Signal(s) cannot be both permissive and interlock: " + ", ".join(overlap),
        )
    )

    if standard.fault_status_member is not None:
        fault_status_ok = (
            standard.fault_status_member in status
            and len(item.faults) >= standard.min_faults
        )
        results.append(
            _result(
                "CTRL-E321",
                fault_status_ok,
                item.id,
                (
                    f"{standard.fault_status_member} has explicit fault-source coverage."
                ),
                (
                    f"{standard.id} requires {standard.fault_status_member} with explicit "
                    "fault-source coverage."
                ),
            )
        )

    results.append(
        _result(
            "CTRL-E405",
            standard.command_model in {
                "SEAL_IN_PRIMARY_STOP_DOMINANT",
                "MUTUALLY_EXCLUSIVE_LEVEL",
            },
            item.id,
            f"Command semantics are explicit: {standard.command_model}.",
            f"Unsupported command semantics {standard.command_model!r}.",
        )
    )

    faceplate_ok = item.hmi.faceplate == standard.default_faceplate
    results.append(
        _result(
            "CTRL-E410",
            faceplate_ok,
            item.id,
            f"Qualified faceplate {standard.default_faceplate} selected.",
            (
                f"Faceplate {item.hmi.faceplate!r} does not match qualified standard "
                f"{standard.default_faceplate!r}."
            ),
        )
    )

    for alarm in item.alarms:
        source_ok = alarm.source_signal is not None
        results.append(
            _result(
                "CTRL-E500",
                source_ok,
                f"{item.id}:{alarm.id}",
                f"Alarm {alarm.id} is explicitly bound to signal {alarm.source_signal}.",
                (
                    f"Alarm {alarm.id} requires an explicit source_signal for deterministic "
                    "PLC/HMI binding."
                ),
            )
        )
        response_ok = bool(alarm.operator_response.strip())
        results.append(
            _result(
                "CTRL-W500",
                response_ok,
                f"{item.id}:{alarm.id}",
                f"Alarm {alarm.id} includes operator response guidance.",
                f"Alarm {alarm.id} is missing operator response guidance.",
            )
        )

    alarm_sources = {
        alarm.source_signal for alarm in item.alarms if alarm.source_signal is not None
    }
    missing_fault_alarm = sorted(set(item.faults) - alarm_sources)
    results.append(
        _result(
            "CTRL-E331",
            not missing_fault_alarm,
            item.id,
            "Every declared fault has an operator-visible alarm binding.",
            "Fault source(s) missing alarm coverage: " + ", ".join(missing_fault_alarm),
        )
    )

    for requirement in item.requirements:
        assertion_required = requirement.criticality in {"HIGH", "CRITICAL"}
        assertion_ok = requirement.assertion is not None
        results.append(
            ControlsRuleResult(
                id="CTRL-E600" if assertion_required else "CTRL-W600",
                status=(
                    "PASS"
                    if assertion_ok
                    else "FAIL"
                    if assertion_required
                    else "WARN"
                ),
                subject=f"{item.id}:{requirement.id}",
                summary=(
                    "Requirement has a deterministic structured assertion."
                    if assertion_ok
                    else (
                        f"{requirement.criticality} requirement requires a structured "
                        "assertion for deterministic FAT generation."
                        if assertion_required
                        else "Text-only requirement remains traceable but cannot produce "
                        "deterministic requirement-specific FAT proof."
                    )
                ),
            )
        )

    mapped_members = {mapping.member for mapping in item.io}
    duplicate_io_semantics = len(mapped_members) != len(item.io)
    results.append(
        _result(
            "CTRL-E700",
            not duplicate_io_semantics,
            item.id,
            "I/O semantic members are uniquely mapped.",
            "One or more logical I/O members are mapped more than once.",
        )
    )

    missing_release_io = sorted(
        set(required_release_io_refs(item)) - mapped_members
    )
    results.append(
        ControlsRuleResult(
            id="CTRL-W710",
            status="PASS" if not missing_release_io else "WARN",
            subject=item.id,
            summary=(
                "Required release I/O coverage is complete."
                if not missing_release_io
                else (
                    "Engineering staging may continue, but release handoff requires "
                    "I/O mapping for: " + ", ".join(missing_release_io)
                )
            ),
        )
    )

    return results


def evaluate_controls_rules(spec: ControlSystemSpec) -> tuple[ControlsRuleResult, ...]:
    """Evaluate company rules in canonical order independent of author input order."""

    results: list[ControlsRuleResult] = []
    assigned = {item.controller for item in spec.equipment}
    for controller in sorted(spec.controllers, key=lambda value: value.id):
        results.append(
            _result(
                "CTRL-E020",
                controller.id in assigned,
                controller.id,
                "Controller has at least one assigned equipment object.",
                "Controller has no assigned equipment; empty generated projects are prohibited.",
            )
        )
    for item in sorted(spec.equipment, key=lambda value: value.id):
        results.extend(_equipment_rules(item))
    return tuple(
        sorted(
            results,
            key=lambda value: (value.subject, value.id, value.status, value.summary),
        )
    )


def rules_pass(results: tuple[ControlsRuleResult, ...]) -> bool:
    return all(item.status != "FAIL" for item in results)
