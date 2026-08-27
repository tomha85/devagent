from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import hashlib
import re
from pathlib import Path
import xml.etree.ElementTree as ET

from devagent.plc.analysis import build_dependency_graph
from devagent.plc.fat_procedure_v12 import enrich_fat_procedures
from devagent.plc.models import (
    FATTestCase,
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
from devagent.plc.production_utils import stable_id
from devagent.plc import siemens_scl_control_flow_v2 as _v2
from devagent.plc import siemens_tia_v1 as _v1


_INSTALLED = False
_PREVIOUS_ANALYZER = _v1.analyze_siemens_tia
_PREVIOUS_CAPABILITY = _v1.siemens_capability_profile

_CALL = re.compile(
    r'^\s*#?(?P<name>"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<args>.*)\)\s*;?\s*$',
    re.IGNORECASE | re.DOTALL,
)
_DB_HEADER = re.compile(
    r'^\s*DATA_BLOCK\s+(?P<db>"[^"]+"|[A-Za-z_][A-Za-z0-9_]*)'
    r'(?:\s+(?P<type>"[^"]+"|[A-Za-z_][A-Za-z0-9_]*))?',
    re.IGNORECASE,
)
_CONTROL_OPEN = re.compile(r'^\s*(IF|CASE|FOR|WHILE|REPEAT)\b', re.IGNORECASE)
_CONTROL_CLOSE = re.compile(
    r'^\s*(END_IF|END_CASE|END_FOR|END_WHILE|UNTIL|END_REPEAT)\b',
    re.IGNORECASE,
)
_SIMPLE_REF = re.compile(
    r'^\s*#?(?:(?:"[^"]+")|(?:[A-Za-z_][A-Za-z0-9_]*))'
    r'(?:\.(?:(?:"[^"]+")|(?:[A-Za-z_][A-Za-z0-9_]*)))*\s*$'
)
_LITERAL = re.compile(
    r'^\s*(?:TRUE|FALSE|[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|T#[A-Za-z0-9_.]+)\s*$',
    re.IGNORECASE,
)
_BOOL_TYPES = {"BOOL", "BOOLEAN"}
_MAX_CALLS = 4096
_MAX_ARGS = 64
_MAX_PROJECTED_LOGIC = 4096


@dataclass(frozen=True)
class PLCBlockParameter:
    name: str
    direction: str
    data_type: str
    has_default: bool = False


@dataclass(frozen=True)
class PLCBlock:
    id: str
    name: str
    kind: str
    language: str
    parameters: tuple[PLCBlockParameter, ...]
    source_protected: bool
    source: PLCSourceRef


@dataclass(frozen=True)
class PLCInstanceDB:
    id: str
    name: str
    block_type: str
    source: PLCSourceRef


@dataclass(frozen=True)
class PLCParameterBinding:
    formal: str
    actual: str
    direction: str
    operator: str


@dataclass(frozen=True)
class PLCCallBinding:
    id: str
    caller_block: str
    call_symbol: str
    callee_block: str | None
    instance_db: str | None
    bindings: tuple[PLCParameterBinding, ...]
    source: PLCSourceRef
    semantic_state: PLCSemanticState
    resolution: str


@dataclass(frozen=True)
class SiemensV3Facts:
    blocks: tuple[PLCBlock, ...]
    instance_dbs: tuple[PLCInstanceDB, ...]
    calls: tuple[PLCCallBinding, ...]
    reachable_blocks: tuple[str, ...]
    unreachable_blocks: tuple[str, ...]
    active_call_gaps: tuple[str, ...]
    recursive_blocks: tuple[str, ...]
    writer_conflicts: tuple[str, ...]
    projected_logic_ids: tuple[str, ...]


def _clean(value: str) -> str:
    value = str(value or "").strip()
    if value.startswith("#"):
        value = value[1:].strip()
    return _v1._clean_name(value)


def _type_name(value: str) -> str:
    text = str(value or "").strip()
    if ":=" in text:
        text = text.split(":=", 1)[0].strip()
    return _clean(text)


def _block_kind(kind: str) -> str:
    return {
        "ORGANIZATION_BLOCK": "OB",
        "FUNCTION_BLOCK": "FB",
        "FUNCTION": "FC",
        "OB": "OB",
        "FB": "FB",
        "FC": "FC",
    }.get(kind.upper(), kind.upper())


def _source_ref(
    artifact: str,
    controller: str,
    block_name: str,
    line: str | None = None,
) -> PLCSourceRef:
    return PLCSourceRef(
        artifact,
        controller,
        program=block_name,
        routine=block_name,
        line=line,
    )


def _parse_parameters(block) -> tuple[PLCBlockParameter, ...]:
    params: list[PLCBlockParameter] = []
    section: str | None = None
    for raw in block.lines[1:]:
        stripped = raw.strip()
        if re.match(r"^BEGIN\b", stripped, flags=re.IGNORECASE):
            break
        start = _v1._VAR_START.match(stripped)
        if start:
            section = start.group(1).upper()
            continue
        if re.match(r"^END_VAR\b", stripped, flags=re.IGNORECASE):
            section = None
            continue
        if section not in {"VAR_INPUT", "VAR_OUTPUT", "VAR_IN_OUT", "VAR_STAT"}:
            continue
        declaration = _v1._DECLARATION.match(stripped)
        if declaration is None:
            continue
        dtype = _type_name(declaration.group("dtype"))
        has_default = ":=" in stripped
        for raw_name in declaration.group("names").split(","):
            name = _clean(raw_name)
            if name:
                params.append(PLCBlockParameter(name, section, dtype, has_default))
    return tuple(params)


def _xml_parameters(block) -> tuple[PLCBlockParameter, ...]:
    result: list[PLCBlockParameter] = []
    for section in block.iter():
        if _v1._local_name(section.tag).casefold() != "section":
            continue
        direction = str(
            section.attrib.get("Name") or section.attrib.get("name") or "INTERFACE"
        ).upper()
        if direction not in {
            "INPUT",
            "OUTPUT",
            "INOUT",
            "STATIC",
            "VAR_INPUT",
            "VAR_OUTPUT",
            "VAR_IN_OUT",
            "VAR_STAT",
        }:
            continue
        direction = {
            "INPUT": "VAR_INPUT",
            "OUTPUT": "VAR_OUTPUT",
            "INOUT": "VAR_IN_OUT",
            "STATIC": "VAR_STAT",
        }.get(direction, direction)
        for member in section:
            if _v1._local_name(member.tag).casefold() != "member":
                continue
            name = _clean(
                str(member.attrib.get("Name") or member.attrib.get("name") or "")
            )
            dtype = _type_name(
                str(
                    member.attrib.get("Datatype")
                    or member.attrib.get("DataType")
                    or member.attrib.get("datatype")
                    or "UNKNOWN"
                )
            )
            if name:
                result.append(PLCBlockParameter(name, direction, dtype, False))
    return tuple(result)


def _inventory(
    path: Path,
    project,
) -> tuple[tuple[PLCBlock, ...], tuple[PLCInstanceDB, ...]]:
    _, files = _v1._supported_sources(path)
    artifact = project.metadata.source_path
    controller = project.metadata.controller_name
    blocks: dict[str, PLCBlock] = {}
    instance_candidates: list[tuple[str, str, PLCSourceRef]] = []

    for source_path, relative in files:
        if source_path.suffix.lower() not in {".scl", ".db", ".udt"}:
            continue
        for block in _v1._extract_source_blocks(source_path, relative):
            kind = _block_kind(block.kind)
            if kind in {"OB", "FB", "FC"}:
                key = block.name.casefold()
                candidate = PLCBlock(
                    id=f"SIEMENS-BLOCK:{kind}:{block.name}",
                    name=block.name,
                    kind=kind,
                    language="SCL",
                    parameters=_parse_parameters(block),
                    source_protected=False,
                    source=_source_ref(
                        artifact,
                        controller,
                        block.name,
                        str(block.start_line),
                    ),
                )
                blocks[key] = candidate
            elif block.kind == "DATA_BLOCK":
                header = _DB_HEADER.match(block.lines[0])
                declared_type = (
                    _clean(header.group("type"))
                    if header and header.group("type")
                    else ""
                )
                if not declared_type:
                    for raw in block.lines[1:]:
                        stripped = raw.strip()
                        if not stripped or stripped.startswith("{"):
                            continue
                        if re.match(r"^(VAR|BEGIN)\b", stripped, flags=re.IGNORECASE):
                            break
                        token = _clean(stripped.rstrip(";"))
                        if token:
                            declared_type = token
                            break
                if declared_type:
                    instance_candidates.append(
                        (
                            block.name,
                            declared_type,
                            _source_ref(
                                artifact,
                                controller,
                                block.name,
                                str(block.start_line),
                            ),
                        )
                    )

    for source_path, relative in files:
        if source_path.suffix.lower() != ".xml":
            continue
        try:
            root = ET.parse(source_path).getroot()
        except ET.ParseError:
            continue
        for element in root.iter():
            local = _v1._local_name(element.tag)
            if local not in {
                "SW.Blocks.OB",
                "SW.Blocks.FB",
                "SW.Blocks.FC",
                "SW.Blocks.DB",
            }:
                continue
            kind = local.rsplit(".", 1)[-1]
            name = _clean(
                _v1._child_text(element, "Name")
                or f"{kind}_{element.attrib.get('ID', 'unknown')}"
            )
            if kind in {"OB", "FB", "FC"} and name.casefold() not in blocks:
                language = (
                    _v1._child_text(element, "ProgrammingLanguage") or "UNKNOWN"
                ).upper()
                protected = _v1._protection_flag(element)
                blocks[name.casefold()] = PLCBlock(
                    id=f"SIEMENS-BLOCK:{kind}:{name}",
                    name=name,
                    kind=kind,
                    language=language,
                    parameters=_xml_parameters(element),
                    source_protected=protected,
                    source=_source_ref(artifact, controller, name, "XML"),
                )
            elif kind == "DB":
                dtype = (
                    _v1._child_text(element, "InstanceOfName")
                    or _v1._child_text(element, "InstanceOf")
                    or _v1._child_text(element, "BlockType")
                    or ""
                )
                if dtype:
                    instance_candidates.append(
                        (
                            name,
                            _clean(dtype),
                            _source_ref(artifact, controller, name, "XML"),
                        )
                    )

    fb_names = {
        item.name.casefold(): item.name
        for item in blocks.values()
        if item.kind == "FB"
    }
    instances: dict[str, PLCInstanceDB] = {}
    for name, dtype, source in instance_candidates:
        canonical = fb_names.get(dtype.casefold())
        if canonical is None:
            continue
        instances.setdefault(
            name.casefold(),
            PLCInstanceDB(
                id=f"SIEMENS-INSTANCE-DB:{name}",
                name=name,
                block_type=canonical,
                source=source,
            ),
        )
    return (
        tuple(
            sorted(
                blocks.values(),
                key=lambda item: (item.kind, item.name.casefold()),
            )
        ),
        tuple(sorted(instances.values(), key=lambda item: item.name.casefold())),
    )


def _split_args(text: str) -> list[str] | None:
    parts: list[str] = []
    buffer: list[str] = []
    depth = 0
    quote = False
    for char in text:
        if char == '"':
            quote = not quote
            buffer.append(char)
            continue
        if not quote:
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
    if quote or depth:
        return None
    tail = "".join(buffer).strip()
    if tail:
        parts.append(tail)
    return parts if len(parts) <= _MAX_ARGS else None


def _symbol_type(project, caller: str, ref: str) -> str | None:
    clean = _clean(ref)
    scope = f"program:{caller}".casefold()
    for tag in project.tags:
        if tag.scope.casefold() == scope and tag.name.casefold() == clean.casefold():
            return _type_name(tag.data_type)
    for tag in project.tags:
        if (
            tag.scope.casefold() == "controller"
            and tag.name.casefold() == clean.casefold()
        ):
            return _type_name(tag.data_type)
    return None


def _simple_actual(
    project,
    caller: str,
    value: str,
    *,
    allow_literal: bool,
) -> tuple[str, str | None]:
    raw = str(value or "").strip()
    if allow_literal and _LITERAL.fullmatch(raw):
        return raw.upper(), "LITERAL"
    if not _SIMPLE_REF.fullmatch(raw):
        return "", None
    ref = _clean(raw)
    dtype = _symbol_type(project, caller, ref)
    if dtype is None:
        return "", None
    return ref, dtype


def _top_level_call_ids(project) -> set[str]:
    result: set[str] = set()
    for statements in _v2._group_scl_statements(project).values():
        depth = 0
        for statement in statements:
            text = statement.text.strip()
            if _CONTROL_CLOSE.match(text):
                depth = max(0, depth - 1)
                continue
            call = _CALL.match(text)
            if call and depth == 0:
                result.add(statement.id)
            if _CONTROL_OPEN.match(text):
                depth += 1
    return result


def _block_maps(blocks: tuple[PLCBlock, ...]):
    by_name: dict[str, list[PLCBlock]] = defaultdict(list)
    for block in blocks:
        by_name[block.name.casefold()].append(block)
    return by_name


def _param_map(block: PLCBlock) -> dict[str, PLCBlockParameter]:
    return {item.name.casefold(): item for item in block.parameters}


def _parse_bindings(project, caller: PLCBlock, callee: PLCBlock, arg_text: str):
    parts = _split_args(arg_text)
    if parts is None:
        return (), "call_argument_grammar_unsupported"
    params = _param_map(callee)
    bindings: list[PLCParameterBinding] = []
    seen: set[str] = set()
    for part in parts:
        operator = "=>" if "=>" in part else ":=" if ":=" in part else ""
        if not operator:
            return tuple(bindings), "positional_call_binding_unsupported"
        formal_raw, actual_raw = part.split(operator, 1)
        formal = _clean(formal_raw)
        param = params.get(formal.casefold())
        if param is None:
            return tuple(bindings), f"unknown_formal:{formal}"
        if formal.casefold() in seen:
            return tuple(bindings), f"duplicate_formal:{formal}"
        seen.add(formal.casefold())
        expected_operator = "=>" if param.direction == "VAR_OUTPUT" else ":="
        if param.direction == "VAR_STAT" or operator != expected_operator:
            return tuple(bindings), f"invalid_binding_direction:{formal}"
        actual, actual_type = _simple_actual(
            project,
            caller.name,
            actual_raw,
            allow_literal=param.direction == "VAR_INPUT",
        )
        if not actual:
            return tuple(bindings), f"complex_or_unknown_actual:{formal}"
        if param.direction in {"VAR_OUTPUT", "VAR_IN_OUT"} and actual_type == "LITERAL":
            return tuple(bindings), f"non_addressable_actual:{formal}"
        bindings.append(
            PLCParameterBinding(formal, actual, param.direction, operator)
        )
    required = {
        item.name.casefold()
        for item in callee.parameters
        if item.direction == "VAR_IN_OUT"
        or (item.direction == "VAR_INPUT" and not item.has_default)
    }
    missing = sorted(required - seen)
    if missing:
        return tuple(bindings), f"missing_required_binding:{','.join(missing)}"
    return tuple(bindings), "bound"


def _multi_instance_type(caller: PLCBlock, symbol: str) -> str | None:
    for param in caller.parameters:
        if (
            param.direction == "VAR_STAT"
            and param.name.casefold() == symbol.casefold()
        ):
            return _type_name(param.data_type)
    return None


def _resolve_call(
    project,
    statement,
    caller: PLCBlock,
    by_name: dict[str, list[PLCBlock]],
    instances: dict[str, PLCInstanceDB],
    top_level: set[str],
) -> PLCCallBinding | None:
    match = _CALL.match(statement.text)
    if match is None:
        return None
    symbol = _clean(match.group("name"))
    call_id = f"SIEMENS-CALL:{statement.id}"
    if statement.id not in top_level:
        return PLCCallBinding(
            call_id,
            caller.name,
            symbol,
            None,
            None,
            (),
            statement.source,
            PLCSemanticState.PARTIAL,
            "call_inside_unmodeled_control",
        )

    candidates: list[tuple[PLCBlock, str | None, str]] = []
    direct = [
        item for item in by_name.get(symbol.casefold(), []) if item.kind == "FC"
    ]
    for item in direct:
        candidates.append((item, None, "direct_fc"))

    instance = instances.get(symbol.casefold())
    if instance is not None:
        for item in by_name.get(instance.block_type.casefold(), []):
            if item.kind == "FB":
                candidates.append((item, instance.name, "instance_db"))

    multi_type = _multi_instance_type(caller, symbol)
    if multi_type:
        for item in by_name.get(multi_type.casefold(), []):
            if item.kind == "FB":
                candidates.append(
                    (item, f"{caller.name}.{symbol}", "multi_instance")
                )

    unique = {
        (
            item.name.casefold(),
            (instance_name or "").casefold(),
            mode,
        ): (item, instance_name, mode)
        for item, instance_name, mode in candidates
    }
    if len(unique) != 1:
        return PLCCallBinding(
            call_id,
            caller.name,
            symbol,
            None,
            None,
            (),
            statement.source,
            PLCSemanticState.PARTIAL,
            "ambiguous_or_unresolved_target",
        )

    callee, instance_name, mode = next(iter(unique.values()))
    if callee.source_protected:
        return PLCCallBinding(
            call_id,
            caller.name,
            symbol,
            callee.name,
            instance_name,
            (),
            statement.source,
            PLCSemanticState.PARTIAL,
            "callee_protected",
        )
    bindings, status = _parse_bindings(
        project,
        caller,
        callee,
        match.group("args"),
    )
    semantic = (
        PLCSemanticState.FULL
        if status == "bound"
        else PLCSemanticState.PARTIAL
    )
    resolution = mode if semantic is PLCSemanticState.FULL else status
    return PLCCallBinding(
        call_id,
        caller.name,
        symbol,
        callee.name,
        instance_name,
        bindings,
        statement.source,
        semantic,
        resolution,
    )


def _recursive_nodes(calls: list[PLCCallBinding]) -> set[str]:
    graph: dict[str, set[str]] = defaultdict(set)
    nodes: set[str] = set()
    for call in calls:
        if call.callee_block is None:
            continue
        source = call.caller_block.casefold()
        target = call.callee_block.casefold()
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
    blocks: tuple[PLCBlock, ...],
    calls: list[PLCCallBinding],
):
    roots = {item.name.casefold() for item in blocks if item.kind == "OB"}
    outgoing: dict[str, list[PLCCallBinding]] = defaultdict(list)
    for call in calls:
        outgoing[call.caller_block.casefold()].append(call)
    reachable = set(roots)
    queue = list(sorted(roots))
    while queue:
        caller = queue.pop(0)
        for call in outgoing.get(caller, ()):
            if call.callee_block is None:
                continue
            target = call.callee_block.casefold()
            if target not in reachable:
                reachable.add(target)
                queue.append(target)
    callable_blocks = {
        item.name.casefold(): item.name
        for item in blocks
        if item.kind in {"FB", "FC"}
    }
    unreachable = tuple(
        sorted(
            (
                name
                for folded, name in callable_blocks.items()
                if folded not in reachable
            ),
            key=str.casefold,
        )
    )
    names = {item.name.casefold(): item.name for item in blocks}
    reachable_names = tuple(
        sorted(
            (names.get(item, item) for item in reachable),
            key=str.casefold,
        )
    )
    active_gaps = tuple(
        call.id
        for call in calls
        if call.caller_block.casefold() in reachable
        and call.semantic_state is not PLCSemanticState.FULL
    )
    return reachable_names, unreachable, active_gaps


def _binding_refs(call: PLCCallBinding):
    reads: list[str] = []
    writes: list[str] = []
    for item in call.bindings:
        if item.actual in {"TRUE", "FALSE"} or _LITERAL.fullmatch(item.actual):
            continue
        if (
            item.direction in {"VAR_INPUT", "VAR_IN_OUT"}
            and item.actual not in reads
        ):
            reads.append(item.actual)
        if (
            item.direction in {"VAR_OUTPUT", "VAR_IN_OUT"}
            and item.actual not in writes
        ):
            writes.append(item.actual)
    return tuple(reads), tuple(writes)


def _update_call_statements(project, calls: list[PLCCallBinding]):
    by_id = {
        call.id.removeprefix("SIEMENS-CALL:"): call
        for call in calls
    }
    updated = []
    for statement in project.logic_statements:
        call = by_id.get(statement.id)
        if call is None:
            updated.append(statement)
            continue
        reads, writes = _binding_refs(call)
        updated.append(
            replace(
                statement,
                reads=reads,
                writes=writes,
                calls=(call.callee_block or call.call_symbol,),
                semantic_state=call.semantic_state,
            )
        )
    project.logic_statements = updated
    project.instruction_semantic_count = sum(
        item.semantic_state is PLCSemanticState.FULL
        for item in project.logic_statements
    )
    scl = [item for item in project.logic_statements if item.language == "SCL"]
    project.st_statement_total = len(scl)
    project.st_statement_semantic_count = sum(
        item.semantic_state is PLCSemanticState.FULL for item in scl
    )
    project.partially_modeled_instruction_names = sorted(
        {
            item.language
            for item in project.logic_statements
            if item.semantic_state is not PLCSemanticState.FULL
        }
    )


def _identity(project, caller: str, ref: str) -> tuple[str, str]:
    scope = f"program:{caller}".casefold()
    clean = _clean(ref)
    for tag in project.tags:
        if tag.scope.casefold() == scope and tag.name.casefold() == clean.casefold():
            return scope, clean.casefold()
    for tag in project.tags:
        if (
            tag.scope.casefold() == "controller"
            and tag.name.casefold() == clean.casefold()
        ):
            return "controller", clean.casefold()
    return "symbol", f"{caller.casefold()}::{clean.casefold()}"


def _writer_conflicts(
    project,
    calls: list[PLCCallBinding],
    reachable: set[str],
) -> tuple[set[str], tuple[str, ...]]:
    targets: dict[tuple[str, str], list[PLCCallBinding]] = defaultdict(list)
    direct: dict[tuple[str, str], list[str]] = defaultdict(list)
    for statement in project.logic_statements:
        if _CALL.match(statement.text):
            continue
        caller = statement.source.program or statement.owner_name
        if not caller:
            continue
        for ref in statement.writes:
            direct[_identity(project, caller, ref)].append(statement.id)

    for call in calls:
        if call.caller_block.casefold() not in reachable:
            continue
        for binding in call.bindings:
            if binding.direction != "VAR_OUTPUT":
                continue
            targets[_identity(project, call.caller_block, binding.actual)].append(call)

    blocked_calls: set[str] = set()
    labels: list[str] = []
    for identity, writers in targets.items():
        count = len(writers) + len(direct.get(identity, ()))
        if count <= 1:
            continue
        labels.append(f"{identity[0]}::{identity[1]}")
        blocked_calls.update(item.id for item in writers)
    return blocked_calls, tuple(sorted(labels))


def _bool_type(value: str) -> bool:
    return _type_name(value).upper() in _BOOL_TYPES


def _project_logic(
    project,
    facts_calls: list[PLCCallBinding],
    blocks: tuple[PLCBlock, ...],
    reachable: set[str],
    blocked_calls: set[str],
):
    block_by_name = {item.name.casefold(): item for item in blocks}
    projected_ids: list[str] = []
    existing = {item.id for item in project.output_logic}
    for _ in range(max(1, len(blocks) + 1)):
        new_items: list[PLCOutputLogic] = []
        by_program: dict[str, list[PLCOutputLogic]] = defaultdict(list)
        for logic in project.output_logic:
            if (
                logic.semantic_state is PLCSemanticState.FULL
                and logic.source.program
            ):
                by_program[logic.source.program.casefold()].append(logic)

        for call in facts_calls:
            if (
                call.semantic_state is not PLCSemanticState.FULL
                or call.callee_block is None
                or call.id in blocked_calls
                or call.caller_block.casefold() not in reachable
            ):
                continue
            callee = block_by_name.get(call.callee_block.casefold())
            if callee is None:
                continue
            params = _param_map(callee)
            binding_by_formal = {
                item.formal.casefold(): item for item in call.bindings
            }
            for logic in by_program.get(callee.name.casefold(), ()):
                output_param = params.get(logic.output_tag.casefold())
                if (
                    output_param is None
                    or output_param.direction != "VAR_OUTPUT"
                    or not _bool_type(output_param.data_type)
                ):
                    continue
                output_binding = binding_by_formal.get(
                    output_param.name.casefold()
                )
                if output_binding is None:
                    continue
                actual_output_type = _symbol_type(
                    project,
                    call.caller_block,
                    output_binding.actual,
                )
                if actual_output_type is None or not _bool_type(actual_output_type):
                    continue

                projected_paths: list[PLCLogicPath] = []
                valid = True
                for path in logic.paths:
                    terms = []
                    contradiction = False
                    for term in path.terms:
                        formal = params.get(term.tag.casefold())
                        if (
                            formal is None
                            or formal.direction != "VAR_INPUT"
                            or not _bool_type(formal.data_type)
                        ):
                            valid = False
                            break
                        binding = binding_by_formal.get(formal.name.casefold())
                        if binding is None:
                            valid = False
                            break
                        if binding.actual in {"TRUE", "FALSE"}:
                            value = binding.actual == "TRUE"
                            if value != term.required:
                                contradiction = True
                                break
                            continue
                        actual_type = _symbol_type(
                            project,
                            call.caller_block,
                            binding.actual,
                        )
                        if actual_type is None or not _bool_type(actual_type):
                            valid = False
                            break
                        terms.append(replace(term, tag=binding.actual))
                    if not valid:
                        break
                    if not contradiction:
                        projected_paths.append(PLCLogicPath(tuple(terms)))
                if not valid:
                    continue

                signature = (
                    f"{call.id}:{logic.id}:{output_binding.actual}:"
                    + repr(
                        [
                            [
                                (term.tag.casefold(), term.required)
                                for term in path.terms
                            ]
                            for path in projected_paths
                        ]
                    )
                )
                digest = hashlib.sha1(signature.encode("utf-8")).hexdigest()[:14]
                logic_id = f"SIEMENS-CALL3-{digest}"
                if logic_id in existing:
                    continue
                new_items.append(
                    PLCOutputLogic(
                        id=logic_id,
                        output_tag=output_binding.actual,
                        instruction="ASSIGN_BOOL",
                        paths=tuple(projected_paths),
                        source=call.source,
                        language="SCL",
                        origin=(
                            f"SIEMENS_SCL_CALL_V3:{call.id}:{logic.id}"
                        ),
                        semantic_state=PLCSemanticState.FULL,
                    )
                )
                existing.add(logic_id)
                projected_ids.append(logic_id)
                if len(projected_ids) > _MAX_PROJECTED_LOGIC:
                    return tuple(projected_ids)
        if not new_items:
            break
        project.output_logic.extend(new_items)
    return tuple(projected_ids)


def _augment_graph(
    base: PLCDependencyGraph,
    facts: SiemensV3Facts,
) -> PLCDependencyGraph:
    edges = list(base.edges)
    seen = {
        (item.source, item.target, item.kind, item.evidence_id)
        for item in edges
    }

    def add(source: str, target: str, kind: str, evidence: str) -> None:
        key = (source, target, kind, evidence)
        if key not in seen:
            seen.add(key)
            edges.append(PLCDependencyEdge(source, target, kind, evidence))

    reachable = {item.casefold() for item in facts.reachable_blocks}
    for call in facts.calls:
        caller = f"BLOCK:{call.caller_block}"
        target = f"BLOCK:{call.callee_block or call.call_symbol}"
        add(caller, target, "CALLS_BLOCK", call.id)
        if (
            call.callee_block
            and call.caller_block.casefold() in reachable
        ):
            add(caller, target, "REACHABLE_CALL", call.id)
        if call.instance_db:
            add(
                call.id,
                f"INSTANCE_DB:{call.instance_db}",
                "USES_INSTANCE_DB",
                call.id,
            )
        for binding in call.bindings:
            add(
                call.id,
                binding.actual,
                f"BINDS_{binding.direction.removeprefix('VAR_')}",
                call.id,
            )
    return PLCDependencyGraph(
        edges=edges,
        unknown_instruction_names=list(base.unknown_instruction_names),
    )


def _call_gap_fat(project, facts: SiemensV3Facts) -> list[FATTestCase]:
    active = set(facts.active_call_gaps)
    tests: list[FATTestCase] = []
    for call in facts.calls:
        if call.id not in active:
            continue
        digest = hashlib.sha1(
            f"{call.id}:{call.resolution}".encode()
        ).hexdigest()[:10]
        tests.append(
            FATTestCase(
                id=f"FAT-SIEMENS-CALL-{digest}",
                title=(
                    f"Verify Siemens call binding for {call.call_symbol} "
                    f"at {call.source.locator}"
                ),
                source=call.source,
                output_tag=call.callee_block or call.call_symbol,
                preconditions={},
                expected=(
                    "Engineer evidence must confirm the intended called block/instance, "
                    "parameter mapping, and observed behavior for this source-linked call."
                ),
                method="RUNTIME_FAT_REQUIRED",
                scenario="SIEMENS_CALL_RUNTIME",
                limitations=(
                    f"Static call proof withheld: {call.resolution}.",
                    "DevAgent does not execute PLCSIM, HIL, or a real PLC.",
                ),
            )
        )
    return enrich_fat_procedures(project, tests)


def _facts(project) -> SiemensV3Facts | None:
    return getattr(project, "_siemens_v3_facts", None)


def siemens_capability_profile_v3(project) -> dict[str, object]:
    facts = _facts(project)
    if facts is None:
        return dict(_PREVIOUS_CAPABILITY(project))
    profile = dict(_PREVIOUS_CAPABILITY(project))
    profile.update(
        {
            "schema": "devagent-siemens-tia-capability-v3",
            "blocks": len(facts.blocks),
            "instance_dbs": len(facts.instance_dbs),
            "calls": len(facts.calls),
            "calls_bound": sum(
                item.semantic_state is PLCSemanticState.FULL
                for item in facts.calls
            ),
            "reachable_blocks": list(facts.reachable_blocks),
            "unreachable_blocks": list(facts.unreachable_blocks),
            "recursive_blocks": list(facts.recursive_blocks),
            "cross_block_writer_conflicts": list(facts.writer_conflicts),
            "execution_closure": (
                "COMPLETE"
                if not facts.active_call_gaps and not facts.recursive_blocks
                else "PARTIAL_FAIL_CLOSED"
            ),
            "projected_call_theorems": len(facts.projected_logic_ids),
            "bounded_call_semantics": (
                "top-level named FC calls and FB instance/multi-instance calls with "
                "exact interface binding; cross-block Boolean output proof is projected "
                "only through reachable, acyclic, uniquely-owned calls"
            ),
        }
    )
    return profile


def _v3_checks(project, facts: SiemensV3Facts) -> list[StaticCheck]:
    calls = len(facts.calls)
    bound = sum(
        item.semantic_state is PLCSemanticState.FULL
        for item in facts.calls
    )
    return [
        StaticCheck(
            "SIEMENS_V3_CALL_BINDING",
            (
                StaticCheckStatus.PASS
                if calls == bound
                else StaticCheckStatus.NOT_PROVEN
            ),
            (
                f"Deterministically bound {bound}/{calls} discovered Siemens SCL "
                "call(s) to FB/FC targets and named interfaces."
            ),
            tuple(item.id for item in facts.calls),
        ),
        StaticCheck(
            "SIEMENS_V3_EXECUTION_CLOSURE",
            (
                StaticCheckStatus.PASS
                if not facts.active_call_gaps and not facts.recursive_blocks
                else StaticCheckStatus.NOT_PROVEN
            ),
            (
                f"OB-rooted execution closure reaches {len(facts.reachable_blocks)} "
                f"block(s); active unresolved/partial calls={len(facts.active_call_gaps)}, "
                f"recursive blocks={len(facts.recursive_blocks)}."
            ),
            tuple(facts.active_call_gaps),
        ),
        StaticCheck(
            "SIEMENS_V3_UNREACHABLE_BLOCKS",
            (
                StaticCheckStatus.WARN
                if facts.unreachable_blocks
                else StaticCheckStatus.PASS
            ),
            (
                f"{len(facts.unreachable_blocks)} imported FB/FC block(s) are not "
                "reachable from an OB through resolved call targets."
                if facts.unreachable_blocks
                else "Every imported FB/FC block is reachable from an OB through the resolved call graph."
            ),
            tuple(f"BLOCK:{item}" for item in facts.unreachable_blocks),
        ),
        StaticCheck(
            "SIEMENS_V3_CROSS_BLOCK_WRITERS",
            (
                StaticCheckStatus.NOT_PROVEN
                if facts.writer_conflicts
                else StaticCheckStatus.PASS
            ),
            (
                f"{len(facts.writer_conflicts)} reachable call output target(s) have "
                "competing direct/call writers."
                if facts.writer_conflicts
                else "No competing reachable direct/call writers were found for projected call outputs."
            ),
            facts.writer_conflicts,
        ),
    ]


def analyze_siemens_tia_v3(path: Path) -> PLCEngineeringResult:
    base = _PREVIOUS_ANALYZER(path)
    project = base.project
    blocks, instance_dbs = _inventory(path, project)
    if (
        not any(item.kind in {"FB", "FC"} for item in blocks)
        and not any(_CALL.match(item.text) for item in project.logic_statements)
    ):
        return base

    by_name = _block_maps(blocks)
    block_by_name = {item.name.casefold(): item for item in blocks}
    instances = {item.name.casefold(): item for item in instance_dbs}
    top_level = _top_level_call_ids(project)
    calls: list[PLCCallBinding] = []
    call_statements = [
        item for item in project.logic_statements if _CALL.match(item.text)
    ]
    if len(call_statements) > _MAX_CALLS:
        project.warnings.append(
            f"Siemens V3 call inventory exceeds {_MAX_CALLS} bounded calls; remaining call behavior is withheld."
        )
    for statement in call_statements[:_MAX_CALLS]:
        caller = block_by_name.get(
            (statement.source.program or statement.owner_name).casefold()
        )
        if caller is None:
            continue
        resolved = _resolve_call(
            project,
            statement,
            caller,
            by_name,
            instances,
            top_level,
        )
        if resolved is not None:
            calls.append(resolved)

    recursive = _recursive_nodes(calls)
    if recursive:
        calls = [
            replace(
                call,
                semantic_state=PLCSemanticState.PARTIAL,
                resolution="recursive_call_cycle_unsupported",
            )
            if call.callee_block
            and call.caller_block.casefold() in recursive
            and call.callee_block.casefold() in recursive
            else call
            for call in calls
        ]

    reachable_names, unreachable, active_gaps = _reachability(blocks, calls)
    reachable = {item.casefold() for item in reachable_names}
    blocked_calls, writer_conflicts = _writer_conflicts(
        project,
        calls,
        reachable,
    )

    if blocked_calls:
        calls = [
            replace(
                call,
                semantic_state=PLCSemanticState.PARTIAL,
                resolution="competing_output_writer",
            )
            if call.id in blocked_calls
            else call
            for call in calls
        ]
        reachable_names, unreachable, active_gaps = _reachability(blocks, calls)
        reachable = {item.casefold() for item in reachable_names}

    _update_call_statements(project, calls)
    projected = _project_logic(
        project,
        calls,
        blocks,
        reachable,
        blocked_calls,
    )

    facts = SiemensV3Facts(
        blocks=blocks,
        instance_dbs=instance_dbs,
        calls=tuple(calls),
        reachable_blocks=reachable_names,
        unreachable_blocks=unreachable,
        active_call_gaps=active_gaps,
        recursive_blocks=tuple(
            sorted(
                (
                    block_by_name.get(item).name
                    if block_by_name.get(item)
                    else item
                )
                for item in recursive
            )
        ),
        writer_conflicts=writer_conflicts,
        projected_logic_ids=projected,
    )
    setattr(project, "_siemens_v3_facts", facts)
    project.metadata = replace(
        project.metadata,
        schema_revision="SIEMENS-TIA-EXPORT-V3",
    )

    graph = _augment_graph(build_dependency_graph(project), facts)
    fat_tests = _v1._siemens_fat_tests(project)
    fat_tests.extend(_call_gap_fat(project, facts))
    fat_by_id = {item.id: item for item in fat_tests}
    fat_tests = list(fat_by_id.values())
    checks = _v1._siemens_checks(project, graph, fat_tests)
    checks.extend(_v3_checks(project, facts))
    profile = siemens_capability_profile_v3(project)
    outcome = (
        PLCOutcome.STATICALLY_VERIFIED
        if profile["static_contract"] == "COMPLETE"
        and profile["execution_closure"] == "COMPLETE"
        and not writer_conflicts
        else PLCOutcome.PARTIALLY_VERIFIED
    )
    if profile["static_contract"] == "NO_EXECUTABLE_LOGIC":
        outcome = PLCOutcome.BLOCKED

    limitations = [
        item.replace("Siemens V2", "Siemens V3")
        for item in base.limitations
        if "calls" not in item.casefold()
    ]
    limitations.append(
        "Siemens V3 proves only bounded top-level named SCL calls: direct FC, FB instance-DB, and FB VAR_STAT multi-instance bindings. Guarded/positional/complex/ambiguous/protected/recursive calls remain PARTIAL and require engineer FAT."
    )
    limitations.append(
        "Cross-block Boolean requirement proof is emitted only when block identity, instance identity when required, named parameter bindings, OB reachability, callee Boolean theorem, Boolean types, and reachable writer uniqueness are all proven."
    )
    return PLCEngineeringResult(
        outcome,
        project,
        graph,
        fat_tests,
        checks,
        list(dict.fromkeys(limitations)),
    )


def _projected_logic(project):
    facts = _facts(project)
    if facts is None:
        return {}
    ids = set(facts.projected_logic_ids)
    return {
        item.id: item
        for item in project.output_logic
        if item.id in ids
    }


def _local_callable_proof_ids(project) -> set[str]:
    facts = _facts(project)
    if facts is None:
        return set()
    callable_names = {
        item.name.casefold()
        for item in facts.blocks
        if item.kind in {"FB", "FC"}
    }
    projected = set(facts.projected_logic_ids)
    return {
        logic.id
        for logic in project.output_logic
        if logic.id not in projected
        and (logic.source.program or "").casefold() in callable_names
    }


def _v3_verify_requirement(
    previous,
    requirement,
    engineering,
    evidence,
    tests,
):
    result = previous(requirement, engineering, evidence, tests)
    if str(engineering.project.metadata.vendor).casefold() != "siemens":
        return result
    project = engineering.project
    facts = _facts(project)
    if facts is None:
        return result

    projected = _projected_logic(project)
    projected_evidence = [
        item for item in result.evidence_ids if item in projected
    ]
    local_proof = _local_callable_proof_ids(project)
    if (
        result.status
        in {RequirementStatus.STATICALLY_VERIFIED, RequirementStatus.CONFLICT}
        and not projected_evidence
        and any(item in local_proof for item in result.evidence_ids)
    ):
        return RequirementVerification(
            result.requirement_id,
            RequirementStatus.TRACEABLE_NOT_PROVEN,
            (
                "A deterministic local Siemens FB/FC theorem matches this requirement, "
                "but V3 withholds cross-block proof because no reachable, uniquely-bound "
                "OB call projection proves that local interface theorem at the required "
                "caller symbol."
            ),
            result.evidence_ids,
            result.matched_tags,
            result.linked_test_ids,
            result.confidence,
            result.ai_assisted,
        )

    if (
        projected_evidence
        and result.status
        in {RequirementStatus.STATICALLY_VERIFIED, RequirementStatus.CONFLICT}
    ):
        call_ids: list[str] = []
        for logic_id in projected_evidence:
            logic = projected[logic_id]
            for call in facts.calls:
                if f":{call.id}:" in logic.origin and call.id not in call_ids:
                    call_ids.append(call.id)
        return replace(
            result,
            summary=(
                result.summary
                + " Siemens V3 additionally proves the referenced FB/FC call target, "
                "instance identity when required, named interface bindings, OB reachability, "
                "Boolean type compatibility, and reachable writer uniqueness."
            ),
            evidence_ids=tuple(
                dict.fromkeys([*result.evidence_ids, *call_ids])
            ),
        )
    return result


def _v3_evidence(previous, engineering):
    items = list(previous(engineering))
    project = engineering.project
    facts = _facts(project)
    if facts is None:
        return items
    existing = {item.id for item in items}
    reachable = {item.casefold() for item in facts.reachable_blocks}
    for block in facts.blocks:
        if block.id in existing:
            continue
        items.append(
            EvidenceItem(
                block.id,
                "SIEMENS_BLOCK",
                f"{block.kind} {block.name} interface with {len(block.parameters)} parameter(s).",
                block.source.locator,
                project.metadata.source_sha256,
                {
                    "name": block.name,
                    "kind": block.kind,
                    "language": block.language,
                    "source_protected": block.source_protected,
                    "parameters": [
                        {
                            "name": item.name,
                            "direction": item.direction,
                            "data_type": item.data_type,
                            "has_default": item.has_default,
                        }
                        for item in block.parameters
                    ],
                    "reachable_from_ob": block.name.casefold() in reachable,
                },
            )
        )
    for instance in facts.instance_dbs:
        items.append(
            EvidenceItem(
                instance.id,
                "SIEMENS_INSTANCE_DB",
                f"Instance DB {instance.name} binds FB type {instance.block_type}.",
                instance.source.locator,
                project.metadata.source_sha256,
                {
                    "name": instance.name,
                    "block_type": instance.block_type,
                },
            )
        )
    for call in facts.calls:
        items.append(
            EvidenceItem(
                call.id,
                "SIEMENS_CALL_BINDING",
                (
                    f"{call.caller_block} -> {call.callee_block or call.call_symbol}: "
                    f"{call.semantic_state.value} ({call.resolution})."
                ),
                call.source.locator,
                project.metadata.source_sha256,
                {
                    "caller": call.caller_block,
                    "call_symbol": call.call_symbol,
                    "callee": call.callee_block,
                    "instance_db": call.instance_db,
                    "semantic_state": call.semantic_state.value,
                    "resolution": call.resolution,
                    "bindings": [
                        {
                            "formal": item.formal,
                            "actual": item.actual,
                            "direction": item.direction,
                            "operator": item.operator,
                        }
                        for item in call.bindings
                    ],
                },
            )
        )
    return items


def _v3_risks(
    previous,
    engineering,
    verifications,
    executions,
    engineering_findings,
):
    risks = list(
        previous(
            engineering,
            verifications,
            executions,
            engineering_findings,
        )
    )
    project = engineering.project
    facts = _facts(project)
    if facts is None:
        return risks
    active = set(facts.active_call_gaps)
    for call in facts.calls:
        if call.id not in active:
            continue
        risks.append(
            RiskFinding(
                stable_id("RISK", "SIEMENS_CALL_BINDING", call.id),
                "CALL_BINDING",
                f"Siemens call is not deterministically bound: {call.call_symbol}",
                Severity.HIGH,
                f"V3 withheld call proof at {call.source.locator}: {call.resolution}.",
                (
                    "Downstream block behavior and requirement/FAT traceability may depend "
                    "on an unresolved target, instance, interface, control context, or recursive path."
                ),
                (
                    "Correct/export the exact call/interface/instance evidence or execute "
                    "the generated engineer FAT procedure; do not promote this call to static verification."
                ),
                (call.id,),
            )
        )
    if facts.unreachable_blocks:
        risks.append(
            RiskFinding(
                stable_id(
                    "RISK",
                    "SIEMENS_UNREACHABLE",
                    *facts.unreachable_blocks,
                ),
                "UNREACHABLE_LOGIC",
                "Imported Siemens FB/FC blocks are unreachable from an OB",
                Severity.MEDIUM,
                (
                    f"{len(facts.unreachable_blocks)} block(s) are not reached by the "
                    f"resolved OB-rooted call graph: {', '.join(facts.unreachable_blocks[:8])}."
                ),
                (
                    "Unreachable implementation may be dead/obsolete logic or may indicate "
                    "an incomplete export/call binding; its local theorem must not prove active machine behavior."
                ),
                (
                    "Confirm intended execution entrypoints and remove, document, or correctly "
                    "bind unreachable blocks before relying on them for requirements."
                ),
                tuple(f"BLOCK:{item}" for item in facts.unreachable_blocks),
            )
        )
    if facts.writer_conflicts:
        risks.append(
            RiskFinding(
                stable_id(
                    "RISK",
                    "SIEMENS_CROSS_BLOCK_WRITER",
                    *facts.writer_conflicts,
                ),
                "MULTIPLE_WRITERS",
                "Competing reachable Siemens writers block cross-block proof",
                Severity.HIGH,
                (
                    f"{len(facts.writer_conflicts)} projected call output target(s) have "
                    "more than one reachable direct/call writer."
                ),
                (
                    "Final value can depend on scan/call order or arbitration outside the bounded V3 theorem."
                ),
                (
                    "Disposition writer ownership and execution order; rerun affected requirement "
                    "verification and FAT after the conflict is resolved."
                ),
                facts.writer_conflicts,
            )
        )
    if facts.recursive_blocks:
        risks.append(
            RiskFinding(
                stable_id(
                    "RISK",
                    "SIEMENS_RECURSION",
                    *facts.recursive_blocks,
                ),
                "CALL_RECURSION",
                "Recursive Siemens call cycle is outside V3 execution closure",
                Severity.HIGH,
                (
                    f"Recursive/cyclic call graph includes: {', '.join(facts.recursive_blocks)}."
                ),
                "A bounded acyclic execution closure cannot be established.",
                (
                    "Refactor/disposition the recursion or retain the path as runtime-dependent "
                    "and validate it with explicit engineer evidence."
                ),
                tuple(f"BLOCK:{item}" for item in facts.recursive_blocks),
            )
        )
    return risks


def _v3_semantic_section(previous, project) -> str:
    base = previous(project)
    facts = _facts(project)
    if facts is None:
        return base
    profile = siemens_capability_profile_v3(project)
    insertion = (
        "### Siemens V3 Call/Interface Execution Closure\n\n"
        f"- Canonical OB/FB/FC blocks: **{profile['blocks']}**\n"
        f"- Instance DBs: **{profile['instance_dbs']}**\n"
        f"- Deterministically bound calls: **{profile['calls_bound']}/{profile['calls']}**\n"
        f"- OB-rooted execution closure: **{profile['execution_closure']}**\n"
        f"- Reachable blocks: **{len(profile['reachable_blocks'])}**\n"
        f"- Unreachable FB/FC blocks: **{len(profile['unreachable_blocks'])}**\n"
        f"- Cross-block Boolean theorem projections: **{profile['projected_call_theorems']}**\n"
        "- Requirement PASS across FB/FC boundaries requires proven target identity, instance identity when applicable, named interface bindings, OB reachability, callee Boolean theorem, compatible Boolean types, and unique reachable writer ownership.\n"
        "- Guarded, positional, complex-expression, ambiguous, protected, recursive, or otherwise unsupported calls remain PARTIAL and receive engineer-executed FAT instead of static PASS.\n"
        "- V3 still does not execute PLCSIM, HIL, or a real PLC.\n\n"
    )
    marker = "### Explicit Siemens V2 Boundaries"
    if marker in base:
        return base.replace(marker, insertion + marker, 1)
    return base + "\n\n" + insertion.rstrip() + "\n"


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import siemens_integration_v1 as _integration

    previous_verify = _integration._siemens_verify_requirement
    previous_evidence = _integration._siemens_evidence_index
    previous_risks = _integration._siemens_detect_risks
    previous_section = _integration._siemens_semantic_section

    _v1.analyze_siemens_tia = analyze_siemens_tia_v3
    _v1.siemens_capability_profile = siemens_capability_profile_v3
    _dispatch.analyze_siemens_tia = analyze_siemens_tia_v3
    _integration.siemens_capability_profile = siemens_capability_profile_v3

    def verify_requirement(requirement, engineering, evidence, tests):
        return _v3_verify_requirement(
            previous_verify,
            requirement,
            engineering,
            evidence,
            tests,
        )

    def evidence_index(engineering):
        return _v3_evidence(previous_evidence, engineering)

    def detect_risks(
        engineering,
        verifications,
        executions,
        engineering_findings,
    ):
        return _v3_risks(
            previous_risks,
            engineering,
            verifications,
            executions,
            engineering_findings,
        )

    def semantic_section(project):
        return _v3_semantic_section(previous_section, project)

    _integration._siemens_verify_requirement = verify_requirement
    _integration._siemens_evidence_index = evidence_index
    _integration._siemens_detect_risks = detect_risks
    _integration._siemens_semantic_section = semantic_section
    _INSTALLED = True


__all__ = [
    "PLCBlock",
    "PLCBlockParameter",
    "PLCCallBinding",
    "PLCInstanceDB",
    "PLCParameterBinding",
    "SiemensV3Facts",
    "analyze_siemens_tia_v3",
    "install",
    "siemens_capability_profile_v3",
]
