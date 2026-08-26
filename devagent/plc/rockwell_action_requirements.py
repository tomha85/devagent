from __future__ import annotations

import re
from typing import Callable

from devagent.plc import production_verification as _verification
from devagent.plc.production_models import RequirementStatus, RequirementVerification
from devagent.plc.production_utils import explicit_bool, tag_occurs
from devagent.plc.rockwell_alias_hardening import (
    canonical_tag_identity,
    distinct_named_tag_identities,
    identity_is_resolved,
    storage_identities_overlap,
)
from devagent.plc.rockwell_general_actions import action_models


_PREVIOUS_VERIFY_REQUIREMENT: Callable | None = None
_INSTALLED = False

_MOVE_WORDS = re.compile(
    r"\b(receive|receives|receiving|copy|copied|copies|move|moved|moves|equal|equals|same\s+value)\b",
    re.IGNORECASE,
)
_RESET_WORDS = re.compile(r"\b(reset|resets|resetting|clear|cleared|zero)\b", re.IGNORECASE)


def _unique_named_identity(project, tag: str):
    identities = distinct_named_tag_identities(project, tag)
    if len(identities) != 1 or not identity_is_resolved(identities[0]):
        return None
    return identities[0]


def _relation_is_explicit(text: str, model) -> bool:
    instruction = model.instruction.upper()
    if instruction in {"MOV", "MOVE", "COP", "CPS"}:
        if not model.input_refs or not _MOVE_WORDS.search(text):
            return False
        return all(tag_occurs(text, ref) for ref in model.input_refs)
    if instruction == "CLR":
        escaped = re.escape(model.output_tag)
        return re.search(
            rf"{escaped}\s*(?:=|==|is|shall\s+be|must\s+be|becomes?|set\s+to|cleared\s+to)\s*0\b",
            text,
            flags=re.IGNORECASE,
        ) is not None
    if instruction == "RES":
        return _RESET_WORDS.search(text) is not None and tag_occurs(text, model.output_tag)
    # Arithmetic/expression semantics are available for FAT generation, but a
    # natural-language algebra parser is not yet qualified. Do not infer that a
    # requirement means ADD/SUB/etc merely because its tags co-occur.
    return False


def _proven_paths(text: str, model):
    proven = []
    for path in model.paths:
        assignments: dict[str, bool] = {}
        valid = True
        for term in path.terms:
            value = explicit_bool(text, term.tag)
            if value is None or value is not term.required:
                valid = False
                break
            assignments[term.tag] = value
        if valid:
            proven.append((path, assignments))
    return proven


def _action_requirement_proof(requirement, engineering, tests, previous: RequirementVerification):
    if previous.status is not RequirementStatus.TRACEABLE_NOT_PROVEN:
        return previous

    project = engineering.project
    matched = {tag.casefold() for tag in previous.matched_tags}
    candidates = []
    for model in action_models(project):
        if model.output_tag.casefold() not in matched or not tag_occurs(requirement.text, model.output_tag):
            continue
        if not _relation_is_explicit(requirement.text, model):
            continue
        target = _unique_named_identity(project, model.output_tag)
        model_identity = canonical_tag_identity(project, model.output_tag, model.source.program)
        if target is None or not identity_is_resolved(model_identity) or not storage_identities_overlap(target, model_identity):
            continue
        paths = _proven_paths(requirement.text, model)
        if not paths:
            continue
        candidates.append((model, paths))

    # A deterministic requirement proof must identify exactly one action site.
    # Multiple matching writers/actions remain traceable but not proven.
    if len(candidates) != 1:
        return previous

    model, paths = candidates[0]
    linked: set[str] = set(previous.linked_test_ids)
    for test in tests:
        if (
            test.scenario != "ACTION_PATH"
            or test.output_tag.casefold() != model.output_tag.casefold()
            or test.source.locator != model.source.locator
        ):
            continue
        if any(
            all(test.preconditions.get(tag) == value for tag, value in assignments.items())
            and len(test.preconditions) == len(assignments)
            for _, assignments in paths
        ):
            linked.add(test.id)

    return RequirementVerification(
        requirement_id=previous.requirement_id,
        status=RequirementStatus.ACTION_EFFECT_PROVEN,
        summary=(
            f"Deterministic Rockwell action effect proven at {model.source.locator}: {model.expected_effect}. "
            "The requirement explicitly names the action relationship and all modeled Boolean rung conditions. "
            "This proves the local instruction effect only; final scan value, later writers, runtime faults, physical I/O, and process behavior remain separate evidence questions."
        ),
        evidence_ids=tuple(dict.fromkeys([*previous.evidence_ids, model.rung_id])),
        matched_tags=previous.matched_tags,
        linked_test_ids=tuple(sorted(linked)),
        confidence=1.0,
        ai_assisted=False,
    )


def verify_requirement(requirement, engineering, evidence, tests):
    if _PREVIOUS_VERIFY_REQUIREMENT is None:  # pragma: no cover
        raise RuntimeError("Rockwell action requirement semantics were not installed")
    previous = _PREVIOUS_VERIFY_REQUIREMENT(requirement, engineering, evidence, tests)
    return _action_requirement_proof(requirement, engineering, tests, previous)


def install() -> None:
    global _INSTALLED, _PREVIOUS_VERIFY_REQUIREMENT
    if _INSTALLED:
        return
    _PREVIOUS_VERIFY_REQUIREMENT = _verification.verify_requirement
    _verification.verify_requirement = verify_requirement
    _INSTALLED = True


__all__ = ["install", "verify_requirement"]
