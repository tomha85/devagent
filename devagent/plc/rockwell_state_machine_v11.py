from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import re

from devagent.plc.models import FATTestCase, PLCDependencyEdge, StaticCheck, StaticCheckStatus
from devagent.plc.rockwell_alias_hardening import canonical_tag_identity, identity_is_resolved
from devagent.plc.rockwell_entrypoint_hardening import routine_has_execution_entry
from devagent.plc.v2_semantics import _first_ref, _has_variable_subscript


_INT_LITERAL = re.compile(r"[-+]?\d+")
_ALLOWED_DETERMINISTIC = frozenset({"XIC", "XIO", "EQU", "EQ", "MOV", "MOVE", "NOP"})


@dataclass(frozen=True)
class RockwellStateTransition:
    id: str
    rung_id: str
    state_tag: str
    from_state: int
    to_state: int
    deterministic_action: bool
    source: object


def _int_literal(value: str) -> int | None:
    value = value.strip()
    if not _INT_LITERAL.fullmatch(value):
        return None
    try:
        return int(value, 10)
    except ValueError:
        return None


def _fixed_ref(value: str) -> str | None:
    if _has_variable_subscript(value):
        return None
    return _first_ref(value)


def _equ_state(instruction):
    if instruction.name.upper() not in {"EQU", "EQ"} or len(instruction.arguments) < 2:
        return None
    left, right = instruction.arguments[:2]
    left_ref = _fixed_ref(left)
    right_ref = _fixed_ref(right)
    left_literal = _int_literal(left)
    right_literal = _int_literal(right)
    if left_ref is not None and right_literal is not None:
        return left_ref, right_literal
    if right_ref is not None and left_literal is not None:
        return right_ref, left_literal
    return None


def _move_state(instruction):
    if instruction.name.upper() not in {"MOV", "MOVE"} or len(instruction.arguments) < 2:
        return None
    source, destination = instruction.arguments[:2]
    literal = _int_literal(source)
    target = _fixed_ref(destination)
    if literal is None or target is None:
        return None
    return target, literal


def state_transitions(project) -> list[RockwellStateTransition]:
    """Discover conventional integer state transitions without sample-specific names.

    A transition candidate requires an equality comparison of a fixed tag to an
    integer state and a MOV/MOVE of an integer into the same canonical Rockwell
    storage identity on one reachable rung. The local MOV action is considered
    deterministic only when every rung instruction belongs to the bounded
    XIC/XIO/EQU/MOV/NOP grammar; otherwise the transition remains a traceable
    runtime candidate because motion/AOI/program-control semantics may affect
    execution.
    """
    result: list[RockwellStateTransition] = []
    counts: Counter[tuple[tuple[str, str], int, int]] = Counter()
    for rung in project.rungs:
        if not routine_has_execution_entry(project, rung.program, rung.routine):
            continue
        comparisons = [item for item in (_equ_state(ins) for ins in rung.instructions) if item is not None]
        moves = [item for item in (_move_state(ins) for ins in rung.instructions) if item is not None]
        if not comparisons or not moves:
            continue
        deterministic = all(ins.name.upper() in _ALLOWED_DETERMINISTIC for ins in rung.instructions)
        for compare_tag, from_state in comparisons:
            compare_identity = canonical_tag_identity(project, compare_tag, rung.program)
            if not identity_is_resolved(compare_identity):
                continue
            for move_tag, to_state in moves:
                move_identity = canonical_tag_identity(project, move_tag, rung.program)
                if not identity_is_resolved(move_identity) or move_identity != compare_identity:
                    continue
                key = (compare_identity, from_state, to_state)
                counts[key] += 1
                digest = hashlib.sha1(
                    f"{rung.id}:{compare_identity}:{from_state}:{to_state}:{counts[key]}".encode("utf-8")
                ).hexdigest()[:12]
                result.append(
                    RockwellStateTransition(
                        id=f"STATE-TRANSITION-{digest}",
                        rung_id=rung.id,
                        state_tag=move_tag,
                        from_state=from_state,
                        to_state=to_state,
                        deterministic_action=deterministic,
                        source=rung.source,
                    )
                )
    return result


def add_state_machine_edges(project, graph) -> None:
    seen = {(edge.source, edge.target, edge.kind, edge.evidence_id) for edge in graph.edges}
    for transition in state_transitions(project):
        source = f"{transition.state_tag}={transition.to_state}"
        target = f"{transition.state_tag}={transition.from_state}"
        key = (source, target, "STATE_TRANSITION", transition.id)
        if key in seen:
            continue
        seen.add(key)
        graph.edges.append(
            PLCDependencyEdge(
                source=source,
                target=target,
                kind="STATE_TRANSITION",
                evidence_id=transition.id,
            )
        )


def generate_state_machine_fat_tests(project) -> list[FATTestCase]:
    tests: list[FATTestCase] = []
    for transition in state_transitions(project):
        digest = hashlib.sha1(f"{transition.id}:runtime".encode("utf-8")).hexdigest()[:10]
        proof = "bounded deterministic local action" if transition.deterministic_action else "traceable source transition"
        tests.append(
            FATTestCase(
                id=f"FAT-STATE-{digest}",
                title=(
                    f"Exercise state transition {transition.state_tag}: "
                    f"{transition.from_state} -> {transition.to_state} at {transition.source.locator}"
                ),
                source=transition.source,
                output_tag=transition.state_tag,
                preconditions={},
                expected=(
                    f"Starting from {transition.state_tag}={transition.from_state}, establish the evidence-linked rung conditions "
                    f"and verify {transition.state_tag} transitions to {transition.to_state}; source classification: {proof}"
                ),
                limitations=(
                    "The generic FAT schema does not fabricate numeric/controller setup values; the qualified runtime adapter must establish the exact state and source-linked enabling conditions.",
                    "Later writers, task ordering, motion/AOI behavior, process physics, and controller faults require separate runtime evidence.",
                    "PASS requires authenticated qualified Logix Echo/HIL/controller evidence bound to the exact project and test-plan hashes.",
                ),
                scenario="STATE_TRANSITION_RUNTIME",
            )
        )
    return tests


def state_machine_check(project) -> StaticCheck:
    transitions = state_transitions(project)
    if not transitions:
        return StaticCheck(
            id="ROCKWELL_STATE_MACHINE_DISCOVERY",
            status=StaticCheckStatus.PASS,
            summary="No conventional reachable EQU(state, constant) + MOV(constant, state) transition pattern was discovered.",
        )
    deterministic = sum(1 for item in transitions if item.deterministic_action)
    runtime = len(transitions) - deterministic
    tags = sorted({item.state_tag for item in transitions}, key=str.casefold)
    return StaticCheck(
        id="ROCKWELL_STATE_MACHINE_DISCOVERY",
        status=StaticCheckStatus.WARN if runtime else StaticCheckStatus.PASS,
        summary=(
            f"Discovered {len(transitions)} transition(s) across {len(tags)} state tag(s): "
            f"{deterministic} bounded deterministic local action(s), {runtime} traceable runtime transition(s). "
            "Runtime-classified transitions are never promoted to PASS from source structure alone."
        ),
        evidence=tuple(item.rung_id for item in transitions),
    )


def state_machine_profile(project) -> dict[str, object]:
    transitions = state_transitions(project)
    by_tag: dict[str, list[dict[str, object]]] = defaultdict(list)
    for item in transitions:
        by_tag[item.state_tag].append(
            {
                "from": item.from_state,
                "to": item.to_state,
                "deterministic_action": item.deterministic_action,
                "evidence_id": item.rung_id,
            }
        )
    return {
        "schema": "devagent-rockwell-state-machine-v1",
        "transition_count": len(transitions),
        "state_tag_count": len(by_tag),
        "state_tags": {key: value for key, value in sorted(by_tag.items(), key=lambda item: item[0].casefold())},
        "runtime_evidence_required": any(not item.deterministic_action for item in transitions),
    }


__all__ = [
    "RockwellStateTransition",
    "add_state_machine_edges",
    "generate_state_machine_fat_tests",
    "state_machine_check",
    "state_machine_profile",
    "state_transitions",
]
