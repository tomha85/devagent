from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import re
from collections import defaultdict

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
from devagent.plc.production_models import RiskFinding, Severity
from devagent.plc.production_utils import stable_id
from devagent.plc import siemens_tia_v1 as _v1
from devagent.plc import siemens_scl_control_flow_v2 as _v2
from devagent.plc import siemens_call_graph_v3 as _v3
from devagent.plc import siemens_flgnet_v4 as _v4


_INSTALLED = False
_PREVIOUS_ANALYZER = _v1.analyze_siemens_tia
_PREVIOUS_CAPABILITY = _v1.siemens_capability_profile

_CASE = re.compile(r"^\s*CASE\s+(?P<state>.+?)\s+OF\s*;?\s*$", re.IGNORECASE)
_END_CASE = re.compile(r"^\s*END_CASE\s*;?\s*$", re.IGNORECASE)
_IF = re.compile(r"^\s*IF\s+(?P<expr>.+?)\s+THEN\s*;?\s*$", re.IGNORECASE)
_ELSIF = re.compile(r"^\s*ELSIF\s+(?P<expr>.+?)\s+THEN\s*;?\s*$", re.IGNORECASE)
_ELSE = re.compile(r"^\s*ELSE\s*;?\s*$", re.IGNORECASE)
_END_IF = re.compile(r"^\s*END_IF\s*;?\s*$", re.IGNORECASE)
_UNSUPPORTED_CONTROL = re.compile(
    r"^\s*(CASE|FOR|WHILE|REPEAT|END_FOR|END_WHILE|UNTIL|END_REPEAT)\b",
    re.IGNORECASE,
)
_LABEL = re.compile(
    r'^\s*(?P<label>(?:[-+]?\d+)|(?:\d+#(?:[0-9A-Fa-f_]+))|(?:#?[A-Za-z_][A-Za-z0-9_]*(?:#[A-Za-z_][A-Za-z0-9_]*)?))\s*:\s*$'
)
_SIMPLE_TARGET = re.compile(
    r'^\s*(?P<target>(?:[-+]?\d+)|(?:\d+#(?:[0-9A-Fa-f_]+))|(?:#?[A-Za-z_][A-Za-z0-9_]*(?:#[A-Za-z_][A-Za-z0-9_]*)?))\s*$'
)
_CALL = re.compile(r'^\s*#?(?P<name>"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)\s*\(')
_RUNTIME_FB_TYPES = {"TON", "TOF", "TP", "CTU", "CTD"}
_MAX_MACHINES = 64
_MAX_STATES = 128
_MAX_TRANSITIONS = 512


@dataclass(frozen=True)
class SiemensV5TransitionFact:
    id: str
    machine_id: str
    block: str
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
class SiemensV5StateMachineFact:
    id: str
    block: str
    state_tag: str
    state_type: str
    states: tuple[str, ...]
    transitions: tuple[SiemensV5TransitionFact, ...]
    case_line: int
    end_line: int
    modeled_lines: tuple[int, ...]
    runtime_dependencies: tuple[str, ...]
    dangling_targets: tuple[str, ...]
    overlap_conflicts: tuple[str, ...]
    terminal_states: tuple[str, ...]
    has_default_branch: bool
    semantic_state: PLCSemanticState
    reason: str


@dataclass(frozen=True)
class SiemensV5StateMachineFacts:
    machines: tuple[SiemensV5StateMachineFact, ...]


class _Unsupported(ValueError):
    pass


def _line_number(statement) -> int | None:
    raw = statement.source.line
    if raw is None:
        return None
    try:
        return int(str(raw))
    except ValueError:
        return None


def _normalize_ref(value: str) -> str | None:
    raw = str(value or "").strip()
    if raw.startswith("#"):
        raw = raw[1:]
    if not raw or any(token in raw for token in ("[", "]", "%", "(", ")", ":=", ";")):
        return None
    refs = _v1._extract_refs(raw)
    if len(refs) != 1:
        return None
    return refs[0]


def _normalize_state_value(value: str) -> str | None:
    match = _SIMPLE_TARGET.match(str(value or ""))
    if match is None:
        return None
    target = match.group("target").strip()
    if target.startswith("#"):
        target = target[1:]
    return target.upper() if "#" in target and not target[0].isdigit() else target


def _state_type(project, block: str, state_tag: str) -> str | None:
    dtype = _v3._symbol_type(project, block, state_tag)
    if dtype is None:
        return None
    normalized = _v3._type_name(dtype).upper()
    if normalized in {"BOOL", "BOOLEAN", "REAL", "LREAL", "TIME", "LTIME"}:
        return None
    if any(token in normalized for token in ("ARRAY", "STRUCT", "STRING", "WSTRING")):
        return None
    return normalized or None


def _block_exec_lines(block) -> list[tuple[int, str]]:
    begin = None
    for index, raw in enumerate(block.lines):
        if re.match(r"^\s*BEGIN\b", raw, flags=re.IGNORECASE):
            begin = index + 1
            break
    if begin is None:
        return []
    result = []
    for offset in range(begin, len(block.lines) - 1):
        text = block.lines[offset].strip()
        if text:
            result.append((block.start_line + offset, text))
    return result


def _guard_paths(expr: str):
    ast = _v1._parse_bool_ast(expr)
    if ast is None:
        raise _Unsupported("unsupported_boolean_guard")
    paths = _v1._dnf(ast)
    if paths is None:
        raise _Unsupported("guard_complexity_limit")
    return ast, tuple(
        tuple(sorted(path.items(), key=lambda item: item[0].casefold()))
        for path in paths
    )


def _exclusive_paths(guards: list[object], index: int, *, is_else: bool):
    paths = _v2._exclusive_guard(guards, index, is_else=is_else)
    if paths is None:
        raise _Unsupported("exclusive_guard_complexity_limit")
    return tuple(
        tuple(sorted(path.items(), key=lambda item: item[0].casefold()))
        for path in paths
    )


def _paths_overlap(left, right) -> bool:
    if not left or not right:
        return False
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


def _timer_call(project, block: str, text: str) -> str | None:
    match = _CALL.match(text)
    if match is None:
        return None
    name = _v1._clean_name(match.group("name"))
    dtype = _v3._symbol_type(project, block, name)
    if dtype is None:
        raise _Unsupported(f"unresolved_call:{name}")
    normalized = _v3._type_name(dtype).upper()
    if normalized not in _RUNTIME_FB_TYPES:
        raise _Unsupported(f"unsupported_state_machine_call:{name}:{normalized}")
    return f"{name}:{normalized}"


def _state_assignment(text: str, state_tag: str):
    match = _v1._ASSIGNMENT.match(text)
    if match is None:
        return None
    lhs = _normalize_ref(match.group("lhs"))
    if lhs is None or lhs.casefold() != state_tag.casefold():
        return None
    target = _normalize_state_value(match.group("rhs").strip())
    if target is None:
        raise _Unsupported("state_target_must_be_simple_literal")
    return target


def _collect_if(lines: list[tuple[int, str]], start: int, state_tag: str):
    first_line, first_text = lines[start]
    first = _IF.match(first_text)
    if first is None:
        return None

    arms: list[dict[str, object]] = [{"guard": first.group("expr"), "lines": [], "control_line": first_line}]
    control_lines = {first_line}
    cursor = start + 1
    nested_depth = 0
    saw_else = False

    while cursor < len(lines):
        line, text = lines[cursor]
        if _IF.match(text):
            nested_depth += 1
            arms[-1]["lines"].append((line, text))
            cursor += 1
            continue
        if _END_IF.match(text):
            if nested_depth:
                nested_depth -= 1
                arms[-1]["lines"].append((line, text))
                cursor += 1
                continue
            control_lines.add(line)
            break
        if nested_depth:
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

    guards = []
    for arm in arms:
        if arm["guard"] is None:
            continue
        ast, _paths = _guard_paths(str(arm["guard"]))
        guards.append(ast)

    transitions = []
    modeled = set(control_lines)
    for index, arm in enumerate(arms):
        assignments = []
        unsupported_state_writes = []
        for line, text in arm["lines"]:
            assignment = _state_assignment(text, state_tag)
            if assignment is not None:
                assignments.append((line, assignment))
                continue
            raw_assignment = _v1._ASSIGNMENT.match(text)
            if raw_assignment is not None:
                lhs = _normalize_ref(raw_assignment.group("lhs"))
                if lhs and lhs.casefold() == state_tag.casefold():
                    unsupported_state_writes.append(line)
                continue
            if _CALL.match(text):
                raise _Unsupported("call_inside_transition_arm")
            if _IF.match(text) or _END_IF.match(text):
                raise _Unsupported("nested_if_in_transition")
        if unsupported_state_writes:
            raise _Unsupported("unsupported_state_write")
        if len(assignments) > 1:
            raise _Unsupported("multiple_state_writes_in_if_arm")
        if not assignments:
            continue

        is_else = arm["guard"] is None
        guard_index = len([a for a in arms[:index] if a["guard"] is not None])
        if is_else:
            paths = _exclusive_paths(guards, len(guards), is_else=True)
            guard_text = "ELSE"
        else:
            paths = _exclusive_paths(guards, guard_index, is_else=False)
            guard_text = str(arm["guard"])
        transitions.append(
            (
                assignments[0][0],
                assignments[0][1],
                paths,
                guard_text,
                tuple(sorted(set((*control_lines, assignments[0][0])))),
            )
        )
        modeled.add(assignments[0][0])

    return cursor, transitions, modeled


def _parse_machine(project, block, lines: list[tuple[int, str]], start: int, runtime_by_line: dict[int, str]):
    case_line, case_text = lines[start]
    match = _CASE.match(case_text)
    if match is None:
        return None
    state_tag = _normalize_ref(match.group("state"))
    if state_tag is None:
        raise _Unsupported("case_state_reference_required")
    state_type = _state_type(project, block.name, state_tag)
    if state_type is None:
        raise _Unsupported("case_state_type_unsupported")

    cursor = start + 1
    states = []
    branches: dict[str, list[tuple[int, str]]] = {}
    branch_order: list[str] = []
    current: str | None = None
    default_lines: list[tuple[int, str]] = []
    default_mode = False
    end_line = None
    if_depth = 0

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
                raise _Unsupported("label_after_case_else")
            value = _normalize_state_value(label.group("label"))
            if value is None:
                raise _Unsupported("invalid_state_label")
            if any(value.casefold() == existing.casefold() for existing in states):
                raise _Unsupported(f"duplicate_state_label:{value}")
            states.append(value)
            branch_order.append(value)
            branches[value] = []
            current = value
            cursor += 1
            continue
        if current is None:
            raise _Unsupported(f"case_content_before_label:{line}")
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

    transitions: list[SiemensV5TransitionFact] = []
    modeled_lines = {case_line, end_line}
    runtime_deps = set()
    reasons = []
    complete = True

    state_label_lines: dict[str, int] = {}
    for idx in range(start + 1, cursor):
        line, text = lines[idx]
        label = _LABEL.match(text)
        if label:
            value = _normalize_state_value(label.group("label"))
            if value is not None:
                state_label_lines[value.casefold()] = line
                modeled_lines.add(line)

    for source_state in branch_order:
        body = branches[source_state]
        pos = 0
        while pos < len(body):
            line, text = body[pos]
            if _IF.match(text):
                try:
                    end_index, raw_transitions, modeled = _collect_if(body, pos, state_tag)
                except _Unsupported as exc:
                    complete = False
                    reasons.append(str(exc))
                    pos += 1
                    continue
                for assign_line, target, paths, guard_text, evidence in raw_transitions:
                    guard_refs = {name for path in paths for name, _ in path}
                    if any(ref.casefold() == state_tag.casefold() for ref in guard_refs):
                        complete = False
                        reasons.append("guard_references_case_state")
                        continue
                    deps = tuple(sorted(
                        {
                            dep
                            for dep_line, dep in runtime_by_line.items()
                            if dep_line < line and dep_line >= state_label_lines.get(source_state.casefold(), case_line)
                            and any(
                                ref.casefold() == dep.split(":", 1)[0].casefold()
                                or ref.casefold().startswith(dep.split(":", 1)[0].casefold() + ".")
                                for ref in guard_refs
                            )
                        },
                        key=str.casefold,
                    ))
                    runtime_deps.update(deps)
                    digest = hashlib.sha1(
                        f"{block.name}:{state_tag}:{source_state}:{target}:{assign_line}:{guard_text}".encode()
                    ).hexdigest()[:14]
                    transitions.append(SiemensV5TransitionFact(
                        id=f"SIEMENS-STATE5-{digest}",
                        machine_id="",
                        block=block.name,
                        state_tag=state_tag,
                        source_state=source_state,
                        target_state=target,
                        guard_paths=paths,
                        source_line=assign_line,
                        evidence_lines=tuple(sorted(set(evidence))),
                        guard_text=guard_text,
                        runtime_dependencies=deps,
                    ))
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
                pos += 1
                continue
            assignment = _state_assignment(text, state_tag)
            if assignment is not None:
                digest = hashlib.sha1(
                    f"{block.name}:{state_tag}:{source_state}:{assignment}:{line}:TRUE".encode()
                ).hexdigest()[:14]
                transitions.append(SiemensV5TransitionFact(
                    id=f"SIEMENS-STATE5-{digest}",
                    machine_id="",
                    block=block.name,
                    state_tag=state_tag,
                    source_state=source_state,
                    target_state=assignment,
                    guard_paths=((),),
                    source_line=line,
                    evidence_lines=(line,),
                    guard_text="TRUE",
                ))
                modeled_lines.add(line)
                pos += 1
                continue
            raw_assignment = _v1._ASSIGNMENT.match(text)
            if raw_assignment is not None:
                lhs = _normalize_ref(raw_assignment.group("lhs"))
                if lhs and lhs.casefold() == state_tag.casefold():
                    complete = False
                    reasons.append("unsupported_state_write")
                pos += 1
                continue
            if _CALL.match(text):
                try:
                    dep = _timer_call(project, block.name, text)
                except _Unsupported as exc:
                    complete = False
                    reasons.append(str(exc))
                    pos += 1
                    continue
                if dep:
                    runtime_by_line[line] = dep
                    runtime_deps.add(dep)
                pos += 1
                continue
            pos += 1

    if len(transitions) > _MAX_TRANSITIONS:
        raise _Unsupported("transition_count_limit")

    machine_digest = hashlib.sha1(
        f"{block.relative_path}:{block.name}:{case_line}:{state_tag}".encode()
    ).hexdigest()[:14]
    machine_id = f"SIEMENS-SM5-{machine_digest}"
    transitions = [replace(item, machine_id=machine_id) for item in transitions]

    defined = {state.casefold(): state for state in states}
    dangling = sorted(
        {
            transition.target_state
            for transition in transitions
            if transition.target_state.casefold() not in defined
        },
        key=str.casefold,
    )

    conflicts = []
    grouped = defaultdict(list)
    for transition in transitions:
        grouped[transition.source_state.casefold()].append(transition)
    for source_key, items in grouped.items():
        for index, left in enumerate(items):
            for right in items[index + 1 :]:
                if left.target_state.casefold() == right.target_state.casefold():
                    continue
                if _paths_overlap(left.guard_paths, right.guard_paths):
                    conflicts.append(
                        f"{defined.get(source_key, left.source_state)}:{left.target_state}|{right.target_state}"
                    )

    outgoing = {state.casefold(): 0 for state in states}
    for transition in transitions:
        if transition.source_state.casefold() in outgoing:
            outgoing[transition.source_state.casefold()] += 1
    terminal = tuple(
        state for state in states if outgoing.get(state.casefold(), 0) == 0
    )

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
    reason = (
        "bounded_case_state_machine"
        if complete
        else ",".join(dict.fromkeys(reasons)) or "state_machine_partial"
    )

    return cursor, SiemensV5StateMachineFact(
        id=machine_id,
        block=block.name,
        state_tag=state_tag,
        state_type=state_type,
        states=tuple(states),
        transitions=tuple(transitions),
        case_line=case_line,
        end_line=end_line,
        modeled_lines=tuple(sorted(modeled_lines)),
        runtime_dependencies=tuple(sorted(runtime_deps, key=str.casefold)),
        dangling_targets=tuple(dangling),
        overlap_conflicts=tuple(sorted(set(conflicts), key=str.casefold)),
        terminal_states=terminal,
        has_default_branch=default_mode,
        semantic_state=semantic,
        reason=reason,
    )


def _discover_machines(path: Path, project):
    machines = []
    try:
        _root, files = _v1._supported_sources(path)
    except Exception:
        return ()
    for source, relative in files:
        if source.suffix.lower() not in {".scl", ".db", ".udt"}:
            continue
        try:
            blocks = _v1._extract_source_blocks(source, relative)
        except Exception:
            continue
        for block in blocks:
            if block.kind not in {"ORGANIZATION_BLOCK", "FUNCTION_BLOCK", "FUNCTION"}:
                continue
            lines = _block_exec_lines(block)
            runtime_by_line: dict[int, str] = {}
            cursor = 0
            while cursor < len(lines):
                if _CASE.match(lines[cursor][1]) is None:
                    cursor += 1
                    continue
                if len(machines) >= _MAX_MACHINES:
                    return tuple(machines)
                try:
                    end_index, machine = _parse_machine(
                        project, block, lines, cursor, runtime_by_line
                    )
                except _Unsupported:
                    cursor += 1
                    continue
                machines.append(machine)
                cursor = end_index + 1
    return tuple(machines)


def _upgrade_statements(project, machines):
    full_lines: dict[tuple[str, int], SiemensV5StateMachineFact] = {}
    transitions_by_line: dict[tuple[str, int], SiemensV5TransitionFact] = {}
    for machine in machines:
        if machine.semantic_state is not PLCSemanticState.FULL:
            continue
        for line in machine.modeled_lines:
            full_lines[(machine.block.casefold(), line)] = machine
        for transition in machine.transitions:
            transitions_by_line[(machine.block.casefold(), transition.source_line)] = transition
    if not full_lines:
        return
    updated = []
    for statement in project.logic_statements:
        line = _line_number(statement)
        block = (statement.source.program or statement.owner_name or "").casefold()
        key = (block, line or -1)
        machine = full_lines.get(key)
        if machine is None or statement.language != "SCL":
            updated.append(statement)
            continue
        reads = statement.reads
        if _CASE.match(statement.text):
            reads = tuple(dict.fromkeys((*reads, machine.state_tag)))
        transition = transitions_by_line.get(key)
        if transition is not None:
            guard_reads = tuple(
                sorted(
                    {name for path in transition.guard_paths for name, _ in path},
                    key=str.casefold,
                )
            )
            reads = tuple(dict.fromkeys((*reads, *guard_reads)))
        updated.append(
            replace(
                statement,
                reads=reads,
                semantic_state=PLCSemanticState.FULL,
            )
        )
    project.logic_statements = updated
    _v4._refresh_counts(project)


def _facts(project):
    return getattr(project, "_siemens_v5_state_machine_facts", None)


def siemens_capability_profile_v5(project):
    profile = dict(_PREVIOUS_CAPABILITY(project))
    facts = _facts(project)
    profile["schema"] = "devagent-siemens-tia-capability-v5"
    if facts is None:
        profile.update({
            "state_machines": 0,
            "state_machine_states": 0,
            "state_machine_transitions": 0,
            "state_machine_runtime_dependencies": 0,
            "state_machine_dangling_targets": 0,
            "state_machine_overlap_conflicts": 0,
            "state_machine_contract": "NONE",
            "bounded_state_machine_contract": (
                "single-level SCL CASE over one scalar state tag with simple literal states "
                "and Boolean-guarded/direct state assignments"
            ),
        })
        return profile
    machines = facts.machines
    partial = [m for m in machines if m.semantic_state is not PLCSemanticState.FULL]
    profile.update({
        "state_machines": len(machines),
        "state_machine_states": sum(len(m.states) for m in machines),
        "state_machine_transitions": sum(len(m.transitions) for m in machines),
        "state_machine_runtime_dependencies": sum(len(m.runtime_dependencies) for m in machines),
        "state_machine_dangling_targets": sum(len(m.dangling_targets) for m in machines),
        "state_machine_overlap_conflicts": sum(len(m.overlap_conflicts) for m in machines),
        "state_machine_terminal_states": sum(len(m.terminal_states) for m in machines),
        "state_machine_contract": (
            "COMPLETE" if machines and not partial else "PARTIAL_FAIL_CLOSED" if machines else "NONE"
        ),
        "bounded_state_machine_contract": (
            "single-level SCL CASE over one scalar state tag with simple literal states "
            "and Boolean-guarded/direct state assignments"
        ),
    })
    return profile


def _state_machine_fat(project, machines):
    tests = []
    statement_by_line = {
        ((s.source.program or s.owner_name or "").casefold(), _line_number(s)): s
        for s in project.logic_statements
        if _line_number(s) is not None
    }
    for machine in machines:
        source_statement = statement_by_line.get((machine.block.casefold(), machine.case_line))
        if source_statement is None:
            continue
        base_source = source_statement.source
        digest = hashlib.sha1(f"{machine.id}:startup".encode()).hexdigest()[:10]
        tests.append(FATTestCase(
            id=f"FAT-SIEMENS-SM5-{digest}",
            title=f"Verify startup/reset state establishment for {machine.state_tag} in {machine.block}",
            source=base_source,
            output_tag=machine.state_tag,
            preconditions={},
            expected=(
                f"Engineer evidence identifies the intended startup/reset value for {machine.state_tag}, "
                "confirms retentivity behavior, and shows the first enabled CASE state is intentional."
            ),
            method="RUNTIME_FAT_REQUIRED",
            scenario="SIEMENS_STATE_STARTUP",
            limitations=(
                "The engineering export does not prove CPU startup value, retained-memory history, or reset sequencing.",
                "DevAgent does not execute PLCSIM, HIL, or a real PLC.",
            ),
            watch_tags=(machine.state_tag,),
        ))
        for transition in machine.transitions:
            tdigest = hashlib.sha1(f"{transition.id}:fat".encode()).hexdigest()[:10]
            refs = tuple(
                sorted(
                    {name for path in transition.guard_paths for name, _ in path},
                    key=str.casefold,
                )
            )
            pre = {}
            if len(transition.guard_paths) == 1:
                pre = dict(transition.guard_paths[0])
            runtime_text = (
                f" Runtime dependency: {', '.join(transition.runtime_dependencies)}."
                if transition.runtime_dependencies
                else ""
            )
            tests.append(FATTestCase(
                id=f"FAT-SIEMENS-SM5-{tdigest}",
                title=(
                    f"Verify state transition {machine.state_tag}: "
                    f"{transition.source_state} -> {transition.target_state}"
                ),
                source=base_source,
                output_tag=machine.state_tag,
                preconditions=pre,
                expected=(
                    f"Starting from {machine.state_tag}={transition.source_state}, the source-linked guard "
                    f"({transition.guard_text}) causes {machine.state_tag} to transition to "
                    f"{transition.target_state}; with the guard not satisfied, no unmodeled transition occurs."
                    + runtime_text
                ),
                method="RUNTIME_FAT_REQUIRED",
                scenario="SIEMENS_STATE_TRANSITION",
                limitations=(
                    "Static analysis proves only the bounded source transition relation and guard binding; "
                    "scan timing, prior retained state, I/O timing, and process physics are runtime evidence.",
                    "DevAgent does not execute PLCSIM, HIL, or a real PLC.",
                ),
                watch_tags=tuple(dict.fromkeys((machine.state_tag, *refs))),
            ))
        if machine.dangling_targets or machine.overlap_conflicts or machine.has_default_branch:
            digest = hashlib.sha1(f"{machine.id}:gap".encode()).hexdigest()[:10]
            tests.append(FATTestCase(
                id=f"FAT-SIEMENS-SM5-{digest}",
                title=f"Resolve fail-closed sequencing gaps for {machine.state_tag} in {machine.block}",
                source=base_source,
                output_tag=machine.state_tag,
                preconditions={},
                expected=(
                    "Engineer review/runtime evidence must resolve every dangling target, overlapping transition "
                    "condition, or unmodeled CASE ELSE behavior before sequence behavior is accepted."
                ),
                method="RUNTIME_FAT_REQUIRED",
                scenario="SIEMENS_STATE_GAP",
                limitations=(
                    f"Dangling targets: {', '.join(machine.dangling_targets) or 'none'}.",
                    f"Overlap conflicts: {', '.join(machine.overlap_conflicts) or 'none'}.",
                ),
                watch_tags=(machine.state_tag,),
            ))
    return enrich_fat_procedures(project, tests)


def _v5_checks(machines):
    if not machines:
        return [
            StaticCheck(
                "SIEMENS_V5_STATE_MACHINE",
                StaticCheckStatus.WARN,
                "No bounded Siemens SCL CASE state machine was recognized; V5 made no state-machine proof claim.",
            )
        ]
    full = sum(m.semantic_state is PLCSemanticState.FULL for m in machines)
    transitions = sum(len(m.transitions) for m in machines)
    dangling = sum(len(m.dangling_targets) for m in machines)
    conflicts = sum(len(m.overlap_conflicts) for m in machines)
    runtime = sum(len(m.runtime_dependencies) for m in machines)
    evidence = tuple(m.id for m in machines)
    return [
        StaticCheck(
            "SIEMENS_V5_STATE_MACHINE",
            StaticCheckStatus.PASS if full == len(machines) else StaticCheckStatus.WARN,
            (
                f"Modeled {full}/{len(machines)} bounded Siemens SCL CASE state machine(s) with "
                f"{transitions} explicit transition(s); unsupported variants remain fail-closed."
            ),
            evidence,
        ),
        StaticCheck(
            "SIEMENS_V5_TRANSITION_DETERMINISM",
            StaticCheckStatus.PASS if not dangling and not conflicts else StaticCheckStatus.NOT_PROVEN,
            f"Transition integrity: dangling targets={dangling}, overlapping different-target guards={conflicts}.",
            evidence,
        ),
        StaticCheck(
            "SIEMENS_V5_SEQUENCE_RUNTIME",
            StaticCheckStatus.NOT_PROVEN,
            (
                f"Sequence execution remains engineer-runtime evidence; {runtime} timer/counter dependency "
                "binding(s) require runtime FAT and no initial/retained state is statically assumed."
            ),
            evidence,
        ),
    ]


def analyze_siemens_tia_v5(path: Path) -> PLCEngineeringResult:
    base = _PREVIOUS_ANALYZER(path)
    project = base.project
    machines = _discover_machines(path, project)
    if not machines:
        return base

    _upgrade_statements(project, machines)
    facts = SiemensV5StateMachineFacts(machines)
    setattr(project, "_siemens_v5_state_machine_facts", facts)
    project.metadata = replace(project.metadata, schema_revision="SIEMENS-TIA-EXPORT-V5")

    graph = build_dependency_graph(project)
    v3facts = _v3._facts(project)
    if v3facts is not None:
        graph = _v3._augment_graph(graph, v3facts)

    fat_tests = list(base.fat_tests)
    full_state_lines = {
        (m.block.casefold(), t.source_line)
        for m in machines
        if m.semantic_state is PLCSemanticState.FULL
        for t in m.transitions
    }
    filtered = []
    for test in fat_tests:
        line = None
        try:
            line = int(str(test.source.line)) if test.source.line is not None else None
        except ValueError:
            pass
        if (
            test.scenario == "SCL_RUNTIME"
            and line is not None
            and ((test.source.program or "").casefold(), line) in full_state_lines
        ):
            continue
        filtered.append(test)
    fat_tests = filtered
    fat_tests.extend(_state_machine_fat(project, machines))
    fat_tests = list({item.id: item for item in fat_tests}.values())

    fresh_base_checks = {
        item.id: item for item in _v1._siemens_checks(project, graph, fat_tests)
    }
    checks = []
    seen_check_ids = set()
    for item in base.static_checks:
        current = fresh_base_checks.get(item.id, item)
        checks.append(current)
        seen_check_ids.add(current.id)
    for item in fresh_base_checks.values():
        if item.id not in seen_check_ids:
            checks.append(item)
            seen_check_ids.add(item.id)
    for item in _v5_checks(machines):
        if item.id in seen_check_ids:
            checks = [current for current in checks if current.id != item.id]
        checks.append(item)
        seen_check_ids.add(item.id)

    profile = siemens_capability_profile_v5(project)
    state_complete = profile["state_machine_contract"] in {"COMPLETE", "NONE"}
    outcome = (
        PLCOutcome.STATICALLY_VERIFIED
        if profile["static_contract"] == "COMPLETE" and state_complete
        else PLCOutcome.PARTIALLY_VERIFIED
    )
    if profile["static_contract"] == "NO_EXECUTABLE_LOGIC":
        outcome = PLCOutcome.BLOCKED

    limitations = []
    for item in base.limitations:
        item = item.replace(
            "Siemens V4 retains the V2 bounded IF/ELSIF/ELSE theorem and adds only its declared LAD/FBD FlgNet Boolean subset; other controls, calls outside V3 closure, visual instructions, GRAPH/STL, and unsupported networks remain fail-closed.",
            "Siemens V5 retains the qualified V2/V3/V4 theorems and additionally models only the declared bounded SCL CASE state-machine subset; loops, nested/complex CASE logic, unsupported calls, GRAPH/STL, and unsupported networks remain fail-closed.",
        )
        limitations.append(item)
    limitations.append(
        "Siemens V5 adds a bounded SCL CASE state-machine theorem for one scalar state tag, explicit simple state labels/targets, direct or Boolean IF/ELSIF/ELSE guarded state writes, and deterministic transition-overlap checks."
    )
    limitations.append(
        "V5 does not assume startup/retained state and does not statically PASS scan timing, process timing, timer/counter evolution, called-block side effects, nested controls, GRAPH sequences, or complex state expressions; those remain fail-closed and/or engineer FAT."
    )
    return PLCEngineeringResult(
        outcome,
        project,
        graph,
        fat_tests,
        checks,
        list(dict.fromkeys(limitations)),
    )


def _semantic_section(previous, project):
    base = previous(project)
    facts = _facts(project)
    if facts is None:
        return base
    profile = siemens_capability_profile_v5(project)
    text = (
        "### Siemens V5 Sequencing / State Machines\n\n"
        f"- Bounded SCL CASE state machines: **{profile['state_machines']}**\n"
        f"- Explicit states: **{profile['state_machine_states']}**\n"
        f"- Explicit transitions: **{profile['state_machine_transitions']}**\n"
        f"- Timer/counter runtime dependencies: **{profile['state_machine_runtime_dependencies']}**\n"
        f"- Dangling targets: **{profile['state_machine_dangling_targets']}**\n"
        f"- Overlapping different-target guards: **{profile['state_machine_overlap_conflicts']}**\n"
        "- Static proof covers only the bounded source transition relation. Startup/retained state, scan timing, process physics, and timer/counter evolution require engineer runtime evidence.\n\n"
    )
    marker = "### Siemens V4 Extended FlgNet Actions / Calls"
    return base.replace(marker, text + marker, 1) if marker in base else base + "\n\n" + text


def _risks(previous, engineering, verifications, executions, engineering_findings):
    result = list(previous(engineering, verifications, executions, engineering_findings))
    facts = _facts(engineering.project)
    if facts is None:
        return result
    for machine in facts.machines:
        if machine.overlap_conflicts:
            result.append(RiskFinding(
                stable_id("RISK", "SIEMENS_STATE_OVERLAP_V5", machine.id),
                "SEQUENCE_AMBIGUITY",
                f"Siemens state machine {machine.state_tag} has overlapping transition guards",
                Severity.HIGH,
                (
                    "Different target transitions can be enabled together from the same source state: "
                    f"{', '.join(machine.overlap_conflicts)}."
                ),
                "Multiple state writes in one scan can make final state depend on statement order instead of an exclusive transition contract.",
                "Make transition priority explicit with a proven IF/ELSIF/ELSE chain or mutually exclusive guards, then execute transition FAT.",
                (machine.id,),
            ))
        if machine.dangling_targets:
            result.append(RiskFinding(
                stable_id("RISK", "SIEMENS_STATE_DANGLING_V5", machine.id),
                "SEQUENCE_GAP",
                f"Siemens state machine {machine.state_tag} targets undefined CASE states",
                Severity.HIGH,
                (
                    "Transition target(s) are not represented by a CASE branch: "
                    f"{', '.join(machine.dangling_targets)}."
                ),
                "A transition can move control into a state for which this bounded CASE theorem has no defined branch behavior.",
                "Add/confirm the missing state branch or correct the target, then execute the linked sequence FAT.",
                (machine.id,),
            ))
        if machine.runtime_dependencies:
            result.append(RiskFinding(
                stable_id("RISK", "SIEMENS_STATE_RUNTIME_V5", machine.id),
                "STATEFUL_LOGIC",
                f"Siemens state machine {machine.state_tag} depends on runtime timer/counter state",
                Severity.MEDIUM,
                f"Runtime dependency binding(s): {', '.join(machine.runtime_dependencies)}.",
                "Transition structure can be statically traced while timing/counting evolution still depends on scan/runtime behavior.",
                "Execute linked FAT across nominal, boundary, reset, restart, and timeout/count scenarios.",
                (machine.id,),
            ))
    return result


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import siemens_integration_v1 as _integration

    previous_section = _integration._siemens_semantic_section
    previous_risks = _integration._siemens_detect_risks

    _v1.analyze_siemens_tia = analyze_siemens_tia_v5
    _v1.siemens_capability_profile = siemens_capability_profile_v5
    _dispatch.analyze_siemens_tia = analyze_siemens_tia_v5
    _integration.siemens_capability_profile = siemens_capability_profile_v5

    def semantic_section(project):
        return _semantic_section(previous_section, project)

    def detect_risks(engineering, verifications, executions, engineering_findings):
        return _risks(previous_risks, engineering, verifications, executions, engineering_findings)

    _integration._siemens_semantic_section = semantic_section
    _integration._siemens_detect_risks = detect_risks
    _INSTALLED = True


__all__ = [
    "SiemensV5StateMachineFact",
    "SiemensV5StateMachineFacts",
    "SiemensV5TransitionFact",
    "analyze_siemens_tia_v5",
    "install",
    "siemens_capability_profile_v5",
]
