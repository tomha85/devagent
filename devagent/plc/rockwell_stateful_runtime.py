from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib

from devagent.plc.models import (
    FATTestCase,
    PLCBooleanTerm,
    PLCLogicPath,
    PLCSemanticState,
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


_STATEFUL = frozenset({"TON", "TOF", "RTO", "CTU", "CTD"})
_CONDITIONS = frozenset({"XIC", "XIO"})
_ALLOWED = _STATEFUL | _CONDITIONS | {"NOP"}
_WARNING_PREFIX = "Rockwell V10 stateful runtime semantics: "


@dataclass(frozen=True)
class RockwellStatefulModel:
    id: str
    rung_id: str
    instruction: str
    structure_tag: str
    input_refs: tuple[str, ...]
    paths: tuple[PLCLogicPath, ...]
    runtime_expectation: str
    source: object


def _fixed_ref(value: str) -> str | None:
    if _has_variable_subscript(value):
        return None
    return _first_ref(value)


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


def _expectation(name: str, tag: str) -> str:
    if name == "TON":
        return (
            f"With the modeled rung path held TRUE, {tag}.EN becomes TRUE, {tag}.ACC advances using controller time, "
            f"and {tag}.DN becomes TRUE only after the configured preset is satisfied"
        )
    if name == "TOF":
        return (
            f"After first establishing the modeled rung path TRUE, transition it FALSE; {tag} performs off-delay timing "
            f"and {tag}.DN clears only after the configured preset is satisfied"
        )
    if name == "RTO":
        return (
            f"With the modeled rung path TRUE, {tag}.ACC accumulates using controller time and retains accumulated state "
            f"across a FALSE rung condition until the configured reset action is executed"
        )
    if name == "CTU":
        return (
            f"Drive the modeled rung path FALSE then TRUE; {tag}.ACC increments on the false-to-true transition "
            f"and {tag}.DN reflects the configured preset comparison"
        )
    return (
        f"Drive the modeled rung path FALSE then TRUE; {tag}.ACC decrements on the false-to-true transition "
        f"and {tag}.DN reflects the configured counter state"
    )


def _walk(nodes, incoming, occurrences):
    states = incoming
    for node in nodes:
        if isinstance(node, _Branch):
            endings: list[dict[str, bool]] = []
            for branch in node.paths:
                endings.extend(_walk(branch, [dict(item) for item in states], occurrences))
            states = _dedupe_states(endings)
            continue

        name = node.name.upper()
        if name in _CONDITIONS:
            if not node.arguments:
                return []
            tag = _fixed_ref(node.arguments[0])
            if tag is None:
                return []
            required = name == "XIC"
            updated_states: list[dict[str, bool]] = []
            for state in states:
                updated = _merge_term(state, tag, required)
                if updated is not None:
                    updated_states.append(updated)
            states = _dedupe_states(updated_states)
            continue

        if name in _STATEFUL:
            if not node.arguments:
                return []
            structure = _fixed_ref(node.arguments[0])
            if structure is None:
                return []
            input_refs: list[str] = []
            for argument in node.arguments[1:]:
                for ref in _refs(argument):
                    if ref not in input_refs:
                        input_refs.append(ref)
            occurrences.append((node, structure, tuple(input_refs), [dict(item) for item in states]))
            continue

        if name == "NOP":
            continue
        return []
    return states


def stateful_models_for_rung(project, rung) -> list[RockwellStatefulModel]:
    if not rung.instructions or not any(item.name.upper() in _STATEFUL for item in rung.instructions):
        return []
    if any(item.name.upper() not in _ALLOWED for item in rung.instructions):
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

    result: list[RockwellStatefulModel] = []
    counts: Counter[tuple[str, str]] = Counter()
    for instruction, structure, input_refs, states in occurrences:
        paths = _paths(states)
        if not paths:
            continue
        name = instruction.name.upper()
        key = (name, structure.casefold())
        counts[key] += 1
        digest = hashlib.sha1(
            f"{rung.id}:{name}:{structure}:{counts[key]}".encode("utf-8")
        ).hexdigest()[:12]
        result.append(
            RockwellStatefulModel(
                id=f"STATEFUL-RLL-{digest}",
                rung_id=rung.id,
                instruction=name,
                structure_tag=structure,
                input_refs=input_refs,
                paths=paths,
                runtime_expectation=_expectation(name, structure),
                source=rung.source,
            )
        )
    return result


def stateful_models(project) -> list[RockwellStatefulModel]:
    result: list[RockwellStatefulModel] = []
    for rung in tuple(getattr(project, "rungs", ()) or ()):
        result.extend(stateful_models_for_rung(project, rung))
    return result


def augment_stateful_semantics(project) -> None:
    """Mark time/edge-dependent timer/counter semantics PARTIAL even when operands parse."""
    names = sorted(
        {
            instruction.name.upper()
            for rung in project.rungs
            for instruction in rung.instructions
            if instruction.name.upper() in _STATEFUL
        }
    )
    if not names:
        return
    project.partially_modeled_instruction_names = sorted(
        set(project.partially_modeled_instruction_names) | set(names), key=str.casefold
    )
    retained = [warning for warning in project.warnings if not warning.startswith(_WARNING_PREFIX)]
    retained.append(
        _WARNING_PREFIX
        + "timer/counter instruction(s) require engineer FAT observation of time/edge behavior: "
        + ", ".join(names)
    )
    project.warnings = retained


def generate_stateful_fat_tests(project) -> list[FATTestCase]:
    tests: list[FATTestCase] = []
    for model in stateful_models(project):
        for index, path in enumerate(model.paths, start=1):
            preconditions = {term.tag: term.required for term in path.terms}
            digest = hashlib.sha1(f"{model.id}:fat:{index}".encode("utf-8")).hexdigest()[:10]
            suffix = f" path {index}" if len(model.paths) > 1 else ""
            tests.append(
                FATTestCase(
                    id=f"FAT-STATEFUL-{digest}",
                    title=f"FAT-check {model.instruction} {model.structure_tag}{suffix} at {model.source.locator}",
                    source=model.source,
                    output_tag=model.structure_tag,
                    preconditions=dict(sorted(preconditions.items())),
                    expected=model.runtime_expectation,
                    limitations=(
                        "This is an engineer-executed FAT recommendation; static source analysis does not execute controller time, prescan, edge storage, or retentive state.",
                        "DevAgent does not connect to or execute the external simulator/HIL/controller used by the engineer for this FAT procedure.",
                    ),
                    scenario="STATEFUL_RUNTIME",
                )
            )
    return tests


def stateful_runtime_check(project) -> StaticCheck:
    candidates = [
        rung
        for rung in project.rungs
        if routine_has_execution_entry(project, rung.program, rung.routine)
        and any(item.name.upper() in _STATEFUL for item in rung.instructions)
    ]
    if not candidates:
        return StaticCheck(
            id="ROCKWELL_STATEFUL_RUNTIME_SEMANTICS",
            status=StaticCheckStatus.PASS,
            summary="No reachable TON/TOF/RTO/CTU/CTD instructions require stateful FAT modeling.",
        )
    models = stateful_models(project)
    modeled_rungs = {model.rung_id for model in models}
    withheld = [rung.id for rung in candidates if rung.id not in modeled_rungs]
    return StaticCheck(
        id="ROCKWELL_STATEFUL_RUNTIME_SEMANTICS",
        status=StaticCheckStatus.WARN,
        summary=(
            f"Generated bounded FAT semantics for {len(models)} timer/counter occurrence(s) across {len(modeled_rungs)} rung(s). "
            "Timer/counter behavior remains PARTIAL until the PLC engineer performs the recommended FAT procedure."
            + (f" {len(withheld)} reachable stateful rung(s) are also withheld from FAT generation." if withheld else "")
        ),
        evidence=tuple(withheld),
    )


def stateful_profile(project) -> dict[str, object]:
    models = stateful_models(project)
    counts = Counter(model.instruction for model in models)
    return {
        "schema": "devagent-rockwell-stateful-runtime-v1",
        "modeled_occurrences": len(models),
        "instructions": dict(sorted(counts.items())),
        "requires_qualified_runtime_evidence": bool(models),
    }


__all__ = [
    "RockwellStatefulModel",
    "augment_stateful_semantics",
    "generate_stateful_fat_tests",
    "stateful_models",
    "stateful_profile",
    "stateful_runtime_check",
]
