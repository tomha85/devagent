from __future__ import annotations

import math
import re
import struct
import sys
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
_REAL_MAX = 3.4028234663852886e38
_REAL_MIN_NORMAL = 1.1754943508222875e-38
_LREAL_MAX = sys.float_info.max
_LREAL_MIN_NORMAL = sys.float_info.min
_DISJUNCTION = re.compile(r"\b(?:OR|XOR)\b|\|\|", re.IGNORECASE)
_NEGATION = re.compile(r"\bNOT\b", re.IGNORECASE)
_UNSUPPORTED_BOOLEAN = re.compile(r"\b(?:NAND|NOR|XNOR|OR|XOR|NOT)\b|&&|\|\|", re.IGNORECASE)
_IF_THEN = re.compile(
    r"^\s*(?:IF|WHEN|WHENEVER)\b(?P<antecedent>.+?)\bTHEN\b(?P<consequent>.+?)\s*$",
    re.IGNORECASE | re.DOTALL,
)
_PROGRAM_QUALIFIED = re.compile(r"^Program:([^\.]+)\.(.+)$", re.IGNORECASE)
_BOOL_WORDS = {
    "true": True,
    "on": True,
    "active": True,
    "false": False,
    "off": False,
    "inactive": False,
}


def _normalize_model_value(data_type: str, value: int | float) -> int | float | None:
    bounds = _INTEGER_RANGES.get(data_type)
    if bounds is not None:
        if not isinstance(value, int) or isinstance(value, bool) or not (bounds[0] <= value <= bounds[1]):
            return None
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric):
        return None
    if data_type == "REAL":
        try:
            normalized = struct.unpack(">f", struct.pack(">f", numeric))[0]
        except (OverflowError, struct.error):
            return None
        if not math.isfinite(normalized) or abs(normalized) > _REAL_MAX:
            return None
        if normalized != 0.0 and abs(normalized) < _REAL_MIN_NORMAL:
            return None
        return float(normalized)
    if data_type == "LREAL":
        if abs(numeric) > _LREAL_MAX:
            return None
        if numeric != 0.0 and abs(numeric) < _LREAL_MIN_NORMAL:
            return None
        return numeric
    return None


def _representable(data_type: str, value: int | float) -> bool:
    return _normalize_model_value(data_type, value) is not None


def _condition_has_witness(data_type: str, operator: str, threshold: int | float) -> bool:
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        return False
    try:
        numeric = float(threshold)
    except (TypeError, ValueError, OverflowError):
        return False
    if not math.isfinite(numeric):
        return False

    bounds = _INTEGER_RANGES.get(data_type)
    if bounds is not None:
        if not _representable(data_type, threshold):
            return False
        low, high = bounds
        return {
            ">": high > threshold,
            ">=": high >= threshold,
            "<": low < threshold,
            "<=": low <= threshold,
            "==": True,
            "!=": low != high or low != threshold,
        }[operator]

    if data_type == "REAL":
        if abs(numeric) > _REAL_MAX:
            return operator in {"<", "<=", "!="} if numeric > 0 else operator in {">", ">=", "!="}
        if operator == "==":
            normalized = _normalize_model_value("REAL", numeric)
            return normalized is not None and normalized == numeric
        return {
            ">": _REAL_MAX > numeric,
            ">=": _REAL_MAX >= numeric,
            "<": -_REAL_MAX < numeric,
            "<=": -_REAL_MAX <= numeric,
            "!=": True,
        }[operator]

    if data_type == "LREAL":
        if abs(numeric) > _LREAL_MAX:
            return False
        if operator == "==":
            return _representable("LREAL", numeric)
        return {
            ">": _LREAL_MAX > numeric,
            ">=": _LREAL_MAX >= numeric,
            "<": -_LREAL_MAX < numeric,
            "<=": -_LREAL_MAX <= numeric,
            "!=": True,
        }[operator]
    return False


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


def _canonical_tag_identity(project, ref: str, default_program: str | None) -> tuple[str, str]:
    """Resolve one Rockwell reference to a scope-aware case-folded identity."""
    value = ref.strip()
    qualified = _PROGRAM_QUALIFIED.match(value)
    if qualified is not None:
        program, remainder = qualified.groups()
        return f"program:{program.casefold()}", remainder.casefold()

    top = re.split(r"[.\[]", value, maxsplit=1)[0]
    suffix = value[len(top) :].casefold()
    top_folded = top.casefold()
    if default_program:
        wanted_scope = f"program:{default_program}".casefold()
        matches = [
            tag for tag in project.tags
            if tag.name.casefold() == top_folded and tag.scope.casefold() == wanted_scope
        ]
        if len(matches) == 1:
            return wanted_scope, matches[0].name.casefold() + suffix
    controller = [
        tag for tag in project.tags
        if tag.name.casefold() == top_folded and tag.scope.casefold() == "controller"
    ]
    if len(controller) == 1:
        return "controller", controller[0].name.casefold() + suffix
    fallback_scope = f"program:{default_program}".casefold() if default_program else "unresolved"
    return fallback_scope, value.casefold()


def _has_other_writer(project, model) -> bool:
    target = _canonical_tag_identity(project, model.output_tag, model.program)
    for rung in project.rungs:
        if rung.id == model.rung_id:
            continue
        if any(_canonical_tag_identity(project, write, rung.program) == target for write in rung.writes):
            return True
    for statement in project.logic_statements:
        if statement.language == "RLL" and _same_source(statement, model):
            continue
        statement_program = statement.source.program or (
            statement.owner_name if statement.owner_type == "program" else None
        )
        if any(
            _canonical_tag_identity(project, write, statement_program) == target
            for write in statement.writes
        ):
            return True
    return False


def compare_models(project):
    """Return only V8 compare models that satisfy the final fail-closed guards."""
    result = []
    for model in _ORIGINAL_COMPARE_MODELS(project):
        if not _final_ote(project, model.rung_id):
            continue
        threshold = _normalize_model_value(model.input_type, model.threshold)
        if threshold is None:
            continue
        result.append(
            replace(
                model,
                threshold=threshold,
                # Recompute from canonical identities instead of retaining the
                # original raw global writer-name count.
                single_writer=not _has_other_writer(project, model),
            )
        )
    return result


def _model_key(model) -> tuple[str | None, str | None, str | None, str]:
    return (model.source.program, model.source.routine, model.source.rung, model.output_tag.casefold())


def generate_compare_fat_tests(project):
    models = {_model_key(model): model for model in compare_models(project)}
    result = []
    for test in _ORIGINAL_GENERATE_FAT(project):
        key = (test.source.program, test.source.routine, test.source.rung, test.output_tag.casefold())
        model = models.get(key)
        if model is None or not model.single_writer:
            continue
        raw_value = test.preconditions.get(model.input_tag)
        if not isinstance(raw_value, (int, float)) or isinstance(raw_value, bool):
            continue
        normalized = _normalize_model_value(model.input_type, raw_value)
        if normalized is None:
            continue
        expected_true = test.scenario == "THRESHOLD_TRUE"
        if _base._eval(model.operator, normalized, model.threshold) != expected_true:
            continue
        preconditions = dict(test.preconditions)
        preconditions[model.input_tag] = normalized
        result.append(replace(test, preconditions=dict(sorted(preconditions.items()))))
    return result


def rockwell_compare_check(project):
    check = _ORIGINAL_COMPARE_CHECK(project)
    models = compare_models(project)
    multi_writer_models = [model for model in models if not model.single_writer]
    generated = generate_compare_fat_tests(project)
    scenarios: dict[tuple[str | None, str | None, str | None, str], set[str]] = {}
    for test in generated:
        key = (test.source.program, test.source.routine, test.source.rung, test.output_tag.casefold())
        scenarios.setdefault(key, set()).add(test.scenario)
    incomplete_witness_models = [
        model
        for model in models
        if model.single_writer
        and scenarios.get(_model_key(model), set()) != {"THRESHOLD_TRUE", "THRESHOLD_FALSE"}
    ]
    if not incomplete_witness_models and not multi_writer_models:
        return check
    evidence = tuple(
        dict.fromkeys(
            [
                *check.evidence,
                *(item.rung_id for item in incomplete_witness_models),
                *(item.rung_id for item in multi_writer_models),
            ]
        )
    )
    details = []
    if incomplete_witness_models:
        details.append(
            f"{len(incomplete_witness_models)} modeled rung(s) cannot produce both TRUE and FALSE representable FAT witnesses in the declared data-type domain."
        )
    if multi_writer_models:
        details.append(
            f"{len(multi_writer_models)} compare output(s) have additional executable writers; single-writer threshold proof/FAT is withheld."
        )
    return StaticCheck(
        id=check.id,
        status=StaticCheckStatus.WARN,
        summary=check.summary + " " + " ".join(details),
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


def _numeric_condition_pattern(tag: str) -> re.Pattern[str]:
    escaped = re.escape(tag)
    return re.compile(
        rf"(?<![A-Za-z0-9_]){escaped}\s*(>=|<=|<>|!=|==|=|>|<)\s*"
        rf"((?:(?:SINT|INT|DINT|LINT|REAL|LREAL)#)?[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)",
        re.IGNORECASE,
    )


def _parse_numeric_condition(segment: str, tag: str):
    matches = list(_numeric_condition_pattern(tag).finditer(segment))
    if len(matches) != 1:
        return None
    operator = {"=": "==", "<>": "!="}.get(matches[0].group(1), matches[0].group(1))
    threshold = _base._numeric(matches[0].group(2))
    return None if threshold is None else (operator, threshold)


def _full_numeric_condition(segment: str, tag: str):
    match = _numeric_condition_pattern(tag).fullmatch(segment.strip())
    if match is None:
        return None
    operator = {"=": "==", "<>": "!="}.get(match.group(1), match.group(1))
    threshold = _base._numeric(match.group(2))
    return None if threshold is None else (operator, threshold)


def _full_bool_assertion(segment: str, tag: str) -> bool | None:
    escaped = re.escape(tag)
    patterns = [
        re.compile(
            rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])\s*(?:=|==|is|shall\s+be|must\s+be)\s*"
            rf"(TRUE|FALSE|ON|OFF|ACTIVE|INACTIVE)\b",
            re.IGNORECASE,
        ),
        re.compile(
            rf"\b(TRUE|FALSE|ON|OFF|ACTIVE|INACTIVE)\s*(?:=|==|for)\s*"
            rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        ),
    ]
    matches = [match for pattern in patterns for match in [pattern.fullmatch(segment.strip())] if match]
    if len(matches) != 1:
        return None
    return _BOOL_WORDS[matches[0].group(1).casefold()]


def _parse_supported_antecedent(antecedent: str, model):
    if _UNSUPPORTED_BOOLEAN.search(antecedent) or any(char in antecedent for char in "()"):
        return None
    clauses = [item.strip() for item in re.split(r"\bAND\b", antecedent, flags=re.IGNORECASE)]
    if not clauses or any(not item for item in clauses):
        return None
    condition = None
    seen_contacts: set[str] = set()
    for clause in clauses:
        numeric = _full_numeric_condition(clause, model.input_tag)
        if numeric is not None:
            if condition is not None:
                return None
            condition = numeric
            continue
        matched_contact = None
        for tag, _ in model.contacts:
            value = _full_bool_assertion(clause, tag)
            if value is not None:
                if matched_contact is not None or tag.casefold() in seen_contacts:
                    return None
                matched_contact = tag
        if matched_contact is None:
            return None
        seen_contacts.add(matched_contact.casefold())
    return condition


def _unsafe_requirement(requirement, model, reason: str) -> RequirementVerification:
    return RequirementVerification(
        requirement.id,
        RequirementStatus.TRACEABLE_NOT_PROVEN,
        reason,
        (model.rung_id,),
        (model.input_tag, model.output_tag),
    )


def verify_typed_compare_requirement(requirement, engineering, evidence, tests):
    """Require a fully parsed conjunctive antecedent and explicit consequent."""
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

        condition = _parse_supported_antecedent(antecedent, model)
        output_state = _full_bool_assertion(consequent, model.output_tag)
        if condition is None:
            return _unsafe_requirement(
                requirement,
                model,
                "Typed requirement antecedent is outside the supported conjunction grammar; unparsed Boolean operators or clauses cannot be statically proven.",
            )
        if output_state is None:
            return _unsafe_requirement(
                requirement,
                model,
                "Typed requirement does not contain exactly one unambiguous output-state assertion as the complete consequent.",
            )

        operator, threshold = condition
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
