from __future__ import annotations

from dataclasses import dataclass, replace
import re

from devagent.plc.models import PLCLogicStatement, PLCSemanticState, PLCSourceRef
from devagent.plc import v2_guardrails as _guard
from devagent.plc import v2_semantics as _v2


_LHS = r"[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?(?:\.[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?)*"
_ASSIGNMENT = re.compile(rf"(?P<lhs>{_LHS})\s*:=\s*(?P<rhs>.*?)(?=;|$)", re.IGNORECASE)
_IF = re.compile(r"^\s*IF\s+(?P<expr>.+?)\s+THEN\b", re.IGNORECASE)
_ELSIF = re.compile(r"^\s*ELSIF\s+(?P<expr>.+?)\s+THEN\b", re.IGNORECASE)
_CASE = re.compile(r"^\s*CASE\s+(?P<expr>.+?)\s+OF\b", re.IGNORECASE)
_CASE_LABEL = re.compile(
    r"^\s*(?:[-+]?\d+(?:\s*,\s*[-+]?\d+)*(?:\s*\.\.\s*[-+]?\d+)?|[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<body>.*)$"
)
_TYPE_CONVERSIONS = {
    "BOOL", "SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT",
    "REAL", "LREAL", "TIME", "TIME32", "LTIME", "DATE", "DT", "LDT", "TOD", "STRING",
}
_SAFE_FUNCTIONS = {name.upper() for name in _v2._ST_SAFE_FUNCTIONS} | _TYPE_CONVERSIONS
_UNSUPPORTED_CONTROL = {"FOR", "WHILE", "REPEAT", "UNTIL", "EXIT", "RETURN"}
_INSTALLED = False
_PREVIOUS_PARSER = None
_PREVIOUS_HARDENER = None


@dataclass
class _Frame:
    kind: str
    refs: set[str]


def _strip_comments(text: str, in_comment: bool) -> tuple[str, bool]:
    out: list[str] = []
    index = 0
    while index < len(text):
        if in_comment:
            end = text.find("*)", index)
            if end < 0:
                return "".join(out), True
            in_comment = False
            index = end + 2
            continue
        start = text.find("(*", index)
        slash = text.find("//", index)
        if slash >= 0 and (start < 0 or slash < start):
            out.append(text[index:slash])
            return "".join(out), False
        if start < 0:
            out.append(text[index:])
            return "".join(out), False
        out.append(text[index:start])
        index = start + 2
        in_comment = True
    return "".join(out), in_comment


def _pop_frame(frames: list[_Frame], kind: str) -> bool:
    if not frames or frames[-1].kind != kind:
        return False
    frames.pop()
    return True


def _active_refs(frames: list[_Frame]) -> set[str]:
    refs: set[str] = set()
    for frame in frames:
        refs.update(frame.refs)
    return refs


def _control_state(code: str, frames: list[_Frame], ref_fn):
    """Return reads, optional close-kind, state, and assignment body."""
    upper = code.upper()
    reads: set[str] = set()
    state = PLCSemanticState.FULL
    close_after: str | None = None

    if_match = _IF.match(code)
    elsif_match = _ELSIF.match(code)
    case_match = _CASE.match(code)

    if if_match:
        refs = set(ref_fn(if_match.group("expr")))
        frames.append(_Frame("IF", refs))
        reads.update(refs)
    elif elsif_match:
        refs = set(ref_fn(elsif_match.group("expr")))
        if frames and frames[-1].kind == "IF":
            # The selected ELSIF path also depends on previous IF/ELSIF tests.
            frames[-1].refs.update(refs)
            reads.update(frames[-1].refs)
        else:
            reads.update(refs)
            state = PLCSemanticState.PARTIAL
    elif case_match:
        refs = set(ref_fn(case_match.group("expr")))
        frames.append(_Frame("CASE", refs))
        reads.update(refs)
    elif re.match(r"^\s*ELSE\b", code, flags=re.IGNORECASE):
        if frames:
            reads.update(frames[-1].refs)
        else:
            state = PLCSemanticState.PARTIAL
    elif re.match(r"^\s*END_IF\b", code, flags=re.IGNORECASE):
        reads.update(_active_refs(frames))
        close_after = "IF"
    elif re.match(r"^\s*END_CASE\b", code, flags=re.IGNORECASE):
        reads.update(_active_refs(frames))
        close_after = "CASE"
    else:
        reads.update(_active_refs(frames))

    body = code
    label = _CASE_LABEL.match(body)
    if label and frames and frames[-1].kind == "CASE":
        body = label.group("body").strip()
        reads.update(frames[-1].refs)

    recognized_control_only = bool(
        if_match
        or elsif_match
        or case_match
        or re.match(r"^\s*(ELSE|END_IF|END_CASE)\b", code, flags=re.IGNORECASE)
        or (label and frames and frames[-1].kind == "CASE")
    )
    return reads, close_after, state, body, recognized_control_only, upper


def parse_st_lines_v10(
    lines,
    *,
    artifact: str,
    controller: str,
    owner_type: str,
    owner_name: str,
    routine_name: str,
    known_aois: set[str],
):
    """Normalize bounded ST control/dataflow without claiming runtime execution.

    IF/ELSIF/ELSE and CASE/ELSE are modeled as source-traceable control
    dependencies. Loops, early-return flow, instruction-style calls, indirect
    indexing, and unresolved function/AOI semantics remain PARTIAL.
    """
    result: list[PLCLogicStatement] = []
    frames: list[_Frame] = []
    in_comment = False

    for ordinal, line in enumerate(lines):
        number = line.attrib.get("Number", str(ordinal))
        raw = "".join(line.itertext()).strip()
        if not raw:
            continue
        code, in_comment = _strip_comments(raw, in_comment)
        code = code.strip()
        if not code:
            continue

        source = PLCSourceRef(
            artifact=artifact,
            controller=controller,
            program=owner_name if owner_type == "program" else None,
            aoi=owner_name if owner_type == "aoi" else None,
            routine=routine_name,
            line=number,
        )
        reads, close_after, state, body, recognized_control_only, upper = _control_state(
            code, frames, _v2._refs
        )
        writes: set[str] = set()

        for assignment in _ASSIGNMENT.finditer(body):
            lhs = assignment.group("lhs")
            rhs = assignment.group("rhs").strip()
            if not rhs:
                state = PLCSemanticState.PARTIAL
                continue
            writes.add(lhs)
            reads.update(_v2._refs(rhs))

        calls = {
            name
            for name in _v2._ST_CALL.findall(code)
            if name.upper() not in _SAFE_FUNCTIONS
        }
        # Instruction-style calls and AOI calls need a dedicated ST call-site
        # binding theorem before they can be FULL.
        if calls:
            state = PLCSemanticState.PARTIAL
        if any(re.search(rf"\b{keyword}\b", upper) for keyword in _UNSUPPORTED_CONTROL):
            state = PLCSemanticState.PARTIAL
        if not writes and not recognized_control_only and not calls and code.strip("; "):
            state = PLCSemanticState.PARTIAL

        result.append(
            PLCLogicStatement(
                id=_v2._statement_id(source, raw, "STMT-ST"),
                language="ST",
                owner_type=owner_type,
                owner_name=owner_name,
                routine=routine_name,
                locator=number,
                text=raw,
                reads=tuple(sorted(reads)),
                writes=tuple(sorted(writes)),
                calls=tuple(sorted(calls)),
                semantic_state=state,
                source=source,
            )
        )

        if close_after is not None and not _pop_frame(frames, close_after):
            result[-1] = replace(result[-1], semantic_state=PLCSemanticState.PARTIAL)

    if frames or in_comment:
        result = [replace(item, semantic_state=PLCSemanticState.PARTIAL) for item in result]
    return result


def harden_st_statements_v10(project) -> None:
    """Revalidate V10 ST facts with literal/index/control fail-closed guards."""
    stacks: dict[tuple[str, str, str], list[_Frame]] = {}
    routine_indices: dict[tuple[str, str, str], list[int]] = {}
    hardened: list[PLCLogicStatement] = []

    for statement in project.logic_statements:
        if statement.language != "ST":
            hardened.append(statement)
            continue

        key = (statement.owner_type, statement.owner_name, statement.routine)
        frames = stacks.setdefault(key, [])
        routine_indices.setdefault(key, []).append(len(hardened))
        code, unterminated_comment = _strip_comments(statement.text, False)
        code = code.strip()
        if not code:
            hardened.append(replace(statement, semantic_state=PLCSemanticState.PARTIAL))
            continue

        reads, close_after, state, body, recognized_control_only, upper = _control_state(
            code, frames, _guard._refs
        )
        writes: set[str] = set()
        assignments = list(_ASSIGNMENT.finditer(body))
        for assignment in assignments:
            lhs = assignment.group("lhs")
            rhs = assignment.group("rhs").strip()
            if not rhs:
                state = PLCSemanticState.PARTIAL
                continue
            writes.add(lhs)
            reads.update(_guard._refs(rhs))

        scrubbed = _guard._scrub_literals(code)
        calls = {
            name
            for name in _guard._CALL.findall(scrubbed)
            if name.upper() not in _SAFE_FUNCTIONS
        }
        if calls:
            state = PLCSemanticState.PARTIAL
        if any(re.search(rf"\b{keyword}\b", upper) for keyword in _UNSUPPORTED_CONTROL):
            state = PLCSemanticState.PARTIAL
        if _guard._has_variable_subscript(code):
            state = PLCSemanticState.PARTIAL
        if unterminated_comment:
            state = PLCSemanticState.PARTIAL
        if not assignments and not recognized_control_only and not calls and code.strip("; "):
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
        if close_after is not None and not _pop_frame(frames, close_after):
            hardened[-1] = replace(hardened[-1], semantic_state=PLCSemanticState.PARTIAL)

    # Any routine with an unmatched IF/CASE frame is syntactically incomplete;
    # downgrade every ST fact from that routine so dependency/writer proofs cannot
    # use a fragment with unknown control scope.
    for key, frames in stacks.items():
        if not frames:
            continue
        for index in routine_indices.get(key, []):
            hardened[index] = replace(hardened[index], semantic_state=PLCSemanticState.PARTIAL)

    project.logic_statements = hardened
    project.st_statement_total = sum(1 for item in hardened if item.language == "ST")
    project.st_statement_semantic_count = sum(
        1
        for item in hardened
        if item.language == "ST" and item.semantic_state is PLCSemanticState.FULL
    )


def install() -> None:
    global _INSTALLED, _PREVIOUS_PARSER, _PREVIOUS_HARDENER
    if _INSTALLED:
        return
    _PREVIOUS_PARSER = _v2._parse_st_lines
    _PREVIOUS_HARDENER = _guard._harden_st_statements
    _v2._parse_st_lines = parse_st_lines_v10
    _guard._harden_st_statements = harden_st_statements_v10
    _INSTALLED = True


__all__ = ["harden_st_statements_v10", "install", "parse_st_lines_v10"]
