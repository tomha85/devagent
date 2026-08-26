from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from devagent.plc.models import (
    CanonicalPLCProject,
    PLCAddOnInstruction,
    PLCBooleanTerm,
    PLCInstruction,
    PLCLogicPath,
    PLCLogicStatement,
    PLCOutputLogic,
    PLCAOIParameter,
    PLCRung,
    PLCSemanticState,
    PLCSourceRef,
)
from devagent.plc.rockwell_l5x import _instruction_semantics, _instructions


_SIMPLE_BOOLEAN = {"XIC", "XIO", "OTE", "OTL", "OTU"}
_PARTIAL_VENDOR_INSTRUCTIONS = {"MAH", "MAJ", "MCPM", "MCS", "MCTO", "MDCC"}
_UNKNOWN_WARNING_PREFIX = "Instruction semantics not modeled for: "
_MAX_BOOLEAN_PATHS = 256
_IDENTIFIER = re.compile(
    r"[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?(?:\.[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?)*"
)
_INSTRUCTION_START = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_ST_ASSIGNMENT = re.compile(
    r"(?P<lhs>[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?(?:\.[A-Za-z_][A-Za-z0-9_:.]*(?:\[[^\]]+\])?)*)\s*:=\s*(?P<rhs>.+)",
    re.IGNORECASE,
)
_ST_IF = re.compile(r"^\s*IF\s+(?P<expr>.+?)\s+THEN\b", re.IGNORECASE)
_ST_ELSIF = re.compile(r"^\s*ELSIF\s+(?P<expr>.+?)\s+THEN\b", re.IGNORECASE)
_ST_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_ST_KEYWORDS = {
    "IF", "THEN", "ELSIF", "ELSE", "END_IF", "TRUE", "FALSE", "AND", "OR", "NOT",
    "XOR", "MOD", "TO", "BY", "DO", "END_FOR", "END_WHILE", "END_REPEAT", "END_CASE",
    "CASE", "OF", "FOR", "WHILE", "REPEAT", "UNTIL", "RETURN", "EXIT",
}
_ST_SAFE_FUNCTIONS = {
    "ABS", "SQRT", "SQR", "MIN", "MAX", "LIMIT", "SIN", "COS", "TAN", "ASIN", "ACOS", "ATAN",
    "LN", "LOG", "EXP", "TRUNC", "ROUND",
}
_ST_UNSUPPORTED_CONTROL = {"CASE", "FOR", "WHILE", "REPEAT", "UNTIL"}


@dataclass(frozen=True)
class _Branch:
    paths: tuple[tuple[object, ...], ...]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local_name(child.tag) == name]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    for child in list(element):
        if _local_name(child.tag) == name:
            return child
    return None


def _text_child(element: ET.Element, name: str) -> str | None:
    found = _child(element, name)
    if found is None:
        return None
    text = "".join(found.itertext()).strip()
    return text or None


def _bool_attr(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _refs(value: str) -> tuple[str, ...]:
    result: list[str] = []
    call_names = {item.upper() for item in _ST_CALL.findall(value)}
    for token in _IDENTIFIER.findall(value):
        upper = token.upper()
        if upper in _ST_KEYWORDS or upper in call_names:
            continue
        if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", token):
            continue
        if token not in result:
            result.append(token)
    return tuple(result)


def _first_ref(value: str) -> str | None:
    refs = _refs(value)
    return refs[0] if refs else None


def _has_variable_subscript(value: str) -> bool:
    for expression in re.findall(r"\[([^\]]+)\]", value):
        if not re.fullmatch(r"[-+]?\d+", expression.strip()):
            return True
    return False


def _scan_neutral_tokens(text: str) -> list[object]:
    tokens: list[object] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace() or char == ";":
            index += 1
            continue
        if char in "[],":
            tokens.append(char)
            index += 1
            continue
        match = _INSTRUCTION_START.match(text, index)
        if match is None:
            raise ValueError(f"unsupported neutral-text token near {text[index:index + 32]!r}")
        opening = match.end() - 1
        depth = 1
        quote: str | None = None
        cursor = opening + 1
        while cursor < len(text) and depth:
            current = text[cursor]
            if quote is not None:
                if current == quote:
                    quote = None
            elif current in {'\"', "'"}:
                quote = current
            elif current == "(":
                depth += 1
            elif current == ")":
                depth -= 1
            cursor += 1
        if depth:
            raise ValueError("unterminated neutral-text instruction")
        parsed = _instructions(text[index:cursor])
        if len(parsed) != 1:
            raise ValueError("unable to isolate neutral-text instruction")
        tokens.append(parsed[0])
        index = cursor
    return tokens


def _parse_sequence(tokens: list[object], index: int = 0, stop: set[str] | None = None):
    stop = stop or set()
    result: list[object] = []
    while index < len(tokens):
        token = tokens[index]
        if isinstance(token, str) and token in stop:
            break
        if token == "[":
            index += 1
            paths: list[tuple[object, ...]] = []
            while True:
                path, index = _parse_sequence(tokens, index, {",", "]"})
                paths.append(tuple(path))
                if index >= len(tokens):
                    raise ValueError("unterminated neutral-text branch")
                delimiter = tokens[index]
                if delimiter == ",":
                    index += 1
                    continue
                if delimiter == "]":
                    index += 1
                    break
                raise ValueError("invalid neutral-text branch delimiter")
            if len(paths) < 2:
                raise ValueError("neutral-text branch has fewer than two paths")
            result.append(_Branch(tuple(paths)))
            continue
        if isinstance(token, str):
            raise ValueError(f"unexpected neutral-text delimiter {token!r}")
        result.append(token)
        index += 1
    return result, index


def _merge_term(state: dict[str, bool], tag: str, required: bool) -> dict[str, bool] | None:
    if tag in state and state[tag] != required:
        return None
    updated = dict(state)
    updated[tag] = required
    return updated


def _state_key(state: dict[str, bool]) -> tuple[tuple[str, bool], ...]:
    return tuple(sorted(state.items()))


def _dedupe_states(states: Iterable[dict[str, bool]]) -> list[dict[str, bool]]:
    result: list[dict[str, bool]] = []
    seen: set[tuple[tuple[str, bool], ...]] = set()
    for state in states:
        key = _state_key(state)
        if key in seen:
            continue
        seen.add(key)
        result.append(state)
        if len(result) > _MAX_BOOLEAN_PATHS:
            raise ValueError("boolean branch path limit exceeded")
    return result


def _execute_boolean_nodes(
    nodes: tuple[object, ...] | list[object],
    incoming: list[dict[str, bool]],
    outputs: dict[tuple[str, str], list[dict[str, bool]]],
) -> list[dict[str, bool]]:
    states = incoming
    for node in nodes:
        if isinstance(node, _Branch):
            branch_states: list[dict[str, bool]] = []
            for path in node.paths:
                branch_states.extend(_execute_boolean_nodes(path, [dict(item) for item in states], outputs))
            states = _dedupe_states(branch_states)
            continue
        if not isinstance(node, PLCInstruction):
            raise ValueError("invalid boolean node")
        name = node.name.upper()
        if name not in _SIMPLE_BOOLEAN:
            raise ValueError(f"instruction {node.name} is not in deterministic boolean branch model")
        if not node.arguments:
            raise ValueError(f"instruction {node.name} has no operand")
        tag = _first_ref(node.arguments[0])
        if tag is None or _has_variable_subscript(node.arguments[0]):
            raise ValueError(f"instruction {node.name} operand is not a fixed tag reference")
        if name in {"XIC", "XIO"}:
            required = name == "XIC"
            next_states: list[dict[str, bool]] = []
            for state in states:
                updated = _merge_term(state, tag, required)
                if updated is not None:
                    next_states.append(updated)
            states = _dedupe_states(next_states)
        else:
            outputs.setdefault((name, tag), []).extend(dict(item) for item in states)
    return states


def derive_rll_output_logic(rung: PLCRung) -> list[PLCOutputLogic]:
    """Normalize deterministic XIC/XIO boolean paths, including nested Rockwell branches."""
    if not rung.instructions or any(ins.name.upper() not in _SIMPLE_BOOLEAN for ins in rung.instructions):
        return []
    try:
        tokens = _scan_neutral_tokens(rung.text)
        nodes, index = _parse_sequence(tokens)
        if index != len(tokens):
            return []
        outputs: dict[tuple[str, str], list[dict[str, bool]]] = {}
        _execute_boolean_nodes(nodes, [{}], outputs)
    except ValueError:
        return []

    normalized: list[PLCOutputLogic] = []
    for (instruction, output), states in sorted(outputs.items()):
        paths: list[PLCLogicPath] = []
        seen: set[tuple[tuple[str, bool], ...]] = set()
        for state in states:
            key = _state_key(state)
            if key in seen:
                continue
            seen.add(key)
            paths.append(
                PLCLogicPath(
                    terms=tuple(PLCBooleanTerm(tag=tag, required=value) for tag, value in key)
                )
            )
        if not paths:
            continue
        digest = hashlib.sha1(f"{rung.id}:{instruction}:{output}".encode("utf-8")).hexdigest()[:12]
        normalized.append(
            PLCOutputLogic(
                id=f"LOGIC-RLL-{digest}",
                output_tag=output,
                instruction=instruction,
                paths=tuple(paths),
                source=rung.source,
                language="RLL",
                origin="RUNG",
            )
        )
    return normalized


def _statement_id(source: PLCSourceRef, text: str, prefix: str) -> str:
    payload = f"{source.locator}\x1f{text}".encode("utf-8")
    return f"{prefix}-{hashlib.sha1(payload).hexdigest()[:16]}"


def _parse_st_lines(
    lines: list[ET.Element],
    *,
    artifact: str,
    controller: str,
    owner_type: str,
    owner_name: str,
    routine_name: str,
    known_aois: set[str],
) -> list[PLCLogicStatement]:
    result: list[PLCLogicStatement] = []
    condition_stack: list[tuple[str, ...] | None] = []
    for ordinal, line in enumerate(lines):
        number = line.attrib.get("Number", str(ordinal))
        text = "".join(line.itertext()).strip()
        if not text:
            continue
        source = PLCSourceRef(
            artifact=artifact,
            controller=controller,
            program=owner_name if owner_type == "program" else None,
            aoi=owner_name if owner_type == "aoi" else None,
            routine=routine_name,
            line=number,
        )
        code = re.sub(r"\(\*.*?\*\)", " ", text, flags=re.DOTALL).strip()
        upper = code.upper()
        reads: set[str] = set()
        writes: set[str] = set()
        calls = {name for name in _ST_CALL.findall(code) if name.upper() not in _ST_SAFE_FUNCTIONS}
        state = PLCSemanticState.FULL

        elsif = _ST_ELSIF.match(code)
        if_match = _ST_IF.match(code)
        if elsif:
            condition = tuple(_refs(elsif.group("expr")))
            if condition_stack:
                condition_stack[-1] = condition
            else:
                condition_stack.append(condition)
            reads.update(condition)
        elif if_match:
            condition = tuple(_refs(if_match.group("expr")))
            condition_stack.append(condition)
            reads.update(condition)
        elif re.match(r"^\s*ELSE\b", code, flags=re.IGNORECASE):
            if condition_stack:
                if condition_stack[-1]:
                    reads.update(condition_stack[-1] or ())
                condition_stack[-1] = None
            state = PLCSemanticState.PARTIAL

        for condition in condition_stack:
            if condition:
                reads.update(condition)
            elif condition is None:
                state = PLCSemanticState.PARTIAL

        assignments = list(_ST_ASSIGNMENT.finditer(code))
        for assignment in assignments:
            writes.add(assignment.group("lhs"))
            reads.update(_refs(assignment.group("rhs")))

        for call in calls:
            if call not in known_aois:
                state = PLCSemanticState.PARTIAL
        if any(re.search(rf"\b{keyword}\b", upper) for keyword in _ST_UNSUPPORTED_CONTROL):
            state = PLCSemanticState.PARTIAL
        if not assignments and not if_match and not elsif and not re.match(r"^\s*(ELSE|END_IF)\b", code, flags=re.IGNORECASE):
            if calls:
                state = PLCSemanticState.PARTIAL
            elif code.strip("; "):
                state = PLCSemanticState.PARTIAL

        result.append(
            PLCLogicStatement(
                id=_statement_id(source, text, "STMT-ST"),
                language="ST",
                owner_type=owner_type,
                owner_name=owner_name,
                routine=routine_name,
                locator=number,
                text=text,
                reads=tuple(sorted(reads)),
                writes=tuple(sorted(writes)),
                calls=tuple(sorted(calls)),
                semantic_state=state,
                source=source,
            )
        )

        if re.search(r"\bEND_IF\b", upper) and condition_stack:
            condition_stack.pop()
    return result


def _aoi_parameters(element: ET.Element) -> tuple[PLCAOIParameter, ...]:
    parameters_element = _child(element, "Parameters")
    if parameters_element is None:
        return ()
    result: list[PLCAOIParameter] = []
    for parameter in _children(parameters_element, "Parameter"):
        name = parameter.attrib.get("Name", "").strip()
        result.append(
            PLCAOIParameter(
                name=name,
                usage=parameter.attrib.get("Usage", "Input"),
                data_type=parameter.attrib.get("DataType"),
                required=_bool_attr(parameter.attrib.get("Required")),
                visible=_bool_attr(parameter.attrib.get("Visible")),
                system_defined=name.casefold() in {"enablein", "enableout"},
            )
        )
    return tuple(result)


def _program_st_statements(root: ET.Element, project: CanonicalPLCProject) -> list[PLCLogicStatement]:
    controller = next((item for item in root.iter() if _local_name(item.tag) == "Controller"), None)
    if controller is None:
        return []
    programs = _child(controller, "Programs")
    if programs is None:
        return []
    result: list[PLCLogicStatement] = []
    known_aois = {aoi.name for aoi in project.aois}
    for program in _children(programs, "Program"):
        program_name = program.attrib.get("Name", "").strip()
        routines = _child(program, "Routines")
        if routines is None:
            continue
        for routine in _children(routines, "Routine"):
            if routine.attrib.get("Type", "").upper() != "ST":
                continue
            st = _child(routine, "STContent")
            if st is None:
                continue
            result.extend(
                _parse_st_lines(
                    _children(st, "Line"),
                    artifact=project.metadata.source_path,
                    controller=project.metadata.controller_name,
                    owner_type="program",
                    owner_name=program_name,
                    routine_name=routine.attrib.get("Name", "").strip(),
                    known_aois=known_aois,
                )
            )
    return result


def _aoi_internal_semantics(root: ET.Element, project: CanonicalPLCProject) -> tuple[list[PLCLogicStatement], list[PLCOutputLogic]]:
    controller = next((item for item in root.iter() if _local_name(item.tag) == "Controller"), None)
    if controller is None:
        return [], []
    definitions = _child(controller, "AddOnInstructionDefinitions")
    if definitions is None:
        return [], []

    all_parameters: dict[str, tuple[PLCAOIParameter, ...]] = {}
    elements: dict[str, ET.Element] = {}
    for element in _children(definitions, "AddOnInstructionDefinition"):
        name = element.attrib.get("Name", "").strip()
        all_parameters[name] = _aoi_parameters(element)
        elements[name] = element

    statements: list[PLCLogicStatement] = []
    output_logic: list[PLCOutputLogic] = []
    updated_aois: list[PLCAddOnInstruction] = []
    known_aois = set(all_parameters)
    project.aoi_internal_total = len(project.aois)

    for original in project.aois:
        element = elements.get(original.name)
        params = all_parameters.get(original.name, original.parameters)
        routine_ids: list[str] = []
        body_states: list[PLCSemanticState] = []
        supported_body = not original.source_protected and element is not None
        routines = _child(element, "Routines") if element is not None else None
        if routines is None:
            supported_body = False
        else:
            for routine in _children(routines, "Routine"):
                routine_name = routine.attrib.get("Name", "").strip()
                routine_type = routine.attrib.get("Type", "UNKNOWN").upper()
                routine_id = f"rockwell://{project.metadata.controller_name}/aoi/{original.name}/routine/{routine_name}"
                routine_ids.append(routine_id)
                if routine_type == "RLL":
                    rll = _child(routine, "RLLContent")
                    if rll is None:
                        continue
                    for ordinal, rung_element in enumerate(_children(rll, "Rung")):
                        number = rung_element.attrib.get("Number", str(ordinal))
                        text = _text_child(rung_element, "Text") or ""
                        ins = _instructions(text)
                        reads: set[str] = set()
                        writes: set[str] = set()
                        calls: set[str] = set()
                        refs: set[str] = set()
                        known = True
                        for instruction in ins:
                            r, w, c, ref, semantic = _instruction_semantics(instruction, all_parameters)
                            reads.update(r)
                            writes.update(w)
                            calls.update(c)
                            refs.update(ref)
                            known = known and semantic
                        state = PLCSemanticState.FULL if known else PLCSemanticState.PARTIAL
                        body_states.append(state)
                        source = PLCSourceRef(
                            artifact=project.metadata.source_path,
                            controller=project.metadata.controller_name,
                            aoi=original.name,
                            routine=routine_name,
                            rung=number,
                        )
                        statements.append(
                            PLCLogicStatement(
                                id=_statement_id(source, text, "STMT-AOI-RLL"),
                                language="RLL",
                                owner_type="aoi",
                                owner_name=original.name,
                                routine=routine_name,
                                locator=number,
                                text=text,
                                reads=tuple(sorted(reads)),
                                writes=tuple(sorted(writes)),
                                calls=tuple(sorted(calls)),
                                semantic_state=state,
                                source=source,
                            )
                        )
                        temp = PLCRung(
                            id=f"{routine_id}/rung/{number}",
                            program=f"AOI:{original.name}",
                            routine=routine_name,
                            number=number,
                            text=text,
                            comment=_text_child(rung_element, "Comment"),
                            instructions=ins,
                            reads=tuple(sorted(reads)),
                            writes=tuple(sorted(writes)),
                            calls=tuple(sorted(calls)),
                            references=tuple(sorted(refs)),
                            source=source,
                        )
                        for logic in derive_rll_output_logic(temp):
                            output_logic.append(replace(logic, origin=f"AOI_INTERNAL:{original.name}"))
                elif routine_type == "ST":
                    st = _child(routine, "STContent")
                    if st is None:
                        supported_body = False
                        continue
                    parsed = _parse_st_lines(
                        _children(st, "Line"),
                        artifact=project.metadata.source_path,
                        controller=project.metadata.controller_name,
                        owner_type="aoi",
                        owner_name=original.name,
                        routine_name=routine_name,
                        known_aois=known_aois,
                    )
                    statements.extend(parsed)
                    body_states.extend(item.semantic_state for item in parsed)
                else:
                    supported_body = False
        if any(state is not PLCSemanticState.FULL for state in body_states):
            supported_body = False
        modeled = bool(routine_ids) and supported_body
        if modeled:
            project.aoi_internal_modeled_count += 1
        updated_aois.append(
            replace(
                original,
                parameters=params,
                routine_ids=tuple(routine_ids),
                internal_body_modeled=modeled,
            )
        )
    project.aois = updated_aois
    return statements, output_logic


def _resolve_tag(project: CanonicalPLCProject, rung: PLCRung, name: str):
    root = name.split(".", 1)[0].split("[", 1)[0]
    candidates = [tag for tag in project.tags if tag.name.casefold() == root.casefold()]
    for tag in candidates:
        if tag.scope.casefold() == f"program:{rung.program}".casefold():
            return tag
    for tag in candidates:
        if tag.scope == "controller":
            return tag
    return candidates[0] if len(candidates) == 1 else None


def _aoi_call_bindings(project: CanonicalPLCProject, internal_logic: list[PLCOutputLogic]) -> None:
    aois = {aoi.name: aoi for aoi in project.aois}
    internal_by_aoi: dict[str, list[PLCOutputLogic]] = {}
    for logic in internal_logic:
        if logic.source.aoi:
            internal_by_aoi.setdefault(logic.source.aoi, []).append(logic)

    updated_rungs: list[PLCRung] = []
    translated_logic: list[PLCOutputLogic] = []
    project.aoi_call_total = 0
    project.aoi_call_bound_count = 0
    for rung in project.rungs:
        reads = set(rung.reads)
        writes = set(rung.writes)
        references = set(rung.references)
        for instruction in rung.instructions:
            aoi = aois.get(instruction.name)
            if aoi is None or not instruction.arguments:
                continue
            project.aoi_call_total += 1
            backing = _first_ref(instruction.arguments[0])
            backing_tag = _resolve_tag(project, rung, backing) if backing else None
            if backing_tag is None or backing_tag.data_type.casefold() != aoi.name.casefold():
                for argument in instruction.arguments:
                    references.update(_refs(argument))
                continue
            reads.add(backing)
            writes.add(backing)
            references.add(backing)

            user_parameters = [param for param in aoi.parameters if not param.system_defined]
            call_args = list(instruction.arguments[1:])
            bindable = user_parameters
            if len(call_args) != len(bindable):
                visible_required = [param for param in user_parameters if param.required or param.visible]
                if len(call_args) == len(visible_required):
                    bindable = visible_required
                else:
                    for argument in call_args:
                        references.update(_refs(argument))
                    continue
            mapping: dict[str, str] = {}
            for parameter, argument in zip(bindable, call_args):
                ref = _first_ref(argument)
                if ref is None:
                    continue
                mapping[parameter.name] = ref
                usage = parameter.usage.casefold()
                if usage in {"input", "inout"}:
                    reads.add(ref)
                if usage in {"output", "inout"}:
                    writes.add(ref)
                references.add(ref)

            project.aoi_call_bound_count += 1
            if not aoi.internal_body_modeled:
                continue
            for internal in internal_by_aoi.get(aoi.name, []):
                external_output = mapping.get(internal.output_tag)
                if external_output is None:
                    continue
                translated_paths: list[PLCLogicPath] = []
                safe = True
                for path in internal.paths:
                    translated_terms: list[PLCBooleanTerm] = []
                    for term in path.terms:
                        external = mapping.get(term.tag)
                        if external is None:
                            safe = False
                            break
                        translated_terms.append(PLCBooleanTerm(tag=external, required=term.required))
                    if not safe:
                        break
                    translated_paths.append(PLCLogicPath(terms=tuple(translated_terms)))
                if not safe or not translated_paths:
                    continue
                digest = hashlib.sha1(
                    f"{rung.id}:{aoi.name}:{internal.id}:{external_output}".encode("utf-8")
                ).hexdigest()[:12]
                translated_logic.append(
                    PLCOutputLogic(
                        id=f"LOGIC-AOI-CALL-{digest}",
                        output_tag=external_output,
                        instruction=internal.instruction,
                        paths=tuple(translated_paths),
                        source=rung.source,
                        language="RLL",
                        origin=f"AOI_CALL:{aoi.name}",
                    )
                )
        updated_rungs.append(
            replace(
                rung,
                reads=tuple(sorted(reads)),
                writes=tuple(sorted(writes)),
                references=tuple(sorted(references | reads | writes)),
            )
        )
    project.rungs = updated_rungs
    project.output_logic.extend(translated_logic)


def _recognize_partial_vendor_instructions(project: CanonicalPLCProject) -> None:
    partial = sorted(
        name for name in project.unknown_instruction_names if name.upper() in _PARTIAL_VENDOR_INSTRUCTIONS
    )
    if not partial:
        return
    partial_folded = {name.casefold() for name in partial}
    project.partially_modeled_instruction_names = sorted(
        set(project.partially_modeled_instruction_names) | set(partial), key=str.casefold
    )
    project.unknown_instruction_names = [
        name for name in project.unknown_instruction_names if name.casefold() not in partial_folded
    ]
    retained = [warning for warning in project.warnings if not warning.startswith(_UNKNOWN_WARNING_PREFIX)]
    if project.unknown_instruction_names:
        retained.append(f"{_UNKNOWN_WARNING_PREFIX}{', '.join(project.unknown_instruction_names)}")
    retained.append(
        "Recognized but directionally partial Rockwell motion instructions: " + ", ".join(partial)
    )
    project.warnings = retained


def apply_v2_semantics(project: CanonicalPLCProject) -> CanonicalPLCProject:
    """Augment the V1 canonical project with bounded V2 branch, AOI, and ST semantics."""
    payload = Path(project.metadata.source_path).read_bytes()
    root = ET.fromstring(payload)

    _recognize_partial_vendor_instructions(project)
    project.warnings = [
        warning
        for warning in project.warnings
        if not warning.startswith("PLC V1 dependency semantics cover RLL routines only;")
    ]
    unsupported_routine_types = sorted(
        {routine.routine_type for routine in project.routines if routine.routine_type not in {"RLL", "ST"}}
    )
    if unsupported_routine_types:
        project.warnings.append(
            "PLC V2 deterministic semantics do not yet cover routine types: "
            + ", ".join(unsupported_routine_types)
        )

    branch_total = 0
    branch_modeled = 0
    output_logic: list[PLCOutputLogic] = []
    for rung in project.rungs:
        has_branch = False
        depth = 0
        quote: str | None = None
        for char in rung.text:
            if quote is not None:
                if char == quote:
                    quote = None
                continue
            if char in {'\"', "'"}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")" and depth:
                depth -= 1
            elif char == "[" and depth == 0:
                has_branch = True
                break
        if has_branch:
            branch_total += 1
        derived = derive_rll_output_logic(rung)
        if derived:
            output_logic.extend(derived)
            if has_branch:
                branch_modeled += 1
    project.branch_rung_total = branch_total
    project.branch_rung_semantic_count = branch_modeled
    project.output_logic = output_logic

    program_st = _program_st_statements(root, project)
    aoi_statements, aoi_logic = _aoi_internal_semantics(root, project)
    project.logic_statements = [*program_st, *aoi_statements]
    project.output_logic.extend(aoi_logic)
    project.st_statement_total = sum(1 for item in project.logic_statements if item.language == "ST")
    project.st_statement_semantic_count = sum(
        1
        for item in project.logic_statements
        if item.language == "ST" and item.semantic_state is PLCSemanticState.FULL
    )

    _aoi_call_bindings(project, aoi_logic)
    return project
