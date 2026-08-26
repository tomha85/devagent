from __future__ import annotations

from collections import defaultdict

from devagent.plc import production_evidence as _evidence
from devagent.plc import production_review as _review
from devagent.plc.production_models import EngineeringFinding, RiskFinding, Severity
from devagent.plc.production_utils import stable_id
from devagent.plc.rockwell_alias_hardening import canonical_tag_identity, identity_is_resolved
from devagent.plc.rockwell_entrypoint_hardening import routine_has_execution_entry
from devagent.plc.rockwell_state_machine_v11 import state_transitions


_ORIGINAL_FINDINGS = _evidence.deterministic_engineering_findings
_ORIGINAL_RISKS = _review.detect_risks


def _routine_evidence(project, routine):
    ids = [rung.id for rung in project.rungs if rung.program == routine.program and rung.routine == routine.name]
    ids.extend(
        statement.id
        for statement in project.logic_statements
        if statement.source.program == routine.program and statement.source.routine == routine.name
    )
    return tuple(dict.fromkeys(ids))


def _unreachable_routines(project):
    result = []
    for routine in project.routines:
        evidence = _routine_evidence(project, routine)
        if not evidence:
            continue
        if routine_has_execution_entry(project, routine.program, routine.name):
            continue
        result.append((routine, evidence))
    return result


def _linear_contradictions(project):
    """Find impossible XIC/XIO pairs only on non-branched linear RLL rungs."""
    result = []
    for rung in project.rungs:
        # Flattened instruction order is not sufficient to reason across OR branches.
        # Restrict this theorem to linear rungs where opposing contacts are provably
        # conjunctive in the same execution path.
        if "[" in rung.text or "]" in rung.text:
            continue
        states = {}
        contradiction = None
        for instruction in rung.instructions:
            name = instruction.name.upper()
            if name not in {"XIC", "XIO"} or not instruction.arguments:
                continue
            ref = instruction.arguments[0].strip()
            identity = canonical_tag_identity(project, ref, rung.program)
            if not identity_is_resolved(identity):
                continue
            required = name == "XIC"
            previous = states.get(identity)
            if previous is not None and previous != required:
                contradiction = (ref, identity)
                break
            states[identity] = required
        if contradiction is not None:
            result.append((rung, contradiction[0], contradiction[1]))
    return result


def _sequence_ambiguities(project):
    groups = defaultdict(list)
    for transition in state_transitions(project):
        groups[(transition.state_tag.casefold(), transition.from_state)].append(transition)
    result = []
    for (_, from_state), transitions in groups.items():
        destinations = {item.to_state for item in transitions}
        if len(destinations) > 1:
            result.append((transitions[0].state_tag, from_state, tuple(transitions)))
    return result


def deterministic_engineering_findings(engineering, valid_evidence_ids):
    findings = list(_ORIGINAL_FINDINGS(engineering, valid_evidence_ids))
    project = engineering.project

    graph_evidence = tuple(
        dict.fromkeys(
            edge.evidence_id
            for edge in engineering.graph.edges
            if edge.evidence_id in valid_evidence_ids
        )
    )[:20]
    findings.append(
        EngineeringFinding(
            "ENG-CAUSE-EFFECT-GRAPH",
            "CAUSE_EFFECT",
            "Evidence-linked cause/effect graph built",
            Severity.INFO,
            f"Built {len(engineering.graph.edges)} dependency/call/state-transition edge(s) from the canonical PLC project for downstream traceability.",
            "Use the graph to trace affected writers, readers, requirements, and FAT candidates; unsupported semantics remain outside deterministic cause/effect claims.",
            graph_evidence,
        )
    )

    for routine, evidence in _unreachable_routines(project):
        usable = tuple(item for item in evidence if item in valid_evidence_ids)
        findings.append(
            EngineeringFinding(
                stable_id("ENG", "UNREACHABLE", routine.program, routine.name),
                "UNREACHABLE_LOGIC",
                f"Routine is not reachable from an active task/Main/JSR entry: {routine.program}/{routine.name}",
                Severity.MEDIUM,
                "The routine contains exported logic but is not in the modeled active controller execution closure. It may be intentionally inactive, obsolete, service-only, or missing a call/schedule.",
                "Confirm the task/program/routine scheduling intent. Remove dead logic or document why the routine must remain inactive before relying on it for requirements or FAT coverage.",
                usable,
            )
        )

    for rung, ref, identity in _linear_contradictions(project):
        findings.append(
            EngineeringFinding(
                stable_id("ENG", "CONTRADICTION", rung.id, repr(identity)),
                "CONTRADICTORY_LOGIC",
                f"Contradictory linear contact conditions for {ref}",
                Severity.HIGH,
                f"The same canonical storage identity is required both TRUE and FALSE on one non-branched linear rung at {rung.source.locator}; that conjunction cannot become true.",
                "Review the rung for an unintended XIC/XIO combination, stale condition, or missing branch. Treat the downstream action as unreachable until corrected or explicitly justified.",
                (rung.id,) if rung.id in valid_evidence_ids else (),
            )
        )

    transitions = state_transitions(project)
    if transitions:
        evidence = tuple(
            dict.fromkeys(item.rung_id for item in transitions if item.rung_id in valid_evidence_ids)
        )[:20]
        findings.append(
            EngineeringFinding(
                "ENG-SEQUENCE-MODEL",
                "SEQUENCING",
                "Conventional PLC sequence/state transitions discovered",
                Severity.INFO,
                f"Discovered {len(transitions)} evidence-linked state transition(s) across {len({item.state_tag.casefold() for item in transitions})} state variable(s).",
                "Review transition coverage, writer ownership, reset/fault paths, and the generated state-transition FAT procedures before commissioning.",
                evidence,
            )
        )

    for state_tag, from_state, transitions_for_state in _sequence_ambiguities(project):
        destinations = sorted({item.to_state for item in transitions_for_state})
        findings.append(
            EngineeringFinding(
                stable_id("ENG", "SEQUENCE_BRANCH", state_tag.casefold(), str(from_state)),
                "SEQUENCING",
                f"State {state_tag}={from_state} has multiple discovered next states",
                Severity.MEDIUM,
                f"The sequence can transition from {from_state} to {', '.join(str(item) for item in destinations)} across separate evidence-linked rungs. This may be intentional, but priority/mutual exclusivity is not proven by transition discovery alone.",
                "Review transition conditions and scan order, then execute the generated state-transition FAT procedures for each competing path and fault/inhibit condition.",
                tuple(dict.fromkeys(item.rung_id for item in transitions_for_state)),
            )
        )
    return findings


def detect_risks(engineering, verifications, executions, engineering_findings):
    risks = list(_ORIGINAL_RISKS(engineering, verifications, executions, engineering_findings))
    project = engineering.project
    existing = {(item.category, item.title.casefold()) for item in risks}

    def add(risk):
        key = (risk.category, risk.title.casefold())
        if key not in existing:
            existing.add(key)
            risks.append(risk)

    for routine, evidence in _unreachable_routines(project):
        add(
            RiskFinding(
                stable_id("RISK", "UNREACHABLE", routine.program, routine.name),
                "UNREACHABLE_LOGIC",
                f"Unreachable exported routine {routine.program}/{routine.name}",
                Severity.MEDIUM,
                "Exported logic exists outside the active task/Main/JSR execution closure.",
                "Required behavior could be absent at runtime if engineers assume this routine is active; alternatively stale logic can mislead maintenance and FAT planning.",
                "Confirm scheduling/call intent and disposition the routine before release or commissioning review.",
                evidence,
            )
        )

    for rung, ref, identity in _linear_contradictions(project):
        add(
            RiskFinding(
                stable_id("RISK", "CONTRADICTION", rung.id, repr(identity)),
                "CONTRADICTORY_LOGIC",
                f"Impossible linear Boolean path at {rung.source.locator}",
                Severity.HIGH,
                f"{ref} is required both TRUE and FALSE in one non-branched linear rung path.",
                "The rung action cannot execute under the modeled Boolean semantics, which can create a stalled sequence or permanently inactive output.",
                "Correct or explicitly justify the contradictory contact logic and regenerate affected FAT procedures.",
                (rung.id,),
            )
        )

    for state_tag, from_state, transitions_for_state in _sequence_ambiguities(project):
        destinations = sorted({item.to_state for item in transitions_for_state})
        add(
            RiskFinding(
                stable_id("RISK", "SEQUENCE_BRANCH", state_tag.casefold(), str(from_state)),
                "SEQUENCING",
                f"Competing sequence transitions from {state_tag}={from_state}",
                Severity.MEDIUM,
                f"Multiple discovered next states ({', '.join(str(item) for item in destinations)}) exist from the same source state.",
                "If conditions overlap, final state can depend on scan order or later writers; if they are mutually exclusive, that exclusivity still needs engineering confirmation/FAT coverage.",
                "Review writer order and transition conditions and execute each generated state-transition FAT procedure, including overlap/inhibit cases where applicable.",
                tuple(dict.fromkeys(item.rung_id for item in transitions_for_state)),
            )
        )
    return risks


def install() -> None:
    _evidence.deterministic_engineering_findings = deterministic_engineering_findings
    _review.detect_risks = detect_risks


__all__ = ["detect_risks", "deterministic_engineering_findings", "install"]
