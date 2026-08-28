from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from devagent.plc import schneider_graphical_v4 as _v4
from devagent.plc import schneider_identity_types_v8 as _v8
from devagent.plc import schneider_interlock_permissive_v6 as _v6
from devagent.plc.production_models import RequirementStatus, RequirementVerification


_INSTALLED = False
_PREVIOUS_LD_GROUP = _v4._ld_group
_PREVIOUS_V4_RENDER = _v4._v4_render


def _semantic_description(value: object) -> str | None:
    """Return engineering semantic text, excluding synthetic source provenance.

    V1 stores source-path provenance in ``PLCTag.description`` when the exchange
    XML does not expose an engineering description. V6/V7 token classifiers must
    never infer interlock/fault/recovery intent from filenames such as
    ``OutputGuard.xst`` or ``SafeRecovery.xst``.
    """

    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.casefold()
    if lowered.startswith("control expert export source "):
        return None
    if lowered.startswith("control expert address ") and "; source " in lowered:
        return None
    return text


def _tag_metadata(project) -> dict[str, tuple[str, str | None]]:
    result: dict[str, tuple[str, str | None]] = {}
    for tag in project.tags:
        result.setdefault(
            tag.name.casefold(),
            (tag.name, _semantic_description(tag.description)),
        )
    return result


def _normal_transition_requirement(requirement, engineering, tests, previous, facts):
    """Keep non-restrictive sequence/liveness claims runtime-evidence gated.

    V6 can prove restrictive every-path invariants (``only when`` / interlock
    dominance). A source transition relation does not prove that a controller will
    execute the transition at runtime, so ordinary ``when X, state shall move``
    requirements retain the V5 TRACEABLE_NOT_PROVEN result.
    """

    return previous


def _scope_requirement(previous, requirement, engineering, evidence, tests):
    """Fail closed only for requirement symbols that are canonically ambiguous.

    V8 typed-Boolean hardening already removes a theorem when one of that
    theorem's actual dependencies cannot be resolved as Boolean. Do not demote a
    valid V6 verdict merely because an unrelated reference in the same source
    statement remains unresolved.
    """

    result = previous(requirement, engineering, evidence, tests)
    if not str(engineering.project.metadata.vendor).casefold().startswith("schneider"):
        return result
    if result.status not in {
        RequirementStatus.STATICALLY_VERIFIED,
        RequirementStatus.CONFLICT,
    }:
        return result

    facts = _v8._facts(engineering.project)
    if facts is None:
        return result

    roots: dict[str, set[str]] = defaultdict(set)
    exact_roots: set[str] = set()
    for symbol in facts.symbols:
        if symbol.scope.casefold() != "controller" or not symbol.canonical_path:
            continue
        roots[symbol.canonical_path[-1]].add(symbol.display_path.casefold())
        if len(symbol.canonical_path) == 1:
            exact_roots.add(symbol.canonical_path[0])

    ambiguous: list[str] = []
    for tag in result.matched_tags:
        clean = _v8._clean(tag).casefold()
        if (
            "." not in clean
            and not clean.startswith("%")
            and clean not in exact_roots
            and len(roots.get(clean, ())) > 1
        ):
            ambiguous.append(tag)

    if not ambiguous:
        return result

    return RequirementVerification(
        result.requirement_id,
        RequirementStatus.TRACEABLE_NOT_PROVEN,
        (
            "Schneider V8 withheld the static verdict because unqualified matched "
            "symbol(s) map to multiple canonical project members: "
            + ", ".join(ambiguous)
            + "."
        ),
        result.evidence_ids,
        result.matched_tags,
        result.linked_test_ids,
        result.confidence,
        result.ai_assisted,
    )


def _ld_group(*args, **kwargs):
    statement, outputs, fact = _PREVIOUS_LD_GROUP(*args, **kwargs)
    outputs = tuple(
        replace(item, instruction="LD_COIL") if item.language == "LD" else item
        for item in outputs
    )
    return statement, outputs, fact


def _v4_render(previous, project):
    text = _PREVIOUS_V4_RENDER(previous, project)
    return text.replace(
        "complete Control Expert cell geometry",
        "whole Control Expert cell geometry",
    )


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    # These are compatibility/fail-closed corrections only. They do not widen
    # the V1-V9 theorem surface or convert runtime evidence into static proof.
    _v6._tag_metadata = _tag_metadata
    _v6._normal_transition_requirement = _normal_transition_requirement
    _v8._scope_requirement = _scope_requirement
    _v4._ld_group = _ld_group
    _v4._v4_render = _v4_render
    _INSTALLED = True


__all__ = ["install"]
