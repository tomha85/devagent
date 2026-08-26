from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass, replace
from typing import Any

from devagent.plc.models import FATTestCase, StaticCheck, StaticCheckStatus
from devagent.plc.production_models import RequirementStatus, RequirementVerification
from devagent.plc.production_utils import explicit_bool, tag_occurs

_UNKNOWN_WARNING_PREFIX = "Instruction semantics not modeled for: "
_COMPARE_OPERATORS = {
    "EQU": "==",
    "EQ": "==",
    "NEQ": "!=",
    "NE": "!=",
    "LES": "<",
    "LT": "<",
    "LEQ": "<=",
    "LE": "<=",
    "GRT": ">",
    "GT": ">",
    "GEQ": ">=",
    "GE": ">=",
}
_V36_COMPARE_ALIASES = {"EQ", "NE", "LT", "LE", "GT", "GE", "LIMIT"}
_ALLOWED_LINEAR = {*_COMPARE_OPERATORS, "XIC", "XIO", "OTE"}
_NUMERIC_TYPES = {"SINT", "INT", "DINT", "LINT", "REAL", "LREAL"}
_INTEGER_RANGES = {
    "SINT": (-128, 127),
    "INT": (-32768, 32767),
    "DINT": (-2147483648, 2147483647),
    "LINT": (-(2**63), 2**63 - 1),
}
_FIXED_REF = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_:]*(?:\[\d+\])?(?:\.[A-Za-z_][A-Za-z0-9_:]*(?:\[\d+\])?)*$"
)
_NUMERIC_LITERAL = re.compile(
    r"^(?:(?:SINT|INT|DINT|LINT|REAL|LREAL)#)?([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)$",
    re.IGNORECASE,
)
_CONDITIONAL = re.compile(r"(?:->|=>|\b(?:IF|WHEN|WHENEVER)\b.+\b(?:THEN|SHALL|MUST)\b)", re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class CompareRungModel:
    rung_id: str
    source: Any
    program: str
    output_tag: str
    input_tag: str
    input_type: str
    operator: str
    threshold: int | float
    contacts: tuple[tuple[str, bool], ...]
    instruction: str
    single_writer: bool


def _fixed_ref(value: str) -> str | None:
    stripped = value.strip()
    return stripped if _FIXED_REF.fullmatch(stripped) else None


def _numeric(value: str) -> int | float | None:
    match = _NUMERIC_LITERAL.fullmatch(value.strip())
    if match is None:
        return None
    raw = match.group(1)
    try:
        if any(char in raw.lower() for char in (".", "e")):
            result = float(raw)
            return result if math.isfinite(result) else None
        return int(raw, 10)
    except ValueError:
        return None


def _swap_operator(operator: str) -> str:
    return {">": "<", ">=": "<=", "<": ">", "<=": ">=", "==": "==", "!=": "!="}[operator]


def _negate_operator(operator: str) -> str:
    return {">": "<=", ">=": "<", "<": ">=", "<=": ">", "==": "!=", "!=": "=="}[operator]


def _base_and_members(ref: str) -> tuple[str, tuple[str, ...], str | None]:
    program_name: str | None = None
    value = ref
    if ref.startswith("Program:") and "." in ref:
        prefix, value = ref.split(".", 1)
        program_name = prefix.split(":", 1)[1]
    parts = value.split(".")
    clean = tuple(re.sub(r"\[\d+\]$", "", part) for part in parts)
    return clean[0], clean[1:], program_name


def _resolve_type(project, rung, ref: str) -> str | None:
    base, members, explicit_program = _base_and_members(ref)
    program_scope = explicit_program or rung.program
    candidates = [
        tag for tag in project.tags
        if tag.name == base and tag.scope == f"program:{program_scope}"
    ]
    if len(candidates) != 1:
        candidates = [tag for tag in project.tags if tag.name == base and tag.scope == "controller"]
    if len(candidates) != 1:
        return None
    current = candidates[0].data_type
    type_by_name = {item.name: item for item in project.data_types}
    for member_name in members:
        definition = type_by_name.get(current)
        if definition is None:
            return None
        matches = [member for member in definition.members if member.name == member_name]
        if len(matches) != 1:
            return None
        current = matches[0].data_type
    return current.upper()


def _operand_refs(value: str) -> tuple[str, ...]:
    ref = _fixed_ref(value)
    return (ref,) if ref is not None else ()


def augment_compare_instruction_semantics(project) -> None:
    """Recognize v36+ compare aliases as deterministic read-only instructions."""
    unknown_upper = {name.upper() for name in project.unknown_instruction_names}
    newly_supported = 0
    normalized = []
    for rung in project.rungs:
        reads = set(rung.reads)
        references = set(rung.references)
        for instruction in rung.instructions:
            name = instruction.name.upper()
            if name not in _V36_COMPARE_ALIASES:
                continue
            for argument in instruction.arguments:
                refs = _operand_refs(argument)
                reads.update(refs)
                references.update(refs)
            if name in unknown_upper:
                newly_supported += 1
        normalized.append(replace(rung, reads=tuple(sorted(reads)), references=tuple(sorted(references))))
    project.rungs = normalized
    if newly_supported:
        project.instruction_semantic_count = min(
            project.instruction_total,
            project.instruction_semantic_count + newly_supported,
        )
    project.unknown_instruction_names = sorted(
        name for name in project.unknown_instruction_names if name.upper() not in _V36_COMPARE_ALIASES
    )
    retained = [item for item in project.warnings if not item.startswith(_UNKNOWN_WARNING_PREFIX)]
    if project.unknown_instruction_names:
        retained.append(_UNKNOWN_WARNING_PREFIX + ", ".join(project.unknown_instruction_names))
    project.warnings = retained


def compare_models(project) -> list[CompareRungModel]:
    writers: dict[str, set[str]] = {}
    for rung in project.rungs:
        for output in rung.writes:
            writers.setdefault(output, set()).add(rung.id)

    result: list[CompareRungModel] = []
    for rung in project.rungs:
        # Top-level branch syntax is withheld. Fixed numeric array subscripts are
        # allowed, but any other '[' token makes this bounded model ambiguous.
        if re.search(r"\[(?!\d+\])", rung.text):
            continue
        names = [instruction.name.upper() for instruction in rung.instructions]
        if not names or any(name not in _ALLOWED_LINEAR for name in names):
            continue
        compare_items = [instruction for instruction in rung.instructions if instruction.name.upper() in _COMPARE_OPERATORS]
        outputs = [instruction for instruction in rung.instructions if instruction.name.upper() == "OTE"]
        if len(compare_items) != 1 or len(outputs) != 1 or len(outputs[0].arguments) != 1:
            continue
        output = _fixed_ref(outputs[0].arguments[0])
        compare = compare_items[0]
        if output is None or len(compare.arguments) != 2:
            continue
        left_number = _numeric(compare.arguments[0])
        right_number = _numeric(compare.arguments[1])
        left_ref = _fixed_ref(compare.arguments[0])
        right_ref = _fixed_ref(compare.arguments[1])
        operator = _COMPARE_OPERATORS[compare.name.upper()]
        if left_ref is not None and right_number is not None and left_number is None:
            input_ref = left_ref
            threshold = right_number
        elif right_ref is not None and left_number is not None and right_number is None:
            input_ref = right_ref
            threshold = left_number
            operator = _swap_operator(operator)
        else:
            continue
        input_type = _resolve_type(project, rung, input_ref)
        if input_type not in _NUMERIC_TYPES:
            continue
        contacts: dict[str, bool] = {}
        valid = True
        for instruction in rung.instructions:
            name = instruction.name.upper()
            if name not in {"XIC", "XIO"}:
                continue
            if len(instruction.arguments) != 1:
                valid = False
                break
            tag = _fixed_ref(instruction.arguments[0])
            required = name == "XIC"
            if tag is None or (tag in contacts and contacts[tag] != required):
                valid = False
                break
            contacts[tag] = required
        if not valid:
            continue
        result.append(
            CompareRungModel(
                rung_id=rung.id,
                source=rung.source,
                program=rung.program,
                output_tag=output,
                input_tag=input_ref,
                input_type=input_type,
                operator=operator,
                threshold=threshold,
                contacts=tuple(sorted(contacts.items())),
                instruction=compare.name.upper(),
                single_writer=len(writers.get(output, set())) == 1,
            )
        )
    return result


def _in_range(data_type: str, value: int | float) -> bool:
    bounds = _INTEGER_RANGES.get(data_type)
    if bounds is None:
        return math.isfinite(float(value))
    return isinstance(value, int) and bounds[0] <= value <= bounds[1]


def _step(data_type: str, threshold: int | float) -> int | float:
    if data_type in _INTEGER_RANGES:
        return 1
    return max(0.001, abs(float(threshold)) * 1e-6)


def _sample_pair(model: CompareRungModel) -> tuple[int | float | None, int | float | None]:
    t = model.threshold
    step = _step(model.input_type, t)
    low = t - step
    high = t + step
    candidates = {
        ">": (high, t),
        ">=": (t, low),
        "<": (low, t),
        "<=": (t, high),
        "==": (t, high),
        "!=": (high, t),
    }
    true_value, false_value = candidates[model.operator]
    if model.input_type in _INTEGER_RANGES:
        true_value = int(true_value)
        false_value = int(false_value)
    return (
        true_value if _in_range(model.input_type, true_value) else None,
        false_value if _in_range(model.input_type, false_value) else None,
    )


def generate_compare_fat_tests(project) -> list[FATTestCase]:
    tests: list[FATTestCase] = []
    for model in compare_models(project):
        if not model.single_writer:
            continue
        true_value, false_value = _sample_pair(model)
        for expected, value, scenario in (
            (True, true_value, "THRESHOLD_TRUE"),
            (False, false_value, "THRESHOLD_FALSE"),
        ):
            if value is None:
                continue
            preconditions: dict[str, Any] = dict(model.contacts)
            preconditions[model.input_tag] = value
            digest = hashlib.sha1(
                f"{model.rung_id}:{scenario}:{model.input_tag}:{value}".encode("utf-8")
            ).hexdigest()[:10]
            tests.append(
                FATTestCase(
                    id=f"FAT-CMP-{digest}",
                    title=f"Verify {model.input_tag} {model.operator} {model.threshold} drives {model.output_tag}",
                    source=model.source,
                    output_tag=model.output_tag,
                    preconditions=dict(sorted(preconditions.items())),
                    expected=f"{model.output_tag}={'TRUE' if expected else 'FALSE'} for the modeled single-writer compare rung",
                    limitations=(
                        "Generated from deterministic linear compare+OTE semantics; no PLC scan was executed by static analysis.",
                        "Execution is simulator-only unless a separately qualified hardware policy explicitly permits otherwise.",
                    ),
                    scenario=scenario,
                )
            )
    return tests


def rockwell_compare_check(project) -> StaticCheck:
    compare_rungs = [
        rung for rung in project.rungs
        if any(instruction.name.upper() in _COMPARE_OPERATORS for instruction in rung.instructions)
    ]
    models = compare_models(project)
    modeled_ids = {item.rung_id for item in models}
    unmodeled = [rung for rung in compare_rungs if rung.id not in modeled_ids]
    return StaticCheck(
        id="ROCKWELL_TYPED_COMPARE_SEMANTICS",
        status=StaticCheckStatus.WARN if unmodeled else StaticCheckStatus.PASS,
        summary=(
            f"Modeled {len(models)}/{len(compare_rungs)} compare-bearing RLL rung(s) as bounded typed linear compare semantics; "
            f"{len(unmodeled)} remain withheld from threshold FAT generation."
            if compare_rungs
            else "No compare-bearing RLL rungs require typed threshold modeling."
        ),
        evidence=tuple(rung.id for rung in unmodeled),
    )


def _parse_requirement_condition(text: str, tag: str) -> tuple[str, int | float] | None:
    if _CONDITIONAL.search(text) is None:
        return None
    escaped = re.escape(tag)
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_]){escaped}\s*(>=|<=|<>|!=|==|=|>|<)\s*"
        rf"((?:(?:SINT|INT|DINT|LINT|REAL|LREAL)#)?[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?)",
        re.IGNORECASE,
    )
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        return None
    operator = {"=": "==", "<>": "!="}.get(matches[0].group(1), matches[0].group(1))
    threshold = _numeric(matches[0].group(2))
    if threshold is None:
        return None
    return operator, threshold


def _eval(operator: str, left: int | float, right: int | float) -> bool:
    return {
        ">": left > right,
        ">=": left >= right,
        "<": left < right,
        "<=": left <= right,
        "==": left == right,
        "!=": left != right,
    }[operator]


def _implies(source_op: str, source_value: int | float, target_op: str, target_value: int | float) -> bool:
    if source_op == "==":
        return _eval(target_op, source_value, target_value)
    if target_op == "==":
        return False
    if source_op == "!=":
        return target_op == "!=" and source_value == target_value
    if target_op == "!=":
        if source_op == ">":
            return source_value >= target_value
        if source_op == ">=":
            return source_value > target_value
        if source_op == "<":
            return source_value <= target_value
        if source_op == "<=":
            return source_value < target_value
        return False
    rules = {
        (">", ">") : source_value >= target_value,
        (">", ">="): source_value >= target_value,
        (">=", ">") : source_value > target_value,
        (">=", ">="): source_value >= target_value,
        ("<", "<") : source_value <= target_value,
        ("<", "<="): source_value <= target_value,
        ("<=", "<") : source_value < target_value,
        ("<=", "<="): source_value <= target_value,
    }
    return rules.get((source_op, target_op), False)


def verify_typed_compare_requirement(requirement, engineering, evidence, tests) -> RequirementVerification | None:
    project = engineering.project
    models = compare_models(project)
    if not models:
        return None
    candidate_models = []
    for model in models:
        expected = explicit_bool(requirement.text, model.output_tag)
        condition = _parse_requirement_condition(requirement.text, model.input_tag)
        if expected is not None and condition is not None:
            candidate_models.append((model, expected, condition))
    if not candidate_models:
        return None
    if len(candidate_models) != 1:
        matched = sorted({item for model, _, _ in candidate_models for item in (model.input_tag, model.output_tag)})
        return RequirementVerification(
            requirement.id,
            RequirementStatus.TRACEABLE_NOT_PROVEN,
            "Requirement maps to multiple typed compare/OTE candidates; deterministic writer selection is withheld.",
            tuple(model.rung_id for model, _, _ in candidate_models),
            tuple(matched),
        )
    model, expected, (req_operator, req_threshold) = candidate_models[0]
    matched_tags = [model.input_tag, model.output_tag]
    matched_tags.extend(tag for tag, _ in model.contacts if tag_occurs(requirement.text, tag))
    evidence_ids = [model.rung_id]
    for tag in project.tags:
        if tag.name in matched_tags:
            evidence_ids.append(f"TAG:{tag.scope}:{tag.name}")
    if not model.single_writer:
        return RequirementVerification(
            requirement.id,
            RequirementStatus.TRACEABLE_NOT_PROVEN,
            f"{model.output_tag} has multiple writer rungs; typed compare proof is withheld until deterministic writer order is established.",
            tuple(dict.fromkeys(evidence_ids)),
            tuple(dict.fromkeys(matched_tags)),
        )
    comparator_true = _implies(req_operator, req_threshold, model.operator, model.threshold)
    comparator_false = _implies(req_operator, req_threshold, _negate_operator(model.operator), model.threshold)
    contact_values = {tag: explicit_bool(requirement.text, tag) for tag, _ in model.contacts}
    contact_false = any(
        contact_values[tag] is not None and contact_values[tag] != required
        for tag, required in model.contacts
    )
    contacts_true = all(contact_values[tag] == required for tag, required in model.contacts)

    proven = False
    conflict = False
    if expected:
        proven = comparator_true and contacts_true
        conflict = comparator_false or contact_false
    else:
        proven = comparator_false or contact_false
        conflict = comparator_true and contacts_true

    linked: list[str] = []
    for test in tests:
        if test.output_tag != model.output_tag or test.scenario not in {"THRESHOLD_TRUE", "THRESHOLD_FALSE"}:
            continue
        value = test.preconditions.get(model.input_tag)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if not _eval(req_operator, value, req_threshold):
            continue
        test_expected = f"{model.output_tag}={'TRUE' if expected else 'FALSE'}"
        if test_expected in test.expected:
            linked.append(test.id)

    status = (
        RequirementStatus.STATICALLY_VERIFIED
        if proven
        else RequirementStatus.CONFLICT
        if conflict
        else RequirementStatus.TRACEABLE_NOT_PROVEN
    )
    summary = (
        f"Explicit typed condition {model.input_tag} {req_operator} {req_threshold} deterministically implies "
        f"{model.output_tag}={'TRUE' if expected else 'FALSE'} in the single-writer linear {model.instruction}+OTE rung."
        if proven
        else f"Explicit typed condition is incompatible with required {model.output_tag} state in the single-writer linear compare+OTE rung."
        if conflict
        else "Requirement maps to a typed compare/OTE rung, but its explicit conditions do not fully imply either verification or conflict."
    )
    return RequirementVerification(
        requirement.id,
        status,
        summary,
        tuple(dict.fromkeys(evidence_ids)),
        tuple(dict.fromkeys(matched_tags)),
        tuple(sorted(linked)),
    )


__all__ = [
    "augment_compare_instruction_semantics",
    "compare_models",
    "generate_compare_fat_tests",
    "rockwell_compare_check",
    "verify_typed_compare_requirement",
]
