from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import re

from devagent.plc.analysis import build_dependency_graph
from devagent.plc.fat_procedure_v12 import enrich_fat_procedures
from devagent.plc.models import (
    FATTestCase,
    PLCEngineeringResult,
    PLCOutcome,
    PLCSemanticState,
    StaticCheck,
    StaticCheckStatus,
)
from devagent.plc.production_models import (
    EvidenceItem,
    RequirementStatus,
    RiskFinding,
    Severity,
)
from devagent.plc.production_utils import stable_id
from devagent.plc import schneider_call_graph_v3 as _v3
from devagent.plc import schneider_control_expert_v1 as _v1
from devagent.plc import schneider_graphical_v4 as _v4
from devagent.plc import schneider_st_control_flow_v2 as _v2


_INSTALLED = False
_PREVIOUS_ANALYZER = _v1.analyze_schneider_control_expert
_PREVIOUS_CAPABILITY = _v1.schneider_capability_profile

_CASE = re.compile(r"^\s*CASE\s+(?P<state>.+?)\s+OF\s*;?\s*$", re.IGNORECASE)
_END_CASE = re.compile(r"^\s*END_CASE\s*;?\s*$", re.IGNORECASE)
_IF = re.compile(r"^\s*IF\s+(?P<expr>.+?)\s+THEN\s*;?\s*$", re.IGNORECASE)
_ELSIF = re.compile(r"^\s*ELSIF\s+(?P<expr>.+?)\s+THEN\s*;?\s*$", re.IGNORECASE)
_ELSE = re.compile(r"^\s*ELSE\s*;?\s*$", re.IGNORECASE)
_END_IF = re.compile(r"^\s*END_IF\s*;?\s*$", re.IGNORECASE)
_UNSUPPORTED_CONTROL = re.compile(
    r"^\s*(CASE|FOR|WHILE|REPEAT|END_CASE|END_FOR|END_WHILE|UNTIL|END_REPEAT)\b",
    re.IGNORECASE,
)
_LABEL = re.compile(
    r"^\s*(?P<label>(?:[-+]?\d+)|(?:\d+#[0-9A-Fa-f_]+)|"
    r"(?:[A-Za-z_][A-Za-z0-9_.]*#[+-]?[0-9A-Fa-f_]+)|"
    r"(?:[A-Za-z_][A-Za-z0-9_.]*))\s*:\s*$"
)
_TARGET = re.compile(
    r"^\s*(?P<target>(?:[-+]?\d+)|(?:\d+#[0-9A-Fa-f_]+)|"
    r"(?:[A-Za-z_][A-Za-z0-9_.]*#[+-]?[0-9A-Fa-f_]+)|"
    r"(?:[A-Za-z_][A-Za-z0-9_.]*))\s*$"
)
_SIMPLE_REF = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_INTEGER_TYPES = {
    "SINT", "USINT", "INT", "UINT", "DINT", "UDINT", "LINT", "ULINT",
    "BYTE", "WORD", "DWORD", "LWORD",
}
_BOOL_TYPES = {"BOOL", "EBOOL", "BOOLEAN"}
_RUNTIME_BOOL_PINS = {
    "TON": {"Q"},
    "TOF": {"Q"},
    "TP": {"Q"},
    "CTU": {"Q"},
    "CTD": {"Q"},
    "CTUD": {"QU", "QD"},
}
_MAX_MACHINES = 64
_MAX_STATES = 128
_MAX_TRANSITIONS = 512
_MAX_PATHS = 64
_MAX_TERMS = 24


@dataclass(frozen=True)
class SchneiderV5TransitionFact:
    id: str
    machine_id: str
    section: str
    state_tag: str
    source_state: str
    target_state: str
    guard_paths: tuple[tuple[tuple[str, bool], ...], ...]
    source_line: int
    evidence_lines: tuple[int, ...]
    guard_text: str
    runtime_dependencies: tuple[str, ...] = ()
    semantic_state: PLCSemanticState = PLCSemanticState.FULL
    reason: str = "bounded_case_transition"


@dataclass(frozen=True)
class SchneiderV5StateMachineFact:
    id: str
    section: str
    relative_path: str
    state_tag: str
    state_type: str
    states: tuple[str, ...]
    transitions: tuple[SchneiderV5TransitionFact, ...]
    case_line: int
    end_line: int
    modeled_lines: tuple[int, ...]
    runtime_dependencies: tuple[str, ...]
    dangling_targets: tuple[str, ...]
    overlap_conflicts: tuple[str, ...]
    terminal_states: tuple[str, ...]
    entry_candidates: tuple[str, ...]
    writer_conflicts: tuple[str, ...]
    has_default_branch: bool
    semantic_state: PLCSemanticState
    reason: str


@dataclass(frozen=True)
class SchneiderV5Facts:
    machines: tuple[SchneiderV5StateMachineFact, ...]


class _Unsupported(ValueError):
    pass


def _statement_line(statement) -> int | None:
    return _v2._statement_line(statement)


def _normalize_ref(value: str) -> str | None:
    raw = str(value or "").strip()
    if not _SIMPLE_REF.fullmatch(raw):
        return None
    refs = _v1._extract_refs(raw)
    if len(refs) != 1 or refs[0].casefold() != raw.casefold():
        return None
    return refs[0]


def _normalize_state_value(value: str) -> str | None:
    match = _TARGET.match(str(value or ""))
    if match is None:
        return None
    target = match.group("target").strip()
    if "#" in target:
        prefix, payload = target.split("#", 1)
        if prefix.isdigit():
            return f"{prefix}#{payload.upper()}"
        return f"{prefix.upper()}#{payload.upper()}"
    return target


def _state_type(globals_by_name: dict[str, tuple[str, str]], state_tag: str) -> str | None:
    symbol = globals_by_name.get(state_tag.casefold())
    if symbol is None:
        return None
    dtype = _v3._type_name(symbol[1])
    return dtype if dtype in _INTEGER_TYPES else None


def _guard_paths(expr: str) -> tuple[object, tuple[tuple[tuple[str, bool], ...], ...]]:
    ast = _v1._parse_bool_ast(expr)
    if ast is None:
        raise _Unsupported("unsupported_boolean_guard")
    dnf = _v1._dnf(ast)
    if dnf is None or len(dnf) > _MAX_PATHS:
        raise _Unsupported("guard_complexity_limit")
    normalized = []
    for path in dnf:
        if len(path) > _MAX_TERMS:
            raise _Unsupported("guard_term_limit")
        normalized.append(tuple(sorted(path.items(), key=lambda item: item[0].casefold())))
    return ast, tuple(normalized)


def _exclusive_paths(asts: list[object], index: int, *, is_else: bool):
    paths = _v2._exclusive_guard(asts, index, is_else=is_else)
    if paths is None or len(paths) > _MAX_PATHS:
        raise _Unsupported("exclusive_guard_complexity_limit")
    normalized = []
    for path in paths:
        if len(path) > _MAX_TERMS:
            raise _Unsupported("exclusive_guard_term_limit")
        normalized.append(tuple(sorted(path.items(), key=lambda item: item[0].casefold())))
    return tuple(normalized)


def _paths_overlap(left, right) -> bool:
    for lpath in left:
        lmap = {name.casefold(): value for name, value in lpath}
        for rpath in right:
            conflict = False
            for name, value in rpath:
                existing = lmap.get(name.casefold())
                if existing is not None and existing != value:
                    conflict = True
                    break
            if not conflict:
                return True
    return False


def _runtime_dependency(
    ref: str,
    globals_by_name: dict[str, tuple[str, str]],
    project,
) -> str | None:
    exact = [
        tag for tag in project.tags
        if tag.scope.casefold() == "controller" and tag.name.casefold() == ref.casefold()
    ]
    if len(exact) == 1 and _v3._type_name(exact[0].data_type) in _BOOL_TYPES:
        return None

    if "." not in ref:
        raise _Unsupported(f"unresolved_or_non_boolean_guard:{ref}")
    base, pin = ref.split(".", 1)
    if "." in pin:
        raise _Unsupported(f"complex_runtime_guard:{ref}")
    symbol = globals_by_name.get(base.casefold())
    if symbol is None:
        raise _Unsupported(f"unresolved_guard:{ref}")
    dtype = _v3._type_name(symbol[1])
    pins = _RUNTIME_BOOL_PINS.get(dtype)
    if pins is None or pin.upper() not in pins:
        raise _Unsupported(f"unsupported_runtime_guard:{ref}:{dtype}")
    return f"{symbol[0]}:{dtype}"


def _validate_guard_refs(
    paths,
    *,
    globals_by_name: dict[str, tuple[str, str]],
    project,
    state_tag: str,
    prior_writes: set[str],
) -> tuple[str, ...]:
    refs = {name for path in paths for name, _value in path}
    if any(ref.casefold() == state_tag.casefold() for ref in refs):
        raise _Unsupported("guard_references_case_state")
    if any(ref.casefold() in prior_writes for ref in refs):
        raise _Unsupported("guard_depends_on_in_scan_write")
    deps = set()
    for ref in refs:
        dep = _runtime_dependency(ref, globals_by_name, project)
        if dep:
            deps.add(dep)
    return tuple(sorted(deps, key=str.casefold))


def _state_assignment(text: str, state_tag: str):
    match = _v1._ASSIGNMENT.match(text if text.rstrip().endswith(";") else text + ";")
    if match is None:
        return None
    lhs = _v1._lhs_ref(match.group("lhs"))
    if lhs is None or lhs.casefold() != state_tag.casefold():
        return None
    target = _normalize_state_value(match.group("rhs").strip())
    if target is None:
        raise _Unsupported("state_target_must_be_simple_literal")
    return target


def _assignment_lhs(text: str) -> str | None:
    match = _v1._ASSIGNMENT.match(text if text.rstrip().endswith(";") else text + ";")
    if match is None:
        return None
    return _v1._lhs_ref(match.group("lhs"))


def _runtime_call(
    text: str,
    globals_by_name: dict[str, tuple[str, str]],
) -> str | None:
    match = _v1._CALL.match(text)
    if match is None:
        return None
    symbol = match.group("name").strip()
    if "." in symbol:
        raise _Unsupported(f"unsupported_call_symbol:{symbol}")
    info = globals_by_name.get(symbol.casefold())
    if info is None:
        raise _Unsupported(f"unresolved_call:{symbol}")
    dtype = _v3._type_name(info[1])
    if dtype not in _RUNTIME_BOOL_PINS:
        raise _Unsupported(f"unsupported_call_in_case_branch:{symbol}:{dtype}")
    return f"{info[0]}:{dtype}"


def _collect_if(
    body: list[tuple[int, str]],
    start: int,
    *,
    state_tag: str,
    globals_by_name: dict[str, tuple[str, str]],
    project,
    prior_writes: set[str],
):
    first_line, first_text = body[start]
    first = _IF.match(first_text)
    if first is None:
        return None

    arms: list[dict[str, object]] = [
        {"guard": first.group("expr"), "lines": [], "control_line": first_line}
    ]
    control_lines = {first_line}
    cursor = start + 1
    depth = 0
    saw_else = False

    while cursor < len(body):
        line, text = body[cursor]
        if _IF.match(text):
            depth += 1
            arms[-1]["lines"].append((line, text))
            cursor += 1
            continue
        if _END_IF.match(text):
            if depth:
                depth -= 1
                arms[-1]["lines"].append((line, text))
                cursor += 1
                continue
            control_lines.add(line)
            break
        if depth:
            arms[-1]["lines"].append((line, text))
            cursor += 1
            continue
        match = _ELSIF.match(text)
        if match:
            if saw_else:
                raise _Unsupported("elsif_after_else")
            arms.append({"guard": match.group("expr"), "lines": [], "control_line": line})
            control_lines.add(line)
            cursor += 1
            continue
        if _ELSE.match(text):
            if saw_else:
                raise _Unsupported("duplicate_else")
            saw_else = True
            arms.append({"guard": None, "lines": [], "control_line": line})
            control_lines.add(line)
            cursor += 1
            continue
        if _UNSUPPORTED_CONTROL.match(text):
            raise _Unsupported("nested_control_in_transition")
        arms[-1]["lines"].append((line, text))
        cursor += 1
    else:
        raise _Unsupported("unterminated_if")

    guard_asts = []
    for arm in arms:
        if arm["guard"] is None:
            continue
        ast, _paths = _guard_paths(str(arm["guard"]))
        guard_asts.append(ast)

    transitions = []
    modeled = set(control_lines)
    for index, arm in enumerate(arms):
        assignments = []
        for line, text in arm["lines"]:
            if _IF.match(text) or _END_IF.match(text):
                raise _Unsupported("nested_if_in_transition")
            assignment = _state_assignment(text, state_tag)
            if assignment is not None:
                assignments.append((line, assignment))
                continue
            lhs = _assignment_lhs(text)
            if lhs is not None:
                raise _Unsupported("non_state_write_inside_transition_arm")
            if _v1._CALL.match(text):
                raise _Unsupported("call_inside_transition_arm")
            if text.strip():
                raise _Unsupported("unsupported_statement_inside_transition_arm")

        if len(assignments) > 1:
            raise _Unsupported("multiple_state_writes_in_if_arm")
        if not assignments:
            continue

        is_else = arm["guard"] is None
        guard_index = len([item for item in arms[:index] if item["guard"] is not None])
        if is_else:
            paths = _exclusive_paths(guard_asts, len(guard_asts), is_else=True)
            guard_text = "ELSE"
        else:
            paths = _exclusive_paths(guard_asts, guard_index, is_else=False)
            guard_text = str(arm["guard"])

        deps = _validate_guard_refs(
            paths,
            globals_by_name=globals_by_name,
            project=project,
            state_tag=state_tag,
            prior_writes=prior_writes,
        )
        assign_line, target = assignments[0]
        evidence = tuple(sorted(set((*control_lines, assign_line))))
        transitions.append((assign_line, target, paths, guard_text, evidence, deps))
        modeled.add(assign_line)

    return cursor, transitions, modeled


def _skip_if_region(body: list[tuple[int, str]], start: int) -> int:
    depth = 0
    for index in range(start, len(body)):
        text = body[index][1]
        if _IF.match(text):
            depth += 1
        elif _END_IF.match(text):
            depth -= 1
            if depth == 0:
                return index + 1
    return len(body)


def _skip_unsupported_region(body: list[tuple[int, str]], start: int) -> int:
    open_re = re.compile(r"^\s*(FOR|WHILE|REPEAT)\b", re.IGNORECASE)
    close_re = re.compile(r"^\s*(END_FOR|END_WHILE|UNTIL|END_REPEAT)\b", re.IGNORECASE)
    expected = {
        "FOR": {"END_FOR"},
        "WHILE": {"END_WHILE"},
        "REPEAT": {"UNTIL", "END_REPEAT"},
    }
    stack: list[str] = []
    for index in range(start, len(body)):
        text = body[index][1]
        opened = open_re.match(text)
        if opened:
            stack.append(opened.group(1).upper())
            continue
        closed = close_re.match(text)
        if closed and stack and closed.group(1).upper() in expected[stack[-1]]:
            stack.pop()
            if not stack:
                return index + 1
    return len(body)


def _case_end(lines: list[tuple[int, str]], start: int) -> int:
    depth = 0
    for index in range(start + 1, len(lines)):
        text = lines[index][1]
        if _CASE.match(text):
            depth += 1
            continue
        if _END_CASE.match(text):
            if depth:
                depth -= 1
                continue
            return index
    return len(lines) - 1


def _partial_machine(
    *,
    section: str,
    relative: str,
    case_line: int,
    end_line: int,
    state_tag: str,
    reason: str,
) -> SchneiderV5StateMachineFact:
    digest = hashlib.sha1(
        f"{relative}:{section}:{case_line}:{state_tag}:{reason}".encode()
    ).hexdigest()[:14]
    return SchneiderV5StateMachineFact(
        id=f"SCHNEIDER-SM5-{digest}",
        section=section,
        relative_path=relative,
        state_tag=state_tag or "<unresolved>",
        state_type="UNKNOWN",
        states=(),
        transitions=(),
        case_line=case_line,
        end_line=end_line,
        modeled_lines=(case_line,),
        runtime_dependencies=(),
        dangling_targets=(),
        overlap_conflicts=(),
        terminal_states=(),
        entry_candidates=(),
        writer_conflicts=(),
        has_default_branch=False,
        semantic_state=PLCSemanticState.PARTIAL,
        reason=reason,
    )


def _parse_machine(
    *,
    project,
    section: str,
    relative: str,
    lines: list[tuple[int, str]],
    start: int,
    globals_by_name: dict[str, tuple[str, str]],
):
    case_line, case_text = lines[start]
    match = _CASE.match(case_text)
    if match is None:
        return None

    state_tag = _normalize_ref(match.group("state"))
    if state_tag is None:
        raise _Unsupported("case_state_reference_required")
    state_type = _state_type(globals_by_name, state_tag)
    if state_type is None:
        raise _Unsupported("case_state_type_must_be_exported_integer_scalar")

    cursor = start + 1
    states: list[str] = []
    branch_order: list[str] = []
    branches: dict[str, list[tuple[int, str]]] = {}
    label_lines: dict[str, int] = {}
    current: str | None = None
    default_mode = False
    default_lines: list[tuple[int, str]] = []
    if_depth = 0
    end_line = None

    while cursor < len(lines):
        line, text = lines[cursor]
        if _CASE.match(text):
            raise _Unsupported("nested_case_unsupported")
        if _END_CASE.match(text) and if_depth == 0:
            end_line = line
            break

        if _IF.match(text):
            if_depth += 1
        elif _END_IF.match(text):
            if_depth = max(0, if_depth - 1)

        if if_depth == 0 and _ELSE.match(text):
            default_mode = True
            current = "__DEFAULT__"
            cursor += 1
            continue

        label = _LABEL.match(text) if if_depth == 0 else None
        if label:
            if default_mode:
                raise _Unsupported("state_label_after_case_else")
            value = _normalize_state_value(label.group("label"))
            if value is None:
                raise _Unsupported("invalid_state_label")
            if any(value.casefold() == item.casefold() for item in states):
                raise _Unsupported(f"duplicate_state_label:{value}")
            states.append(value)
            branch_order.append(value)
            branches[value] = []
            label_lines[value.casefold()] = line
            current = value
            cursor += 1
            continue

        if current is None:
            raise _Unsupported(f"case_content_before_state_label:{line}")
        if current == "__DEFAULT__":
            default_lines.append((line, text))
        else:
            branches[current].append((line, text))
        cursor += 1

    if end_line is None:
        raise _Unsupported("unterminated_case")
    if not states:
        raise _Unsupported("case_has_no_states")
    if len(states) > _MAX_STATES:
        raise _Unsupported("state_count_limit")

    transitions: list[SchneiderV5TransitionFact] = []
    modeled_lines = {case_line, end_line, *label_lines.values()}
    runtime_deps: set[str] = set()
    reasons: list[str] = []
    complete = True

    for source_state in branch_order:
        body = branches[source_state]
        pos = 0
        prior_writes: set[str] = set()
        while pos < len(body):
            line, text = body[pos]

            if _IF.match(text):
                try:
                    end_index, raw_transitions, modeled = _collect_if(
                        body,
                        pos,
                        state_tag=state_tag,
                        globals_by_name=globals_by_name,
                        project=project,
                        prior_writes=prior_writes,
                    )
                except _Unsupported as exc:
                    complete = False
                    reasons.append(str(exc))
                    pos = _skip_if_region(body, pos)
                    continue
                for assign_line, target, paths, guard_text, evidence, deps in raw_transitions:
                    runtime_deps.update(deps)
                    digest = hashlib.sha1(
                        f"{relative}:{section}:{state_tag}:{source_state}:{target}:{assign_line}:{guard_text}".encode()
                    ).hexdigest()[:14]
                    transitions.append(
                        SchneiderV5TransitionFact(
                            id=f"SCHNEIDER-STATE5-{digest}",
                            machine_id="",
                            section=section,
                            state_tag=state_tag,
                            source_state=source_state,
                            target_state=target,
                            guard_paths=paths,
                            source_line=assign_line,
                            evidence_lines=evidence,
                            guard_text=guard_text,
                            runtime_dependencies=deps,
                        )
                    )
                modeled_lines.update(modeled)
                pos = end_index + 1
                continue

            if _ELSIF.match(text) or _ELSE.match(text) or _END_IF.match(text):
                complete = False
                reasons.append("unbalanced_if_control")
                pos += 1
                continue

            if _UNSUPPORTED_CONTROL.match(text):
                complete = False
                reasons.append("unsupported_nested_control")
                if re.match(r"^\s*(FOR|WHILE|REPEAT)\b", text, flags=re.IGNORECASE):
                    pos = _skip_unsupported_region(body, pos)
                else:
                    pos += 1
                continue

            assignment = _state_assignment(text, state_tag)
            if assignment is not None:
                digest = hashlib.sha1(
                    f"{relative}:{section}:{state_tag}:{source_state}:{assignment}:{line}:TRUE".encode()
                ).hexdigest()[:14]
                transitions.append(
                    SchneiderV5TransitionFact(
                        id=f"SCHNEIDER-STATE5-{digest}",
                        machine_id="",
                        section=section,
                        state_tag=state_tag,
                        source_state=source_state,
                        target_state=assignment,
                        guard_paths=((),),
                        source_line=line,
                        evidence_lines=(line,),
                        guard_text="TRUE",
                    )
                )
                modeled_lines.add(line)
                pos += 1
                continue

            lhs = _assignment_lhs(text)
            if lhs is not None:
                if lhs.casefold() == state_tag.casefold():
                    complete = False
                    reasons.append("unsupported_state_write")
                else:
                    prior_writes.add(lhs.casefold())
                pos += 1
                continue

            if _v1._CALL.match(text):
                try:
                    dep = _runtime_call(text, globals_by_name)
                except _Unsupported as exc:
                    complete = False
                    reasons.append(str(exc))
                    pos += 1
                    continue
                if dep:
                    runtime_deps.add(dep)
                pos += 1
                continue

            if text.strip():
                complete = False
                reasons.append(f"unsupported_case_statement:{line}")
            pos += 1

    if len(transitions) > _MAX_TRANSITIONS:
        raise _Unsupported("transition_count_limit")

    digest = hashlib.sha1(
        f"{relative}:{section}:{case_line}:{state_tag}".encode()
    ).hexdigest()[:14]
    machine_id = f"SCHNEIDER-SM5-{digest}"
    transitions = [replace(item, machine_id=machine_id) for item in transitions]

    defined = {state.casefold(): state for state in states}
    dangling = tuple(
        sorted(
            {
                item.target_state
                for item in transitions
                if item.target_state.casefold() not in defined
            },
            key=str.casefold,
        )
    )

    conflicts = []
    grouped: dict[str, list[SchneiderV5TransitionFact]] = defaultdict(list)
    for transition in transitions:
        grouped[transition.source_state.casefold()].append(transition)
    for source_key, items in grouped.items():
        for index, left in enumerate(items):
            for right in items[index + 1:]:
                if left.target_state.casefold() == right.target_state.casefold():
                    continue
                if _paths_overlap(left.guard_paths, right.guard_paths):
                    source = defined.get(source_key, left.source_state)
                    conflicts.append(f"{source}:{left.target_state}|{right.target_state}")

    outgoing = {state.casefold(): 0 for state in states}
    incoming = {state.casefold(): 0 for state in states}
    for transition in transitions:
        if transition.source_state.casefold() in outgoing:
            outgoing[transition.source_state.casefold()] += 1
        if transition.target_state.casefold() in incoming:
            incoming[transition.target_state.casefold()] += 1

    terminal = tuple(state for state in states if outgoing[state.casefold()] == 0)
    entry = tuple(state for state in states if incoming[state.casefold()] == 0)

    if not transitions:
        complete = False
        reasons.append("no_state_transitions")
    if dangling:
        complete = False
        reasons.append("dangling_transition_target")
    if conflicts:
        complete = False
        reasons.append("overlapping_transition_guards")
    if default_mode:
        complete = False
        reasons.append("case_else_behavior_not_modeled")

    semantic = PLCSemanticState.FULL if complete else PLCSemanticState.PARTIAL
    reason = "bounded_case_state_machine" if complete else ",".join(dict.fromkeys(reasons))

    return cursor, SchneiderV5StateMachineFact(
        id=machine_id,
        section=section,
        relative_path=relative,
        state_tag=state_tag,
        state_type=state_type,
        states=tuple(states),
        transitions=tuple(transitions),
        case_line=case_line,
        end_line=end_line,
        modeled_lines=tuple(sorted(modeled_lines)),
        runtime_dependencies=tuple(sorted(runtime_deps, key=str.casefold)),
        dangling_targets=dangling,
        overlap_conflicts=tuple(sorted(set(conflicts), key=str.casefold)),
        terminal_states=terminal,
        entry_candidates=entry,
        writer_conflicts=(),
        has_default_branch=default_mode,
        semantic_state=semantic,
        reason=reason or "state_machine_partial",
    )


def _discover_machines(path: Path, project) -> tuple[SchneiderV5StateMachineFact, ...]:
    globals_by_name = _v3._global_symbols(path)
    result: list[SchneiderV5StateMachineFact] = []
    for section, relative, raw_text in _v2._iter_st_sources(path):
        clean = _v1._strip_comments(raw_text)
        lines = [
            (index, text.strip())
            for index, text in enumerate(clean.splitlines(), start=1)
            if text.strip()
        ]
        cursor = 0
        while cursor < len(lines):
            case_match = _CASE.match(lines[cursor][1])
            if case_match is None:
                cursor += 1
                continue
            if len(result) >= _MAX_MACHINES:
                return tuple(result)
            case_line = lines[cursor][0]
            raw_state = case_match.group("state").strip()
            state_tag = _normalize_ref(raw_state) or raw_state
            try:
                end_index, machine = _parse_machine(
                    project=project,
                    section=section,
                    relative=relative,
                    lines=lines,
                    start=cursor,
                    globals_by_name=globals_by_name,
                )
            except _Unsupported as exc:
                end_index = _case_end(lines, cursor)
                result.append(
                    _partial_machine(
                        section=section,
                        relative=relative,
                        case_line=case_line,
                        end_line=lines[end_index][0] if lines else case_line,
                        state_tag=state_tag,
                        reason=str(exc),
                    )
                )
                cursor = end_index + 1
                continue
            result.append(machine)
            cursor = end_index + 1
    return tuple(result)


def _reconcile_writers(project, machines):
    if not machines:
        return tuple(machines)

    modeled_lines: dict[str, set[tuple[str, int]]] = defaultdict(set)
    machine_counts: dict[str, int] = defaultdict(int)
    for machine in machines:
        key = machine.state_tag.casefold()
        machine_counts[key] += 1
        for transition in machine.transitions:
            modeled_lines[key].add((machine.section.casefold(), transition.source_line))

    external: dict[str, list[str]] = defaultdict(list)
    labels: dict[str, str] = {}
    for statement in project.logic_statements:
        line = _statement_line(statement)
        owner = (
            statement.source.routine
            or statement.routine
            or statement.owner_name
            or ""
        ).casefold()
        for write in statement.writes:
            key = write.casefold()
            if key not in machine_counts:
                continue
            labels.setdefault(key, write)
            if line is None or (owner, line) not in modeled_lines[key]:
                external[key].append(statement.id)

    updated = []
    for machine in machines:
        key = machine.state_tag.casefold()
        conflicts = list(external.get(key, ()))
        if machine_counts[key] > 1:
            conflicts.append(f"multiple_case_machines:{labels.get(key, machine.state_tag)}")
        if not conflicts:
            updated.append(machine)
            continue
        reasons = [item for item in machine.reason.split(",") if item and item != "bounded_case_state_machine"]
        reasons.append("competing_state_writer")
        updated.append(
            replace(
                machine,
                writer_conflicts=tuple(sorted(set(conflicts), key=str.casefold)),
                semantic_state=PLCSemanticState.PARTIAL,
                reason=",".join(dict.fromkeys(reasons)),
            )
        )
    return tuple(updated)


def _upgrade_statements(project, machines) -> None:
    full_lines: set[tuple[str, int]] = set()
    for machine in machines:
        if machine.semantic_state is not PLCSemanticState.FULL:
            continue
        for line in machine.modeled_lines:
            full_lines.add((machine.section.casefold(), line))

    if not full_lines:
        return
    updated = []
    for statement in project.logic_statements:
        if statement.language != "ST":
            updated.append(statement)
            continue
        line = _statement_line(statement)
        owner = (
            statement.source.routine
            or statement.routine
            or statement.owner_name
            or ""
        ).casefold()
        if line is None or (owner, line) not in full_lines:
            updated.append(statement)
            continue
        updated.append(replace(statement, semantic_state=PLCSemanticState.FULL))
    project.logic_statements = updated
    _v4._refresh_counts(project)


def _facts(project) -> SchneiderV5Facts | None:
    return getattr(project, "_schneider_v5_facts", None)


def schneider_capability_profile_v5(project) -> dict[str, object]:
    profile = dict(_PREVIOUS_CAPABILITY(project))
    facts = _facts(project)
    profile["schema"] = "devagent-schneider-control-expert-capability-v5"
    if facts is None:
        profile.update(
            {
                "state_machines": 0,
                "state_machine_full": 0,
                "state_machine_partial": 0,
                "state_machine_states": 0,
                "state_machine_transitions": 0,
                "state_machine_runtime_dependencies": 0,
                "state_machine_dangling_targets": 0,
                "state_machine_overlap_conflicts": 0,
                "state_machine_terminal_states": 0,
                "state_machine_entry_candidates": 0,
                "state_machine_writer_conflicts": 0,
                "state_machine_contract": "NONE",
                "bounded_state_machine_semantics": (
                    "single-level Control Expert ST CASE over one exported integer state tag "
                    "with simple state labels/targets and direct or Boolean IF/ELSIF/ELSE guarded state writes"
                ),
            }
        )
        return profile

    machines = facts.machines
    full = sum(item.semantic_state is PLCSemanticState.FULL for item in machines)
    profile.update(
        {
            "state_machines": len(machines),
            "state_machine_full": full,
            "state_machine_partial": len(machines) - full,
            "state_machine_states": sum(len(item.states) for item in machines),
            "state_machine_transitions": sum(len(item.transitions) for item in machines),
            "state_machine_runtime_dependencies": sum(len(item.runtime_dependencies) for item in machines),
            "state_machine_dangling_targets": sum(len(item.dangling_targets) for item in machines),
            "state_machine_overlap_conflicts": sum(len(item.overlap_conflicts) for item in machines),
            "state_machine_terminal_states": sum(len(item.terminal_states) for item in machines),
            "state_machine_entry_candidates": sum(len(item.entry_candidates) for item in machines),
            "state_machine_writer_conflicts": sum(len(item.writer_conflicts) for item in machines),
            "state_machine_contract": (
                "COMPLETE" if machines and full == len(machines) else "PARTIAL_FAIL_CLOSED"
            ),
            "bounded_state_machine_semantics": (
                "single-level Control Expert ST CASE over one exported integer state tag "
                "with simple state labels/targets and direct or Boolean IF/ELSIF/ELSE guarded state writes"
            ),
        }
    )
    return profile


def _source_for_machine(project, machine):
    matches = [
        item for item in project.logic_statements
        if item.language == "ST"
        and (item.source.routine or item.routine or item.owner_name or "").casefold()
        == machine.section.casefold()
        and _statement_line(item) == machine.case_line
    ]
    if len(matches) == 1:
        return matches[0].source
    return _v3._source_ref(project, machine.section, str(machine.case_line), relative=machine.relative_path)


def _state_machine_fat(project, machines) -> list[FATTestCase]:
    tests = []
    for machine in machines:
        source = _source_for_machine(project, machine)
        digest = hashlib.sha1(f"{machine.id}:startup".encode()).hexdigest()[:10]
        tests.append(
            FATTestCase(
                id=f"FAT-SCHNEIDER-SM5-{digest}",
                title=f"Verify startup/reset state for {machine.state_tag} in {machine.section}",
                source=source,
                output_tag=machine.state_tag,
                preconditions={},
                expected=(
                    f"Engineer evidence establishes the intended startup/reset value of {machine.state_tag}, "
                    "retentivity behavior, and the first enabled CASE state before automatic sequence operation."
                ),
                method="RUNTIME_FAT_REQUIRED",
                scenario="SCHNEIDER_STATE_STARTUP",
                limitations=(
                    "The Control Expert export does not prove retained-memory history, CPU restart behavior, or process initial conditions.",
                    "DevAgent does not execute Control Expert Simulator, HIL, or a real Modicon PLC.",
                ),
                watch_tags=(machine.state_tag,),
            )
        )

        for transition in machine.transitions:
            refs = tuple(
                sorted(
                    {name for path in transition.guard_paths for name, _value in path},
                    key=str.casefold,
                )
            )
            preconditions = {}
            if len(transition.guard_paths) == 1:
                preconditions = dict(transition.guard_paths[0])
            digest = hashlib.sha1(f"{transition.id}:fat".encode()).hexdigest()[:10]
            runtime_text = (
                f" Runtime dependency: {', '.join(transition.runtime_dependencies)}."
                if transition.runtime_dependencies
                else ""
            )
            tests.append(
                FATTestCase(
                    id=f"FAT-SCHNEIDER-SM5-{digest}",
                    title=(
                        f"Verify state transition {machine.state_tag}: "
                        f"{transition.source_state} -> {transition.target_state}"
                    ),
                    source=source,
                    output_tag=machine.state_tag,
                    preconditions=preconditions,
                    expected=(
                        f"With {machine.state_tag}={transition.source_state}, the source-linked guard "
                        f"({transition.guard_text}) causes {machine.state_tag} to transition to "
                        f"{transition.target_state}; when the guard is not satisfied, no unintended "
                        f"transition occurs.{runtime_text}"
                    ),
                    method="RUNTIME_FAT_REQUIRED",
                    scenario="SCHNEIDER_STATE_TRANSITION",
                    limitations=(
                        "Static V5 evidence proves only the bounded source transition relation; scan timing, retained state, I/O timing, timer/counter evolution, and process physics remain runtime evidence.",
                        "DevAgent does not execute Control Expert Simulator, HIL, or a real Modicon PLC.",
                    ),
                    watch_tags=tuple(dict.fromkeys((machine.state_tag, *refs))),
                )
            )

        if (
            machine.semantic_state is not PLCSemanticState.FULL
            or machine.dangling_targets
            or machine.overlap_conflicts
            or machine.writer_conflicts
            or machine.has_default_branch
        ):
            digest = hashlib.sha1(f"{machine.id}:gap".encode()).hexdigest()[:10]
            tests.append(
                FATTestCase(
                    id=f"FAT-SCHNEIDER-SM5-{digest}",
                    title=f"Resolve fail-closed sequence gaps for {machine.state_tag} in {machine.section}",
                    source=source,
                    output_tag=machine.state_tag,
                    preconditions={},
                    expected=(
                        "Engineer review/runtime evidence resolves every V5 fail-closed sequence gap "
                        "before the state machine is accepted for commissioning."
                    ),
                    method="RUNTIME_FAT_REQUIRED",
                    scenario="SCHNEIDER_STATE_GAP",
                    limitations=(
                        f"V5 machine state: {machine.semantic_state.value}; reason: {machine.reason}.",
                        f"Dangling targets: {', '.join(machine.dangling_targets) or 'none'}; "
                        f"overlap conflicts: {', '.join(machine.overlap_conflicts) or 'none'}.",
                    ),
                    watch_tags=(machine.state_tag,),
                )
            )
    return enrich_fat_procedures(project, tests)


def _v5_checks(machines) -> list[StaticCheck]:
    if not machines:
        return [
            StaticCheck(
                "SCHNEIDER_V5_STATE_MACHINE",
                StaticCheckStatus.WARN,
                "No bounded Control Expert ST CASE state machine was recognized; V5 made no sequencing proof claim.",
            )
        ]

    full = sum(item.semantic_state is PLCSemanticState.FULL for item in machines)
    transitions = sum(len(item.transitions) for item in machines)
    dangling = sum(len(item.dangling_targets) for item in machines)
    overlap = sum(len(item.overlap_conflicts) for item in machines)
    writers = sum(len(item.writer_conflicts) for item in machines)
    runtime = sum(len(item.runtime_dependencies) for item in machines)
    evidence = tuple(item.id for item in machines)
    return [
        StaticCheck(
            "SCHNEIDER_V5_STATE_MACHINE",
            StaticCheckStatus.PASS if full == len(machines) else StaticCheckStatus.NOT_PROVEN,
            (
                f"Modeled {full}/{len(machines)} bounded Control Expert ST CASE state machine(s) "
                f"with {transitions} explicit transition(s); unsupported variants remain fail-closed."
            ),
            evidence,
        ),
        StaticCheck(
            "SCHNEIDER_V5_TRANSITION_INTEGRITY",
            StaticCheckStatus.PASS if not dangling and not overlap else StaticCheckStatus.NOT_PROVEN,
            f"Transition integrity: dangling targets={dangling}, overlapping different-target guards={overlap}.",
            evidence,
        ),
        StaticCheck(
            "SCHNEIDER_V5_STATE_WRITERS",
            StaticCheckStatus.PASS if not writers else StaticCheckStatus.NOT_PROVEN,
            f"State writer ownership: competing writer evidence={writers}.",
            evidence,
        ),
        StaticCheck(
            "SCHNEIDER_V5_SEQUENCE_RUNTIME",
            StaticCheckStatus.NOT_PROVEN,
            (
                f"Sequence execution remains engineer runtime evidence; {runtime} timer/counter dependency "
                "binding(s) do not prove time/count evolution, startup state, retentivity, scan order, or process behavior."
            ),
            evidence,
        ),
    ]


def analyze_schneider_control_expert_v5(path: Path) -> PLCEngineeringResult:
    base = _PREVIOUS_ANALYZER(path)
    project = base.project
    machines = _discover_machines(path, project)
    if not machines:
        return base

    machines = _reconcile_writers(project, machines)
    _upgrade_statements(project, machines)
    facts = SchneiderV5Facts(tuple(machines))
    setattr(project, "_schneider_v5_facts", facts)
    _v4._refresh_counts(project)

    graph = build_dependency_graph(project)
    v3facts = _v3._facts(project)
    if v3facts is not None:
        graph = _v3._augment_graph(graph, v3facts)

    v4facts = _v4._facts(project)
    v4_ids = set(v4facts.modeled_logic_ids) if v4facts is not None else set()
    fat_tests = _v4._normalize_fat(project, _v1._fat_tests(project), v4_ids)
    if v3facts is not None:
        fat_tests.extend(_v3._call_gap_fat(project, v3facts))
    if v4facts is not None:
        fat_tests.extend(_v4._gap_fat(project, v4facts))
    fat_tests.extend(_state_machine_fat(project, machines))
    fat_tests = list({item.id: item for item in fat_tests}.values())

    checks = _v1._checks(project, graph, fat_tests)
    if v3facts is not None:
        checks.extend(_v3._v3_checks(project, v3facts))
    if v4facts is not None:
        checks.extend(_v4._v4_checks(v4facts))
    checks.extend(_v5_checks(machines))

    profile = schneider_capability_profile_v5(project)
    closure_complete = (
        v3facts is None
        or (profile.get("execution_closure") == "COMPLETE" and not v3facts.writer_conflicts)
    )
    graphical_complete = (
        v4facts is None
        or (
            not v4facts.partial
            and not v4facts.withheld
            and not v4facts.writer_conflicts
        )
    )
    state_complete = profile["state_machine_contract"] == "COMPLETE"

    if profile["static_contract"] == "NO_EXECUTABLE_LOGIC":
        outcome = PLCOutcome.BLOCKED
    elif (
        profile["static_contract"] == "COMPLETE"
        and closure_complete
        and graphical_complete
        and state_complete
    ):
        outcome = PLCOutcome.STATICALLY_VERIFIED
    else:
        outcome = PLCOutcome.PARTIALLY_VERIFIED

    limitations = [item.replace("Schneider V4", "Schneider V5") for item in base.limitations]
    limitations.append(
        "Schneider V5 adds a bounded Control Expert ST CASE sequencing theorem for one exported integer state tag, simple state labels/targets, and direct or Boolean IF/ELSIF/ELSE guarded state writes. Nested CASE/IF controls, loops, CASE ELSE behavior, unsupported calls, complex state expressions, ambiguous writers, and unsupported guard types remain fail-closed."
    )
    limitations.append(
        "The V5 transition graph is source-structural proof, not runtime sequence PASS. Startup/retained state, scan order, timer/counter evolution, I/O refresh, process timing/physics, Control Expert Simulator, HIL, and real Modicon execution require engineer evidence."
    )
    return PLCEngineeringResult(
        outcome,
        project,
        graph,
        fat_tests,
        checks,
        list(dict.fromkeys(limitations)),
    )


def _v5_verify_requirement(previous, requirement, engineering, evidence, tests):
    result = previous(requirement, engineering, evidence, tests)
    facts = _facts(engineering.project)
    if facts is None or result.status is not RequirementStatus.TRACEABLE_NOT_PROVEN:
        return result

    matched = {tag.casefold() for tag in result.matched_tags}
    related = [
        machine for machine in facts.machines
        if machine.state_tag.casefold() in matched
    ]
    if len(related) != 1:
        return result

    machine = related[0]
    evidence_ids = tuple(
        dict.fromkeys(
            [
                *result.evidence_ids,
                machine.id,
                *(item.id for item in machine.transitions),
            ]
        )
    )
    return replace(
        result,
        summary=(
            f"{result.summary} Schneider V5 provides a source-linked bounded CASE transition graph "
            f"for {machine.state_tag}, but sequence requirements remain TRACEABLE_NOT_PROVEN until "
            "startup/retentivity, scan/runtime behavior, and engineer FAT evidence are satisfied."
        ),
        evidence_ids=evidence_ids,
    )


def _v5_evidence(previous, engineering):
    items = list(previous(engineering))
    facts = _facts(engineering.project)
    if facts is None:
        return items

    project = engineering.project
    existing = {item.id for item in items}
    for machine in facts.machines:
        if machine.id not in existing:
            items.append(
                EvidenceItem(
                    machine.id,
                    "SCHNEIDER_STATE_MACHINE_V5",
                    (
                        f"Control Expert ST CASE {machine.section}: {machine.state_tag} "
                        f"has {len(machine.states)} state(s), {len(machine.transitions)} transition(s), "
                        f"{machine.semantic_state.value} ({machine.reason})."
                    ),
                    f"{machine.relative_path}:{machine.section}:{machine.case_line}-{machine.end_line}",
                    project.metadata.source_sha256,
                    {
                        "section": machine.section,
                        "state_tag": machine.state_tag,
                        "state_type": machine.state_type,
                        "states": list(machine.states),
                        "semantic_state": machine.semantic_state.value,
                        "reason": machine.reason,
                        "runtime_dependencies": list(machine.runtime_dependencies),
                        "dangling_targets": list(machine.dangling_targets),
                        "overlap_conflicts": list(machine.overlap_conflicts),
                        "terminal_states": list(machine.terminal_states),
                        "entry_candidates": list(machine.entry_candidates),
                        "writer_conflicts": list(machine.writer_conflicts),
                        "has_default_branch": machine.has_default_branch,
                    },
                )
            )
        for transition in machine.transitions:
            if transition.id in existing:
                continue
            items.append(
                EvidenceItem(
                    transition.id,
                    "SCHNEIDER_STATE_TRANSITION_V5",
                    (
                        f"{machine.state_tag}: {transition.source_state} -> {transition.target_state} "
                        f"when {transition.guard_text}."
                    ),
                    f"{machine.relative_path}:{machine.section}:{transition.source_line}",
                    project.metadata.source_sha256,
                    {
                        "machine_id": machine.id,
                        "section": machine.section,
                        "state_tag": machine.state_tag,
                        "source_state": transition.source_state,
                        "target_state": transition.target_state,
                        "guard_text": transition.guard_text,
                        "guard_paths": [
                            [{"tag": name, "required": value} for name, value in path]
                            for path in transition.guard_paths
                        ],
                        "runtime_dependencies": list(transition.runtime_dependencies),
                        "semantic_state": transition.semantic_state.value,
                    },
                )
            )
    return items


def _v5_risks(previous, engineering, verifications, executions, engineering_findings):
    risks = list(previous(engineering, verifications, executions, engineering_findings))
    facts = _facts(engineering.project)
    if facts is None:
        return risks

    for machine in facts.machines:
        if machine.semantic_state is not PLCSemanticState.FULL:
            risks.append(
                RiskFinding(
                    stable_id("RISK", "SCHNEIDER_STATE_COVERAGE_V5", machine.id, machine.reason),
                    "SEMANTIC_COVERAGE",
                    f"Schneider sequence {machine.state_tag} remains outside complete V5 proof",
                    Severity.HIGH,
                    f"{machine.section} is {machine.semantic_state.value}: {machine.reason}.",
                    "An incomplete state-transition theorem can hide commissioning behavior that is not safe to promote from traceability to verification.",
                    "Resolve the source-linked V5 gap and execute the generated sequence FAT before release readiness.",
                    (machine.id,),
                )
            )
        if machine.overlap_conflicts:
            risks.append(
                RiskFinding(
                    stable_id("RISK", "SCHNEIDER_STATE_OVERLAP_V5", machine.id),
                    "SEQUENCE_AMBIGUITY",
                    f"Schneider state machine {machine.state_tag} has overlapping transition guards",
                    Severity.HIGH,
                    f"Different target transitions may be enabled together: {', '.join(machine.overlap_conflicts)}.",
                    "Final state can depend on statement order instead of an explicit mutually-exclusive transition contract.",
                    "Make transition priority explicit or guards mutually exclusive, then execute transition FAT.",
                    (machine.id,),
                )
            )
        if machine.dangling_targets:
            risks.append(
                RiskFinding(
                    stable_id("RISK", "SCHNEIDER_STATE_DANGLING_V5", machine.id),
                    "SEQUENCE_GAP",
                    f"Schneider state machine {machine.state_tag} targets undefined CASE states",
                    Severity.HIGH,
                    f"Undefined target(s): {', '.join(machine.dangling_targets)}.",
                    "A transition can move the state tag to a value without a modeled CASE branch.",
                    "Add/confirm the missing state branch or correct the target, then execute the linked sequence FAT.",
                    (machine.id,),
                )
            )
        if machine.writer_conflicts:
            risks.append(
                RiskFinding(
                    stable_id("RISK", "SCHNEIDER_STATE_WRITER_V5", machine.id, *machine.writer_conflicts),
                    "MULTIPLE_WRITERS",
                    f"Competing writers block Schneider state-machine proof for {machine.state_tag}",
                    Severity.HIGH,
                    f"Competing writer evidence: {', '.join(machine.writer_conflicts[:8])}.",
                    "Final state can depend on section/task order or an unmodeled source writer.",
                    "Establish unique state ownership or explicit arbitration, then rerun sequence analysis and FAT.",
                    (machine.id, *machine.writer_conflicts),
                )
            )
        if machine.terminal_states:
            risks.append(
                RiskFinding(
                    stable_id("RISK", "SCHNEIDER_STATE_TERMINAL_V5", machine.id, *machine.terminal_states),
                    "SEQUENCE_DEAD_END",
                    f"Schneider state machine {machine.state_tag} contains terminal state(s)",
                    Severity.MEDIUM,
                    f"No outgoing transition is modeled for: {', '.join(machine.terminal_states)}.",
                    "A terminal state may be intentional, but an unintended dead-end can halt automatic operation.",
                    "Confirm each terminal state is intentional and execute recovery/restart FAT from that state.",
                    (machine.id,),
                )
            )
        if machine.entry_candidates:
            risks.append(
                RiskFinding(
                    stable_id("RISK", "SCHNEIDER_STATE_ENTRY_V5", machine.id, *machine.entry_candidates),
                    "SEQUENCE_ENTRY",
                    f"Schneider state machine {machine.state_tag} has state(s) with no incoming transition",
                    Severity.MEDIUM,
                    f"Potential startup/entry state(s): {', '.join(machine.entry_candidates)}.",
                    "Static source alone does not prove which state is established after cold/warm restart or retained-memory recovery.",
                    "Verify startup/reset/retentivity behavior with the linked engineer FAT procedure.",
                    (machine.id,),
                )
            )
        if machine.runtime_dependencies:
            risks.append(
                RiskFinding(
                    stable_id("RISK", "SCHNEIDER_STATE_RUNTIME_V5", machine.id, *machine.runtime_dependencies),
                    "STATEFUL_LOGIC",
                    f"Schneider state machine {machine.state_tag} depends on timer/counter runtime state",
                    Severity.MEDIUM,
                    f"Runtime dependency binding(s): {', '.join(machine.runtime_dependencies)}.",
                    "The transition relation is source-linked while timing/counting evolution remains dependent on scans and runtime state.",
                    "Execute nominal, boundary, timeout/count, reset, and restart sequence FAT.",
                    (machine.id,),
                )
            )
    return risks


def _v5_render(previous, project) -> str:
    base = previous(project)
    facts = _facts(project)
    if facts is None:
        return base
    profile = schneider_capability_profile_v5(project)
    insertion = (
        "### Schneider V5 Sequencing / State Machines\n\n"
        f"- Bounded ST CASE state machines: **{profile['state_machines']}**\n"
        f"- Fully modeled machines: **{profile['state_machine_full']}**\n"
        f"- PARTIAL machines: **{profile['state_machine_partial']}**\n"
        f"- Explicit states: **{profile['state_machine_states']}**\n"
        f"- Explicit transitions: **{profile['state_machine_transitions']}**\n"
        f"- Timer/counter runtime dependencies: **{profile['state_machine_runtime_dependencies']}**\n"
        f"- Dangling transition targets: **{profile['state_machine_dangling_targets']}**\n"
        f"- Overlapping different-target guards: **{profile['state_machine_overlap_conflicts']}**\n"
        f"- State writer conflicts: **{profile['state_machine_writer_conflicts']}**\n"
        f"- Terminal/dead-end candidates: **{profile['state_machine_terminal_states']}**\n"
        f"- Startup/entry candidates: **{profile['state_machine_entry_candidates']}**\n"
        "- V5 statically models only a bounded source transition relation for one exported integer state tag with simple CASE labels/targets and direct or Boolean IF/ELSIF/ELSE guarded state writes.\n"
        "- CASE ELSE behavior, nested controls, loops, unsupported calls, complex state expressions, ambiguous writers, unsupported guard types, and statement-order-dependent transition arms remain fail-closed.\n"
        "- Startup/retained state, scan order, timer/counter evolution, I/O timing, process physics, Control Expert Simulator, HIL, and real Modicon execution require engineer runtime evidence.\n\n"
    )
    marker = "### Schneider V4 LD/FBD Boolean Theorem"
    if marker in base:
        return base.replace(marker, insertion + marker, 1)
    return base + "\n\n" + insertion.rstrip() + "\n"


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import schneider_integration_v1 as _integration
    from devagent.plc import schneider_report_install_v1 as _report

    previous_verify = _integration._verify_requirement
    previous_evidence = _integration._evidence_index
    previous_risks = _integration._detect_risks
    previous_render = _report._render

    _v1.analyze_schneider_control_expert = analyze_schneider_control_expert_v5
    _v1.schneider_capability_profile = schneider_capability_profile_v5
    _dispatch.analyze_schneider_control_expert = analyze_schneider_control_expert_v5
    _integration.schneider_capability_profile = schneider_capability_profile_v5

    def verify_requirement(requirement, engineering, evidence, tests):
        return _v5_verify_requirement(previous_verify, requirement, engineering, evidence, tests)

    def evidence_index(engineering):
        return _v5_evidence(previous_evidence, engineering)

    def detect_risks(engineering, verifications, executions, engineering_findings):
        return _v5_risks(previous_risks, engineering, verifications, executions, engineering_findings)

    def render(project):
        return _v5_render(previous_render, project)

    _integration._verify_requirement = verify_requirement
    _integration._evidence_index = evidence_index
    _integration._detect_risks = detect_risks
    _report._render = render
    _INSTALLED = True


__all__ = [
    "SchneiderV5Facts",
    "SchneiderV5StateMachineFact",
    "SchneiderV5TransitionFact",
    "analyze_schneider_control_expert_v5",
    "install",
    "schneider_capability_profile_v5",
]
