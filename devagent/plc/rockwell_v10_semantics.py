from __future__ import annotations

import re
from dataclasses import replace

from devagent.plc import production_verification as _verification
from devagent.plc.models import PLCSemanticState
from devagent.plc.production_models import (
    ExecutionStatus,
    RequirementStatus,
    RequirementVerification,
)
from devagent.plc.production_utils import explicit_bool
from devagent.plc.rockwell_alias_hardening import (
    canonical_tag_identity,
    distinct_named_tag_identities,
    identity_is_resolved,
)
from devagent.plc.rockwell_semantic_capabilities import retentive_action_value

_PREVIOUS_VERIFY_REQUIREMENT = _verification.verify_requirement
_PREVIOUS_PROMOTE_REQUIREMENT_EXECUTION = _verification.promote_requirement_execution
_PROGRAM_QUALIFIED = re.compile(
    r"Program:([^\.\s]+)\.([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)
_INSTALLED = False


def _qualified_program(text: str, output: str) -> str | None:
    programs = {
        match.group(1)
        for match in _PROGRAM_QUALIFIED.finditer(text)
        if match.group(2).casefold() == output.casefold()
    }
    return next(iter(programs)) if len(programs) == 1 else None


def _path_activation(logic, assignment: dict[str, bool]) -> str:
    """Return PROVEN, POSSIBLE, or IMPOSSIBLE for execution of one output action."""

    possible = 0
    definite = 0
    for path in logic.paths:
        contradicted = False
        complete = True
        for term in path.terms:
            if term.tag not in assignment:
                complete = False
                continue
            if assignment[term.tag] != term.required:
                contradicted = True
                break
        if contradicted:
            continue
        possible += 1
        if complete:
            definite += 1
    if definite:
        return "PROVEN"
    if possible:
        return "POSSIBLE"
    return "IMPOSSIBLE"


def _retentive_action_proof(requirement, engineering, tests, previous: RequirementVerification):
    if previous.status is not RequirementStatus.TRACEABLE_NOT_PROVEN:
        return previous

    project = engineering.project
    modeled_outputs = {
        logic.output_tag
        for logic in project.output_logic
        if logic.semantic_state is PLCSemanticState.FULL
        and not logic.origin.startswith("AOI_INTERNAL:")
        and retentive_action_value(logic.instruction) is not None
    }
    explicit_outputs = sorted(
        {
            tag
            for tag in previous.matched_tags
            if tag in modeled_outputs and explicit_bool(requirement.text, tag) is not None
        },
        key=str.casefold,
    )
    if len(explicit_outputs) != 1:
        return previous

    output = explicit_outputs[0]
    expected = explicit_bool(requirement.text, output)
    if expected is None:
        return previous

    qualified_program = _qualified_program(requirement.text, output)
    identities = distinct_named_tag_identities(project, output)
    if qualified_program is None and len(identities) > 1:
        return previous

    assignment = {
        tag: value
        for tag in previous.matched_tags
        if tag.casefold() != output.casefold()
        for value in [explicit_bool(requirement.text, tag)]
        if value is not None
    }
    if not assignment:
        return previous

    relevant = [
        logic
        for logic in project.output_logic
        if logic.semantic_state is PLCSemanticState.FULL
        and not logic.origin.startswith("AOI_INTERNAL:")
        and logic.output_tag.casefold() == output.casefold()
        and retentive_action_value(logic.instruction) is not None
        and (
            qualified_program is None
            or (logic.source.program or "").casefold() == qualified_program.casefold()
        )
    ]
    proven = []
    opposing_possible = []
    for logic in relevant:
        identity = canonical_tag_identity(project, logic.output_tag, logic.source.program)
        if not identity_is_resolved(identity):
            continue
        activation = _path_activation(logic, assignment)
        effect = retentive_action_value(logic.instruction)
        if activation == "PROVEN" and effect is expected:
            proven.append(logic)
        elif activation != "IMPOSSIBLE" and effect is not expected:
            opposing_possible.append(logic)

    if not proven:
        return previous

    proven_sources = {logic.source.locator for logic in proven}
    linked = set(previous.linked_test_ids)
    for test in tests:
        if test.output_tag.casefold() != output.casefold():
            continue
        if test.source.locator not in proven_sources:
            continue
        if test.scenario != "POSITIVE_PATH":
            continue
        if all(test.preconditions.get(tag) == value for tag, value in assignment.items()):
            linked.add(test.id)

    effects = ", ".join(
        f"{logic.instruction.upper()} at {logic.source.locator}"
        for logic in proven
    )
    summary = (
        f"Deterministic retentive action effect proven: {effects} writes "
        f"{output}={'TRUE' if expected else 'FALSE'} when the specified Boolean path executes. "
        "This proves the local instruction action only; retained/final scan state remains NOT_PROVEN "
        "until writer ordering is deterministically resolved or qualified dynamic execution supplies evidence."
    )
    if opposing_possible:
        summary += (
            f" {len(opposing_possible)} opposite retentive writer path(s) remain possible under the supplied conditions, "
            "so final-state proof is explicitly withheld."
        )

    return RequirementVerification(
        requirement_id=previous.requirement_id,
        status=RequirementStatus.ACTION_EFFECT_PROVEN,
        summary=summary,
        evidence_ids=tuple(
            dict.fromkeys(
                [*previous.evidence_ids, *(logic.id for logic in proven), *(logic.id for logic in opposing_possible)]
            )
        ),
        matched_tags=previous.matched_tags,
        linked_test_ids=tuple(sorted(linked)),
        confidence=1.0,
        ai_assisted=False,
    )


def verify_requirement(requirement, engineering, evidence, tests):
    """Extend the V9 fail-closed theorem with bounded OTL/OTU action-effect proof."""

    previous = _PREVIOUS_VERIFY_REQUIREMENT(requirement, engineering, evidence, tests)
    return _retentive_action_proof(requirement, engineering, tests, previous)


def promote_requirement_execution(verifications, executions):
    """Allow qualified execution to promote action-effect evidence to dynamic proof."""

    result = _PREVIOUS_PROMOTE_REQUIREMENT_EXECUTION(verifications, executions)
    statuses = {item.test_id: item.status for item in executions}
    promoted = []
    for item in result:
        if item.status is RequirementStatus.ACTION_EFFECT_PROVEN and item.linked_test_ids:
            known = [statuses.get(test_id, ExecutionStatus.NOT_RUN) for test_id in item.linked_test_ids]
            if known and all(status is ExecutionStatus.PASS for status in known):
                item = replace(
                    item,
                    status=RequirementStatus.DYNAMICALLY_VERIFIED,
                    summary=item.summary
                    + " Linked qualified-backend execution evidence passed, promoting the requirement to dynamic verification.",
                )
            elif any(status is ExecutionStatus.FAIL for status in known):
                item = replace(
                    item,
                    status=RequirementStatus.CONFLICT,
                    summary=item.summary
                    + " Linked qualified-backend execution evidence contains a FAIL result.",
                )
        promoted.append(item)
    return promoted


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _verification.verify_requirement = verify_requirement
    _verification.promote_requirement_execution = promote_requirement_execution
    _INSTALLED = True


__all__ = ["install", "promote_requirement_execution", "verify_requirement"]
