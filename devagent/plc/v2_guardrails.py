from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from pathlib import Path

from devagent.plc.models import CanonicalPLCProject, PLCLogicStatement, PLCSemanticState


_MAX_L5X_BYTES = 128 * 1024 * 1024
_IDENTIFIER = re.compile(
    r"[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?(?:\.[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?)*"
)
_ASSIGNMENT = re.compile(
    r"(?P<lhs>[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?(?:\.[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?)*)\s*:=\s*(?P<rhs>[^;]+)",
    re.IGNORECASE,
)
_IF = re.compile(r"^\s*IF\s+(?P<expr>.+?)\s+THEN\b", re.IGNORECASE)
_ELSIF = re.compile(r"^\s*ELSIF\s+(?P<expr>.+?)\s+THEN\b", re.IGNORECASE)
_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_KEYWORDS = {
    "IF", "THEN", "ELSIF", "ELSE", "END_IF", "TRUE", "FALSE", "AND", "OR", "NOT",
    "XOR", "MOD", "TO", "BY", "DO", "END_FOR", "END_WHILE", "END_REPEAT", "END_CASE",
    "CASE", "OF", "FOR", "WHILE", "REPEAT", "UNTIL", "RETURN", "EXIT",
}
_SAFE_FUNCTIONS = {
    "ABS", "SQRT", "SQR", "MIN", "MAX", "LIMIT", "SIN", "COS", "TAN", "ASIN", "ACOS", "ATAN",
    "LN", "LOG", "EXP", "TRUNC", "ROUND",
}
_UNSUPPORTED_CONTROL = {"CASE", "FOR", "WHILE", "REPEAT", "UNTIL"}


def verify_v2_source_unchanged(project: CanonicalPLCProject) -> None:
    """Fail closed if the L5X differs from the payload proven by the V1 parser."""

    source_path = Path(project.metadata.source_path)
    with source_path.open("rb") as handle:
        payload = handle.read(_MAX_L5X_BYTES + 1)
    if len(payload) > _MAX_L5X_BYTES:
        raise ValueError(f"L5X project exceeds {_MAX_L5X_BYTES} bytes during V2 semantic pass")
    if b"\x00" in payload:
        raise ValueError("L5X contains binary NUL bytes during V2 semantic pass")
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("L5X DTD/entities are not accepted during V2 semantic pass")
    if hashlib.sha256(payload).hexdigest() != project.metadata.source_sha256:
        raise ValueError("L5X source changed during analysis; refusing mixed-provenance V2 results")


def _scrub_literals(value: str) -> str:
    value = re.sub(r"'(?:''|[^'])*'|\"(?:\\.|[^\"])*\"", " ", value)
    return re.sub(
        r"\b(?:T|TIME|DATE|D|TOD|DT|SINT|INT|DINT|LINT|USINT|UINT|UDINT|ULINT|REAL|LREAL)#[A-Za-z0-9_:.+-]+",
        " ",
        value,
        flags=re.IGNORECASE,
    )


def _refs(value: str) -> tuple[str, ...]:
    scrubbed = _scrub_literals(value)
    call_names = {name.upper() for name in _CALL.findall(scrubbed)}
    result: list[str] = []
    for token in _IDENTIFIER.findall(scrubbed):
        if token.upper() in _KEYWORDS or token.upper() in call_names:
            continue
        if token not in result:
            result.append(token)
        for expression in re.findall(r"\[([^\]]+)\]", token):
            if re.fullmatch(r"[-+]?\d+", expression.strip()):
                continue
            for index_ref in _refs(expression):
                if index_ref not in result:
                    result.append(index_ref)
    return tuple(result)


def _has_variable_subscript(value: str) -> bool:
    return any(
        not re.fullmatch(r"[-+]?\d+", expression.strip())
        for expression in re.findall(r"\[([^\]]+)\]", _scrub_literals(value))
    )


def _harden_st_statements(project: CanonicalPLCProject) -> None:
    known_aois = {aoi.name for aoi in project.aois}
    condition_stack: dict[tuple[str, str, str], list[tuple[tuple[str, ...] | None, bool]]] = {}
    hardened: list[PLCLogicStatement] = []

    for statement in project.logic_statements:
        if statement.language != "ST":
            hardened.append(statement)
            continue

        key = (statement.owner_type, statement.owner_name, statement.routine)
        stack = condition_stack.setdefault(key, [])
        code = re.sub(r"\(\*.*?\*\)", " ", statement.text, flags=re.DOTALL).strip()
        scrubbed = _scrub_literals(code)
        upper = scrubbed.upper()
        reads: set[str] = set()
        writes: set[str] = set()
        state = statement.semantic_state

        elsif = _ELSIF.match(scrubbed)
        if_match = _IF.match(scrubbed)
        if elsif:
            condition = tuple(_refs(elsif.group("expr")))
            if stack:
                stack[-1] = (condition, True)
            else:
                stack.append((condition, True))
            state = PLCSemanticState.PARTIAL
        elif if_match:
            condition = tuple(_refs(if_match.group("expr")))
            stack.append((condition, False))
        elif re.match(r"^\s*ELSE\b", scrubbed, flags=re.IGNORECASE):
            previous = stack[-1][0] if stack else None
            if stack:
                stack[-1] = (previous, True)
            state = PLCSemanticState.PARTIAL

        for condition, partial in stack:
            if condition:
                reads.update(condition)
            if partial:
                state = PLCSemanticState.PARTIAL

        assignments = list(_ASSIGNMENT.finditer(scrubbed))
        if len(assignments) == 1 and scrubbed.count(":=") == 1:
            assignment = assignments[0]
            writes.add(assignment.group("lhs"))
            reads.update(_refs(assignment.group("rhs")))
            # Inline control plus assignment is deliberately withheld until a full
            # statement AST can prove ordering and branch scope on the same line.
            if if_match or elsif or re.search(r"\b(ELSE|END_IF)\b", upper):
                state = PLCSemanticState.PARTIAL
        elif scrubbed.count(":="):
            # Keep the original write facts for diagnostics, but never derive a
            # dependency from a packed statement line that V2 did not split.
            writes.update(statement.writes)
            reads.update(_refs(scrubbed))
            state = PLCSemanticState.PARTIAL

        calls = {
            name
            for name in _CALL.findall(scrubbed)
            if name.upper() not in _SAFE_FUNCTIONS
        }
        if calls:
            state = PLCSemanticState.PARTIAL
        if any(re.search(rf"\b{keyword}\b", upper) for keyword in _UNSUPPORTED_CONTROL):
            state = PLCSemanticState.PARTIAL
        if _has_variable_subscript(scrubbed):
            state = PLCSemanticState.PARTIAL
        if not assignments and not if_match and not elsif and not re.match(
            r"^\s*(ELSE|END_IF)\b", scrubbed, flags=re.IGNORECASE
        ):
            if scrubbed.strip("; "):
                state = PLCSemanticState.PARTIAL

        hardened.append(
            replace(
                statement,
                reads=tuple(sorted(reads)),
                writes=tuple(sorted(writes)),
                calls=tuple(sorted(calls)),
                semantic_state=state,
            )
        )

        if re.search(r"\bEND_IF\b", upper) and stack:
            stack.pop()

    project.logic_statements = hardened
    project.st_statement_total = sum(1 for item in hardened if item.language == "ST")
    project.st_statement_semantic_count = sum(
        1
        for item in hardened
        if item.language == "ST" and item.semantic_state is PLCSemanticState.FULL
    )


def _harden_nested_aoi_calls(project: CanonicalPLCProject) -> None:
    aoi_names = {aoi.name for aoi in project.aois}
    affected: set[str] = set()
    hardened: list[PLCLogicStatement] = []
    for statement in project.logic_statements:
        nested = statement.owner_type == "aoi" and bool(set(statement.calls) & aoi_names)
        if not nested:
            hardened.append(statement)
            continue

        affected.add(statement.owner_name)
        if statement.language == "RLL":
            # The V1 parser's positional AOI zip is not a proof for nested calls.
            # Remove those directional facts rather than preserving a false mapping.
            hardened.append(
                replace(
                    statement,
                    reads=(),
                    writes=(),
                    semantic_state=PLCSemanticState.PARTIAL,
                )
            )
        else:
            hardened.append(replace(statement, semantic_state=PLCSemanticState.PARTIAL))

    if not affected:
        return
    project.logic_statements = hardened
    project.aois = [
        replace(aoi, internal_body_modeled=False) if aoi.name in affected else aoi
        for aoi in project.aois
    ]
    project.aoi_internal_modeled_count = sum(1 for aoi in project.aois if aoi.internal_body_modeled)
    project.output_logic = [
        logic
        for logic in project.output_logic
        if not any(
            logic.origin in {f"AOI_INTERNAL:{name}", f"AOI_CALL:{name}"}
            for name in affected
        )
    ]
    project.warnings.append(
        "Nested AOI call binding remains PARTIAL for: " + ", ".join(sorted(affected))
    )


def enforce_v2_guardrails(project: CanonicalPLCProject) -> None:
    """Downgrade ambiguous V2 semantics before graph/test/status derivation."""

    _harden_st_statements(project)
    _harden_nested_aoi_calls(project)
