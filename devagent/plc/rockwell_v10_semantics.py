from __future__ import annotations

from collections import Counter
from dataclasses import replace
import re
from typing import Callable

from devagent.plc import production_verification as _verification
from devagent.plc.models import PLCSemanticState
from devagent.plc.production_models import ExecutionStatus, RequirementStatus, RequirementVerification
from devagent.plc.production_utils import explicit_bool
from devagent.plc.rockwell_alias_hardening import (
    canonical_tag_identity,
    canonical_writer_sources,
    distinct_named_tag_identities,
    identity_is_resolved,
    storage_identities_overlap,
)
from devagent.plc.rockwell_entrypoint_hardening import routine_has_execution_entry
from devagent.plc.rockwell_semantic_capabilities import retentive_action_value

_PREVIOUS_VERIFY_REQUIREMENT: Callable | None = None
_PREVIOUS_PROMOTE_REQUIREMENT_EXECUTION: Callable | None = None
_PROGRAM_QUALIFIED = re.compile(r"Program:([^\.\s]+)\.([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_INSTALLED = False


def _qualified_program(text: str, output: str) -> str | None:
    programs = {
        match.group(1)
        for match in _PROGRAM_QUALIFIED.finditer(text)
        if match.group(2).casefold() == output.casefold()
    }
    return next(iter(programs)) if len(programs) == 1 else None


def _resolved_target(project, name: str, qualified_program: str | None):
    if qualified_program:
        identity = canonical_tag_identity(project, name, qualified_program)
        return identity if identity_is_resolved(identity) else None
    identities = distinct_named_tag_identities(project, name)
    if len(identities) != 1 or not identity_is_resolved(identities[0]):
        return None
    return identities[0]


def _path_activation(logic, assignment: dict[str, bool]) -> str:
    """Return PROVEN, POSSIBLE, or IMPOSSIBLE for one Boolean action path set."""
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


def _rung_for_logic(project, logic):
    if not logic.source.program or not logic.source.routine or logic.source.rung is None:
        return None
    matches = [
        rung
        for rung in project.rungs
        if rung.program.casefold() == logic.source.program.casefold()
        and rung.routine.casefold() == logic.source.routine.casefold()
        and str(rung.source.rung if rung.source.rung is not None else rung.number) == str(logic.source.rung)
    ]
    return matches[0] if len(matches) == 1 else None


def _condition_writer_program(project, tag: str, default_program: str) -> str | None:
    identities = distinct_named_tag_identities(project, tag)
    if len(identities) != 1 or not identity_is_resolved(identities[0]):
        return None
    scope = identities[0][0]
    prefix = "program:"
    if scope.startswith(prefix):
        return scope[len(prefix) :]
    return default_program


def _same_routine_scan_final_state(project, output: str, program: str | None, assignment, relevant):
    """Prove final OTL/OTU state only inside one active Main RLL routine.

    Every reachable writer occurrence touching the canonical output storage must
    be represented by exactly one FULL retentive output-logic object. Writer
    rungs must be uniquely ordered, and explicit condition tags must not be
    rewritten by reachable PLC logic. Any wider execution problem fails closed.
    """
    if not program or not assignment or not relevant:
        return None

    programs = [item for item in project.programs if item.name.casefold() == program.casefold()]
    if len(programs) != 1 or not programs[0].main_routine_name:
        return None
    main_routine = programs[0].main_routine_name

    if any(
        not logic.source.program
        or not logic.source.routine
        or logic.source.program.casefold() != program.casefold()
        or logic.source.routine.casefold() != main_routine.casefold()
        or logic.language.upper() != "RLL"
        or logic.semantic_state is not PLCSemanticState.FULL
        or retentive_action_value(logic.instruction) is None
        for logic in relevant
    ):
        return None
    if not routine_has_execution_entry(project, program, main_routine):
        return None

    # First V10 ordering theorem treats requirement conditions as stable values
    # at scan entry. Internally rewritten conditions need a later dataflow theorem.
    for tag in assignment:
        condition_program = _condition_writer_program(project, tag, program)
        if condition_program is None or canonical_writer_sources(project, tag, condition_program):
            return None

    rung_by_logic = []
    for logic in relevant:
        rung = _rung_for_logic(project, logic)
        if rung is None:
            return None
        rung_by_logic.append((logic, rung))

    # The IR currently has rung-level, not instruction-index, source identity.
    # Two writes to the same storage on one rung therefore remain ambiguous.
    expected_occurrences = [rung.id for _, rung in rung_by_logic]
    if len(set(expected_occurrences)) != len(expected_occurrences):
        return None
    if Counter(canonical_writer_sources(project, output, program)) != Counter(expected_occurrences):
        return None

    routine_rungs = [
        rung
        for rung in project.rungs
        if rung.program.casefold() == program.casefold()
        and rung.routine.casefold() == main_routine.casefold()
    ]
    order = {rung.id: index for index, rung in enumerate(routine_rungs)}
    if any(rung.id not in order for _, rung in rung_by_logic):
        return None

    state: bool | None = None
    for logic, rung in sorted(rung_by_logic, key=lambda item: order[item[1].id]):
        activation = _path_activation(logic, assignment)
        effect = retentive_action_value(logic.instruction)
        assert effect is not None
        if activation == "PROVEN":
            state = effect
        elif activation == "POSSIBLE" and state is not effect:
            state = None
    return state


def _retentive_action_proof(requirement, engineering, tests, previous: RequirementVerification):
    if previous.status is not RequirementStatus.TRACEABLE_NOT_PROVEN:
        return previous

    project = engineering.project
    candidates = []
    for tag in previous.matched_tags:
        if explicit_bool(requirement.text, tag) is None:
            continue
        qualified_program = _qualified_program(requirement.text, tag)
        target = _resolved_target(project, tag, qualified_program)
        if target is None:
            continue
        if any(
            logic.semantic_state is PLCSemanticState.FULL
            and not logic.origin.startswith("AOI_INTERNAL:")
            and retentive_action_value(logic.instruction) is not None
            and identity_is_resolved(
                identity := canonical_tag_identity(project, logic.output_tag, logic.source.program)
            )
            and storage_identities_overlap(identity, target)
            for logic in project.output_logic
        ):
            candidates.append((tag, qualified_program, target))
    if len(candidates) != 1:
        return previous

    output, qualified_program, target = candidates[0]
    expected = explicit_bool(requirement.text, output)
    if expected is None:
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

    relevant = []
    for logic in project.output_logic:
        if (
            logic.semantic_state is not PLCSemanticState.FULL
            or logic.origin.startswith("AOI_INTERNAL:")
            or retentive_action_value(logic.instruction) is None
        ):
            continue
        identity = canonical_tag_identity(project, logic.output_tag, logic.source.program)
        if not identity_is_resolved(identity) or not storage_identities_overlap(identity, target):
            continue
        if qualified_program is not None and (
            not logic.source.program or logic.source.program.casefold() != qualified_program.casefold()
        ):
            continue
        relevant.append(logic)

    proven = []
    opposing_possible = []
    for logic in relevant:
        activation = _path_activation(logic, assignment)
        effect = retentive_action_value(logic.instruction)
        if activation == "PROVEN" and effect is expected:
            proven.append(logic)
        elif activation != "IMPOSSIBLE" and effect is not expected:
            opposing_possible.append(logic)

    theorem_program = qualified_program
    if theorem_program is None:
        programs = {logic.source.program for logic in relevant if logic.source.program}
        if len(programs) == 1:
            theorem_program = next(iter(programs))

    final_state = _same_routine_scan_final_state(project, output, theorem_program, assignment, relevant)
    if final_state is not None:
        status = RequirementStatus.STATICALLY_VERIFIED if final_state is expected else RequirementStatus.CONFLICT
        summary = (
            f"Deterministic same-routine Rockwell scan ordering proves final {output}="
            f"{'TRUE' if final_state else 'FALSE'} for the specified stable Boolean conditions. "
            "All reachable writers touching the canonical storage are FULL OTL/OTU actions in one active Main RLL routine, "
            "and their rung order is explicit in the authenticated L5X. "
            "This is a bounded software scan-state proof; physical I/O, controller scheduling outside this routine, and process behavior are not inferred."
        )
        return RequirementVerification(
            requirement_id=previous.requirement_id,
            status=status,
            summary=summary,
            evidence_ids=tuple(dict.fromkeys([*previous.evidence_ids, *(logic.id for logic in relevant)])),
            matched_tags=previous.matched_tags,
            linked_test_ids=(),
            confidence=1.0,
            ai_assisted=False,
        )

    if not proven:
        return previous

    proven_sources = {logic.source.locator for logic in proven}
    linked = set(previous.linked_test_ids)
    for test in tests:
        test_identity = canonical_tag_identity(project, test.output_tag, test.source.program)
        if not identity_is_resolved(test_identity) or not storage_identities_overlap(test_identity, target):
            continue
        if test.source.locator not in proven_sources or test.scenario != "POSITIVE_PATH":
            continue
        if all(test.preconditions.get(tag) == value for tag, value in assignment.items()):
            linked.add(test.id)

    effects = ", ".join(f"{logic.instruction.upper()} at {logic.source.locator}" for logic in proven)
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
            dict.fromkeys([*previous.evidence_ids, *(logic.id for logic in proven), *(logic.id for logic in opposing_possible)])
        ),
        matched_tags=previous.matched_tags,
        linked_test_ids=tuple(sorted(linked)),
        confidence=1.0,
        ai_assisted=False,
    )


def verify_requirement(requirement, engineering, evidence, tests):
    """Extend the installed V9 theorem with bounded OTL/OTU action and scan-state proof."""
    if _PREVIOUS_VERIFY_REQUIREMENT is None:  # pragma: no cover
        raise RuntimeError("Rockwell V10 semantics were not installed")
    previous = _PREVIOUS_VERIFY_REQUIREMENT(requirement, engineering, evidence, tests)
    return _retentive_action_proof(requirement, engineering, tests, previous)


def promote_requirement_execution(verifications, executions):
    """Allow qualified execution to promote action-effect evidence to dynamic proof."""
    if _PREVIOUS_PROMOTE_REQUIREMENT_EXECUTION is None:  # pragma: no cover
        raise RuntimeError("Rockwell V10 semantics were not installed")
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
                    summary=item.summary + " Linked qualified-backend execution evidence passed, promoting the requirement to dynamic verification.",
                )
            elif any(status is ExecutionStatus.FAIL for status in known):
                item = replace(
                    item,
                    status=RequirementStatus.CONFLICT,
                    summary=item.summary + " Linked qualified-backend execution evidence contains a FAIL result.",
                )
        promoted.append(item)
    return promoted


def install() -> None:
    """Install after V9 writer/alias/reachability hardening so it cannot be bypassed."""
    global _INSTALLED, _PREVIOUS_VERIFY_REQUIREMENT, _PREVIOUS_PROMOTE_REQUIREMENT_EXECUTION
    if _INSTALLED:
        return
    _PREVIOUS_VERIFY_REQUIREMENT = _verification.verify_requirement
    _PREVIOUS_PROMOTE_REQUIREMENT_EXECUTION = _verification.promote_requirement_execution
    _verification.verify_requirement = verify_requirement
    _verification.promote_requirement_execution = promote_requirement_execution
    _INSTALLED = True


__all__ = ["install", "promote_requirement_execution", "verify_requirement"]
