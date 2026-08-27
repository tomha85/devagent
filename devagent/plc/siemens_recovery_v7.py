from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import re
from collections import defaultdict

from devagent.plc import siemens_interlock_permissive_v6 as _v6
from devagent.plc import siemens_state_machine_v5 as _v5
from devagent.plc.fat_procedure_v12 import enrich_fat_procedures
from devagent.plc.models import (
    FATTestCase,
    PLCEngineeringResult,
    PLCOutcome,
    PLCSemanticState,
    StaticCheck,
    StaticCheckStatus,
)
from devagent.plc.production_models import RiskFinding, Severity
from devagent.plc.production_utils import stable_id


_INSTALLED = False
_PREVIOUS_ANALYZER = _v6.analyze_siemens_tia_v6
_PREVIOUS_CAPABILITY = _v6.siemens_capability_profile_v6
_FAULT_STATE = re.compile(r"(fault|error|trip|alarm|abort|failed|failure)", re.IGNORECASE)
_MAX_RECOVERY_TESTS = 384


@dataclass(frozen=True)
class SiemensV7RecoveryTransitionFact:
    id: str
    contract_id: str
    transition_id: str
    machine_id: str
    block: str
    state_tag: str
    source_state: str
    target_state: str
    source_line: int
    recovery_terms: tuple[str, ...]
    guard_paths: tuple[tuple[tuple[str, bool], ...], ...]
    runtime_dependencies: tuple[str, ...]
    semantic_state: PLCSemanticState


@dataclass(frozen=True)
class SiemensV7MachineRecoveryFact:
    id: str
    machine_id: str
    block: str
    state_tag: str
    named_fault_states: tuple[str, ...]
    recovery_transitions: tuple[SiemensV7RecoveryTransitionFact, ...]
    fault_recovery_gaps: tuple[str, ...]
    recovery_conflicts: tuple[str, ...]
    runtime_recovery_dependencies: tuple[str, ...]
    semantic_state: PLCSemanticState
    reason: str


@dataclass(frozen=True)
class SiemensV7RecoveryFacts:
    machines: tuple[SiemensV7MachineRecoveryFact, ...]


def _facts(project):
    return getattr(project, "_siemens_v7_recovery_facts", None)


def _is_fault_state(value: str) -> bool:
    if re.fullmatch(r"[-+]?\d+", value):
        return False
    return _FAULT_STATE.search(value.replace("_", " ")) is not None


def _paths_overlap(left, right) -> bool:
    return _v5._paths_overlap(left, right)


def _build_recovery_facts(project) -> SiemensV7RecoveryFacts | None:
    state_facts = _v5._facts(project)
    guard_facts = _v6._facts(project)
    if state_facts is None or guard_facts is None:
        return None

    contracts_by_machine = defaultdict(list)
    for contract in guard_facts.contracts:
        contracts_by_machine[contract.machine_id].append(contract)

    machines: list[SiemensV7MachineRecoveryFact] = []
    for machine in state_facts.machines:
        contracts = contracts_by_machine.get(machine.id, [])
        recovery: list[SiemensV7RecoveryTransitionFact] = []
        for contract in contracts:
            recovery_terms = tuple(
                dict.fromkeys(
                    term.tag
                    for term in contract.terms
                    if term.role == "RECOVERY"
                )
            )
            if not recovery_terms:
                continue
            digest = hashlib.sha1(f"{contract.id}:recovery-v7".encode()).hexdigest()[:14]
            recovery.append(
                SiemensV7RecoveryTransitionFact(
                    id=f"SIEMENS-REC7-{digest}",
                    contract_id=contract.id,
                    transition_id=contract.transition_id,
                    machine_id=machine.id,
                    block=machine.block,
                    state_tag=machine.state_tag,
                    source_state=contract.source_state,
                    target_state=contract.target_state,
                    source_line=contract.source_line,
                    recovery_terms=recovery_terms,
                    guard_paths=contract.guard_paths,
                    runtime_dependencies=contract.runtime_dependencies,
                    semantic_state=contract.semantic_state,
                )
            )

        named_fault_states = tuple(state for state in machine.states if _is_fault_state(state))
        gaps: list[str] = []
        for state in named_fault_states:
            exits = [
                transition
                for transition in recovery
                if transition.source_state.casefold() == state.casefold()
                and not _is_fault_state(transition.target_state)
            ]
            if not exits:
                gaps.append(state)

        conflicts: list[str] = []
        for reset in recovery:
            for other in contracts:
                if other.transition_id == reset.transition_id:
                    continue
                if other.source_state.casefold() != reset.source_state.casefold():
                    continue
                if other.target_state.casefold() == reset.target_state.casefold():
                    continue
                if _paths_overlap(reset.guard_paths, other.guard_paths):
                    conflicts.append(
                        f"{reset.source_state}:{reset.target_state}|{other.target_state}"
                    )

        runtime = tuple(
            sorted(
                {
                    dep
                    for transition in recovery
                    for dep in transition.runtime_dependencies
                },
                key=str.casefold,
            )
        )
        complete = (
            machine.semantic_state is PLCSemanticState.FULL
            and not gaps
            and not conflicts
        )
        semantic = PLCSemanticState.FULL if complete else PLCSemanticState.PARTIAL
        reason_parts = []
        if machine.semantic_state is not PLCSemanticState.FULL:
            reason_parts.append("parent_state_machine_partial")
        if gaps:
            reason_parts.append("named_fault_state_without_explicit_recovery_exit")
        if conflicts:
            reason_parts.append("recovery_transition_overlap")
        reason = (
            "bounded_recovery_topology"
            if semantic is PLCSemanticState.FULL
            else ",".join(reason_parts) or "recovery_partial"
        )
        digest = hashlib.sha1(f"{machine.id}:recovery-machine-v7".encode()).hexdigest()[:14]
        machines.append(
            SiemensV7MachineRecoveryFact(
                id=f"SIEMENS-RM7-{digest}",
                machine_id=machine.id,
                block=machine.block,
                state_tag=machine.state_tag,
                named_fault_states=named_fault_states,
                recovery_transitions=tuple(recovery),
                fault_recovery_gaps=tuple(gaps),
                recovery_conflicts=tuple(sorted(set(conflicts), key=str.casefold)),
                runtime_recovery_dependencies=runtime,
                semantic_state=semantic,
                reason=reason,
            )
        )
    return SiemensV7RecoveryFacts(tuple(machines))


def siemens_capability_profile_v7(project):
    profile = dict(_PREVIOUS_CAPABILITY(project))
    facts = _facts(project)
    profile["schema"] = "devagent-siemens-tia-capability-v7"
    if facts is None:
        profile.update(
            {
                "recovery_machines": 0,
                "recovery_transitions": 0,
                "named_fault_states": 0,
                "fault_recovery_gaps": 0,
                "recovery_conflicts": 0,
                "runtime_recovery_dependencies": 0,
                "recovery_contract": "NONE",
                "restart_retention_contract": "RUNTIME_REQUIRED",
            }
        )
        return profile
    machines = facts.machines
    partial = [m for m in machines if m.semantic_state is not PLCSemanticState.FULL]
    profile.update(
        {
            "recovery_machines": len(machines),
            "recovery_transitions": sum(len(m.recovery_transitions) for m in machines),
            "named_fault_states": sum(len(m.named_fault_states) for m in machines),
            "fault_recovery_gaps": sum(len(m.fault_recovery_gaps) for m in machines),
            "recovery_conflicts": sum(len(m.recovery_conflicts) for m in machines),
            "runtime_recovery_dependencies": sum(
                len(m.runtime_recovery_dependencies) for m in machines
            ),
            "recovery_contract": (
                "COMPLETE"
                if machines and not partial
                else "PARTIAL_FAIL_CLOSED"
                if machines
                else "NONE"
            ),
            "restart_retention_contract": "RUNTIME_REQUIRED",
            "bounded_recovery_contract": (
                "recovery transitions are only those whose V6 guard metadata explicitly "
                "identifies reset/recovery/ack/clear/restart intent; named fault states are "
                "recognized only from non-numeric state labels with explicit fault/error/trip/alarm semantics"
            ),
        }
    )
    return profile


def _source_for_transition(project, transition):
    for statement in project.logic_statements:
        try:
            line = int(str(statement.source.line)) if statement.source.line is not None else None
        except ValueError:
            line = None
        block = statement.source.program or statement.owner_name or ""
        if (
            statement.language == "SCL"
            and block.casefold() == transition.block.casefold()
            and line == transition.source_line
        ):
            return statement.source
    return None


def _machine_source(project, machine):
    state_facts = _v5._facts(project)
    if state_facts is None:
        return None
    state_machine = next(
        (item for item in state_facts.machines if item.id == machine.machine_id),
        None,
    )
    if state_machine is None:
        return None
    for statement in project.logic_statements:
        try:
            line = int(str(statement.source.line)) if statement.source.line is not None else None
        except ValueError:
            line = None
        block = statement.source.program or statement.owner_name or ""
        if (
            statement.language == "SCL"
            and block.casefold() == machine.block.casefold()
            and line == state_machine.case_line
        ):
            return statement.source
    return None


def _recovery_fat(project, machines):
    tests: list[FATTestCase] = []
    for machine in machines:
        base_source = _machine_source(project, machine)
        if base_source is not None and len(tests) < _MAX_RECOVERY_TESTS:
            digest = hashlib.sha1(f"{machine.id}:restart".encode()).hexdigest()[:10]
            tests.append(
                FATTestCase(
                    id=f"FAT-SIEMENS-REC7-{digest}",
                    title=f"Verify restart/retained-state behavior for {machine.state_tag} in {machine.block}",
                    source=base_source,
                    output_tag=machine.state_tag,
                    preconditions={},
                    expected=(
                        f"Engineer evidence records {machine.state_tag} before/after cold start, warm restart, "
                        "download/restart, and approved reset, and confirms the resulting CASE state matches the "
                        "machine requirement without relying on an assumed static startup value."
                    ),
                    method="RUNTIME_FAT_REQUIRED",
                    scenario="SIEMENS_RESTART_RETAINED_STATE",
                    limitations=(
                        "Engineering export alone does not prove retentivity, startup organization-block behavior, or process-safe restart.",
                        "DevAgent does not execute PLCSIM, HIL, or a real PLC.",
                    ),
                    watch_tags=(machine.state_tag,),
                )
            )

        for transition in machine.recovery_transitions:
            if len(tests) >= _MAX_RECOVERY_TESTS:
                break
            source = _source_for_transition(project, transition) or base_source
            if source is None:
                continue
            for path_index, path in enumerate(transition.guard_paths):
                if len(tests) >= _MAX_RECOVERY_TESTS:
                    break
                digest = hashlib.sha1(
                    f"{transition.id}:recovery:{path_index}".encode()
                ).hexdigest()[:10]
                tests.append(
                    FATTestCase(
                        id=f"FAT-SIEMENS-REC7-{digest}",
                        title=(
                            f"Verify recovery transition {transition.state_tag}: "
                            f"{transition.source_state} -> {transition.target_state}"
                        ),
                        source=source,
                        output_tag=transition.state_tag,
                        preconditions=dict(path),
                        expected=(
                            f"With the exact source recovery guard path {dict(path)}, "
                            f"{transition.state_tag} moves from {transition.source_state} to "
                            f"{transition.target_state}. Engineer evidence also verifies outputs/process "
                            "remain safe during and after recovery."
                        ),
                        method="RUNTIME_FAT_REQUIRED",
                        scenario="SIEMENS_FAULT_RECOVERY",
                        limitations=(
                            "Static analysis proves only the bounded source transition relation; safe physical recovery is runtime evidence.",
                            "DevAgent does not execute PLCSIM, HIL, or a real PLC.",
                        ),
                        watch_tags=tuple(
                            dict.fromkeys(
                                (
                                    transition.state_tag,
                                    *transition.recovery_terms,
                                    *(ref for ref, _ in path),
                                )
                            )
                        ),
                    )
                )

        for state in machine.fault_recovery_gaps:
            if base_source is None or len(tests) >= _MAX_RECOVERY_TESTS:
                continue
            digest = hashlib.sha1(f"{machine.id}:gap:{state}".encode()).hexdigest()[:10]
            tests.append(
                FATTestCase(
                    id=f"FAT-SIEMENS-REC7-{digest}",
                    title=f"Resolve recovery gap from named fault state {state} for {machine.state_tag}",
                    source=base_source,
                    output_tag=machine.state_tag,
                    preconditions={},
                    expected=(
                        f"Engineer requirement/source review identifies and validates the intended exit from "
                        f"named fault state {state}; no release claim is accepted while recovery behavior is absent or opaque."
                    ),
                    method="RUNTIME_FAT_REQUIRED",
                    scenario="SIEMENS_FAULT_RECOVERY_GAP",
                    limitations=(
                        "No explicit V6 recovery-labeled transition from this named fault state was proven.",
                    ),
                    watch_tags=(machine.state_tag,),
                )
            )
    return enrich_fat_procedures(project, tests)


def _v7_checks(machines):
    if not machines:
        return [
            StaticCheck(
                "SIEMENS_V7_RECOVERY_TOPOLOGY",
                StaticCheckStatus.WARN,
                "No Siemens V5 state machine was available for V7 recovery/restart analysis.",
            )
        ]
    recovery = sum(len(m.recovery_transitions) for m in machines)
    named_faults = sum(len(m.named_fault_states) for m in machines)
    gaps = sum(len(m.fault_recovery_gaps) for m in machines)
    conflicts = sum(len(m.recovery_conflicts) for m in machines)
    runtime = sum(len(m.runtime_recovery_dependencies) for m in machines)
    evidence = tuple(m.id for m in machines)
    return [
        StaticCheck(
            "SIEMENS_V7_RECOVERY_TOPOLOGY",
            (
                StaticCheckStatus.PASS
                if not gaps and not conflicts
                else StaticCheckStatus.NOT_PROVEN
            ),
            (
                f"Recovery topology: explicit recovery transitions={recovery}, named fault states={named_faults}, "
                f"fault recovery gaps={gaps}, recovery overlaps={conflicts}."
            ),
            evidence,
        ),
        StaticCheck(
            "SIEMENS_V7_RECOVERY_RUNTIME",
            StaticCheckStatus.NOT_PROVEN,
            (
                f"Recovery execution remains runtime evidence; runtime recovery dependencies={runtime}. "
                "Static source analysis does not prove process-safe reset/restart."
            ),
            evidence,
        ),
        StaticCheck(
            "SIEMENS_V7_RESTART_RETENTION",
            StaticCheckStatus.NOT_PROVEN,
            (
                "Cold/warm restart, retained state, startup OB behavior, and post-download recovery require "
                "engineer-executed FAT; no static startup value is assumed."
            ),
            evidence,
        ),
    ]


def analyze_siemens_tia_v7(path) -> PLCEngineeringResult:
    base = _PREVIOUS_ANALYZER(path)
    project = base.project
    guard_facts = _v6._facts(project)
    if guard_facts is None:
        return base

    facts = _build_recovery_facts(project)
    assert facts is not None
    setattr(project, "_siemens_v7_recovery_facts", facts)
    project.metadata = replace(project.metadata, schema_revision="SIEMENS-TIA-EXPORT-V7")

    fat_tests = list(base.fat_tests)
    fat_tests.extend(_recovery_fat(project, facts.machines))
    fat_tests = list({test.id: test for test in fat_tests}.values())

    checks = [item for item in base.static_checks if not item.id.startswith("SIEMENS_V7_")]
    checks.extend(_v7_checks(facts.machines))

    profile = siemens_capability_profile_v7(project)
    recovery_complete = profile["recovery_contract"] in {"COMPLETE", "NONE"}
    outcome = base.outcome
    if base.outcome is PLCOutcome.STATICALLY_VERIFIED and not recovery_complete:
        outcome = PLCOutcome.PARTIALLY_VERIFIED

    limitations = list(base.limitations)
    limitations.append(
        "Siemens V7 recognizes recovery intent only from explicit reset/recovery/ack/clear/restart PLC metadata on a V6-proven transition; it does not infer a reset path from topology alone."
    )
    limitations.append(
        "Named fault-state recovery topology may be statically checked, but safe fault recovery, startup/retained state, output de-energization, I/O/process behavior, and restart sequencing remain engineer runtime evidence."
    )
    return PLCEngineeringResult(
        outcome,
        project,
        base.graph,
        fat_tests,
        checks,
        list(dict.fromkeys(limitations)),
    )


def _semantic_section(previous, project):
    base = previous(project)
    facts = _facts(project)
    if facts is None:
        return base
    profile = siemens_capability_profile_v7(project)
    text = (
        "### Siemens V7 Recovery / Reset / Restart Verification\n\n"
        f"- Recovery transitions: **{profile['recovery_transitions']}**\n"
        f"- Explicit named fault states: **{profile['named_fault_states']}**\n"
        f"- Fault recovery gaps: **{profile['fault_recovery_gaps']}**\n"
        f"- Recovery transition conflicts: **{profile['recovery_conflicts']}**\n"
        f"- Runtime recovery dependencies: **{profile['runtime_recovery_dependencies']}**\n"
        "- Restart/retained-state behavior remains runtime FAT and is never statically assumed.\n\n"
    )
    marker = "### Siemens V6 Interlocks / Permissives / Requirement Traceability"
    return base.replace(marker, text + marker, 1) if marker in base else base + "\n\n" + text


def _risks(previous, engineering, verifications, executions, engineering_findings):
    result = list(previous(engineering, verifications, executions, engineering_findings))
    facts = _facts(engineering.project)
    if facts is None:
        return result
    for machine in facts.machines:
        if machine.fault_recovery_gaps:
            result.append(
                RiskFinding(
                    stable_id("RISK", "SIEMENS_FAULT_RECOVERY_GAP_V7", machine.id),
                    "FAULT_RECOVERY",
                    f"Siemens state machine {machine.state_tag} has named fault state(s) without an explicit recovery exit",
                    Severity.HIGH,
                    f"Named fault recovery gap(s): {', '.join(machine.fault_recovery_gaps)}.",
                    "Operators may enter a fault state whose bounded recovery path is absent or not explicit in the analyzed export.",
                    "Confirm the required recovery sequence, implement/identify the explicit reset path, and execute the linked recovery FAT before release.",
                    (machine.id, *machine.fault_recovery_gaps),
                )
            )
        if machine.recovery_conflicts:
            result.append(
                RiskFinding(
                    stable_id("RISK", "SIEMENS_RECOVERY_CONFLICT_V7", machine.id),
                    "FAULT_RECOVERY",
                    f"Siemens state machine {machine.state_tag} has overlapping recovery and non-recovery transitions",
                    Severity.HIGH,
                    f"Recovery overlap(s): {', '.join(machine.recovery_conflicts)}.",
                    "A reset/recovery request can overlap a different target transition in the same source state.",
                    "Make recovery priority/exclusivity explicit and execute boundary FAT for both transition paths.",
                    (machine.id,),
                )
            )
    return result


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import siemens_integration_v1 as _integration
    from devagent.plc import siemens_tia_v1 as _v1

    previous_section = _integration._siemens_semantic_section
    previous_risks = _integration._siemens_detect_risks

    _v1.analyze_siemens_tia = analyze_siemens_tia_v7
    _v1.siemens_capability_profile = siemens_capability_profile_v7
    _dispatch.analyze_siemens_tia = analyze_siemens_tia_v7
    _integration.siemens_capability_profile = siemens_capability_profile_v7

    def semantic_section(project):
        return _semantic_section(previous_section, project)

    def detect_risks(engineering, verifications, executions, engineering_findings):
        return _risks(previous_risks, engineering, verifications, executions, engineering_findings)

    _integration._siemens_semantic_section = semantic_section
    _integration._siemens_detect_risks = detect_risks
    _INSTALLED = True


__all__ = [
    "SiemensV7MachineRecoveryFact",
    "SiemensV7RecoveryFacts",
    "SiemensV7RecoveryTransitionFact",
    "analyze_siemens_tia_v7",
    "install",
    "siemens_capability_profile_v7",
]
