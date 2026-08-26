from __future__ import annotations

import math
import re
from dataclasses import replace

from devagent.plc import rockwell_compare as _base
from devagent.plc.models import StaticCheck, StaticCheckStatus
from devagent.plc.production_models import RequirementStatus, RequirementVerification

# Preserve references to the original V8 functions before __init__ binds the
# guarded versions onto rockwell_compare. Their module globals intentionally
# remain shared, so internal calls to compare_models use the guarded binding.
_ORIGINAL_COMPARE_MODELS = _base.compare_models
_ORIGINAL_GENERATE_FAT = _base.generate_compare_fat_tests
_ORIGINAL_COMPARE_CHECK = _base.rockwell_compare_check
_ORIGINAL_VERIFY_REQUIREMENT = _base.verify_typed_compare_requirement

_INTEGER_RANGES = {
    "SINT": (-128, 127),
    "INT": (-32768, 32767),
    "DINT": (-2147483648, 2147483647),
    "LINT": (-(2**63), 2**63 - 1),
}
_DISJUNCTION = re.compile(r"\b(?:OR|XOR)\b|\|\|", re.IGNORECASE)
_NEGATION = re.compile(r"\bNOT\b", re.IGNORECASE)
_IF_THEN = re.compile(
    r"^\s*(?:IF|WHEN|WHENEVER)\b(?P<antecedent>.+?)\bTHEN\b(?P<consequent>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _representable(data_type: str, value: int | float) -> bool:
    bounds = _INTEGER_RANGES.get(data_type)
    if bounds is not None:
        return isinstance(value, int) and not isinstance(value, bool) and bounds[0] <= value <= bounds[1]
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _condition_has_witness(data_type: str, operator: str, threshold: int | float) -> bool:
    if not _representable(data_type, threshold):
        return False
    bounds = _INTEGER_RANGES.get(data_type)
    if bounds is None:
        return True
    low, high = bounds
    return {
        ">": high > threshold,
        ">=": high >= threshold,
        "<": low < threshold,
        "<=": low <= threshold,
        "==": True,
        "!=": low != high or low != threshold,
    }[operator]


def _final_ote(project, rung_id: str) -> bool:
    rung = next((item for item in project.rungs if item.id == rung_id), None)
    return bool(rung and rung.instructions and rung.instructions[-1].name.upper() == "OTE")


def _same_source(statement, model) -> bool:
    source = statement.source
    return (
        source.program == model.source.program
        and source.routine == model.source.routine
        and source.rung == model.source.rung
    )


def _has_other_writer(project, model) -> bool:
    for rung in project.rungs:
        if rung.id != model.rung_id and model.output_tag in rung.writes:
            return True
    for statement in project.logic_statements:
        if model.output_tag not in statement.writes:
            continue
        # V2 can expose an RLL statement mirroring the same source rung. That is
        # evidence for the same writer, not an independent writer.
        if statement.language == "RLL" and _same_source(statement, model):
            continue
        return True
    return False


def compare_models(project):
    """Return only V8 compare models that satisfy the final fail-closed guards."""
    result = []
    for model in _ORIGINAL_COMPARE_MODELS(project):
        if not _final_ote(project, model.rung_id):
            continue
        if not _representable(model.input_type, model.threshold):
            continue
        result.append(
            replace(
                model,
                single_writer=model.single_writer and not _has_other_writer(project, model),
            )
        )
    return result


def generate_compare_fat_tests(project):
    # The original generator resolves compare_models from rockwell_compare's
    # module globals, which __init__ binds to the guarded implementation.
    return _ORIGINAL_GENERATE_FAT(project)


def rockwell_compare_check(project):
    # Same shared-global behavior makes excluded unsafe rungs show as WARN.
    check = _ORIGINAL_COMPARE_CHECK(project)
    boundary_models = []
    for model in compare_models(project):
        true_value, false_value = _base._sample_pair(model)
        if true_value is None or false_value is None:
            boundary_models.append(model)
    if not boundary_models:
        return check
    evidence = tuple(dict.fromkeys([*check.evidence, *(item.rung_id for item in boundary_models)]))
    return StaticCheck(
        id=check.id,
        status=StaticCheckStatus.WARN,
        summary=(
            check.summary
            + f" {len(boundary_models)} modeled rung(s) cannot produce both TRUE and FALSE representable FAT witnesses at the data-type boundary."
        ),
        evidence=evidence,
    )


def _split_conditional(text: str) -> tuple[str, str] | None:
    arrows = list(re.finditer(r"->|=>", text))
    if len(arrows) == 1:
        match = arrows[0]
        antecedent = text[: match.start()].strip()
        consequent = text[match.end() :].strip()
        return (antecedent, consequent) if antecedent and consequent else None
    match = _IF_THEN.match(text)
    if match is None:
        return None
    return match.group("antecedent").strip(), match.group("consequent").strip()


def _parse_numeric_condition(segment: str, tag: str):
    escaped = re.escape(tag)
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){escaped}\s*(>=|<=|<>|!=|==|=|>|<)\s*"
        rf"((?:(?:SINT|INT|DINT|LINT|REAL|LREAL)#)?[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(segment))
    if len(matches) != 1:
        return None
    operator = {"=": "==", "<>": "!="}.get(matches[0].group(1), matches[0].group(1))
    threshold = _base._numeric(matches[0].group(2))
    return None if threshold is None else (operator, threshold)


def _unsafe_requirement(requirement, model, reason: str) -> RequirementVerification:
    return RequirementVerification(
        requirement.id,
        RequirementStatus.TRACEABLE_NOT_PROVEN,
        reason,
        (model.rung_id,),
        (model.input_tag, model.output_tag),
    )


def verify_typed_compare_requirement(requirement, engineering, evidence, tests):
    """Require a conjunctive antecedent and explicit consequent before proof."""
    project = engineering.project
    for model in compare_models(project):
        if not _base.tag_occurs(requirement.text, model.input_tag):
            continue
        if _base.explicit_bool(requirement.text, model.output_tag) is None:
            continue

        split = _split_conditional(requirement.text)
        if split is None:
            return _unsafe_requirement(
                requirement,
                model,
                "Typed requirement mentions the compare input/output but lacks an unambiguous antecedent→consequent structure; static proof is withheld.",
            )
        antecedent, consequent = split
        if _DISJUNCTION.search(antecedent) or _DISJUNCTION.search(consequent):
            return _unsafe_requirement(
                requirement,
                model,
                "Typed requirement contains OR/XOR semantics; the bounded verifier proves only conjunctive antecedents and explicit consequents.",
            )
        if _NEGATION.search(antecedent) or _NEGATION.search(consequent):
            return _unsafe_requirement(
                requirement,
                model,
                "Typed requirement contains free-form NOT semantics; static proof is withheld until a full requirement Boolean AST is available.",
            )
        if _base.tag_occurs(antecedent, model.output_tag) or _base.tag_occurs(consequent, model.input_tag):
            return _unsafe_requirement(
                requirement,
                model,
                "Typed requirement places the PLC output in the antecedent or compare input in the consequent; implication direction is not proven.",
            )

        condition = _parse_numeric_condition(antecedent, model.input_tag)
        expected = _base.explicit_bool(consequent, model.output_tag)
        if condition is None or expected is None:
            return _unsafe_requirement(
                requirement,
                model,
                "Typed requirement does not contain exactly one supported numeric antecedent and one explicit output-state consequent.",
            )
        if _parse_numeric_condition(consequent, model.input_tag) is not None:
            return _unsafe_requirement(
                requirement,
                model,
                "Numeric compare condition appears in the consequent; implication direction is not proven.",
            )
        for tag, _ in model.contacts:
            if _base.explicit_bool(consequent, tag) is not None:
                return _unsafe_requirement(
                    requirement,
                    model,
                    "A rung permissive is asserted in the consequent rather than the antecedent; static proof is withheld.",
                )

        operator, threshold = condition
        if not _representable(model.input_type, threshold):
            return _unsafe_requirement(
                requirement,
                model,
                f"Requirement threshold {threshold!r} is not exactly representable in {model.input_type}; typed static proof is withheld.",
            )
        if not _condition_has_witness(model.input_type, operator, threshold):
            return _unsafe_requirement(
                requirement,
                model,
                f"Requirement condition {model.input_tag} {operator} {threshold!r} has no representable {model.input_type} witness; vacuous static proof is withheld.",
            )

    return _ORIGINAL_VERIFY_REQUIREMENT(requirement, engineering, evidence, tests)


def install() -> None:
    """Bind the hardening layer before production verification modules import."""
    _base.compare_models = compare_models
    _base.generate_compare_fat_tests = generate_compare_fat_tests
    _base.rockwell_compare_check = rockwell_compare_check
    _base.verify_typed_compare_requirement = verify_typed_compare_requirement


__all__ = [
    "compare_models",
    "generate_compare_fat_tests",
    "rockwell_compare_check",
    "verify_typed_compare_requirement",
    "install",
]
