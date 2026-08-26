from __future__ import annotations

from dataclasses import dataclass, replace
import re

from devagent.plc.models import PLCLogicStatement, PLCSemanticState, PLCSourceRef
from devagent.plc import v2_semantics as _v2


_LHS = r"[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?(?:\.[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?)*"
_ASSIGNMENT = re.compile(rf"(?P<lhs>{_LHS})\s*:=\s*(?P<rhs>.*?)(?=;|$)", re.IGNORECASE)
_IF = re.compile(r"^\s*IF\s+(?P<expr>.+?)\s+THEN\b", re.IGNORECASE)
_ELSIF = re.compile(r"^\s*ELSIF\s+(?P<expr>.+?)\s+THEN\b", re.IGNORECASE)
_CASE = re.compile(r"^\s*CASE\s+(?P<expr>.+?)\s+OF\b", re.IGNORECASE)
_CASE_LABEL = re.compile(r"^\s*(?:[-+]?\d+(?:\s*,\s*[-+]?\d+)*(?:\s*\.\.\s*[-+]?\d+)?|[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<body>.*)$")
_TYPE_CONVERSIONS = {
    "BOOL", "SINT", "INT", "DINT", "LINT", "USINT", "UINT", "UDINT", "ULINT",
    "REAL", "LREAL", "TIME", "TIME32", "LTIME", "DATE", "DT", "LDT", "TOD", "STRING",
}
_SAFE_FUNCTIONS = {name.upper() for name in _v2._ST_SAFE_FUNCTIONS} | _TYPE_CONVERSIONS
_UNSUPPORTED_CONTROL = {"FOR", "WHILE", "REPEAT", "UNTIL", "EXIT", "RETURN"}
_INSTALLED = False
_PREVIOUS = None


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
    dependencies. Loops, early-return flow, instruction-style calls, and
    unresolved function/AOI semantics remain PARTIAL.
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
        upper = code.upper()
        reads: set[str] = set()
        writes: set[str] = set()
        state = PLCSemanticState.FULL
        close_after: str | None = None

        if_match = _IF.match(code)
        elsif_match = _ELSIF.match(code)
        case_match = _CASE.match(code)

        if if_match:
            refs = set(_v2._refs(if_match.group("expr")))
            frames.append(_Frame("IF", refs))
            reads.update(refs)
        elif elsif_match:
            refs = set(_v2._refs(elsif_match.group("expr")))
            if frames and frames[-1].kind == "IF":
                # An ELSIF branch depends on both the new expression and the
                # previously tested expressions being false.
                frames[-1].refs.update(refs)
                reads.update(frames[-1].refs)
            else:
                reads.update(refs)
                state = PLCSemanticState.PARTIAL
        elif case_match:
            refs = set(_v2._refs(case_match.group("expr")))
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
        # ST call syntax can represent AOIs, program-control instructions, or
        # vendor instructions. Until parameter direction and call execution are
        # bound for that call site, retain the call for traceability but fail
        # closed on FULL behavior semantics.
        if calls:
            state = PLCSemanticState.PARTIAL

        if any(re.search(rf"\b{keyword}\b", upper) for keyword in _UNSUPPORTED_CONTROL):
            state = PLCSemanticState.PARTIAL

        recognized_control_only = bool(
            if_match
            or elsif_match
            or case_match
            or re.match(r"^\s*(ELSE|END_IF|END_CASE)\b", code, flags=re.IGNORECASE)
            or (label and frames and frames[-1].kind == "CASE")
        )
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

    # An unterminated control frame makes routine control structure incomplete.
    # Do not keep apparently FULL statements from a syntactically incomplete
    # routine because their control reachability cannot be trusted.
    if frames or in_comment:
        result = [
            replace(item, semantic_state=PLCSemanticState.PARTIAL)
            for item in result
        ]
    return result


def install() -> None:
    global _INSTALLED, _PREVIOUS
    if _INSTALLED:
        return
    _PREVIOUS = _v2._parse_st_lines
    _v2._parse_st_lines = parse_st_lines_v10
    _INSTALLED = True


__all__ = ["install", "parse_st_lines_v10"]
