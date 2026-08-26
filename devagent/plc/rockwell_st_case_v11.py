from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import replace

from devagent.plc.models import CanonicalPLCProject, PLCLogicStatement, PLCSemanticState
from devagent.plc.rockwell_entrypoint_hardening import routine_has_execution_entry
from devagent.plc import v2_guardrails as _guard


_CASE = re.compile(r"^\s*CASE\s+(?P<expr>.+?)\s+OF\s*;?\s*$", re.IGNORECASE)
_END_CASE = re.compile(r"^\s*END_CASE\s*;?\s*$", re.IGNORECASE)
_ELSE = re.compile(r"^\s*ELSE\s*;?\s*$", re.IGNORECASE)
_LABEL = re.compile(
    r"^\s*(?P<labels>(?:[-+]?\d+|[A-Za-z_][A-Za-z0-9_:.]*)(?:\s*,\s*(?:[-+]?\d+|[A-Za-z_][A-Za-z0-9_:.]*))*)\s*:\s*(?P<body>.*)$",
    re.IGNORECASE,
)
_WARNING_PREFIX = "Rockwell V11 ST CASE semantics: "


def _clean(text: str) -> str:
    return re.sub(r"\(\*.*?\*\)", " ", text, flags=re.DOTALL).strip()


def _safe_expression(value: str) -> bool:
    scrubbed = _guard._scrub_literals(value)
    if _guard._has_variable_subscript(scrubbed):
        return False
    calls = {
        name
        for name in _guard._CALL.findall(scrubbed)
        if name.upper() not in _guard._SAFE_FUNCTIONS
    }
    return not calls


def _single_assignment(value: str):
    scrubbed = _guard._scrub_literals(value).strip()
    assignments = list(_guard._ASSIGNMENT.finditer(scrubbed))
    if len(assignments) != 1 or scrubbed.count(":=") != 1:
        return None
    assignment = assignments[0]
    prefix = scrubbed[: assignment.start()].strip(" ;")
    suffix = scrubbed[assignment.end() :].strip(" ;")
    if prefix or suffix:
        return None
    lhs = assignment.group("lhs")
    rhs = assignment.group("rhs")
    if _guard._has_variable_subscript(lhs) or not _safe_expression(rhs):
        return None
    return lhs, rhs


def _candidate_case_ranges(items: list[PLCLogicStatement]) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start: int | None = None
    depth = 0
    for index, statement in enumerate(items):
        code = _clean(statement.text)
        if _CASE.match(code):
            if depth == 0:
                start = index
            depth += 1
            continue
        if _END_CASE.match(code):
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start is not None:
                ranges.append((start, index))
                start = None
    return ranges


def _promote_case_block(items: list[PLCLogicStatement], start: int, end: int) -> dict[int, PLCLogicStatement] | None:
    header = _clean(items[start].text)
    match = _CASE.match(header)
    if match is None:
        return None
    selector = match.group("expr").strip()
    if not selector or not _safe_expression(selector):
        return None
    selector_refs = tuple(_guard._refs(selector))

    promoted: dict[int, PLCLogicStatement] = {}
    promoted[start] = replace(
        items[start],
        reads=tuple(sorted(selector_refs)),
        writes=(),
        calls=(),
        semantic_state=PLCSemanticState.FULL,
    )

    branch_seen = False
    else_seen = False
    for index in range(start + 1, end):
        statement = items[index]
        code = _clean(statement.text)
        if not code:
            continue
        if _CASE.match(code) or _END_CASE.match(code):
            return None
        if re.search(r"\b(IF|ELSIF|END_IF|FOR|WHILE|REPEAT|UNTIL|RETURN|EXIT)\b", code, flags=re.IGNORECASE):
            return None
        if _ELSE.match(code):
            if else_seen:
                return None
            else_seen = True
            branch_seen = True
            promoted[index] = replace(
                statement,
                reads=tuple(sorted(selector_refs)),
                writes=(),
                calls=(),
                semantic_state=PLCSemanticState.FULL,
            )
            continue

        label = _LABEL.match(code)
        body = code
        if label is not None:
            if else_seen:
                return None
            branch_seen = True
            body = label.group("body").strip()
            if not body:
                promoted[index] = replace(
                    statement,
                    reads=tuple(sorted(selector_refs)),
                    writes=(),
                    calls=(),
                    semantic_state=PLCSemanticState.FULL,
                )
                continue
        elif not branch_seen:
            return None

        assignment = _single_assignment(body)
        if assignment is None:
            return None
        lhs, rhs = assignment
        reads = set(selector_refs)
        reads.update(_guard._refs(rhs))
        promoted[index] = replace(
            statement,
            reads=tuple(sorted(reads)),
            writes=(lhs,),
            calls=(),
            semantic_state=PLCSemanticState.FULL,
        )

    if not branch_seen:
        return None
    promoted[end] = replace(
        items[end],
        reads=tuple(sorted(selector_refs)),
        writes=(),
        calls=(),
        semantic_state=PLCSemanticState.FULL,
    )
    return promoted


def augment_st_case_dataflow(project: CanonicalPLCProject) -> None:
    """Promote only bounded, reachable CASE/OF dataflow to FULL.

    This theorem is deliberately about source-level dataflow, not runtime proof.
    It supports a fixed CASE selector, simple constant/enum labels, ELSE, and
    one fixed-target assignment per source line. Nested CASE, IF/loop control,
    indirect indexing, unresolved calls, packed multi-assignment lines, and
    unreachable routines remain PARTIAL.
    """

    grouped: dict[tuple[str, str, str], list[tuple[int, PLCLogicStatement]]] = defaultdict(list)
    for absolute_index, statement in enumerate(project.logic_statements):
        if statement.language == "ST" and statement.owner_type == "program":
            grouped[(statement.owner_type, statement.owner_name, statement.routine)].append((absolute_index, statement))

    replacements: dict[int, PLCLogicStatement] = {}
    promoted_blocks = 0
    for (_, program, routine), indexed in grouped.items():
        if not routine_has_execution_entry(project, program, routine):
            continue
        local_items = [item for _, item in indexed]
        for start, end in _candidate_case_ranges(local_items):
            promoted = _promote_case_block(local_items, start, end)
            if promoted is None:
                continue
            promoted_blocks += 1
            for local_index, statement in promoted.items():
                replacements[indexed[local_index][0]] = statement

    if replacements:
        project.logic_statements = [
            replacements.get(index, statement)
            for index, statement in enumerate(project.logic_statements)
        ]

    project.st_statement_total = sum(1 for item in project.logic_statements if item.language == "ST")
    project.st_statement_semantic_count = sum(
        1
        for item in project.logic_statements
        if item.language == "ST" and item.semantic_state is PLCSemanticState.FULL
    )

    project.warnings = [warning for warning in project.warnings if not warning.startswith(_WARNING_PREFIX)]
    if promoted_blocks:
        project.warnings.append(
            _WARNING_PREFIX
            + f"modeled {promoted_blocks} reachable bounded CASE block(s) as exact source-level dataflow; runtime behavior still requires normal verification gates"
        )


__all__ = ["augment_st_case_dataflow"]
