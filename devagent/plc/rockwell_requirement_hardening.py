from __future__ import annotations

import re

from devagent.plc import production_verification as _verification
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


def _proof_output(requirement, engineering, result: RequirementVerification):
    explicit = [
        tag
        for tag in result.matched_tags
        if explicit_bool(requirement.text, tag) is not None
    ]
    if len(explicit) != 1:
        return None, None
    output = explicit[0]
    logic = [
        item
        for item in engineering.project.output_logic
        if item.output_tag.casefold() == output.casefold()
        and item.semantic_state is PLCSemanticState.FULL
        and not item.origin.startswith("AOI_INTERNAL:")
    ]
    if len(logic) != 1:
        return output, None
    return output, logic[0]


def verify_requirement(requirement, engineering, evidence, tests):
    """Apply canonical Rockwell writer trust to Boolean proof/conflict claims."""
    result = _ORIGINAL_VERIFY_REQUIREMENT(requirement, engineering, evidence, tests)
    if result.status not in {RequirementStatus.STATICALLY_VERIFIED, RequirementStatus.CONFLICT}:
        return result

    output, logic = _proof_output(requirement, engineering, result)
    if output is None or logic is None:
        return _unsafe(
            result,
            "Rockwell proof could not be bound to exactly one FULL output-logic object after canonical writer validation; static proof is withheld.",
        )

    qualified_program = _qualified_program(requirement.text, output)
    physical_identities = distinct_named_tag_identities(engineering.project, output)
    if qualified_program is None and len(physical_identities) > 1:
        return _unsafe(
            result,
            f"Requirement output {output} is ambiguous across {len(physical_identities)} distinct Rockwell tag scopes; qualify the program/controller identity before static proof.",
        )

    program = qualified_program or logic.source.program
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
