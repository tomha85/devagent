from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib

from devagent.plc.models import (
    FATTestCase,
    PLCBooleanTerm,
    PLCDependencyEdge,
    PLCLogicPath,
    PLCSourceRef,
    StaticCheck,
    StaticCheckStatus,
)
from devagent.plc.rockwell_entrypoint_hardening import routine_has_execution_entry
from devagent.plc.v2_semantics import (
    _Branch,
    _dedupe_states,
    _first_ref,
    _has_variable_subscript,
    _merge_term,
    _parse_sequence,
    _refs,
    _scan_neutral_tokens,
    _state_key,
)


_BOOLEAN_CONDITIONS = frozenset({"XIC", "XIO"})
_TRANSPARENT = frozenset({"NOP"})
_BINARY_ACTIONS = {
    "ADD": "+",
    "SUB": "-",
    "MUL": "*",
    "DIV": "/",
    "MOD": "MOD",
    "AND": "AND",
    "OR": "OR",
    "XOR": "XOR",
}
_UNARY_ACTIONS = {
    "SQR": "SQR",
    "SQRT": "SQRT",
    "ABS": "ABS",
    "NEG": "NEG",
}
_ACTION_INSTRUCTIONS = frozenset(
    {
        "MOV",
        "MOVE",
        "COP",
        "CPS",
        "CLR",
        "CPT",
        "RES",
        *_BINARY_ACTIONS,
        *_UNARY_ACTIONS,
    }
)
_ALLOWED = _BOOLEAN_CONDITIONS | _TRANSPARENT | _ACTION_INSTRUCTIONS


@dataclass(frozen=True)
class RockwellActionModel:
    id: str
    rung_id: str
    instruction: str
    family: str
    output_tag: str
    input_refs: tuple[str, ...]
    paths: tuple[PLCLogicPath, ...]
    expected_effect: str
    source: PLCSourceRef


def _fixed_ref(value: str) -> str | None:
    if _has_variable_subscript(value):
        return None
    return _first_ref(value)


def _effect(instruction):
    name = instruction.name.upper()
    args = instruction.arguments

    if name in {"MOV", "MOVE"} and len(args) >= 2:
        destination = _fixed_ref(args[1])
        if destination is None:
            return None
        return (
            "DATA_MOVE",
            destination,
            tuple(_refs(args[0])),
            f"{destination} receives the value of {args[0].strip()} when the modeled rung path executes",
        )

    if name in {"COP", "CPS"} and len(args) >= 3:
        destination = _fixed_ref(args[1])
        if destination is None:
            return None
        refs = tuple(dict.fromkeys([*_refs(args[0]), *_refs(args[2])]))
        return (
            "DATA_COPY",
            destination,
            refs,
            f"{destination} receives {name} data copied from {args[0].strip()} for length {args[2].strip()} when the modeled rung path executes",
        )

    if name == "CLR" and args:
        destination = _fixed_ref(args[0])
        if destination is None:
            return None
        return (
            "CLEAR",
            destination,
            (),
            f"{destination}=0 when the modeled rung path executes",
        )

    if name in _BINARY_ACTIONS and len(args) >= 3:
        destination = _fixed_ref(args[-1])
        if destination is None:
            return None
        left = args[0].strip()
        right = args[1].strip()
        refs = tuple(dict.fromkeys([*_refs(left), *_refs(right)]))
        operator = _BINARY_ACTIONS[name]
        return (
            "COMPUTE",
            destination,
            refs,
            f"{destination} receives the result of {left} {operator} {right} when the modeled rung path executes",
        )

    if name in _UNARY_ACTIONS and len(args) >= 2:
        destination = _fixed_ref(args[-1])
        if destination is None:
            return None
        operand = args[0].strip()
        refs = tuple(_refs(operand))
        return (
            "COMPUTE",
            destination,
            refs,
            f"{destination} receives {name}({operand}) when the modeled rung path executes",
        )

    if name == "CPT" and len(args) >= 2:
        destination = _fixed_ref(args[0])
        if destination is None:
            return None
        expression = args[1].strip()
        return (
            "COMPUTE_EXPRESSION",
            destination,
            tuple(_refs(expression)),
            f"{destination} receives CPT expression {expression} when the modeled rung path executes",
        )

    if name == "RES" and args:
        destination = _fixed_ref(args[0])
        if destination is None:
            return None
        return (
            "RESET_STATE",
            destination,
            (),
            f"{destination} is reset by RES when the modeled rung path executes",
        )

    return None


def _paths(states: list[dict[str, bool]]) -> tuple[PLCLogicPath, ...]:
    result: list[PLCLogicPath] = []
    seen: set[tuple[tuple[str, bool], ...]] = set()
    for state in states:
        key = _state_key(state)
        if key in seen:
            continue
        seen.add(key)
        result.append(
            PLCLogicPath(
                terms=tuple(PLCBooleanTerm(tag=tag, required=value) for tag, value in key)
            )
        )
    return tuple(result)


def _walk(nodes, incoming, occurrences):
    states = incoming
    for node in nodes:
        if isinstance(node, _Branch):
            branch_end: list[dict[str, bool]] = []
            for branch in node.paths:
                branch_end.extend(_walk(branch, [dict(item) for item in states], occurrences))
            states = _dedupe_states(branch_end)
            continue

        name = node.name.upper()
        if name in _BOOLEAN_CONDITIONS:
            if not node.arguments:
                return []
            tag = _fixed_ref(node.arguments[0])
            if tag is None:
                return []
            required = name == "XIC"
            next_states: list[dict[str, bool]] = []
            for state in states:
                updated = _merge_term(state, tag, required)
                if updated is not None:
                    next_states.append(updated)
            states = _dedupe_states(next_states)
            continue

        if name in _ACTION_INSTRUCTIONS:
            effect = _effect(node)
            if effect is None:
                return []
            occurrences.append((node, effect, [dict(item) for item in states]))
            continue

        if name in _TRANSPARENT:
            continue

        return []
    return states


def action_models_for_rung(project, rung) -> list[RockwellActionModel]:
    """Return bounded action effects only when the complete rung grammar is understood.

    The theorem intentionally excludes compare gates, timers/counters, motion,
    AOIs, program-control, indirect destinations, and any unknown instruction.
    Those surfaces remain structural/PARTIAL until a dedicated theorem models
    their execution semantics.
    """
    if not rung.instructions or any(item.name.upper() not in _ALLOWED for item in rung.instructions):
        return []
    if not routine_has_execution_entry(project, rung.program, rung.routine):
        return []

    try:
        tokens = _scan_neutral_tokens(rung.text)
        nodes, index = _parse_sequence(tokens)
        if index != len(tokens):
            return []
        occurrences = []
        _walk(nodes, [{}], occurrences)
    except (AttributeError, TypeError, ValueError):
        return []

    result: list[RockwellActionModel] = []
    occurrence_counter: Counter[tuple[str, str]] = Counter()
    for instruction, effect, states in occurrences:
        family, output_tag, input_refs, expected_effect = effect
        paths = _paths(states)
        if not paths:
            continue
        key = (instruction.name.upper(), output_tag.casefold())
        occurrence_counter[key] += 1
        ordinal = occurrence_counter[key]
        digest = hashlib.sha1(
            f"{rung.id}:{instruction.name.upper()}:{output_tag}:{ordinal}".encode("utf-8")
        ).hexdigest()[:12]
        result.append(
            RockwellActionModel(
                id=f"ACTION-RLL-{digest}",
                rung_id=rung.id,
                instruction=instruction.name.upper(),
                family=family,
                output_tag=output_tag,
                input_refs=tuple(input_refs),
                paths=paths,
                expected_effect=expected_effect,
                source=rung.source,
            )
        )
    return result


def action_models(project) -> list[RockwellActionModel]:
    result: list[RockwellActionModel] = []
    for rung in project.rungs:
        result.extend(action_models_for_rung(project, rung))
    return result


def add_action_dependencies(project, graph) -> None:
    seen = {(edge.source, edge.target, edge.kind, edge.evidence_id) for edge in graph.edges}
    for model in action_models(project):
        dependencies = list(model.input_refs)
        dependencies.extend(term.tag for path in model.paths for term in path.terms)
        for dependency in dict.fromkeys(dependencies):
            key = (model.output_tag, dependency, "DEPENDS_ON", model.id)
            if key in seen:
                continue
            seen.add(key)
            graph.edges.append(
                PLCDependencyEdge(
                    source=model.output_tag,
                    target=dependency,
                    kind="DEPENDS_ON",
                    evidence_id=model.id,
                )
            )


def generate_action_fat_tests(project) -> list[FATTestCase]:
    tests: list[FATTestCase] = []
    for model in action_models(project):
        for index, path in enumerate(model.paths, start=1):
            preconditions = {term.tag: term.required for term in path.terms}
            digest = hashlib.sha1(f"{model.id}:path:{index}".encode("utf-8")).hexdigest()[:10]
            suffix = f" path {index}" if len(model.paths) > 1 else ""
            tests.append(
                FATTestCase(
                    id=f"FAT-ACTION-{digest}",
                    title=f"Exercise {model.instruction} for {model.output_tag}{suffix} at {model.source.locator}",
                    source=model.source,
                    output_tag=model.output_tag,
                    preconditions=dict(sorted(preconditions.items())),
                    expected=model.expected_effect,
                    limitations=(
                        "Generated from deterministic rung-in/action semantics; no PLC scan was executed.",
                        "The instruction effect is modeled, but process physics, asynchronous modules, faults, task preemption, and later writers require separate evidence.",
                    ),
                    scenario="ACTION_PATH",
                )
            )
    return tests


def rockwell_action_check(project) -> StaticCheck:
    candidate_rungs = [
        rung
        for rung in project.rungs
        if routine_has_execution_entry(project, rung.program, rung.routine)
        and any(item.name.upper() in _ACTION_INSTRUCTIONS for item in rung.instructions)
    ]
    models = action_models(project)
    modeled_rungs = {item.rung_id for item in models}
    withheld = [rung.id for rung in candidate_rungs if rung.id not in modeled_rungs]
    return StaticCheck(
        id="ROCKWELL_ACTION_PATH_SEMANTICS",
        status=StaticCheckStatus.PASS if not withheld else StaticCheckStatus.WARN,
        summary=(
            f"Modeled {len(models)} deterministic data/compute action occurrence(s) across {len(modeled_rungs)} reachable RLL rung(s)."
            if not withheld
            else f"Modeled {len(models)} deterministic data/compute action occurrence(s); {len(withheld)} reachable action-bearing rung(s) remain withheld because their complete rung grammar, destination identity, or control semantics are not yet proven."
        ),
        evidence=tuple(withheld),
    )


def action_profile(project) -> dict[str, object]:
    models = action_models(project)
    by_instruction = Counter(item.instruction for item in models)
    by_family = Counter(item.family for item in models)
    candidate_rungs = [
        rung
        for rung in project.rungs
        if routine_has_execution_entry(project, rung.program, rung.routine)
        and any(item.name.upper() in _ACTION_INSTRUCTIONS for item in rung.instructions)
    ]
    modeled_rungs = {item.rung_id for item in models}
    return {
        "schema": "devagent-rockwell-action-semantics-v1",
        "modeled_actions": len(models),
        "modeled_rungs": len(modeled_rungs),
        "candidate_rungs": len(candidate_rungs),
        "withheld_rungs": len([rung for rung in candidate_rungs if rung.id not in modeled_rungs]),
        "instructions": dict(sorted(by_instruction.items())),
        "families": dict(sorted(by_family.items())),
    }


__all__ = [
    "RockwellActionModel",
    "action_models",
    "action_models_for_rung",
    "action_profile",
    "add_action_dependencies",
    "generate_action_fat_tests",
    "rockwell_action_check",
]
