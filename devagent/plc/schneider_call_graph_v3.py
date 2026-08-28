from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from devagent.plc.analysis import build_dependency_graph
from devagent.plc.fat_procedure_v12 import enrich_fat_procedures
from devagent.plc.models import (
    FATTestCase,
    PLCBooleanTerm,
    PLCDependencyEdge,
    PLCDependencyGraph,
    PLCEngineeringResult,
    PLCLogicPath,
    PLCOutcome,
    PLCOutputLogic,
    PLCSourceRef,
    PLCSemanticState,
    StaticCheck,
    StaticCheckStatus,
)
from devagent.plc.production_models import (
    EvidenceItem,
    RequirementStatus,
    RequirementVerification,
    RiskFinding,
    Severity,
)
from devagent.plc.production_utils import explicit_bool, stable_id
from devagent.plc import schneider_control_expert_v1 as _v1
from devagent.plc import schneider_st_control_flow_v2 as _v2


_INSTALLED = False
_PREVIOUS_ANALYZER = _v1.analyze_schneider_control_expert
_PREVIOUS_CAPABILITY = _v1.schneider_capability_profile

_CALL = re.compile(
    r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_.]*)\s*\((?P<args>.*)\)\s*;?\s*$",
    re.IGNORECASE,
)
_SIMPLE_REF = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LITERAL_BOOL = re.compile(r"^(TRUE|FALSE)$", re.IGNORECASE)
_CONTROL_OPEN = re.compile(r"^\s*(IF|CASE|FOR|WHILE|REPEAT)\b", re.IGNORECASE)
_CONTROL_CLOSE = re.compile(
    r"^\s*(END_IF|END_CASE|END_FOR|END_WHILE|UNTIL|END_REPEAT)\b",
    re.IGNORECASE,
)
_BOOL_TYPES = {"BOOL", "EBOOL", "BOOLEAN"}
_MAX_CALLS = 4096
_MAX_ARGS = 64
_MAX_PROJECTED_LOGIC = 4096
_MAX_DFB_TYPES = 2048


@dataclass(frozen=True)
class SchneiderDFBParameter:
    name: str
    direction: str
    data_type: str
    has_default: bool = False


@dataclass(frozen=True)
class SchneiderDFBType:
    id: str
    name: str
    parameters: tuple[SchneiderDFBParameter, ...]
    local_symbols: tuple[tuple[str, str], ...]
    language: str
    source_protected: bool
    source: PLCSourceRef
    st_source: str | None = None


@dataclass(frozen=True)
class SchneiderDFBInstance:
    id: str
    owner_kind: str
    owner_name: str
    name: str
    block_type: str
    source: PLCSourceRef


@dataclass(frozen=True)
class SchneiderParameterBinding:
    formal: str
    actual: str
    direction: str
    operator: str
    actual_type: str


@dataclass(frozen=True)
class SchneiderCallBinding:
    id: str
    caller_kind: str
    caller_name: str
    call_symbol: str
    callee_type: str | None
    instance_name: str | None
    bindings: tuple[SchneiderParameterBinding, ...]
    source: PLCSourceRef
    semantic_state: PLCSemanticState
    resolution: str
    statement_id: str | None = None


@dataclass(frozen=True)
class SchneiderDFBLogic:
    id: str
    dfb_type: str
    output_formal: str
    paths: tuple[PLCLogicPath, ...]
    source: PLCSourceRef
    origin: str


@dataclass(frozen=True)
class SchneiderV3Facts:
    dfb_types: tuple[SchneiderDFBType, ...]
    instances: tuple[SchneiderDFBInstance, ...]
    calls: tuple[SchneiderCallBinding, ...]
    local_logic: tuple[SchneiderDFBLogic, ...]
    reachable_dfb_types: tuple[str, ...]
    unreachable_dfb_types: tuple[str, ...]
    active_call_gaps: tuple[str, ...]
    recursive_dfb_types: tuple[str, ...]
    writer_conflicts: tuple[str, ...]
    projected_logic_ids: tuple[str, ...]


def _type_name(value: str | None) -> str:
    return str(value or "UNKNOWN").strip().upper()


def _bool_type(value: str | None) -> bool:
    return _type_name(value) in _BOOL_TYPES


def _types_compatible(formal: str, actual: str) -> bool:
    if _bool_type(formal) and _bool_type(actual):
        return True
    return _type_name(formal) == _type_name(actual)


def _source_ref(project, owner: str, line: str | None, *, relative: str | None = None) -> PLCSourceRef:
    locator = line
    if relative and line:
        locator = f"{relative}:{line}"
    elif relative:
        locator = relative
    return PLCSourceRef(
        project.metadata.source_path,
        project.metadata.controller_name,
        program=owner,
        routine=owner,
        line=locator,
    )


def _comment_stripped(text: str) -> list[str]:
    return _v1._strip_comments(text).splitlines()


def _protected_hint(element: ET.Element) -> bool:
    for node in element.iter():
        local = _v1._local_name(node.tag).casefold()
        if "protect" in local:
            raw = " ".join(str(value) for value in node.attrib.values()).casefold()
            text = (node.text or "").casefold()
            if not raw or any(token in raw or token in text for token in ("true", "yes", "protected", "1")):
                return True
        for key, value in node.attrib.items():
            if "protect" in str(key).casefold() and str(value).casefold() not in {"", "false", "no", "0"}:
                return True
    return False


def _parameter_container_direction(local: str) -> str | None:
    return {
        "inputparameters": "INPUT",
        "outputparameters": "OUTPUT",
        "inoutparameters": "INOUT",
    }.get(local.casefold())


def _has_default(variable: ET.Element) -> bool:
    if any(key.casefold() in {"initialvalue", "value", "defaultvalue"} for key in variable.attrib):
        return True
    return any(
        _v1._local_name(child.tag).casefold() in {"initialvalue", "value", "defaultvalue"}
        for child in variable
    )


def _dfb_parameters(fb_source: ET.Element) -> tuple[SchneiderDFBParameter, ...]:
    result: list[SchneiderDFBParameter] = []
    seen: set[str] = set()
    for container in list(fb_source):
        direction = _parameter_container_direction(_v1._local_name(container.tag))
        if direction is None:
            continue
        for variable in container.iter():
            if _v1._local_name(variable.tag) != "variables":
                continue
            name = (variable.attrib.get("name") or "").strip()
            if not name or name.casefold() in seen:
                continue
            seen.add(name.casefold())
            result.append(
                SchneiderDFBParameter(
                    name=name,
                    direction=direction,
                    data_type=_type_name(variable.attrib.get("typeName") or variable.attrib.get("type")),
                    has_default=_has_default(variable),
                )
            )
    return tuple(result)


def _dfb_locals(fb_source: ET.Element) -> tuple[tuple[str, str], ...]:
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for container in list(fb_source):
        if _v1._local_name(container.tag).casefold() not in {
            "publiclocalvariables",
            "privatelocalvariables",
            "localvariables",
        }:
            continue
        for variable in container.iter():
            if _v1._local_name(variable.tag) != "variables":
                continue
            name = (variable.attrib.get("name") or "").strip()
            dtype = _type_name(variable.attrib.get("typeName") or variable.attrib.get("type"))
            if name and name.casefold() not in seen:
                seen.add(name.casefold())
                result.append((name, dtype))
    return tuple(result)


def _dfb_body(fb_source: ET.Element) -> tuple[str, str | None]:
    programs = [item for item in fb_source.iter() if _v1._local_name(item.tag) == "FBProgram"]
    if len(programs) != 1:
        return "UNKNOWN", None
    program = programs[0]
    sources = [
        item
        for item in program.iter()
        if _v1._local_name(item.tag) in {"STSource", "LDSource", "FBDSource", "SFCSource", "ILSource"}
    ]
    if len(sources) != 1:
        return "UNKNOWN", None
    source = sources[0]
    language = {
        "STSource": "ST",
        "LDSource": "LD",
        "FBDSource": "FBD",
        "SFCSource": "SFC",
        "ILSource": "IL",
    }[_v1._local_name(source.tag)]
    return language, "".join(source.itertext()) if language == "ST" else None


def _xml_roots(path: Path):
    _root, files, _total = _v1._preflight_sources(path)
    for source, relative in files:
        try:
            root = ET.parse(source).getroot()
        except ET.ParseError:
            # The qualified V1/V2 analyzer owns malformed-input rejection.
            continue
        yield source, relative, root


def _inventory_dfb_types(path: Path, project) -> tuple[SchneiderDFBType, ...]:
    candidates: list[SchneiderDFBType] = []
    for _source, relative, root in _xml_roots(path):
        for fb_source in (item for item in root.iter() if _v1._local_name(item.tag) == "FBSource"):
            name = (fb_source.attrib.get("nameOfFBType") or fb_source.attrib.get("name") or "").strip()
            if not name:
                continue
            language, st_source = _dfb_body(fb_source)
            protected = _protected_hint(fb_source) or st_source is None
            source_ref = _source_ref(project, name, "DFB", relative=relative)
            candidates.append(
                SchneiderDFBType(
                    id=f"SCHNEIDER-DFB-TYPE:{relative}:{name}",
                    name=name,
                    parameters=_dfb_parameters(fb_source),
                    local_symbols=_dfb_locals(fb_source),
                    language=language,
                    source_protected=protected,
                    source=source_ref,
                    st_source=st_source,
                )
            )
            if len(candidates) >= _MAX_DFB_TYPES:
                return tuple(candidates)
    return tuple(candidates)


def _global_symbols(path: Path) -> dict[str, tuple[str, str]]:
    values: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for _source, relative, root in _xml_roots(path):
        for data_block in (item for item in root.iter() if _v1._local_name(item.tag) == "dataBlock"):
            for variable in data_block.iter():
                if _v1._local_name(variable.tag) != "variables":
                    continue
                name = (variable.attrib.get("name") or "").strip()
                dtype = _type_name(variable.attrib.get("typeName") or variable.attrib.get("type"))
                if name:
                    values[name.casefold()].add((name, dtype))
    result: dict[str, tuple[str, str]] = {}
    for folded, items in values.items():
        types = {dtype for _name, dtype in items}
        if len(types) == 1:
            label = sorted(name for name, _dtype in items)[0]
            result[folded] = (label, next(iter(types)))
    return result


def _instances(
    path: Path,
    project,
    dfb_types: tuple[SchneiderDFBType, ...],
) -> tuple[SchneiderDFBInstance, ...]:
    type_names: dict[str, list[str]] = defaultdict(list)
    for block in dfb_types:
        type_names[block.name.casefold()].append(block.name)

    result: list[SchneiderDFBInstance] = []
    for _source, relative, root in _xml_roots(path):
        for data_block in (item for item in root.iter() if _v1._local_name(item.tag) == "dataBlock"):
            for variable in data_block.iter():
                if _v1._local_name(variable.tag) != "variables":
                    continue
                name = (variable.attrib.get("name") or "").strip()
                dtype = _type_name(variable.attrib.get("typeName") or variable.attrib.get("type"))
                candidates = type_names.get(dtype.casefold(), [])
                if name and len(candidates) == 1:
                    result.append(
                        SchneiderDFBInstance(
                            id=f"SCHNEIDER-DFB-INSTANCE:GLOBAL:{relative}:{name}",
                            owner_kind="GLOBAL",
                            owner_name="GLOBAL",
                            name=name,
                            block_type=candidates[0],
                            source=_source_ref(project, project.metadata.controller_name, f"instance {name}", relative=relative),
                        )
                    )

    for block in dfb_types:
        for name, dtype in block.local_symbols:
            candidates = type_names.get(dtype.casefold(), [])
            if len(candidates) == 1:
                result.append(
                    SchneiderDFBInstance(
                        id=f"SCHNEIDER-DFB-INSTANCE:{block.name}:{name}",
                        owner_kind="DFB",
                        owner_name=block.name,
                        name=name,
                        block_type=candidates[0],
                        source=block.source,
                    )
                )
    return tuple(result)


def _dedupe_dnf(paths: list[dict[str, bool]]) -> tuple[PLCLogicPath, ...] | None:
    unique: list[PLCLogicPath] = []
    seen: set[tuple[tuple[str, bool], ...]] = set()
    for path in paths:
        if len(path) > 16:
            return None
        key = tuple(sorted(((name.casefold(), bool(value)) for name, value in path.items())))
        if key in seen:
            continue
        seen.add(key)
        unique.append(
            PLCLogicPath(
                tuple(
                    PLCBooleanTerm(name, value)
                    for name, value in sorted(path.items(), key=lambda item: item[0].casefold())
                )
            )
        )
    return tuple(unique) if len(unique) <= 32 else None


def _valid_local_logic(
    block: SchneiderDFBType,
    output: str,
    paths: tuple[PLCLogicPath, ...],
) -> bool:
    params = {item.name.casefold(): item for item in block.parameters}
    output_param = params.get(output.casefold())
    if output_param is None or output_param.direction != "OUTPUT" or not _bool_type(output_param.data_type):
        return False
    for path in paths:
        for term in path.terms:
            param = params.get(term.tag.casefold())
            if param is None or param.direction != "INPUT" or not _bool_type(param.data_type):
                return False
    return True


def _local_dfb_logic(block: SchneiderDFBType) -> tuple[SchneiderDFBLogic, ...]:
    if block.source_protected or block.language != "ST" or block.st_source is None:
        return ()
    lines = _comment_stripped(block.st_source)
    candidates: list[SchneiderDFBLogic] = []
    writer_counts: dict[str, int] = defaultdict(int)
    cursor = 0
    while cursor < len(lines):
        text = lines[cursor].strip()
        if not text:
            cursor += 1
            continue
        if _v2._UNSUPPORTED_OPEN.match(text):
            cursor = _v2._skip_unsupported_region(lines, cursor)
            continue
        if _v2._IF.match(text):
            chain = _v2._collect_if_chain(lines, cursor)
            if chain is None:
                cursor += 1
                continue
            cursor = int(chain["end_index"]) + 1
            modeled = _v2._analyze_chain(chain)
            if modeled is None:
                continue
            for output, paths in modeled["outputs"].items():
                if not _valid_local_logic(block, output, paths):
                    continue
                writer_counts[output.casefold()] += 1
                digest = hashlib.sha1(
                    f"{block.id}:if:{chain['start_line']}:{chain['end_line']}:{output}".encode()
                ).hexdigest()[:14]
                candidates.append(
                    SchneiderDFBLogic(
                        id=f"SCHNEIDER-DFB-LOGIC:{digest}",
                        dfb_type=block.name,
                        output_formal=output,
                        paths=paths,
                        source=block.source,
                        origin=f"DFB_ST_IF:{chain['start_line']}-{chain['end_line']}",
                    )
                )
            continue
        if _CONTROL_OPEN.match(text) or _CONTROL_CLOSE.match(text):
            cursor += 1
            continue

        chunks = [chunk.strip() for chunk in text.split(";") if chunk.strip()]
        for chunk in chunks:
            match = _v1._ASSIGNMENT.match(chunk + ";")
            if match is None:
                continue
            lhs = _v1._lhs_ref(match.group("lhs"))
            if lhs is None:
                continue
            ast = _v1._parse_bool_ast(match.group("rhs").strip())
            raw_paths = _v1._dnf(ast) if ast is not None else None
            if raw_paths is None:
                continue
            paths = _dedupe_dnf(raw_paths)
            if paths is None or not _valid_local_logic(block, lhs, paths):
                continue
            writer_counts[lhs.casefold()] += 1
            digest = hashlib.sha1(f"{block.id}:assign:{cursor + 1}:{lhs}:{chunk}".encode()).hexdigest()[:14]
            candidates.append(
                SchneiderDFBLogic(
                    id=f"SCHNEIDER-DFB-LOGIC:{digest}",
                    dfb_type=block.name,
                    output_formal=lhs,
                    paths=paths,
                    source=block.source,
                    origin=f"DFB_ST_ASSIGN:{cursor + 1}",
                )
            )
        cursor += 1

    return tuple(
        item for item in candidates if writer_counts[item.output_formal.casefold()] == 1
    )


def _all_local_logic(dfb_types: tuple[SchneiderDFBType, ...]) -> tuple[SchneiderDFBLogic, ...]:
    result: list[SchneiderDFBLogic] = []
    for block in dfb_types:
        result.extend(_local_dfb_logic(block))
    return tuple(result)


def _split_args(text: str) -> list[str] | None:
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return None
            depth -= 1
        elif char == "," and depth == 0:
            part = "".join(buffer).strip()
            if part:
                parts.append(part)
            buffer = []
            if len(parts) > _MAX_ARGS:
                return None
            continue
        buffer.append(char)
    if depth:
        return None
    tail = "".join(buffer).strip()
    if tail:
        parts.append(tail)
    return parts if len(parts) <= _MAX_ARGS else None


def _owner_symbol_types(
    caller_kind: str,
    caller_name: str,
    globals_by_name: dict[str, tuple[str, str]],
    dfb_by_name: dict[str, SchneiderDFBType],
) -> dict[str, tuple[str, str]]:
    if caller_kind == "SECTION":
        return dict(globals_by_name)
    block = dfb_by_name.get(caller_name.casefold())
    if block is None:
        return {}
    values: dict[str, tuple[str, str]] = {}
    for param in block.parameters:
        values[param.name.casefold()] = (param.name, param.data_type)
    for name, dtype in block.local_symbols:
        values.setdefault(name.casefold(), (name, dtype))
    return values


def _parse_bindings(
    callee: SchneiderDFBType,
    arg_text: str,
    symbols: dict[str, tuple[str, str]],
) -> tuple[tuple[SchneiderParameterBinding, ...], str]:
    parts = _split_args(arg_text)
    if parts is None:
        return (), "call_argument_grammar_unsupported"
    params = {item.name.casefold(): item for item in callee.parameters}
    bindings: list[SchneiderParameterBinding] = []
    seen: set[str] = set()
    for part in parts:
        operator = "=>" if "=>" in part else ":=" if ":=" in part else ""
        if not operator:
            return tuple(bindings), "positional_binding_unsupported"
        formal_raw, actual_raw = part.split(operator, 1)
        formal = formal_raw.strip()
        actual = actual_raw.strip()
        param = params.get(formal.casefold())
        if param is None:
            return tuple(bindings), f"unknown_formal:{formal}"
        if formal.casefold() in seen:
            return tuple(bindings), f"duplicate_formal:{formal}"
        seen.add(formal.casefold())
        expected = "=>" if param.direction == "OUTPUT" else ":="
        if operator != expected:
            return tuple(bindings), f"invalid_binding_direction:{formal}"

        if _LITERAL_BOOL.fullmatch(actual):
            if param.direction != "INPUT" or not _bool_type(param.data_type):
                return tuple(bindings), f"invalid_literal_binding:{formal}"
            actual_label = actual.upper()
            actual_type = "BOOL"
        elif _SIMPLE_REF.fullmatch(actual):
            symbol = symbols.get(actual.casefold())
            if symbol is None:
                return tuple(bindings), f"unknown_actual:{formal}"
            actual_label, actual_type = symbol
            if not _types_compatible(param.data_type, actual_type):
                return tuple(bindings), f"type_mismatch:{formal}"
        else:
            return tuple(bindings), f"complex_actual_unsupported:{formal}"

        bindings.append(
            SchneiderParameterBinding(
                formal=param.name,
                actual=actual_label,
                direction=param.direction,
                operator=operator,
                actual_type=actual_type,
            )
        )

    required = {
        item.name.casefold()
        for item in callee.parameters
        if item.direction == "INOUT" or (item.direction == "INPUT" and not item.has_default)
    }
    missing = sorted(required - seen)
    if missing:
        return tuple(bindings), f"missing_required_binding:{','.join(missing)}"
    return tuple(bindings), "bound"


def _project_statement_for_call(project, section: str, line_no: int):
    matches = []
    for statement in project.logic_statements:
        if statement.language != "ST":
            continue
        owner = statement.source.routine or statement.routine or statement.owner_name or ""
        if owner.casefold() != section.casefold():
            continue
        line = _v2._statement_line(statement)
        if line == line_no and _CALL.match(statement.text):
            matches.append(statement)
    return matches[0] if len(matches) == 1 else None


def _scan_st_calls(text: str) -> list[tuple[int, str, str, bool]]:
    result: list[tuple[int, str, str, bool]] = []
    depth = 0
    for line_no, raw in enumerate(_comment_stripped(text), start=1):
        stripped = raw.strip()
        if not stripped:
            continue
        if _CONTROL_CLOSE.match(stripped):
            depth = max(0, depth - 1)
            continue
        match = _CALL.match(stripped)
        if match:
            result.append((line_no, match.group("name"), match.group("args"), depth == 0))
        if _CONTROL_OPEN.match(stripped):
            depth += 1
    return result


def _raw_call_candidates(path: Path, project, dfb_types: tuple[SchneiderDFBType, ...]):
    result: list[dict[str, object]] = []
    for _source, relative, root in _xml_roots(path):
        for program in (item for item in root.iter() if _v1._local_name(item.tag) == "program"):
            ident = next((item for item in program.iter() if _v1._local_name(item.tag) == "identProgram"), None)
            if ident is None:
                continue
            section = (ident.attrib.get("name") or "").strip()
            st_source = next((item for item in program.iter() if _v1._local_name(item.tag) == "STSource"), None)
            if not section or st_source is None:
                continue
            for line_no, symbol, args, top_level in _scan_st_calls("".join(st_source.itertext())):
                statement = _project_statement_for_call(project, section, line_no)
                result.append(
                    {
                        "caller_kind": "SECTION",
                        "caller_name": section,
                        "symbol": symbol,
                        "args": args,
                        "top_level": top_level,
                        "source": statement.source if statement is not None else _source_ref(project, section, str(line_no), relative=relative),
                        "statement_id": statement.id if statement is not None else None,
                        "relative": relative,
                    }
                )

    for block in dfb_types:
        if block.st_source is None:
            continue
        for line_no, symbol, args, top_level in _scan_st_calls(block.st_source):
            result.append(
                {
                    "caller_kind": "DFB",
                    "caller_name": block.name,
                    "symbol": symbol,
                    "args": args,
                    "top_level": top_level,
                    "source": replace(block.source, line=f"DFB:{line_no}"),
                    "statement_id": None,
                    "relative": block.source.line or "DFB",
                }
            )
    return result


def _resolve_calls(
    path: Path,
    project,
    dfb_types: tuple[SchneiderDFBType, ...],
    instances: tuple[SchneiderDFBInstance, ...],
) -> list[SchneiderCallBinding]:
    type_groups: dict[str, list[SchneiderDFBType]] = defaultdict(list)
    for block in dfb_types:
        type_groups[block.name.casefold()].append(block)
    unique_types = {name: items[0] for name, items in type_groups.items() if len(items) == 1}

    instance_groups: dict[tuple[str, str, str], list[SchneiderDFBInstance]] = defaultdict(list)
    for instance in instances:
        key = (instance.owner_kind, instance.owner_name.casefold(), instance.name.casefold())
        instance_groups[key].append(instance)

    globals_by_name = _global_symbols(path)
    calls: list[SchneiderCallBinding] = []
    raw_calls = _raw_call_candidates(path, project, dfb_types)
    for candidate in raw_calls[:_MAX_CALLS]:
        caller_kind = str(candidate["caller_kind"])
        caller_name = str(candidate["caller_name"])
        symbol = str(candidate["symbol"])
        source = candidate["source"]
        assert isinstance(source, PLCSourceRef)
        signature = f"{caller_kind}:{caller_name}:{source.line}:{symbol}:{candidate['relative']}"
        call_id = f"SCHNEIDER-CALL:{hashlib.sha1(signature.encode()).hexdigest()[:16]}"

        if not bool(candidate["top_level"]):
            calls.append(
                SchneiderCallBinding(
                    call_id,
                    caller_kind,
                    caller_name,
                    symbol,
                    None,
                    None,
                    (),
                    source,
                    PLCSemanticState.PARTIAL,
                    "call_inside_unmodeled_control",
                    candidate["statement_id"],
                )
            )
            continue

        if caller_kind == "SECTION":
            key = ("GLOBAL", "global", symbol.casefold())
        else:
            key = ("DFB", caller_name.casefold(), symbol.casefold())
        instance_candidates = instance_groups.get(key, [])
        if len(instance_candidates) != 1:
            calls.append(
                SchneiderCallBinding(
                    call_id,
                    caller_kind,
                    caller_name,
                    symbol,
                    None,
                    None,
                    (),
                    source,
                    PLCSemanticState.PARTIAL,
                    "ambiguous_or_unresolved_instance",
                    candidate["statement_id"],
                )
            )
            continue

        instance = instance_candidates[0]
        callee = unique_types.get(instance.block_type.casefold())
        if callee is None:
            calls.append(
                SchneiderCallBinding(
                    call_id,
                    caller_kind,
                    caller_name,
                    symbol,
                    None,
                    instance.name,
                    (),
                    source,
                    PLCSemanticState.PARTIAL,
                    "ambiguous_or_unresolved_dfb_type",
                    candidate["statement_id"],
                )
            )
            continue
        if callee.source_protected:
            calls.append(
                SchneiderCallBinding(
                    call_id,
                    caller_kind,
                    caller_name,
                    symbol,
                    callee.name,
                    instance.name,
                    (),
                    source,
                    PLCSemanticState.PARTIAL,
                    "callee_protected_or_body_unavailable",
                    candidate["statement_id"],
                )
            )
            continue

        symbols = _owner_symbol_types(caller_kind, caller_name, globals_by_name, unique_types)
        bindings, status = _parse_bindings(callee, str(candidate["args"]), symbols)
        calls.append(
            SchneiderCallBinding(
                call_id,
                caller_kind,
                caller_name,
                symbol,
                callee.name,
                instance.name,
                bindings,
                source,
                PLCSemanticState.FULL if status == "bound" else PLCSemanticState.PARTIAL,
                "dfb_instance" if status == "bound" else status,
                candidate["statement_id"],
            )
        )
    if len(raw_calls) > _MAX_CALLS:
        project.warnings.append(
            f"Schneider V3 call inventory exceeds {_MAX_CALLS} bounded calls; remaining calls are withheld."
        )
    return calls


def _recursive_nodes(calls: list[SchneiderCallBinding]) -> set[str]:
    graph: dict[str, set[str]] = defaultdict(set)
    nodes: set[str] = set()
    for call in calls:
        if call.caller_kind != "DFB" or call.callee_type is None:
            continue
        source = call.caller_name.casefold()
        target = call.callee_type.casefold()
        graph[source].add(target)
        nodes.update((source, target))

    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    cyclic: set[str] = set()

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        low[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in graph.get(node, ()):
            if target not in indices:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], indices[target])
        if low[node] != indices[node]:
            return
        component: list[str] = []
        while stack:
            item = stack.pop()
            on_stack.remove(item)
            component.append(item)
            if item == node:
                break
        if len(component) > 1:
            cyclic.update(component)
        elif component and component[0] in graph.get(component[0], set()):
            cyclic.add(component[0])

    for node in sorted(nodes):
        if node not in indices:
            visit(node)
    return cyclic


def _reachability(
    dfb_types: tuple[SchneiderDFBType, ...],
    calls: list[SchneiderCallBinding],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    outgoing: dict[str, list[SchneiderCallBinding]] = defaultdict(list)
    reachable: set[str] = set()
    queue: list[str] = []
    for call in calls:
        if call.caller_kind == "SECTION" and call.callee_type:
            target = call.callee_type.casefold()
            if target not in reachable:
                reachable.add(target)
                queue.append(target)
        elif call.caller_kind == "DFB":
            outgoing[call.caller_name.casefold()].append(call)
    while queue:
        caller = queue.pop(0)
        for call in outgoing.get(caller, ()):
            if call.callee_type is None:
                continue
            target = call.callee_type.casefold()
            if target not in reachable:
                reachable.add(target)
                queue.append(target)

    labels = {item.name.casefold(): item.name for item in dfb_types}
    reachable_names = tuple(sorted((labels.get(item, item) for item in reachable), key=str.casefold))
    unreachable = tuple(
        sorted(
            (item.name for item in dfb_types if item.name.casefold() not in reachable),
            key=str.casefold,
        )
    )
    active_gaps = tuple(
        call.id
        for call in calls
        if call.semantic_state is not PLCSemanticState.FULL
        and (
            call.caller_kind == "SECTION"
            or call.caller_name.casefold() in reachable
        )
    )
    return reachable_names, unreachable, active_gaps


def _binding_refs(call: SchneiderCallBinding) -> tuple[tuple[str, ...], tuple[str, ...]]:
    reads: list[str] = []
    writes: list[str] = []
    for binding in call.bindings:
        if _LITERAL_BOOL.fullmatch(binding.actual):
            continue
        if binding.direction in {"INPUT", "INOUT"} and binding.actual not in reads:
            reads.append(binding.actual)
        if binding.direction in {"OUTPUT", "INOUT"} and binding.actual not in writes:
            writes.append(binding.actual)
    return tuple(reads), tuple(writes)


def _update_section_call_statements(project, calls: list[SchneiderCallBinding]) -> None:
    by_statement = {
        call.statement_id: call
        for call in calls
        if call.statement_id is not None and call.caller_kind == "SECTION"
    }
    updated = []
    for statement in project.logic_statements:
        call = by_statement.get(statement.id)
        if call is None or call.semantic_state is not PLCSemanticState.FULL:
            updated.append(statement)
            continue
        reads, writes = _binding_refs(call)
        updated.append(
            replace(
                statement,
                reads=reads,
                writes=writes,
                calls=(call.callee_type or call.call_symbol,),
                # A proven interface binding is not proof of arbitrary DFB runtime behavior.
                semantic_state=PLCSemanticState.PARTIAL,
            )
        )
    project.logic_statements = updated
    project.instruction_semantic_count = sum(
        item.semantic_state is PLCSemanticState.FULL for item in project.logic_statements
    )
    st = [item for item in project.logic_statements if item.language == "ST"]
    project.st_statement_total = len(st)
    project.st_statement_semantic_count = sum(
        item.semantic_state is PLCSemanticState.FULL for item in st
    )
    project.partially_modeled_instruction_names = sorted(
        {item.language for item in project.logic_statements if item.semantic_state is not PLCSemanticState.FULL}
    )


def _writer_conflicts(project, calls: list[SchneiderCallBinding], reachable: set[str]) -> tuple[set[str], tuple[str, ...]]:
    direct: dict[str, list[str]] = defaultdict(list)
    for statement in project.logic_statements:
        if statement.id in {call.statement_id for call in calls if call.statement_id}:
            continue
        for ref in statement.writes:
            direct[ref.casefold()].append(statement.id)

    call_writers: dict[str, list[SchneiderCallBinding]] = defaultdict(list)
    for call in calls:
        if call.caller_kind != "SECTION" or call.semantic_state is not PLCSemanticState.FULL:
            continue
        if call.callee_type is None or call.callee_type.casefold() not in reachable:
            continue
        for binding in call.bindings:
            if binding.direction in {"OUTPUT", "INOUT"} and not _LITERAL_BOOL.fullmatch(binding.actual):
                call_writers[binding.actual.casefold()].append(call)

    blocked: set[str] = set()
    labels: list[str] = []
    for output, writers in call_writers.items():
        if len(writers) + len(direct.get(output, ())) <= 1:
            continue
        blocked.update(item.id for item in writers)
        labels.append(output)
    return blocked, tuple(sorted(labels))


def _project_logic(
    project,
    calls: list[SchneiderCallBinding],
    local_logic: tuple[SchneiderDFBLogic, ...],
    dfb_types: tuple[SchneiderDFBType, ...],
    reachable: set[str],
    blocked_calls: set[str],
) -> tuple[str, ...]:
    blocks = {item.name.casefold(): item for item in dfb_types}
    logic_by_type: dict[str, list[SchneiderDFBLogic]] = defaultdict(list)
    for logic in local_logic:
        logic_by_type[logic.dfb_type.casefold()].append(logic)
    globals_by_name = {tag.name.casefold(): (tag.name, _type_name(tag.data_type)) for tag in project.tags if tag.scope.casefold() == "controller"}

    projected_ids: list[str] = []
    existing = {item.id for item in project.output_logic}
    for call in calls:
        if (
            call.caller_kind != "SECTION"
            or call.semantic_state is not PLCSemanticState.FULL
            or call.callee_type is None
            or call.callee_type.casefold() not in reachable
            or call.id in blocked_calls
        ):
            continue
        block = blocks.get(call.callee_type.casefold())
        if block is None:
            continue
        params = {item.name.casefold(): item for item in block.parameters}
        binding_by_formal = {item.formal.casefold(): item for item in call.bindings}
        for local in logic_by_type.get(block.name.casefold(), ()):
            output_param = params.get(local.output_formal.casefold())
            output_binding = binding_by_formal.get(local.output_formal.casefold())
            if output_param is None or output_binding is None or output_param.direction != "OUTPUT":
                continue
            if not _bool_type(output_param.data_type) or not _bool_type(output_binding.actual_type):
                continue
            if output_binding.actual.casefold() not in globals_by_name:
                continue

            projected_paths: list[PLCLogicPath] = []
            valid = True
            for path in local.paths:
                terms: list[PLCBooleanTerm] = []
                contradiction = False
                for term in path.terms:
                    formal = params.get(term.tag.casefold())
                    binding = binding_by_formal.get(term.tag.casefold())
                    if (
                        formal is None
                        or formal.direction != "INPUT"
                        or not _bool_type(formal.data_type)
                        or binding is None
                        or not _bool_type(binding.actual_type)
                    ):
                        valid = False
                        break
                    if _LITERAL_BOOL.fullmatch(binding.actual):
                        if (binding.actual.upper() == "TRUE") != term.required:
                            contradiction = True
                            break
                        continue
                    if binding.actual.casefold() not in globals_by_name:
                        valid = False
                        break
                    terms.append(PLCBooleanTerm(binding.actual, term.required))
                if not valid:
                    break
                if not contradiction:
                    projected_paths.append(PLCLogicPath(tuple(terms)))
            if not valid:
                continue

            signature = f"{call.id}:{local.id}:{output_binding.actual}:{repr(projected_paths)}"
            digest = hashlib.sha1(signature.encode()).hexdigest()[:14]
            logic_id = f"SCHNEIDER-CALL3-{digest}"
            if logic_id in existing:
                continue
            project.output_logic.append(
                PLCOutputLogic(
                    id=logic_id,
                    output_tag=output_binding.actual,
                    instruction="ASSIGN_BOOL",
                    paths=tuple(projected_paths),
                    source=call.source,
                    language="ST",
                    origin=f"SCHNEIDER_ST_CALL_V3:{call.id}:{local.id}",
                    semantic_state=PLCSemanticState.FULL,
                )
            )
            existing.add(logic_id)
            projected_ids.append(logic_id)
            if len(projected_ids) >= _MAX_PROJECTED_LOGIC:
                return tuple(projected_ids)
    return tuple(projected_ids)


def _augment_graph(base: PLCDependencyGraph, facts: SchneiderV3Facts) -> PLCDependencyGraph:
    edges = list(base.edges)
    seen = {(item.source, item.target, item.kind, item.evidence_id) for item in edges}

    def add(source: str, target: str, kind: str, evidence: str) -> None:
        key = (source, target, kind, evidence)
        if key not in seen:
            seen.add(key)
            edges.append(PLCDependencyEdge(source, target, kind, evidence))

    reachable = {item.casefold() for item in facts.reachable_dfb_types}
    for call in facts.calls:
        caller = f"{call.caller_kind}:{call.caller_name}"
        target = f"DFB:{call.callee_type or call.call_symbol}"
        add(caller, target, "CALLS_DFB", call.id)
        if call.callee_type and (
            call.caller_kind == "SECTION" or call.caller_name.casefold() in reachable
        ):
            add(caller, target, "REACHABLE_CALL", call.id)
        if call.instance_name:
            add(call.id, f"DFB_INSTANCE:{call.instance_name}", "USES_DFB_INSTANCE", call.id)
        for binding in call.bindings:
            add(call.id, binding.actual, f"BINDS_{binding.direction}", call.id)
    return PLCDependencyGraph(edges=edges, unknown_instruction_names=list(base.unknown_instruction_names))


def _call_gap_fat(project, facts: SchneiderV3Facts) -> list[FATTestCase]:
    active = set(facts.active_call_gaps)
    result: list[FATTestCase] = []
    for call in facts.calls:
        if call.id not in active:
            continue
        digest = hashlib.sha1(f"{call.id}:{call.resolution}".encode()).hexdigest()[:10]
        result.append(
            FATTestCase(
                id=f"FAT-SCHNEIDER-CALL-{digest}",
                title=f"Verify Control Expert DFB call {call.call_symbol} at {call.source.locator}",
                source=call.source,
                output_tag=call.callee_type or call.call_symbol,
                preconditions={},
                expected=(
                    "Engineer evidence must confirm the intended DFB instance/type, parameter binding, "
                    "execution context, and observed behavior for this source-linked call."
                ),
                method="RUNTIME_FAT_REQUIRED",
                scenario="SCHNEIDER_DFB_CALL_RUNTIME",
                limitations=(
                    f"Static DFB call closure withheld: {call.resolution}.",
                    "DevAgent does not execute Control Expert Simulator, HIL, or a real Modicon PLC.",
                ),
            )
        )
    return enrich_fat_procedures(project, result)


def _facts(project) -> SchneiderV3Facts | None:
    return getattr(project, "_schneider_v3_facts", None)


def schneider_capability_profile_v3(project) -> dict[str, object]:
    facts = _facts(project)
    profile = dict(_PREVIOUS_CAPABILITY(project))
    if facts is None:
        return profile
    profile.update(
        {
            "schema": "devagent-schneider-control-expert-capability-v3",
            "dfb_types": len(facts.dfb_types),
            "dfb_instances": len(facts.instances),
            "dfb_calls": len(facts.calls),
            "dfb_calls_bound": sum(item.semantic_state is PLCSemanticState.FULL for item in facts.calls),
            "dfb_local_boolean_theorems": len(facts.local_logic),
            "reachable_dfb_types": list(facts.reachable_dfb_types),
            "unreachable_dfb_types": list(facts.unreachable_dfb_types),
            "recursive_dfb_types": list(facts.recursive_dfb_types),
            "cross_boundary_writer_conflicts": list(facts.writer_conflicts),
            "projected_call_theorems": len(facts.projected_logic_ids),
            "execution_closure": (
                "COMPLETE"
                if not facts.active_call_gaps and not facts.recursive_dfb_types
                else "PARTIAL_FAIL_CLOSED"
            ),
            "bounded_call_semantics": (
                "top-level named ST DFB instance calls with exact XDB/XEF interface binding; "
                "Boolean output proof projects only through reachable, acyclic, uniquely-owned calls"
            ),
        }
    )
    return profile


def _v3_checks(project, facts: SchneiderV3Facts) -> list[StaticCheck]:
    calls = len(facts.calls)
    bound = sum(item.semantic_state is PLCSemanticState.FULL for item in facts.calls)
    return [
        StaticCheck(
            "SCHNEIDER_V3_DFB_BINDING",
            StaticCheckStatus.PASS if calls == bound else StaticCheckStatus.NOT_PROVEN,
            f"Deterministically bound {bound}/{calls} discovered Control Expert DFB call(s) to exact instance/type/interface identity.",
            tuple(item.id for item in facts.calls),
        ),
        StaticCheck(
            "SCHNEIDER_V3_EXECUTION_CLOSURE",
            StaticCheckStatus.PASS if not facts.active_call_gaps and not facts.recursive_dfb_types else StaticCheckStatus.NOT_PROVEN,
            (
                f"Section-rooted DFB closure reaches {len(facts.reachable_dfb_types)} DFB type(s); "
                f"active call gaps={len(facts.active_call_gaps)}, recursive DFB types={len(facts.recursive_dfb_types)}."
            ),
            tuple(facts.active_call_gaps),
        ),
        StaticCheck(
            "SCHNEIDER_V3_UNREACHABLE_DFBS",
            StaticCheckStatus.WARN if facts.unreachable_dfb_types else StaticCheckStatus.PASS,
            (
                f"{len(facts.unreachable_dfb_types)} imported DFB type(s) are not reachable from an exported section through resolved calls."
                if facts.unreachable_dfb_types
                else "Every imported DFB type is reachable from an exported section through the resolved call graph."
            ),
            tuple(f"DFB:{item}" for item in facts.unreachable_dfb_types),
        ),
        StaticCheck(
            "SCHNEIDER_V3_CROSS_BOUNDARY_WRITERS",
            StaticCheckStatus.NOT_PROVEN if facts.writer_conflicts else StaticCheckStatus.PASS,
            (
                f"{len(facts.writer_conflicts)} projected DFB output target(s) have competing direct/call writers."
                if facts.writer_conflicts
                else "No competing direct/call writers were found for projected DFB Boolean outputs."
            ),
            facts.writer_conflicts,
        ),
    ]


def analyze_schneider_control_expert_v3(path: Path) -> PLCEngineeringResult:
    base = _PREVIOUS_ANALYZER(path)
    project = base.project
    dfb_types = _inventory_dfb_types(path, project)
    if not dfb_types:
        return base
    instances = _instances(path, project, dfb_types)
    local_logic = _all_local_logic(dfb_types)
    calls = _resolve_calls(path, project, dfb_types, instances)

    recursive = _recursive_nodes(calls)
    if recursive:
        calls = [
            replace(call, semantic_state=PLCSemanticState.PARTIAL, resolution="recursive_dfb_call_cycle_unsupported")
            if call.caller_kind == "DFB"
            and call.callee_type is not None
            and call.caller_name.casefold() in recursive
            and call.callee_type.casefold() in recursive
            else call
            for call in calls
        ]

    reachable_names, unreachable, active_gaps = _reachability(dfb_types, calls)
    reachable = {item.casefold() for item in reachable_names}
    _update_section_call_statements(project, calls)
    blocked_calls, writer_conflicts = _writer_conflicts(project, calls, reachable)
    projected = _project_logic(project, calls, local_logic, dfb_types, reachable, blocked_calls)

    labels = {item.name.casefold(): item.name for item in dfb_types}
    facts = SchneiderV3Facts(
        dfb_types=dfb_types,
        instances=instances,
        calls=tuple(calls),
        local_logic=local_logic,
        reachable_dfb_types=reachable_names,
        unreachable_dfb_types=unreachable,
        active_call_gaps=active_gaps,
        recursive_dfb_types=tuple(sorted((labels.get(item, item) for item in recursive), key=str.casefold)),
        writer_conflicts=writer_conflicts,
        projected_logic_ids=projected,
    )
    setattr(project, "_schneider_v3_facts", facts)

    graph = _augment_graph(build_dependency_graph(project), facts)
    fat_tests = _v1._fat_tests(project)
    fat_tests.extend(_call_gap_fat(project, facts))
    fat_tests = list({item.id: item for item in fat_tests}.values())
    checks = _v1._checks(project, graph, fat_tests)
    checks.extend(_v3_checks(project, facts))
    profile = schneider_capability_profile_v3(project)

    if profile["static_contract"] == "NO_EXECUTABLE_LOGIC":
        outcome = PLCOutcome.BLOCKED
    elif (
        profile["static_contract"] == "COMPLETE"
        and profile["execution_closure"] == "COMPLETE"
        and not writer_conflicts
    ):
        outcome = PLCOutcome.STATICALLY_VERIFIED
    else:
        outcome = PLCOutcome.PARTIALLY_VERIFIED

    limitations = [item.replace("Schneider V2", "Schneider V3") for item in base.limitations]
    limitations.append(
        "Schneider V3 resolves only bounded top-level named ST calls through uniquely identified DFB instances/types and exact named XDB/XEF interfaces. Guarded, positional, complex-expression, ambiguous, protected, recursive, or unresolved calls remain PARTIAL and require engineer FAT."
    )
    limitations.append(
        "A proven DFB interface binding does not make arbitrary DFB runtime behavior FULL. Cross-boundary Boolean proof is projected only from a bounded local DFB Boolean theorem through compatible BOOL/EBOOL input/output bindings, section reachability, acyclic call closure, and unique writer ownership."
    )
    return PLCEngineeringResult(outcome, project, graph, fat_tests, checks, list(dict.fromkeys(limitations)))


def _projected_logic(project) -> dict[str, PLCOutputLogic]:
    facts = _facts(project)
    if facts is None:
        return {}
    ids = set(facts.projected_logic_ids)
    return {item.id: item for item in project.output_logic if item.id in ids}


def _call_for_projected_logic(facts: SchneiderV3Facts, logic: PLCOutputLogic) -> SchneiderCallBinding | None:
    for call in facts.calls:
        if f":{call.id}:" in logic.origin:
            return call
    return None


def _v3_verify_requirement(previous, requirement, engineering, evidence, tests):
    result = previous(requirement, engineering, evidence, tests)
    project = engineering.project
    facts = _facts(project)
    if facts is None:
        return result
    projected = _projected_logic(project)
    projected_evidence = [item for item in result.evidence_ids if item in projected]

    if projected_evidence and result.status in {RequirementStatus.STATICALLY_VERIFIED, RequirementStatus.CONFLICT}:
        logic = projected[projected_evidence[0]]
        call = _call_for_projected_logic(facts, logic)
        suffix = (
            "Schneider V3 additionally proves the DFB instance/type identity, named interface binding, "
            "section reachability, Boolean type compatibility, acyclic closure, and unique writer ownership."
        )
        return replace(
            result,
            summary=f"{result.summary} {suffix}",
            evidence_ids=tuple(dict.fromkeys([*result.evidence_ids, *( [call.id] if call else [] )])),
        )

    if result.status is not RequirementStatus.TRACEABLE_NOT_PROVEN:
        return result
    matched = list(result.matched_tags)
    candidates: list[PLCOutputLogic] = []
    for logic in projected.values():
        explicit = next((tag for tag in matched if tag.casefold() == logic.output_tag.casefold()), None)
        if explicit is not None and explicit_bool(requirement.text, explicit) is not None:
            candidates.append(logic)
    if len(candidates) != 1:
        return result
    logic = candidates[0]
    call = _call_for_projected_logic(facts, logic)
    if call is None or call.id in set(facts.active_call_gaps):
        return result
    if logic.output_tag.casefold() in set(facts.writer_conflicts):
        return result

    output_text = next(tag for tag in matched if tag.casefold() == logic.output_tag.casefold())
    expected = explicit_bool(requirement.text, output_text)
    assert expected is not None
    assignment = {
        tag: value
        for tag in matched
        if tag.casefold() != logic.output_tag.casefold()
        for value in [explicit_bool(requirement.text, tag)]
        if value is not None
    }
    if not assignment:
        return result

    from devagent.plc import schneider_integration_v1 as _integration

    truth = _integration._bool_truth(logic, assignment, expected)
    combined = tuple(dict.fromkeys([*result.evidence_ids, logic.id, call.id]))
    linked = tuple(
        test.id
        for test in tests
        if test.output_tag.casefold() == logic.output_tag.casefold()
        and all(test.preconditions.get(key) == value for key, value in assignment.items())
    )
    if truth == "PROVEN":
        return RequirementVerification(
            requirement.id,
            RequirementStatus.STATICALLY_VERIFIED,
            (
                f"Specified Boolean conditions deterministically imply {logic.output_tag}={'TRUE' if expected else 'FALSE'} "
                "through the Schneider V3 reachable DFB instance/interface projection theorem; arbitrary DFB runtime behavior remains outside this bounded proof."
            ),
            combined,
            tuple(matched),
            linked,
        )
    if truth == "CONFLICT":
        return RequirementVerification(
            requirement.id,
            RequirementStatus.CONFLICT,
            (
                f"Specified Boolean conditions make required {logic.output_tag}={'TRUE' if expected else 'FALSE'} impossible "
                "in the Schneider V3 reachable DFB instance/interface projection theorem."
            ),
            combined,
            tuple(matched),
        )
    return replace(result, evidence_ids=combined)


def _v3_evidence(previous, engineering):
    items = list(previous(engineering))
    facts = _facts(engineering.project)
    if facts is None:
        return items
    project = engineering.project
    existing = {item.id for item in items}
    reachable = {item.casefold() for item in facts.reachable_dfb_types}
    for block in facts.dfb_types:
        if block.id in existing:
            continue
        items.append(
            EvidenceItem(
                block.id,
                "SCHNEIDER_DFB_TYPE",
                f"Control Expert DFB {block.name}: {block.language} with {len(block.parameters)} interface parameter(s).",
                block.source.locator,
                project.metadata.source_sha256,
                {
                    "name": block.name,
                    "language": block.language,
                    "source_protected": block.source_protected,
                    "reachable": block.name.casefold() in reachable,
                    "parameters": [
                        {
                            "name": item.name,
                            "direction": item.direction,
                            "data_type": item.data_type,
                            "has_default": item.has_default,
                        }
                        for item in block.parameters
                    ],
                },
            )
        )
    for instance in facts.instances:
        items.append(
            EvidenceItem(
                instance.id,
                "SCHNEIDER_DFB_INSTANCE",
                f"{instance.owner_kind} {instance.owner_name}: instance {instance.name} -> {instance.block_type}.",
                instance.source.locator,
                project.metadata.source_sha256,
                {
                    "owner_kind": instance.owner_kind,
                    "owner_name": instance.owner_name,
                    "name": instance.name,
                    "block_type": instance.block_type,
                },
            )
        )
    for logic in facts.local_logic:
        items.append(
            EvidenceItem(
                logic.id,
                "SCHNEIDER_DFB_LOCAL_BOOLEAN_LOGIC",
                f"{logic.dfb_type}.{logic.output_formal}: {len(logic.paths)} bounded Boolean path(s).",
                logic.source.locator,
                project.metadata.source_sha256,
                {
                    "dfb_type": logic.dfb_type,
                    "output_formal": logic.output_formal,
                    "origin": logic.origin,
                    "paths": [
                        [{"tag": term.tag, "required": term.required} for term in path.terms]
                        for path in logic.paths
                    ],
                },
            )
        )
    for call in facts.calls:
        items.append(
            EvidenceItem(
                call.id,
                "SCHNEIDER_DFB_CALL_BINDING",
                f"{call.caller_kind} {call.caller_name} -> {call.callee_type or call.call_symbol}: {call.semantic_state.value} ({call.resolution}).",
                call.source.locator,
                project.metadata.source_sha256,
                {
                    "caller_kind": call.caller_kind,
                    "caller": call.caller_name,
                    "call_symbol": call.call_symbol,
                    "callee_type": call.callee_type,
                    "instance_name": call.instance_name,
                    "semantic_state": call.semantic_state.value,
                    "resolution": call.resolution,
                    "bindings": [
                        {
                            "formal": item.formal,
                            "actual": item.actual,
                            "direction": item.direction,
                            "operator": item.operator,
                            "actual_type": item.actual_type,
                        }
                        for item in call.bindings
                    ],
                },
            )
        )
    return items


def _v3_findings(previous, engineering, valid_evidence_ids):
    items = previous(engineering, valid_evidence_ids)
    if _facts(engineering.project) is None:
        return items
    return [
        replace(
            item,
            title=item.title.replace("V2", "V3"),
            summary=item.summary.replace("Schneider V2", "Schneider V3").replace("under the V2 contract", "under the V3 contract"),
            recommendation=item.recommendation.replace("under the V2 contract", "under the V3 contract"),
        )
        for item in items
    ]


def _v3_risks(previous, engineering, verifications, executions, engineering_findings):
    risks = list(previous(engineering, verifications, executions, engineering_findings))
    facts = _facts(engineering.project)
    if facts is None:
        return risks
    active = set(facts.active_call_gaps)
    for call in facts.calls:
        if call.id not in active:
            continue
        risks.append(
            RiskFinding(
                stable_id("RISK", "SCHNEIDER_DFB_BINDING", call.id),
                "CALL_BINDING",
                f"Schneider DFB call is not deterministically closed: {call.call_symbol}",
                Severity.HIGH,
                f"V3 withheld DFB call closure at {call.source.locator}: {call.resolution}.",
                "Downstream DFB behavior and requirement/FAT traceability may depend on an unresolved instance, type, interface, control context, or recursive path.",
                "Correct/export the exact DFB/interface/instance evidence or execute the generated engineer FAT procedure; do not promote this call to static verification.",
                (call.id,),
            )
        )
    if facts.unreachable_dfb_types:
        risks.append(
            RiskFinding(
                stable_id("RISK", "SCHNEIDER_UNREACHABLE_DFB", *facts.unreachable_dfb_types),
                "UNREACHABLE_LOGIC",
                "Imported Schneider DFB types are unreachable from exported sections",
                Severity.MEDIUM,
                f"{len(facts.unreachable_dfb_types)} DFB type(s) are not reached by the resolved section-rooted call graph: {', '.join(facts.unreachable_dfb_types[:8])}.",
                "Unreachable DFB implementation may be obsolete logic or may indicate an incomplete export/call binding; its local theorem cannot prove active machine behavior.",
                "Confirm intended execution entrypoints and remove, document, or correctly bind unreachable DFBs before relying on them for requirements.",
                tuple(f"DFB:{item}" for item in facts.unreachable_dfb_types),
            )
        )
    if facts.writer_conflicts:
        risks.append(
            RiskFinding(
                stable_id("RISK", "SCHNEIDER_DFB_WRITER", *facts.writer_conflicts),
                "MULTIPLE_WRITERS",
                "Competing Schneider writers block DFB cross-boundary proof",
                Severity.HIGH,
                f"{len(facts.writer_conflicts)} projected DFB output target(s) have more than one direct/call writer.",
                "Final value can depend on section/call order or arbitration outside the bounded V3 theorem.",
                "Disposition writer ownership and execution order; rerun affected requirement verification and FAT after the conflict is resolved.",
                facts.writer_conflicts,
            )
        )
    if facts.recursive_dfb_types:
        risks.append(
            RiskFinding(
                stable_id("RISK", "SCHNEIDER_DFB_RECURSION", *facts.recursive_dfb_types),
                "CALL_RECURSION",
                "Recursive Schneider DFB call cycle is outside V3 execution closure",
                Severity.HIGH,
                f"Recursive/cyclic DFB call graph includes: {', '.join(facts.recursive_dfb_types)}.",
                "A bounded acyclic DFB execution closure cannot be established.",
                "Refactor/disposition the recursion or retain the path as runtime-dependent and validate it with explicit engineer evidence.",
                tuple(f"DFB:{item}" for item in facts.recursive_dfb_types),
            )
        )
    return risks


def _v3_render(previous, project) -> str:
    base = previous(project)
    facts = _facts(project)
    if facts is None:
        return base
    profile = schneider_capability_profile_v3(project)
    base = base.replace("Schneider Control Expert V2", "Schneider Control Expert V3")
    base = base.replace("### Explicit Schneider V2 Boundaries", "### Explicit Schneider V3 Boundaries")
    insertion = (
        "### Schneider V3 DFB Call / Interface Closure\n\n"
        f"- DFB types imported: **{profile['dfb_types']}**\n"
        f"- DFB instances identified: **{profile['dfb_instances']}**\n"
        f"- Deterministically bound DFB calls: **{profile['dfb_calls_bound']}/{profile['dfb_calls']}**\n"
        f"- Section-rooted execution closure: **{profile['execution_closure']}**\n"
        f"- Reachable DFB types: **{len(profile['reachable_dfb_types'])}**\n"
        f"- Unreachable DFB types: **{len(profile['unreachable_dfb_types'])}**\n"
        f"- Bounded local DFB Boolean theorems: **{profile['dfb_local_boolean_theorems']}**\n"
        f"- Cross-boundary Boolean theorem projections: **{profile['projected_call_theorems']}**\n"
        "- DFB requirement proof requires exact instance/type identity, named interface bindings, compatible BOOL/EBOOL types, section reachability, acyclic closure, a bounded local DFB Boolean theorem, and unique writer ownership.\n"
        "- A bound DFB call is still not arbitrary runtime proof: timers, counters, state, non-Boolean computation, guarded/complex calls, protected bodies, and external process behavior remain PARTIAL or runtime-evidence gated.\n"
        "- DevAgent still does not execute Control Expert Simulator, HIL, or a real Modicon PLC.\n\n"
    )
    marker = "### Explicit Schneider V3 Boundaries"
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
    previous_findings = _integration._findings
    previous_risks = _integration._detect_risks
    previous_render = _report._render

    _v1.analyze_schneider_control_expert = analyze_schneider_control_expert_v3
    _v1.schneider_capability_profile = schneider_capability_profile_v3
    _dispatch.analyze_schneider_control_expert = analyze_schneider_control_expert_v3
    _integration.schneider_capability_profile = schneider_capability_profile_v3

    def verify_requirement(requirement, engineering, evidence, tests):
        return _v3_verify_requirement(previous_verify, requirement, engineering, evidence, tests)

    def evidence_index(engineering):
        return _v3_evidence(previous_evidence, engineering)

    def findings(engineering, valid_evidence_ids):
        return _v3_findings(previous_findings, engineering, valid_evidence_ids)

    def detect_risks(engineering, verifications, executions, engineering_findings):
        return _v3_risks(previous_risks, engineering, verifications, executions, engineering_findings)

    def render(project):
        return _v3_render(previous_render, project)

    _integration._verify_requirement = verify_requirement
    _integration._evidence_index = evidence_index
    _integration._findings = findings
    _integration._detect_risks = detect_risks
    _report._render = render
    _INSTALLED = True


__all__ = [
    "SchneiderCallBinding",
    "SchneiderDFBInstance",
    "SchneiderDFBLogic",
    "SchneiderDFBParameter",
    "SchneiderDFBType",
    "SchneiderParameterBinding",
    "SchneiderV3Facts",
    "analyze_schneider_control_expert_v3",
    "install",
    "schneider_capability_profile_v3",
]
