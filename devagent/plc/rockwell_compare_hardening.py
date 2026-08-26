from __future__ import annotations

import math

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


def compare_models(project):
    """Return only V8 compare models that satisfy the final fail-closed guards."""
    return [
        model
        for model in _ORIGINAL_COMPARE_MODELS(project)
        if _final_ote(project, model.rung_id)
        and _representable(model.input_type, model.threshold)
    ]


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


def verify_typed_compare_requirement(requirement, engineering, evidence, tests):
    """Reject non-representable or vacuous requirement thresholds before proof."""
    project = engineering.project
    for model in compare_models(project):
        expected = _base.explicit_bool(requirement.text, model.output_tag)
        condition = _base._parse_requirement_condition(requirement.text, model.input_tag)
        if expected is None or condition is None:
            continue
        operator, threshold = condition
        if not _representable(model.input_type, threshold):
            return RequirementVerification(
                requirement.id,
                RequirementStatus.TRACEABLE_NOT_PROVEN,
                f"Requirement threshold {threshold!r} is not exactly representable in {model.input_type}; typed static proof is withheld.",
                (model.rung_id,),
                (model.input_tag, model.output_tag),
            )
        if not _condition_has_witness(model.input_type, operator, threshold):
            return RequirementVerification(
                requirement.id,
                RequirementStatus.TRACEABLE_NOT_PROVEN,
                f"Requirement condition {model.input_tag} {operator} {threshold!r} has no representable {model.input_type} witness; vacuous static proof is withheld.",
                (model.rung_id,),
                (model.input_tag, model.output_tag),
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
