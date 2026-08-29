from __future__ import annotations

from typing import Iterable

from .diagnosis import observations_from_reconciled
from .engineering_context import LiveEngineeringContext, normalize_engineering_identifier
from .reconciled_evidence import ReconciledLiveAgentEvidence
from .stateful_context import LiveStatefulDiagnosis, LiveStatefulModel


def resolve_stateful_model(
    models: Iterable[LiveStatefulModel],
    question: str,
) -> LiveStatefulModel | None:
    text = normalize_engineering_identifier(question)
    if not text:
        return None
    direct = [
        model
        for model in models
        if normalize_engineering_identifier(model.name)
        and normalize_engineering_identifier(model.name) in text
    ]
    if len(direct) == 1:
        return direct[0]
    if len(direct) > 1:
        longest = max(len(normalize_engineering_identifier(item.name)) for item in direct)
        narrowed = [
            item
            for item in direct
            if len(normalize_engineering_identifier(item.name)) == longest
        ]
        return narrowed[0] if len(narrowed) == 1 else None

    lowered = str(question or "").casefold()
    typed = [
        model
        for model in models
        if model.instruction.casefold() in lowered
    ]
    return typed[0] if len(typed) == 1 else None


def required_stateful_tag_ids(
    context: LiveEngineeringContext,
    model: LiveStatefulModel,
) -> tuple[str, ...]:
    references: list[str] = []
    if model.kind.value == "STATE_MACHINE":
        references.append(model.name)
    for path in model.guard_paths:
        references.extend(name for name, _required in path)
    for transition in model.transitions:
        for path in transition.guard_paths:
            references.extend(name for name, _required in path)

    result: list[str] = []
    for reference in references:
        tag = context.unique_tag_for_reference(reference)
        if tag is not None and tag.id not in result:
            result.append(tag.id)
    return tuple(result)


def stateful_observation_map(
    reconciled: ReconciledLiveAgentEvidence,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for item in observations_from_reconciled(reconciled):
        if not item.definitive_current:
            continue
        result[item.tag_name] = item.value
    return result


def render_stateful_diagnosis(diagnosis: LiveStatefulDiagnosis) -> str:
    lines = [
        "Stateful/sequence diagnosis (read-only, deterministic):",
        f"- Model: {diagnosis.name}",
        f"- Status: {diagnosis.status.value}",
    ]
    if diagnosis.current_state is not None:
        lines.append(f"- Current state: {diagnosis.current_state}")
    if diagnosis.candidate_targets:
        lines.append("- Ready target(s): " + ", ".join(diagnosis.candidate_targets))
    if diagnosis.blocking_conditions:
        lines.append("- Blocking condition(s):")
        lines.extend(f"  - {item}" for item in diagnosis.blocking_conditions)
    if diagnosis.unknown_conditions:
        lines.append("- Missing/untrusted condition(s): " + ", ".join(diagnosis.unknown_conditions))
    if diagnosis.source_locators:
        lines.append("- PLC source:")
        lines.extend(f"  - {item}" for item in diagnosis.source_locators)
    lines.append("- " + diagnosis.detail)
    return "\n".join(lines)


__all__ = [
    "resolve_stateful_model",
    "required_stateful_tag_ids",
    "stateful_observation_map",
    "render_stateful_diagnosis",
]
