from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import hashlib

from devagent.plc import schneider_interlock_permissive_v6 as _v6
from devagent.plc import schneider_state_machine_v5 as _v5
from devagent.plc.fat_procedure_v12 import enrich_fat_procedures
from devagent.plc.models import (
    FATTestCase,
    PLCEngineeringResult,
    PLCOutcome,
    PLCSemanticState,
    StaticCheck,
    StaticCheckStatus,
)
from devagent.plc.production_models import EvidenceItem, RiskFinding, Severity
from devagent.plc.production_utils import stable_id


_INSTALLED = False
_PREVIOUS_ANALYZER = _v6.analyze_schneider_control_expert_v6
_PREVIOUS_CAPABILITY = _v6.schneider_capability_profile_v6
_MAX_RECOVERY_TESTS = 768

# V7 deliberately uses explicit semantic tokens only. A positive TRUE assertion
# of one of these tokens may identify a bounded fault-entry path. Negative
# healthy/safe polarity is not inferred because that would guess safety meaning.
_FAULT_ASSERT_TOKENS = {
    "fault", "faulted", "trip", "tripped", "error", "alarm", "emergency",
    "estop", "abort", "aborted", "failed", "failure",
}
# Restart is intentionally NOT a strong recovery authorization. A tag such as
# AutoRestartEnable must never satisfy reset/recovery dominance by name alone.
_STRONG_RECOVERY_TOKENS = {
    "reset", "recover", "recovery", "ack", "acknowledge", "clear", "cleared",
}
_COMMAND_TOKENS = {
    "start", "run", "restart", "resume", "auto", "automatic", "cycle",
    "execute", "enable", "enabled", "command", "cmd",
}


@dataclass(frozen=True)
class SchneiderV7FaultEntryFact:
    id: str
    contract_id: str
    machine_id: str
    section: str
    state_tag: str
    source_state: str
    target_state: str
    source_lines: tuple[int, ...]
    fault_terms: tuple[tuple[str, bool], ...]
    guard_paths: tuple[tuple[tuple[str, bool], ...], ...]
    runtime_dependencies: tuple[str, ...]
    semantic_state: PLCSemanticState


@dataclass(frozen=True)
class SchneiderV7RecoveryTransitionFact:
    id: str
    contract_id: str
    machine_id: str
    section: str
    state_tag: str
    source_state: str
    target_state: str
    source_lines: tuple[int, ...]
    recovery_terms: tuple[tuple[str, bool], ...]
    all_path_recovery_terms: tuple[tuple[str, bool], ...]
    guard_paths: tuple[tuple[tuple[str, bool], ...], ...]
    runtime_dependencies: tuple[str, ...]
    semantic_state: PLCSemanticState


@dataclass(frozen=True)
class SchneiderV7ExitHazardFact:
    id: str
    contract_id: str
    machine_id: str
    section: str
    state_tag: str
    source_state: str
    target_state: str
    kind: str
    command_terms: tuple[str, ...]
    guard_paths: tuple[tuple[tuple[str, bool], ...], ...]
    related_contract_ids: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class SchneiderV7MachineRecoveryFact:
    id: str
    machine_id: str
    section: str
    state_tag: str
    fault_state_candidates: tuple[str, ...]
    fault_states: tuple[str, ...]
    ambiguous_fault_states: tuple[str, ...]
    fault_entries: tuple[SchneiderV7FaultEntryFact, ...]
    recovery_transitions: tuple[SchneiderV7RecoveryTransitionFact, ...]
    fault_latched_states: tuple[str, ...]
    recovery_gaps: tuple[str, ...]
    exit_hazards: tuple[SchneiderV7ExitHazardFact, ...]
    unproven_recovery_sources: tuple[str, ...]
    runtime_dependencies: tuple[str, ...]
    semantic_state: PLCSemanticState
    reason: str


@dataclass(frozen=True)
class SchneiderV7RecoveryFacts:
    machines: tuple[SchneiderV7MachineRecoveryFact, ...]


def _facts(project) -> SchneiderV7RecoveryFacts | None:
    return getattr(project, "_schneider_v7_recovery_facts", None)


def _tokens_for_term(term) -> set[str]:
    return _v6._semantic_tokens(term.tag, term.description)


def _contract_pair_set(contract) -> set[tuple[str, bool]]:
    return {(tag.casefold(), bool(required)) for tag, required in contract.all_path_terms}


def _unique_pairs(pairs) -> tuple[tuple[str, bool], ...]:
    by_key: dict[tuple[str, bool], tuple[str, bool]] = {}
    for tag, required in pairs:
        by_key.setdefault((tag.casefold(), bool(required)), (tag, bool(required)))
    return tuple(sorted(by_key.values(), key=lambda item: (item[0].casefold(), item[1])))


def _fault_assertions(contract) -> tuple[tuple[str, bool], ...]:
    """Positive fault-labelled terms that dominate every grouped source path."""
    all_path = _contract_pair_set(contract)
    result = []
    for term in contract.terms:
        if not term.required:
            continue
        if (term.tag.casefold(), True) not in all_path:
            continue
        if _tokens_for_term(term) & _FAULT_ASSERT_TOKENS:
            result.append((term.tag, True))
    return _unique_pairs(result)


def _strong_recovery_terms(contract, *, all_path_only: bool) -> tuple[tuple[str, bool], ...]:
    all_path = _contract_pair_set(contract)
    result = []
    for term in contract.terms:
        if all_path_only and (term.tag.casefold(), bool(term.required)) not in all_path:
            continue
        if _tokens_for_term(term) & _STRONG_RECOVERY_TOKENS:
            result.append((term.tag, bool(term.required)))
    return _unique_pairs(result)


def _command_terms(contract) -> tuple[str, ...]:
    result = []
    seen = set()
    for term in contract.terms:
        if _tokens_for_term(term) & _COMMAND_TOKENS and term.tag.casefold() not in seen:
            seen.add(term.tag.casefold())
            result.append(term.tag)
    return tuple(sorted(result, key=str.casefold))


def _paths_overlap(left, right) -> bool:
    return _v5._paths_overlap(left, right)


def _fault_entry_fact(contract) -> SchneiderV7FaultEntryFact | None:
    terms = _fault_assertions(contract)
    if not terms:
        return None
    digest = hashlib.sha1(f"{contract.id}:fault-entry-v7".encode()).hexdigest()[:14]
    return SchneiderV7FaultEntryFact(
        id=f"SCHNEIDER-FE7-{digest}",
        contract_id=contract.id,
        machine_id=contract.machine_id,
        section=contract.section,
        state_tag=contract.state_tag,
        source_state=contract.source_state,
        target_state=contract.target_state,
        source_lines=contract.source_lines,
        fault_terms=terms,
        guard_paths=contract.guard_paths,
        runtime_dependencies=contract.runtime_dependencies,
        semantic_state=contract.semantic_state,
    )


def _recovery_fact(contract) -> SchneiderV7RecoveryTransitionFact | None:
    any_terms = _strong_recovery_terms(contract, all_path_only=False)
    if not any_terms:
        return None
    all_path = _strong_recovery_terms(contract, all_path_only=True)
    digest = hashlib.sha1(f"{contract.id}:recovery-v7".encode()).hexdigest()[:14]
    return SchneiderV7RecoveryTransitionFact(
        id=f"SCHNEIDER-REC7-{digest}",
        contract_id=contract.id,
        machine_id=contract.machine_id,
        section=contract.section,
        state_tag=contract.state_tag,
        source_state=contract.source_state,
        target_state=contract.target_state,
        source_lines=contract.source_lines,
        recovery_terms=any_terms,
        all_path_recovery_terms=all_path,
        guard_paths=contract.guard_paths,
        runtime_dependencies=contract.runtime_dependencies,
        semantic_state=contract.semantic_state,
    )


def _hazard(contract, kind: str, summary: str, *, related=()) -> SchneiderV7ExitHazardFact:
    digest = hashlib.sha1(
        f"{contract.id}:{kind}:{'|'.join(sorted(related))}".encode()
    ).hexdigest()[:14]
    return SchneiderV7ExitHazardFact(
        id=f"SCHNEIDER-RH7-{digest}",
        contract_id=contract.id,
        machine_id=contract.machine_id,
        section=contract.section,
        state_tag=contract.state_tag,
        source_state=contract.source_state,
        target_state=contract.target_state,
        kind=kind,
        command_terms=_command_terms(contract),
        guard_paths=contract.guard_paths,
        related_contract_ids=tuple(sorted(set(related))),
        summary=summary,
    )


def _build_recovery_facts(project) -> SchneiderV7RecoveryFacts | None:
    state_facts = _v5._facts(project)
    guard_facts = _v6._facts(project)
    if state_facts is None or guard_facts is None:
        return None

    contracts_by_machine: dict[str, list[object]] = defaultdict(list)
    for contract in guard_facts.transition_contracts:
        contracts_by_machine[contract.machine_id].append(contract)

    machines: list[SchneiderV7MachineRecoveryFact] = []
    for machine in state_facts.machines:
        contracts = contracts_by_machine.get(machine.id, [])
        if not contracts:
            continue

        fault_entries = tuple(
            fact
            for contract in contracts
            for fact in [_fault_entry_fact(contract)]
            if fact is not None
        )
        recovery_all = tuple(
            fact
            for contract in contracts
            for fact in [_recovery_fact(contract)]
            if fact is not None
        )
        if not fault_entries and not recovery_all:
            continue

        entry_contract_ids = {item.contract_id for item in fault_entries}
        candidates = tuple(
            sorted({item.target_state for item in fault_entries}, key=str.casefold)
        )
        ambiguous: list[str] = []
        confirmed: list[str] = []
        for state in candidates:
            incoming = [
                contract
                for contract in contracts
                if contract.target_state.casefold() == state.casefold()
                and contract.source_state.casefold() != state.casefold()
            ]
            if incoming and all(contract.id in entry_contract_ids for contract in incoming):
                confirmed.append(state)
            else:
                ambiguous.append(state)

        confirmed_fold = {state.casefold() for state in confirmed}
        candidate_fold = {state.casefold() for state in candidates}
        recoveries = tuple(
            item
            for item in recovery_all
            if item.source_state.casefold() in candidate_fold
        )
        recovery_by_contract = {item.contract_id: item for item in recoveries}

        hazards: list[SchneiderV7ExitHazardFact] = []
        gaps: list[str] = []
        latched: list[str] = []
        runtime: set[str] = set()
        for entry in fault_entries:
            runtime.update(entry.runtime_dependencies)
        for recovery in recoveries:
            runtime.update(recovery.runtime_dependencies)

        for state in confirmed:
            exits = [
                contract
                for contract in contracts
                if contract.source_state.casefold() == state.casefold()
                and contract.target_state.casefold() != state.casefold()
                and contract.target_state.casefold() not in confirmed_fold
            ]
            dominated = []
            for contract in exits:
                recovery = recovery_by_contract.get(contract.id)
                if recovery is not None and recovery.all_path_recovery_terms:
                    dominated.append(contract)
                    continue
                if recovery is not None:
                    hazards.append(
                        _hazard(
                            contract,
                            "RECOVERY_BYPASS",
                            (
                                f"Recovery-labelled exit {state}->{contract.target_state} has reset/recovery terms on only a subset "
                                "of bounded source paths; an alternate path can leave the fault-associated state without the same recovery authorization."
                            ),
                        )
                    )
                else:
                    commands = _command_terms(contract)
                    kind = "STALE_COMMAND_EXIT" if commands else "UNCOMMANDED_FAULT_EXIT"
                    detail = (
                        f"Command-like term(s) {', '.join(commands)} can participate in exit {state}->{contract.target_state} without all-path reset/recovery dominance."
                        if commands
                        else f"Exit {state}->{contract.target_state} has no all-path reset/recovery authorization."
                    )
                    hazards.append(_hazard(contract, kind, detail))

            if not dominated:
                gaps.append(state)

            overlap_pairs: set[tuple[str, str]] = set()
            for recovery_contract in dominated:
                for other in exits:
                    if other.id == recovery_contract.id or other.target_state.casefold() == recovery_contract.target_state.casefold():
                        continue
                    if not _paths_overlap(recovery_contract.guard_paths, other.guard_paths):
                        continue
                    pair = tuple(sorted((recovery_contract.id, other.id)))
                    if pair in overlap_pairs:
                        continue
                    overlap_pairs.add(pair)
                    hazards.append(
                        _hazard(
                            recovery_contract,
                            "RECOVERY_OVERLAP",
                            (
                                f"Recovery-dominated exit {state}->{recovery_contract.target_state} overlaps a different target "
                                f"{state}->{other.target_state}; source priority/exclusivity is not sufficient for a unique recovery result."
                            ),
                            related=(other.id,),
                        )
                    )

            state_hazards = [item for item in hazards if item.source_state.casefold() == state.casefold()]
            if dominated and not state_hazards:
                latched.append(state)

        recovery_sources = {item.source_state.casefold(): item.source_state for item in recovery_all}
        unproven_sources = tuple(
            sorted(
                [label for key, label in recovery_sources.items() if key not in candidate_fold],
                key=str.casefold,
            )
        )

        if ambiguous:
            # Ambiguous fault identity is itself a fail-closed boundary. The state
            # has at least one fault-labelled incoming path and at least one other
            # incoming route that does not carry the same explicit fault assertion.
            for state in ambiguous:
                contract = next(
                    (c for c in contracts if c.target_state.casefold() == state.casefold()),
                    None,
                )
                if contract is not None:
                    hazards.append(
                        _hazard(
                            contract,
                            "AMBIGUOUS_FAULT_STATE_ENTRY",
                            (
                                f"State {state} has a fault-labelled incoming path but is also reachable through another bounded incoming route without the same explicit fault assertion."
                            ),
                        )
                    )

        complete = (
            machine.semantic_state is PLCSemanticState.FULL
            and bool(confirmed)
            and not ambiguous
            and not gaps
            and not hazards
            and not unproven_sources
        )
        semantic = PLCSemanticState.FULL if complete else PLCSemanticState.PARTIAL
        reasons = []
        if machine.semantic_state is not PLCSemanticState.FULL:
            reasons.append("parent_state_machine_partial")
        if not confirmed:
            reasons.append("fault_identity_not_proven")
        if ambiguous:
            reasons.append("ambiguous_fault_state_entry")
        if gaps:
            reasons.append("fault_state_without_recovery_dominated_exit")
        if hazards:
            reasons.append("recovery_or_restart_hazard")
        if unproven_sources:
            reasons.append("recovery_source_without_fault_identity")
        digest = hashlib.sha1(f"{machine.id}:fault-recovery-v7".encode()).hexdigest()[:14]
        machines.append(
            SchneiderV7MachineRecoveryFact(
                id=f"SCHNEIDER-RM7-{digest}",
                machine_id=machine.id,
                section=machine.section,
                state_tag=machine.state_tag,
                fault_state_candidates=candidates,
                fault_states=tuple(sorted(confirmed, key=str.casefold)),
                ambiguous_fault_states=tuple(sorted(ambiguous, key=str.casefold)),
                fault_entries=fault_entries,
                recovery_transitions=recoveries,
                fault_latched_states=tuple(sorted(latched, key=str.casefold)),
                recovery_gaps=tuple(sorted(set(gaps), key=str.casefold)),
                exit_hazards=tuple(sorted({item.id: item for item in hazards}.values(), key=lambda item: item.id)),
                unproven_recovery_sources=unproven_sources,
                runtime_dependencies=tuple(sorted(runtime, key=str.casefold)),
                semantic_state=semantic,
                reason="bounded_fault_recovery_topology" if semantic is PLCSemanticState.FULL else ",".join(reasons) or "recovery_partial",
            )
        )

    return SchneiderV7RecoveryFacts(tuple(machines)) if machines else None


def schneider_capability_profile_v7(project) -> dict[str, object]:
    profile = dict(_PREVIOUS_CAPABILITY(project))
    facts = _facts(project)
    profile["schema"] = "devagent-schneider-control-expert-capability-v7"
    if facts is None:
        profile.update(
            {
                "recovery_machines": 0,
                "fault_entry_contracts": 0,
                "fault_state_candidates": 0,
                "fault_states": 0,
                "ambiguous_fault_states": 0,
                "recovery_transitions": 0,
                "fault_latched_states": 0,
                "recovery_gaps": 0,
                "recovery_bypass_exits": 0,
                "uncommanded_fault_exits": 0,
                "stale_command_exit_hazards": 0,
                "recovery_overlaps": 0,
                "unproven_recovery_sources": 0,
                "runtime_recovery_dependencies": 0,
                "recovery_contract": "NONE",
                "fault_identity_contract": "EXPLICIT_TRUE_FAULT_TOKEN_EVERY_PATH_ONLY",
                "restart_retention_contract": "RUNTIME_REQUIRED",
            }
        )
        return profile

    machines = facts.machines
    hazards = [item for machine in machines for item in machine.exit_hazards]
    partial = [machine for machine in machines if machine.semantic_state is not PLCSemanticState.FULL]
    profile.update(
        {
            "recovery_machines": len(machines),
            "fault_entry_contracts": sum(len(machine.fault_entries) for machine in machines),
            "fault_state_candidates": sum(len(machine.fault_state_candidates) for machine in machines),
            "fault_states": sum(len(machine.fault_states) for machine in machines),
            "ambiguous_fault_states": sum(len(machine.ambiguous_fault_states) for machine in machines),
            "recovery_transitions": sum(len(machine.recovery_transitions) for machine in machines),
            "fault_latched_states": sum(len(machine.fault_latched_states) for machine in machines),
            "recovery_gaps": sum(len(machine.recovery_gaps) for machine in machines),
            "recovery_bypass_exits": sum(item.kind == "RECOVERY_BYPASS" for item in hazards),
            "uncommanded_fault_exits": sum(item.kind == "UNCOMMANDED_FAULT_EXIT" for item in hazards),
            "stale_command_exit_hazards": sum(item.kind == "STALE_COMMAND_EXIT" for item in hazards),
            "recovery_overlaps": sum(item.kind == "RECOVERY_OVERLAP" for item in hazards),
            "unproven_recovery_sources": sum(len(machine.unproven_recovery_sources) for machine in machines),
            "runtime_recovery_dependencies": sum(len(machine.runtime_dependencies) for machine in machines),
            "recovery_contract": "COMPLETE" if machines and not partial else "PARTIAL_FAIL_CLOSED" if machines else "NONE",
            "fault_identity_contract": "EXPLICIT_TRUE_FAULT_TOKEN_EVERY_PATH_ONLY",
            "restart_retention_contract": "RUNTIME_REQUIRED",
            "bounded_recovery_contract": (
                "V7 identifies a numeric CASE state as fault-associated only when every bounded incoming route to that state is represented by a V6 transition contract carrying a positive TRUE fault/error/trip/alarm/emergency token on every path. "
                "A recovery exit is accepted only when an explicit reset/recover/ack/clear token with exact source polarity dominates every bounded exit path. Restart naming alone is never recovery authorization."
            ),
        }
    )
    return profile


def _machine_source(project, machine_fact):
    state_facts = _v5._facts(project)
    if state_facts is None:
        return None
    machine = next((item for item in state_facts.machines if item.id == machine_fact.machine_id), None)
    if machine is None:
        return None
    for statement in project.logic_statements:
        if statement.language != "ST":
            continue
        owner = statement.source.routine or statement.routine or statement.owner_name or ""
        if owner.casefold() == machine.section.casefold() and _v5._statement_line(statement) == machine.case_line:
            return statement.source
    for statement in project.logic_statements:
        if statement.language == "ST":
            owner = statement.source.routine or statement.routine or statement.owner_name or ""
            if owner.casefold() == machine.section.casefold():
                return statement.source
    return None


def _contract_by_id(project, contract_id):
    facts = _v6._facts(project)
    if facts is None:
        return None
    return next((item for item in facts.transition_contracts if item.id == contract_id), None)


def _source_for_contract(project, contract_id):
    contract = _contract_by_id(project, contract_id)
    return _v6._source_for_transition(project, contract) if contract is not None else None


def _recovery_fat(project, facts) -> list[FATTestCase]:
    tests: list[FATTestCase] = []
    for machine in facts.machines:
        base_source = _machine_source(project, machine)
        if base_source is not None and len(tests) < _MAX_RECOVERY_TESTS:
            digest = hashlib.sha1(f"{machine.id}:restart-retention".encode()).hexdigest()[:10]
            tests.append(
                FATTestCase(
                    id=f"FAT-SCHNEIDER-REC7-{digest}",
                    title=f"Verify restart and retained-state behavior for {machine.state_tag} in {machine.section}",
                    source=base_source,
                    output_tag=machine.state_tag,
                    preconditions={},
                    expected=(
                        f"Engineer evidence records {machine.state_tag} before and after cold start, warm restart, application download/restart, power-cycle behavior, and approved reset. The observed state must match the machine requirement without assuming a static startup or retained value."
                    ),
                    method="RUNTIME_FAT_REQUIRED",
                    scenario="SCHNEIDER_RESTART_RETAINED_STATE_V7",
                    limitations=(
                        "Control Expert export does not prove retained-variable configuration, startup execution order, or process-safe restart behavior.",
                        "DevAgent does not execute Control Expert Simulator, HIL, or a real Modicon PLC.",
                    ),
                    watch_tags=(machine.state_tag,),
                )
            )

        for entry in machine.fault_entries:
            source = _source_for_contract(project, entry.contract_id) or base_source
            if source is None:
                continue
            for path_index, path in enumerate(entry.guard_paths):
                if len(tests) >= _MAX_RECOVERY_TESTS:
                    break
                digest = hashlib.sha1(f"{entry.id}:fault:{path_index}".encode()).hexdigest()[:10]
                tests.append(
                    FATTestCase(
                        id=f"FAT-SCHNEIDER-REC7-{digest}",
                        title=f"Verify fault-entry transition {entry.state_tag}: {entry.source_state}->{entry.target_state}",
                        source=source,
                        output_tag=entry.state_tag,
                        preconditions=dict(path),
                        expected=(
                            f"With exact bounded source conditions {dict(path)}, {entry.state_tag} transitions from {entry.source_state} to fault-associated candidate {entry.target_state}. Engineer evidence confirms outputs/process reach the intended safe condition."
                        ),
                        method="RUNTIME_FAT_REQUIRED",
                        scenario="SCHNEIDER_FAULT_ENTRY_V7",
                        limitations=(
                            "Static V7 identifies only explicit positive fault-labelled source topology; it does not certify the physical safe state.",
                            "DevAgent does not execute Control Expert Simulator, HIL, or a real Modicon PLC.",
                        ),
                        watch_tags=tuple(dict.fromkeys((entry.state_tag, *(name for name, _ in path)))),
                    )
                )

        for recovery in machine.recovery_transitions:
            source = _source_for_contract(project, recovery.contract_id) or base_source
            if source is None:
                continue
            for path_index, path in enumerate(recovery.guard_paths):
                if len(tests) >= _MAX_RECOVERY_TESTS:
                    break
                digest = hashlib.sha1(f"{recovery.id}:recovery:{path_index}".encode()).hexdigest()[:10]
                tests.append(
                    FATTestCase(
                        id=f"FAT-SCHNEIDER-REC7-{digest}",
                        title=f"Verify fault recovery {recovery.state_tag}: {recovery.source_state}->{recovery.target_state}",
                        source=source,
                        output_tag=recovery.state_tag,
                        preconditions=dict(path),
                        expected=(
                            f"Starting in {recovery.source_state}, exact bounded source conditions {dict(path)} produce transition to {recovery.target_state}. Engineer evidence confirms reset acknowledgement, output state, and process restart are safe and intentional."
                        ),
                        method="RUNTIME_FAT_REQUIRED",
                        scenario="SCHNEIDER_FAULT_RECOVERY_V7",
                        limitations=(
                            "Static analysis proves only the bounded source transition relation; safe reset/recovery is runtime evidence.",
                            "DevAgent does not execute Control Expert Simulator, HIL, or a real Modicon PLC.",
                        ),
                        watch_tags=tuple(dict.fromkeys((recovery.state_tag, *(name for name, _ in path)))),
                    )
                )

        for state in machine.recovery_gaps:
            if base_source is None or len(tests) >= _MAX_RECOVERY_TESTS:
                continue
            digest = hashlib.sha1(f"{machine.id}:gap:{state}".encode()).hexdigest()[:10]
            tests.append(
                FATTestCase(
                    id=f"FAT-SCHNEIDER-REC7-{digest}",
                    title=f"Resolve missing recovery-dominated exit from fault-associated state {state}",
                    source=base_source,
                    output_tag=machine.state_tag,
                    preconditions={},
                    expected=(
                        f"Engineer source/requirement review identifies the approved recovery path from {state}; no release acceptance is made while no reset/recovery condition dominates every bounded exit path."
                    ),
                    method="RUNTIME_FAT_REQUIRED",
                    scenario="SCHNEIDER_FAULT_RECOVERY_GAP_V7",
                    limitations=("V7 could not prove an every-path recovery authorization for this fault-associated state.",),
                    watch_tags=(machine.state_tag,),
                )
            )

        for hazard in machine.exit_hazards:
            if len(tests) >= _MAX_RECOVERY_TESTS:
                break
            source = _source_for_contract(project, hazard.contract_id) or base_source
            if source is None:
                continue
            digest = hashlib.sha1(f"{hazard.id}:boundary".encode()).hexdigest()[:10]
            tests.append(
                FATTestCase(
                    id=f"FAT-SCHNEIDER-REC7-{digest}",
                    title=f"Boundary-test {hazard.kind.lower().replace('_', ' ')} for {hazard.state_tag} {hazard.source_state}->{hazard.target_state}",
                    source=source,
                    output_tag=hazard.state_tag,
                    preconditions={},
                    expected=(
                        f"Engineer drives the boundary represented by {hazard.kind} and demonstrates that the machine cannot resume unexpectedly, bypass required reset/recovery authorization, or select a conflicting target. {hazard.summary}"
                    ),
                    method="RUNTIME_FAT_REQUIRED",
                    scenario=f"SCHNEIDER_{hazard.kind}_V7",
                    limitations=(
                        "Static topology finding is not proof of an actual unsafe runtime event; runtime FAT must disposition the path.",
                        "DevAgent does not execute Control Expert Simulator, HIL, or a real Modicon PLC.",
                    ),
                    watch_tags=tuple(dict.fromkeys((hazard.state_tag, *hazard.command_terms))),
                )
            )
    return enrich_fat_procedures(project, tests)


def _v7_checks(facts) -> list[StaticCheck]:
    machines = facts.machines
    if not machines:
        return [
            StaticCheck(
                "SCHNEIDER_V7_FAULT_RECOVERY_TOPOLOGY",
                StaticCheckStatus.WARN,
                "No V6 transition contract with explicit fault/recovery intent was available for Schneider V7 analysis.",
            )
        ]
    profile_counts = {
        "faults": sum(len(m.fault_states) for m in machines),
        "ambiguous": sum(len(m.ambiguous_fault_states) for m in machines),
        "gaps": sum(len(m.recovery_gaps) for m in machines),
        "hazards": sum(len(m.exit_hazards) for m in machines),
        "latched": sum(len(m.fault_latched_states) for m in machines),
        "runtime": sum(len(m.runtime_dependencies) for m in machines),
        "unproven": sum(len(m.unproven_recovery_sources) for m in machines),
    }
    evidence = tuple(machine.id for machine in machines)
    topology_ok = not (
        profile_counts["ambiguous"] or profile_counts["gaps"] or profile_counts["hazards"] or profile_counts["unproven"]
    )
    return [
        StaticCheck(
            "SCHNEIDER_V7_FAULT_RECOVERY_TOPOLOGY",
            StaticCheckStatus.PASS if topology_ok else StaticCheckStatus.NOT_PROVEN,
            (
                f"Fault/recovery topology: confirmed fault-associated states={profile_counts['faults']}, ambiguous fault states={profile_counts['ambiguous']}, "
                f"recovery gaps={profile_counts['gaps']}, exit hazards={profile_counts['hazards']}, unproven recovery sources={profile_counts['unproven']}."
            ),
            evidence,
        ),
        StaticCheck(
            "SCHNEIDER_V7_FAULT_LATCH_DOMINANCE",
            StaticCheckStatus.PASS if profile_counts["faults"] and profile_counts["latched"] == profile_counts["faults"] else StaticCheckStatus.NOT_PROVEN,
            (
                f"Bounded fault-latch topology proven for {profile_counts['latched']}/{profile_counts['faults']} confirmed fault-associated state(s): every modeled exit to a non-fault state must carry explicit strong recovery authorization on every path."
            ),
            evidence,
        ),
        StaticCheck(
            "SCHNEIDER_V7_RECOVERY_RUNTIME",
            StaticCheckStatus.NOT_PROVEN,
            (
                f"Reset/recovery execution remains runtime evidence; runtime-dependent recovery/fault-entry bindings={profile_counts['runtime']}. Static source topology does not prove physical safe recovery."
            ),
            evidence,
        ),
        StaticCheck(
            "SCHNEIDER_V7_RESTART_RETENTION",
            StaticCheckStatus.NOT_PROVEN,
            (
                "Cold/warm restart, retained state, application download/restart, stale command values, task/scan order, field I/O, and process restart require engineer-executed FAT."
            ),
            evidence,
        ),
    ]


def analyze_schneider_control_expert_v7(path) -> PLCEngineeringResult:
    base = _PREVIOUS_ANALYZER(path)
    project = base.project
    facts = _build_recovery_facts(project)
    if facts is None:
        return base
    setattr(project, "_schneider_v7_recovery_facts", facts)
    project.metadata = replace(project.metadata, schema_revision="SCHNEIDER-CONTROL-EXPERT-EXPORT-V7")

    fat_tests = list(base.fat_tests)
    fat_tests.extend(_recovery_fat(project, facts))
    fat_tests = list({test.id: test for test in fat_tests}.values())

    checks = [item for item in base.static_checks if not item.id.startswith("SCHNEIDER_V7_")]
    checks.extend(_v7_checks(facts))

    profile = schneider_capability_profile_v7(project)
    outcome = base.outcome
    if base.outcome is PLCOutcome.STATICALLY_VERIFIED and profile["recovery_contract"] == "PARTIAL_FAIL_CLOSED":
        outcome = PLCOutcome.PARTIALLY_VERIFIED

    limitations = list(base.limitations)
    limitations.append(
        "Schneider V7 does not infer fault identity from numeric CASE values. A state becomes fault-associated only through explicit positive fault/error/trip/alarm/emergency tokens that dominate every bounded incoming route used for that classification."
    )
    limitations.append(
        "V7 accepts reset/recover/ack/clear tokens as bounded recovery intent only when their exact Boolean polarity dominates every modeled exit path. Restart naming alone is not recovery authorization."
    )
    limitations.append(
        "Fault-latch topology is source-only. Retentivity, stale commands, startup/download behavior, task/scan ordering, field I/O, process physics, Control Expert Simulator, HIL, SIL/PL, and real Modicon behavior remain runtime/engineering evidence."
    )
    return PLCEngineeringResult(outcome, project, base.graph, fat_tests, checks, list(dict.fromkeys(limitations)))


def _v7_evidence(previous, engineering):
    items = list(previous(engineering))
    facts = _facts(engineering.project)
    if facts is None:
        return items
    project = engineering.project
    existing = {item.id for item in items}
    for machine in facts.machines:
        if machine.id not in existing:
            items.append(
                EvidenceItem(
                    machine.id,
                    "SCHNEIDER_FAULT_RECOVERY_MACHINE_V7",
                    (
                        f"{machine.state_tag}: fault states={len(machine.fault_states)}, recoveries={len(machine.recovery_transitions)}, "
                        f"latched={len(machine.fault_latched_states)}, gaps={len(machine.recovery_gaps)}, hazards={len(machine.exit_hazards)}, {machine.semantic_state.value}."
                    ),
                    machine.section,
                    project.metadata.source_sha256,
                    {
                        "machine_id": machine.machine_id,
                        "state_tag": machine.state_tag,
                        "fault_state_candidates": list(machine.fault_state_candidates),
                        "fault_states": list(machine.fault_states),
                        "ambiguous_fault_states": list(machine.ambiguous_fault_states),
                        "fault_latched_states": list(machine.fault_latched_states),
                        "recovery_gaps": list(machine.recovery_gaps),
                        "unproven_recovery_sources": list(machine.unproven_recovery_sources),
                        "runtime_dependencies": list(machine.runtime_dependencies),
                        "semantic_state": machine.semantic_state.value,
                        "reason": machine.reason,
                    },
                )
            )
        for entry in machine.fault_entries:
            if entry.id in existing:
                continue
            items.append(
                EvidenceItem(
                    entry.id,
                    "SCHNEIDER_FAULT_ENTRY_V7",
                    f"{entry.state_tag} {entry.source_state}->{entry.target_state}: explicit all-path fault assertion(s) {entry.fault_terms}.",
                    f"{entry.section}:{','.join(str(line) for line in entry.source_lines)}",
                    project.metadata.source_sha256,
                    {
                        "contract_id": entry.contract_id,
                        "fault_terms": [{"tag": tag, "required": required} for tag, required in entry.fault_terms],
                        "guard_paths": [[{"tag": tag, "required": required} for tag, required in path] for path in entry.guard_paths],
                        "runtime_dependencies": list(entry.runtime_dependencies),
                    },
                )
            )
        for recovery in machine.recovery_transitions:
            if recovery.id in existing:
                continue
            items.append(
                EvidenceItem(
                    recovery.id,
                    "SCHNEIDER_FAULT_RECOVERY_V7",
                    (
                        f"{recovery.state_tag} {recovery.source_state}->{recovery.target_state}: recovery terms={recovery.recovery_terms}, "
                        f"all-path recovery terms={recovery.all_path_recovery_terms}."
                    ),
                    f"{recovery.section}:{','.join(str(line) for line in recovery.source_lines)}",
                    project.metadata.source_sha256,
                    {
                        "contract_id": recovery.contract_id,
                        "recovery_terms": [{"tag": tag, "required": required} for tag, required in recovery.recovery_terms],
                        "all_path_recovery_terms": [{"tag": tag, "required": required} for tag, required in recovery.all_path_recovery_terms],
                        "guard_paths": [[{"tag": tag, "required": required} for tag, required in path] for path in recovery.guard_paths],
                        "runtime_dependencies": list(recovery.runtime_dependencies),
                    },
                )
            )
        for hazard in machine.exit_hazards:
            if hazard.id in existing:
                continue
            items.append(
                EvidenceItem(
                    hazard.id,
                    "SCHNEIDER_RECOVERY_HAZARD_V7",
                    hazard.summary,
                    machine.section,
                    project.metadata.source_sha256,
                    {
                        "kind": hazard.kind,
                        "contract_id": hazard.contract_id,
                        "source_state": hazard.source_state,
                        "target_state": hazard.target_state,
                        "command_terms": list(hazard.command_terms),
                        "related_contract_ids": list(hazard.related_contract_ids),
                    },
                )
            )
    return items


def _v7_risks(previous, engineering, verifications, executions, engineering_findings):
    risks = list(previous(engineering, verifications, executions, engineering_findings))
    facts = _facts(engineering.project)
    if facts is None:
        return risks
    for machine in facts.machines:
        if machine.ambiguous_fault_states:
            risks.append(
                RiskFinding(
                    stable_id("RISK", "SCHNEIDER_FAULT_IDENTITY_V7", machine.id),
                    "FAULT_RECOVERY",
                    f"Schneider fault-state identity is ambiguous for {machine.state_tag}",
                    Severity.HIGH,
                    f"Candidate state(s) {', '.join(machine.ambiguous_fault_states)} are reachable through both fault-labelled and non-fault-labelled bounded incoming routes.",
                    "A numeric CASE state cannot be treated as a dedicated fault latch when another modeled entry bypasses the explicit fault assertion.",
                    "Separate the state roles or document/prove every incoming route, then rerun V7 and the linked FAT procedures.",
                    (machine.id,),
                )
            )
        if machine.recovery_gaps:
            risks.append(
                RiskFinding(
                    stable_id("RISK", "SCHNEIDER_RECOVERY_GAP_V7", machine.id),
                    "FAULT_RECOVERY",
                    f"Schneider fault-associated state lacks a recovery-dominated exit for {machine.state_tag}",
                    Severity.HIGH,
                    f"No all-path reset/recover/ack/clear authorization was proven for state(s): {', '.join(machine.recovery_gaps)}.",
                    "Recovery behavior can be absent, opaque, or bypassed by another bounded source path.",
                    "Define the approved reset/recovery path explicitly and execute the generated recovery/restart FAT before release.",
                    (machine.id,),
                )
            )
        if machine.unproven_recovery_sources:
            risks.append(
                RiskFinding(
                    stable_id("RISK", "SCHNEIDER_RECOVERY_SOURCE_V7", machine.id),
                    "FAULT_RECOVERY",
                    f"Schneider recovery-labelled source state has no proven fault identity for {machine.state_tag}",
                    Severity.MEDIUM,
                    f"Recovery-labelled transition source state(s) {', '.join(machine.unproven_recovery_sources)} were found, but V7 could not prove those states are entered only through explicit fault assertions.",
                    "Recovery intent is traceable but cannot be promoted to a fault-latch theorem.",
                    "Confirm state meaning in requirements/descriptions and retain runtime evidence.",
                    (machine.id,),
                )
            )
        for hazard in machine.exit_hazards:
            severity = Severity.HIGH
            title = {
                "RECOVERY_BYPASS": "Recovery authorization does not dominate every exit path",
                "UNCOMMANDED_FAULT_EXIT": "Fault-associated state has an exit without explicit recovery authorization",
                "STALE_COMMAND_EXIT": "Command-like path can exit a fault-associated state without recovery dominance",
                "RECOVERY_OVERLAP": "Recovery path overlaps a competing target transition",
                "AMBIGUOUS_FAULT_STATE_ENTRY": "Fault-associated state identity is not unique",
            }.get(hazard.kind, "Schneider recovery topology hazard")
            risks.append(
                RiskFinding(
                    stable_id("RISK", "SCHNEIDER_RECOVERY_HAZARD_V7", hazard.id),
                    "FAULT_RECOVERY",
                    title,
                    severity,
                    hazard.summary,
                    "Reset/restart behavior may depend on an alternate bounded source path, command persistence, or competing transition relation.",
                    "Review the source/requirement intent and execute the linked boundary FAT with held commands, reset transitions, restart, and competing conditions.",
                    (machine.id, hazard.id, hazard.contract_id, *hazard.related_contract_ids),
                )
            )
    return risks


def _rewrite_v7(value: str) -> str:
    text = str(value)
    for old in (
        "Schneider Control Expert V1", "Schneider Control Expert V2", "Schneider Control Expert V3",
        "Schneider Control Expert V4", "Schneider Control Expert V5", "Schneider Control Expert V6",
        "Schneider V1", "Schneider V2", "Schneider V3", "Schneider V4", "Schneider V5", "Schneider V6",
    ):
        text = text.replace(old, "Schneider V7")
    return text


def _v7_render(previous, project) -> str:
    base = previous(project)
    facts = _facts(project)
    if facts is None:
        return base
    profile = schneider_capability_profile_v7(project)
    text = (
        "### Schneider V7 Fault / Reset / Recovery / Restart\n\n"
        f"- Recovery state machines: **{profile['recovery_machines']}**\n"
        f"- Explicit fault-entry contracts: **{profile['fault_entry_contracts']}**\n"
        f"- Confirmed fault-associated states: **{profile['fault_states']}**\n"
        f"- Ambiguous fault-associated states: **{profile['ambiguous_fault_states']}**\n"
        f"- Recovery transitions: **{profile['recovery_transitions']}**\n"
        f"- Fault states with every-exit recovery dominance: **{profile['fault_latched_states']}**\n"
        f"- Recovery gaps: **{profile['recovery_gaps']}**\n"
        f"- Recovery bypass exits: **{profile['recovery_bypass_exits']}**\n"
        f"- Uncommanded fault exits: **{profile['uncommanded_fault_exits']}**\n"
        f"- Stale-command exit hazards: **{profile['stale_command_exit_hazards']}**\n"
        f"- Recovery overlaps: **{profile['recovery_overlaps']}**\n"
        f"- Recovery-labelled sources without proven fault identity: **{profile['unproven_recovery_sources']}**\n"
        "- Numeric CASE values are never called fault states by value alone; V7 requires explicit positive fault-labelled every-path entry evidence.\n"
        "- Reset/recovery authorization must dominate every modeled exit path. `Restart` naming alone is never accepted as reset authorization.\n"
        "- Cold/warm restart, retentivity, stale commands, scan/I/O timing, process-safe restart, Control Expert Simulator, HIL, SIL/PL, and real Modicon execution remain runtime/engineering evidence.\n\n"
    )
    marker = "### Schneider V6 Interlocks / Permissives / Every-Path Guard Proof"
    return base.replace(marker, text + marker, 1) if marker in base else base + "\n\n" + text


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import schneider_control_expert_v1 as _v1
    from devagent.plc import schneider_integration_v1 as _integration
    from devagent.plc import schneider_report_install_v1 as _report

    previous_evidence = _integration._evidence_index
    previous_findings = _integration._findings
    previous_risks = _integration._detect_risks
    previous_render = _report._render

    _v1.analyze_schneider_control_expert = analyze_schneider_control_expert_v7
    _v1.schneider_capability_profile = schneider_capability_profile_v7
    _dispatch.analyze_schneider_control_expert = analyze_schneider_control_expert_v7
    _integration.schneider_capability_profile = schneider_capability_profile_v7

    def evidence_index(engineering):
        items = _v7_evidence(previous_evidence, engineering)
        if _facts(engineering.project) is None:
            return items
        return [
            replace(item, summary=_rewrite_v7(item.summary))
            if item.kind == "SCHNEIDER_CONTROL_EXPERT_CAPABILITY_PROFILE"
            else item
            for item in items
        ]

    def findings(engineering, valid_evidence_ids):
        items = list(previous_findings(engineering, valid_evidence_ids))
        if _facts(engineering.project) is None:
            return items
        return [
            replace(
                item,
                title=_rewrite_v7(item.title),
                summary=_rewrite_v7(item.summary),
                recommendation=_rewrite_v7(item.recommendation),
            )
            for item in items
        ]

    def detect_risks(engineering, verifications, executions, engineering_findings):
        return _v7_risks(previous_risks, engineering, verifications, executions, engineering_findings)

    def render(project):
        return _v7_render(previous_render, project)

    _integration._evidence_index = evidence_index
    _integration._findings = findings
    _integration._detect_risks = detect_risks
    _report._render = render
    _INSTALLED = True


__all__ = [
    "SchneiderV7ExitHazardFact",
    "SchneiderV7FaultEntryFact",
    "SchneiderV7MachineRecoveryFact",
    "SchneiderV7RecoveryFacts",
    "SchneiderV7RecoveryTransitionFact",
    "analyze_schneider_control_expert_v7",
    "install",
    "schneider_capability_profile_v7",
]
