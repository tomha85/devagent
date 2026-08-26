from __future__ import annotations

import re

from devagent.plc import production_verification as _verification
from devagent.plc import rockwell_compare as _compare
from devagent.plc.models import PLCSemanticState
from devagent.plc.production_models import RequirementStatus, RequirementVerification
from devagent.plc.production_utils import explicit_bool
from devagent.plc.rockwell_alias_hardening import (
    canonical_tag_identity,
    canonical_writer_sources,
    distinct_named_tag_identities,
    identity_is_resolved,
)

_ORIGINAL_VERIFY_REQUIREMENT = _verification.verify_requirement
_PROGRAM_QUALIFIED = re.compile(r"Program:([^\.\s]+)\.([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def _qualified_program(text: str, output: str) -> str | None:
    programs = {
        match.group(1)
        for match in _PROGRAM_QUALIFIED.finditer(text)
        if match.group(2).casefold() == output.casefold()
    }
    return next(iter(programs)) if len(programs) == 1 else None


def _unsafe(result: RequirementVerification, summary: str, evidence_ids=()) -> RequirementVerification:
    return RequirementVerification(
        requirement_id=result.requirement_id,
        status=RequirementStatus.TRACEABLE_NOT_PROVEN,
        summary=summary,
        evidence_ids=tuple(dict.fromkeys([*result.evidence_ids, *evidence_ids])),
        matched_tags=result.matched_tags,
        linked_test_ids=(),
        confidence=result.confidence,
        ai_assisted=result.ai_assisted,
    )


def _proof_target(requirement, engineering, result: RequirementVerification):
    """Resolve the exact output/program already selected by the base theorem.

    Boolean proofs cite one FULL PLCOutputLogic object. Typed compare proofs cite
    the compare rung. We deliberately derive the target from that evidence rather
    than counting every explicit Boolean assignment in the requirement, because
    antecedent inputs such as Start=TRUE and Guard=TRUE are not outputs.
    """

    project = engineering.project
    logic_by_id = {
        logic.id: logic
        for logic in project.output_logic
        if logic.semantic_state is PLCSemanticState.FULL
        and not logic.origin.startswith("AOI_INTERNAL:")
    }
    evidenced_logic = []
    for evidence_id in result.evidence_ids:
        logic = logic_by_id.get(evidence_id)
        if logic is not None and logic not in evidenced_logic:
            evidenced_logic.append(logic)
    if len(evidenced_logic) == 1:
        logic = evidenced_logic[0]
        if explicit_bool(requirement.text, logic.output_tag) is not None:
            return logic.output_tag, logic.source.program

    compare_candidates = []
    evidence_set = set(result.evidence_ids)
    for model in _compare.compare_models(project):
        if model.rung_id not in evidence_set:
            continue
        if explicit_bool(requirement.text, model.output_tag) is None:
            continue
        compare_candidates.append(model)
    if len(compare_candidates) == 1:
        model = compare_candidates[0]
        return model.output_tag, model.program

    return None, None


def verify_requirement(requirement, engineering, evidence, tests):
    """Apply canonical Rockwell writer trust to Boolean/typed proof claims."""
    result = _ORIGINAL_VERIFY_REQUIREMENT(requirement, engineering, evidence, tests)
    if result.status not in {RequirementStatus.STATICALLY_VERIFIED, RequirementStatus.CONFLICT}:
        return result

    output, theorem_program = _proof_target(requirement, engineering, result)
    if output is None:
        return _unsafe(
            result,
            "Rockwell proof could not be bound to exactly one theorem-selected output after canonical writer validation; static proof is withheld.",
        )

    qualified_program = _qualified_program(requirement.text, output)
    physical_identities = distinct_named_tag_identities(engineering.project, output)
    if qualified_program is None and len(physical_identities) > 1:
        return _unsafe(
            result,
            f"Requirement output {output} is ambiguous across {len(physical_identities)} distinct Rockwell tag scopes; qualify the program/controller identity before static proof.",
        )

    program = qualified_program or theorem_program
    identity = canonical_tag_identity(engineering.project, output, program)
    if not identity_is_resolved(identity):
        return _unsafe(
            result,
            f"Requirement output {output} does not resolve to one stable Rockwell tag identity; alias/scope proof is withheld.",
        )

    writers = canonical_writer_sources(engineering.project, output, program)
    if len(writers) != 1:
        return _unsafe(
            result,
            f"Requirement output {output} has {len(writers)} canonical executable writer source(s), including aliases, overlapping storage, or cross-language writes; static verification/conflict is withheld without deterministic writer-order semantics.",
            writers,
        )
    return result


def install() -> None:
    _verification.verify_requirement = verify_requirement


__all__ = ["verify_requirement", "install"]
