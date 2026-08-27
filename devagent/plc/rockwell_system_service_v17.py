from __future__ import annotations

from dataclasses import dataclass
import hashlib

from devagent.plc.models import FATTestCase, PLCSourceRef, StaticCheck, StaticCheckStatus
from devagent.plc.production_models import RiskFinding, Severity
from devagent.plc.production_utils import stable_id
from devagent.plc.rockwell_branch_coverage_v16 import branch_coverage_profile
from devagent.plc.rockwell_entrypoint_hardening import routine_has_execution_entry
from devagent.plc.v2_semantics import _first_ref, _refs

_SYSTEM_SERVICE_NAMES = frozenset({"GSV", "SSV"})
_STANDARD_CATALOG_WARNING_PREFIX = "Rockwell V10 standard catalog: "


@dataclass(frozen=True)
class RockwellSystemServiceCall:
    id: str
    rung_id: str
    instruction: str
    object_class: str
    instance: str
    attribute: str
    data_operand: str
    data_refs: tuple[str, ...]
    source: PLCSourceRef

    @property
    def major_fault_record(self) -> bool:
        return self.attribute.casefold() == "majorfaultrecord"


def _service_calls_for_rung(rung) -> list[RockwellSystemServiceCall]:
    calls: list[RockwellSystemServiceCall] = []
    ordinal = 0
    for instruction in rung.instructions:
        name = instruction.name.upper()
        if name not in _SYSTEM_SERVICE_NAMES or len(instruction.arguments) < 4:
            continue
        ordinal += 1
        object_class = instruction.arguments[0].strip()
        instance = instruction.arguments[1].strip()
        attribute = instruction.arguments[2].strip()
        data_operand = instruction.arguments[3].strip()
        digest = hashlib.sha1(
            f"{rung.id}:{name}:{object_class}:{instance}:{attribute}:{ordinal}".encode("utf-8")
        ).hexdigest()[:12]
        calls.append(
            RockwellSystemServiceCall(
                id=f"SYSTEM-SERVICE-{digest}",
                rung_id=rung.id,
                instruction=name,
                object_class=object_class,
                instance=instance,
                attribute=attribute,
                data_operand=data_operand,
                data_refs=tuple(dict.fromkeys(_refs(data_operand))),
                source=rung.source,
            )
        )
    return calls


def system_service_calls(project) -> list[RockwellSystemServiceCall]:
    """Normalize reachable GSV/SSV signatures without claiming runtime proof."""
    result: list[RockwellSystemServiceCall] = []
    for rung in project.rungs:
        if not routine_has_execution_entry(project, rung.program, rung.routine):
            continue
        result.extend(_service_calls_for_rung(rung))
    return result


def _grouped_calls(project):
    by_rung: dict[str, list[RockwellSystemServiceCall]] = {}
    rung_by_id = {rung.id: rung for rung in project.rungs}
    for call in system_service_calls(project):
        by_rung.setdefault(call.rung_id, []).append(call)
    return [(rung_by_id[rung_id], calls) for rung_id, calls in by_rung.items() if rung_id in rung_by_id]


def _ordered_unique(values) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return tuple(result)


def _watch_tags(rung, calls) -> tuple[str, ...]:
    values: list[str] = []
    for call in calls:
        values.extend(call.data_refs)
    values.extend(rung.reads)
    values.extend(rung.writes)
    return _ordered_unique(values)[:16]


def _service_signature(calls) -> str:
    return ", ".join(
        f"{call.instruction}({call.object_class}, {call.instance}, {call.attribute})"
        for call in calls
    )


def _major_fault_test(rung, calls, test_id: str) -> FATTestCase:
    watch_tags = _watch_tags(rung, calls)
    primary = watch_tags[0] if watch_tags else (_first_ref(calls[0].data_operand) or "MajorFaultRecord")
    signature = _service_signature(calls)
    return FATTestCase(
        id=test_id,
        title=f"Verify major-fault system-service handling at {rung.source.locator}",
        source=rung.source,
        output_tag=primary,
        preconditions={},
        expected=(
            "Under an engineer-approved controlled fault stimulus, the MajorFaultRecord read/capture/writeback sequence "
            "must preserve the diagnostic values required by the application before any clear/writeback action removes or changes them; "
            "the observed controller fault state must match the intended recovery design. PASS requires runtime evidence."
        ),
        method="RUNTIME_FAT_REQUIRED",
        scenario="SYSTEM_SERVICE_RUNTIME",
        purpose=(
            "Verify the runtime behavior and ordering assumptions around Rockwell MajorFaultRecord system services and the surrounding local fault-handling logic."
        ),
        setup_steps=(
            f"Confirm the PLC revision under test matches the analyzed source at {rung.source.locator}.",
            "Use an engineer-approved isolated simulator, HIL/test bench, or dedicated test PLC. Keep production machinery disconnected, inhibited, or otherwise protected according to the approved test procedure.",
            "Prepare watch/trend capture for the listed tags plus the controller fault state before applying any fault stimulus.",
            "Define a controlled, reversible fault stimulus approved for this test environment; do not create a production-machine fault solely to satisfy this FAT case.",
        ),
        action_steps=(
            "Apply the approved controlled fault stimulus and record the controller state immediately before and after the fault-handler logic executes.",
            "Observe the GSV-derived MajorFaultRecord data and any application capture tags before the clear/writeback path can modify the record.",
            "Observe the CLR/SSV/writeback behavior and record whether diagnostic Type/Code or equivalent fault data was preserved where the application expects it.",
            "Record the controller fault/recovery state after the handler and compare it with the intended engineering behavior.",
            "Return the test system to the approved safe baseline and record PASS only when the complete observed sequence satisfies the expected result.",
        ),
        watch_tags=watch_tags,
        evidence_required=(
            "PLC project/revision identifier and source SHA-256 used for the test",
            f"Source reference: {rung.source.locator}",
            f"Observed system-service signature(s): {signature}",
            "Controller fault state before stimulus, during handler execution, and after recovery",
            "Timestamped watch/trend or equivalent evidence showing diagnostic capture and any clear/writeback ordering",
            "Engineer-recorded PASS/FAIL result with tester identity and approved fault-stimulus description",
            "Failure notes and disposition when diagnostic capture, clear/writeback, or recovery differs from expectation",
        ),
        why_required=(
            "GSV/SSV operate on controller/system attributes whose availability, fault-state behavior, execution ordering, and writeback effects are runtime-dependent. "
            "DevAgent can normalize the operands and trace the surrounding logic, but static analysis must not claim this major-fault behavior is proven."
        ),
        failure_implication=(
            "Fault diagnostics may be lost, stale, or cleared before capture, or the controller may not recover as intended. Treat a failure as a commissioning/release blocker until the fault-handler design is reviewed and retested."
        ),
        recommended_environment=(
            "Engineer-selected isolated simulator, HIL/test bench, or dedicated test PLC under approved fault-injection/recovery procedures; not an operating production machine"
        ),
        limitations=(
            "GSV/SSV system-attribute behavior is intentionally PARTIAL in static analysis; this FAT case does not convert it into a static PASS.",
            "DevAgent does not connect to, control, fault, clear, or write to the external PLC/simulator/HIL environment.",
            "Controller firmware, task/fault handling, system-object availability, and runtime ordering must be established by engineer-executed evidence.",
        ),
    )


def _generic_system_service_test(rung, calls, test_id: str) -> FATTestCase:
    watch_tags = _watch_tags(rung, calls)
    primary = watch_tags[0] if watch_tags else (_first_ref(calls[0].data_operand) or "SYSTEM_ATTRIBUTE")
    signature = _service_signature(calls)
    return FATTestCase(
        id=test_id,
        title=f"Verify Rockwell system-service runtime behavior at {rung.source.locator}",
        source=rung.source,
        output_tag=primary,
        preconditions={},
        expected=(
            "The referenced controller/system attribute read or write must produce the intended observable data/state in the surrounding PLC logic. "
            "PASS requires engineer-executed runtime evidence; static analysis makes no system-object timing or side-effect claim."
        ),
        method="RUNTIME_FAT_REQUIRED",
        scenario="SYSTEM_SERVICE_RUNTIME",
        purpose="Verify reachable GSV/SSV system-attribute behavior that cannot be proven from the L5X project alone.",
        setup_steps=(
            f"Confirm the PLC revision under test matches the analyzed source at {rung.source.locator}.",
            "Use an engineer-approved controlled test environment and establish the controller/system state required for the referenced attribute to be meaningful.",
            "Prepare watch/trend capture for the listed tags and any relevant controller/system status exposed by the engineering tool.",
        ),
        action_steps=(
            "Exercise the source logic through the engineer-approved condition that causes the referenced system service to execute.",
            "Record the source/destination data and relevant controller/system state before and after the GSV/SSV instruction executes.",
            "Verify the surrounding PLC logic observes or uses the system-attribute value as intended.",
            "Record PASS only when the observed runtime behavior satisfies the expected result and attach the required evidence.",
        ),
        watch_tags=watch_tags,
        evidence_required=(
            "PLC project/revision identifier and source SHA-256 used for the test",
            f"Source reference: {rung.source.locator}",
            f"Observed system-service signature(s): {signature}",
            "Timestamped watch/trend or equivalent evidence for the listed tags and controller/system state",
            "Engineer-recorded PASS/FAIL result with tester identity",
            "Failure notes and disposition if the system attribute or surrounding PLC behavior differs from expectation",
        ),
        why_required=(
            "GSV/SSV depend on controller/system runtime state. Operand normalization and dataflow traceability are available statically, but the system-object value, timing, side effects, and firmware behavior require engineer-executed FAT evidence."
        ),
        failure_implication=(
            "The PLC may consume stale/incorrect system data or write an unintended controller/system attribute value; review the system-service design before commissioning."
        ),
        recommended_environment=(
            "Engineer-selected simulator, HIL/test bench, or dedicated test PLC under approved engineering procedures"
        ),
        limitations=(
            "System-service runtime semantics are not statically verified.",
            "DevAgent generates the procedure only and never executes GSV/SSV or controls the external test environment.",
        ),
    )


def generate_system_service_fat_tests(project) -> list[FATTestCase]:
    tests: list[FATTestCase] = []
    for rung, calls in _grouped_calls(project):
        signature = "|".join(
            f"{call.instruction}:{call.object_class}:{call.instance}:{call.attribute}:{call.data_operand}"
            for call in calls
        )
        digest = hashlib.sha1(f"{rung.id}:{signature}".encode("utf-8")).hexdigest()[:10]
        test_id = f"FAT-SYSTEM-{digest}"
        if any(call.major_fault_record for call in calls):
            tests.append(_major_fault_test(rung, calls, test_id))
        else:
            tests.append(_generic_system_service_test(rung, calls, test_id))
    return tests


def system_service_profile(project) -> dict[str, object]:
    calls = system_service_calls(project)
    rung_ids = {call.rung_id for call in calls}
    return {
        "schema": "devagent-rockwell-system-service-v17",
        "occurrences": len(calls),
        "rungs": len(rung_ids),
        "gsv_occurrences": sum(call.instruction == "GSV" for call in calls),
        "ssv_occurrences": sum(call.instruction == "SSV" for call in calls),
        "major_fault_record_occurrences": sum(call.major_fault_record for call in calls),
        "runtime_fat_tests": len(generate_system_service_fat_tests(project)),
        "requires_engineer_runtime_evidence": bool(calls),
        "rung_ids": tuple(sorted(rung_ids)),
    }


def rockwell_system_service_check(project) -> StaticCheck:
    profile = system_service_profile(project)
    if not profile["occurrences"]:
        return StaticCheck(
            id="ROCKWELL_SYSTEM_SERVICE_RUNTIME",
            status=StaticCheckStatus.PASS,
            summary="No reachable GSV/SSV system-service instructions require runtime qualification.",
        )
    return StaticCheck(
        id="ROCKWELL_SYSTEM_SERVICE_RUNTIME",
        status=StaticCheckStatus.WARN,
        summary=(
            f"Normalized {profile['occurrences']} reachable GSV/SSV system-service occurrence(s) across {profile['rungs']} rung(s) and generated "
            f"{profile['runtime_fat_tests']} engineer-executed runtime FAT procedure(s). System-attribute timing, values, side effects, and recovery remain PARTIAL until runtime evidence is attached."
        ),
        evidence=tuple(profile["rung_ids"]),
    )


def system_service_risks(project) -> list[RiskFinding]:
    tests_by_rung = {test.source.locator: test for test in generate_system_service_fat_tests(project)}
    result: list[RiskFinding] = []
    for rung, calls in _grouped_calls(project):
        major_fault = any(call.major_fault_record for call in calls)
        test = tests_by_rung.get(rung.source.locator)
        evidence = _ordered_unique((rung.id, *(call.id for call in calls), test.id if test else ""))
        if major_fault:
            result.append(
                RiskFinding(
                    stable_id("RISK", "SYSTEM_SERVICE_MAJOR_FAULT", rung.id),
                    "SYSTEM_SERVICE_RUNTIME",
                    "Major-fault system-service behavior requires controlled runtime FAT",
                    Severity.HIGH,
                    (
                        "Reachable GSV/SSV logic handles the controller MajorFaultRecord together with local capture/clear/writeback behavior. "
                        "The L5X proves the references exist, but not runtime fault-state ordering or whether diagnostic data is preserved before clear/writeback."
                    ),
                    "Fault diagnostics can be lost or the fault-handler recovery behavior can differ from the intended commissioning design.",
                    "Execute the linked controlled major-fault FAT procedure in an isolated approved test environment and retain timestamped diagnostic/recovery evidence.",
                    evidence,
                )
            )
        else:
            attrs = ", ".join(sorted({call.attribute for call in calls}, key=str.casefold))
            result.append(
                RiskFinding(
                    stable_id("RISK", "SYSTEM_SERVICE_RUNTIME", rung.id),
                    "SYSTEM_SERVICE_RUNTIME",
                    f"System-attribute runtime behavior requires FAT ({attrs})",
                    Severity.MEDIUM,
                    "Reachable GSV/SSV logic accesses controller/system attributes whose values and side effects are runtime-dependent.",
                    "Static traceability does not prove the controller/system attribute value, timing, firmware behavior, or downstream runtime effect.",
                    "Execute the linked system-service FAT procedure and attach runtime evidence before relying on the behavior for commissioning acceptance.",
                    evidence,
                )
            )
    return result


def system_services_explain_current_semantic_gap(project) -> bool:
    """True only when the remaining bounded gap is exclusively reachable GSV/SSV logic.

    This helper is used only to replace a vague generic semantic risk with a more
    specific one. It never changes PLCOutcome, static checks, or release policy.
    """
    service_rung_ids = {call.rung_id for call in system_service_calls(project)}
    if not service_rung_ids:
        return False
    if project.unknown_instruction_names:
        return False
    partial = {name.upper() for name in project.partially_modeled_instruction_names}
    if not partial or not partial.issubset(_SYSTEM_SERVICE_NAMES):
        return False
    branch = branch_coverage_profile(project)
    withheld = set(branch.get("withheld_rung_ids", ()))
    if withheld - service_rung_ids:
        return False
    if any(routine.source_protected or routine.routine_type.upper() not in {"RLL", "ST"} for routine in project.routines):
        return False
    if any(aoi.source_protected for aoi in project.aois):
        return False
    if project.aoi_internal_total != project.aoi_internal_modeled_count:
        return False
    if project.aoi_call_total != project.aoi_call_bound_count:
        return False
    if project.st_statement_total != project.st_statement_semantic_count:
        return False
    non_catalog_warnings = [
        warning
        for warning in project.warnings
        if not warning.startswith(_STANDARD_CATALOG_WARNING_PREFIX)
    ]
    return not non_catalog_warnings


__all__ = [
    "RockwellSystemServiceCall",
    "generate_system_service_fat_tests",
    "rockwell_system_service_check",
    "system_service_calls",
    "system_service_profile",
    "system_service_risks",
    "system_services_explain_current_semantic_gap",
]
