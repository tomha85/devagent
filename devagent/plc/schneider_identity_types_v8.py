from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from devagent.plc import schneider_call_graph_v3 as _v3
from devagent.plc import schneider_control_expert_v1 as _v1
from devagent.plc import schneider_fault_recovery_v7 as _v7
from devagent.plc.models import (
    PLCDependencyEdge,
    PLCEngineeringResult,
    PLCOutcome,
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


_INSTALLED = False
_PREVIOUS_ANALYZER = _v7.analyze_schneider_control_expert_v7
_PREVIOUS_CAPABILITY = _v7.schneider_capability_profile_v7

_SIMPLE_TYPES = {
    "BOOL", "EBOOL", "BOOLEAN", "BYTE", "WORD", "DWORD", "LWORD",
    "SINT", "USINT", "INT", "UINT", "DINT", "UDINT", "LINT", "ULINT",
    "REAL", "LREAL", "TIME", "LTIME", "DATE", "TOD", "DT", "STRING",
}
_ARRAY = re.compile(r"^\s*ARRAY\s*\[(?P<dims>[^\]]+)\]\s*OF\s*(?P<element>.+?)\s*$", re.I | re.S)
_INDEXED = re.compile(
    r"(?:%[A-Za-z]+[A-Za-z0-9_.]*|[A-Za-z_][A-Za-z0-9_]*)"
    r"\[[^\]]+\](?:\.[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?)*"
)
_ADDRESS = re.compile(r"^%(?P<area>[IQMKS])(?P<width>[XWDLB]?)(?P<body>[0-9].*)$", re.I)
_MAX_DEPTH = 16
_MAX_MEMBERS = 4096
_MAX_RAW_VARIABLES = 20000


@dataclass(frozen=True)
class SchneiderV8TypeIdentity:
    id: str
    name: str
    kind: str
    element_type: str | None = None
    dimensions: tuple[str, ...] = ()
    members: tuple[tuple[str, str], ...] = ()
    enum_literals: tuple[str, ...] = ()
    origin: str = ""


@dataclass(frozen=True)
class SchneiderV8SymbolIdentity:
    id: str
    scope: str
    display_path: str
    canonical_path: tuple[str, ...]
    data_type: str
    type_id: str | None
    origin: str
    address: str | None = None
    address_kind: str | None = None
    io_area: str | None = None
    synthetic: bool = False


@dataclass(frozen=True)
class SchneiderV8ReferenceBinding:
    id: str
    statement_id: str
    access: str
    raw_ref: str
    canonical_symbol_id: str | None
    canonical_display: str | None
    resolution: str
    semantic_state: PLCSemanticState


@dataclass(frozen=True)
class SchneiderV8IOIdentity:
    id: str
    symbol_id: str
    symbol_display: str
    address: str
    address_kind: str
    io_area: str
    source: str


@dataclass(frozen=True)
class SchneiderV8DFBInstanceIdentity:
    id: str
    instance_id: str
    owner_kind: str
    owner_name: str
    instance_name: str
    block_type: str
    canonical_symbol_id: str | None
    type_id: str | None


@dataclass(frozen=True)
class SchneiderV8Facts:
    project_identity: str
    types: tuple[SchneiderV8TypeIdentity, ...]
    symbols: tuple[SchneiderV8SymbolIdentity, ...]
    bindings: tuple[SchneiderV8ReferenceBinding, ...]
    io_points: tuple[SchneiderV8IOIdentity, ...]
    dfb_instances: tuple[SchneiderV8DFBInstanceIdentity, ...]
    whole_member_overlaps: tuple[tuple[str, str], ...]
    physical_address_aliases: tuple[tuple[str, str], ...]
    ambiguous_references: tuple[str, ...]
    unresolved_references: tuple[str, ...]
    identity_conflicts: tuple[str, ...]
    address_conflicts: tuple[str, ...]


@dataclass(frozen=True)
class _RawVariable:
    name: str
    data_type: str
    address: str | None
    source: str


def _facts(project) -> SchneiderV8Facts | None:
    return getattr(project, "_schneider_v8_identity_facts", None)


def _clean(value: object) -> str:
    return str(value or "").strip().strip('"')


def _type_name(value: object) -> str:
    return _clean(value) or "UNKNOWN"


def _array(value: object):
    match = _ARRAY.match(_type_name(value))
    if not match:
        return None
    dims = tuple(item.strip() for item in match.group("dims").split(",") if item.strip())
    return dims, _type_name(match.group("element"))


def _dimension(element: ET.Element) -> str | None:
    for key in ("dimension", "arrayDimension", "dimensions", "arraySize", "size"):
        raw = (element.attrib.get(key) or "").strip()
        if raw:
            return raw
    return None


def _variable_dtype(element: ET.Element) -> str:
    dtype = _type_name(element.attrib.get("typeName") or element.attrib.get("type"))
    dim = _dimension(element)
    return f"ARRAY[{dim}] OF {dtype}" if dim and _array(dtype) is None else dtype


def _address_attrs(element: ET.Element) -> str | None:
    raw = (
        element.attrib.get("topologicalAddress")
        or element.attrib.get("locatedAddress")
        or element.attrib.get("address")
        or element.attrib.get("topological")
    )
    return _clean(raw) or None


def _normalize_address(value: str | None) -> str | None:
    return re.sub(r"\s+", "", value).upper() if value else None


def _address_kind(value: str | None) -> str | None:
    normalized = _normalize_address(value)
    return ("LOCATED" if normalized.startswith("%") else "TOPOLOGICAL") if normalized else None


def _io_area(value: str | None) -> str:
    normalized = _normalize_address(value) or ""
    match = _ADDRESS.match(normalized)
    if not match:
        return "TOPOLOGICAL" if normalized else "NONE"
    return {"I": "INPUT", "Q": "OUTPUT", "M": "MEMORY", "K": "CONSTANT", "S": "SYSTEM"}.get(
        match.group("area").upper(), "LOCATED"
    )


def _address_dtype(value: str | None) -> str:
    normalized = _normalize_address(value) or ""
    match = _ADDRESS.match(normalized)
    if not match:
        return "UNKNOWN"
    return {"W": "WORD", "D": "DWORD", "L": "LWORD", "B": "BYTE"}.get(match.group("width").upper(), "BOOL")


def _split_ref(value: str) -> tuple[str, ...]:
    text = str(value or "").strip()
    if not text:
        return ()
    if text.startswith("%"):
        return (text,)
    return tuple(part.strip().strip('"') for part in text.split(".") if part.strip().strip('"'))


def _normalize_index(parts: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(re.sub(r"\[[^\]]+\]", "[*]", part).casefold() for part in parts)


def _raw_inventory(files):
    globals_by_name: dict[str, list[_RawVariable]] = defaultdict(list)
    raw_types: dict[str, SchneiderV8TypeIdentity] = {}
    conflicts: list[str] = []
    count = 0

    def add_global(element: ET.Element, relative: str):
        nonlocal count
        if count >= _MAX_RAW_VARIABLES:
            return
        name = _clean(element.attrib.get("name"))
        if not name:
            return
        count += 1
        globals_by_name[name.casefold()].append(
            _RawVariable(name, _variable_dtype(element), _address_attrs(element), relative)
        )

    def parse_ddt(element: ET.Element, relative: str):
        name = _clean(element.attrib.get("name") or element.attrib.get("typeName"))
        if not name:
            return
        members, seen = [], set()
        for child in element.iter():
            if _v1._local_name(child.tag) != "variables":
                continue
            member = _clean(child.attrib.get("name"))
            if not member or member.casefold() in seen:
                continue
            seen.add(member.casefold())
            members.append((member, _variable_dtype(child)))
            if len(members) >= _MAX_MEMBERS:
                break
        if members:
            raw_types.setdefault(
                name.casefold(),
                SchneiderV8TypeIdentity(
                    f"SCHNEIDER-TYPE8:DDT:{hashlib.sha1(name.casefold().encode()).hexdigest()[:16]}",
                    name,
                    "DDT",
                    members=tuple(members),
                    origin=relative,
                ),
            )

    def parse_enum(element: ET.Element, relative: str):
        local = _v1._local_name(element.tag).casefold()
        if local not in {"enum", "enumeratedtype", "enumeration", "enumtype", "enumsource"}:
            return
        name = _clean(element.attrib.get("name") or element.attrib.get("typeName"))
        if not name:
            return
        literals, seen = [], set()
        for child in element.iter():
            if _v1._local_name(child.tag).casefold() not in {"literal", "value", "enumerator", "enumvalue", "element"}:
                continue
            literal = _clean(child.attrib.get("name") or child.attrib.get("value") or child.text)
            if literal and literal.casefold() not in seen:
                seen.add(literal.casefold())
                literals.append(literal)
        if literals:
            raw_types.setdefault(
                name.casefold(),
                SchneiderV8TypeIdentity(
                    f"SCHNEIDER-TYPE8:ENUM:{hashlib.sha1(name.casefold().encode()).hexdigest()[:16]}",
                    name,
                    "ENUM",
                    enum_literals=tuple(literals),
                    origin=relative,
                ),
            )

    def walk(node: ET.Element, relative: str, context: str):
        local = _v1._local_name(node.tag).casefold()
        next_context = context
        if local in {"ddt", "deriveddatatype", "ddtsource"}:
            parse_ddt(node, relative)
            next_context = "TYPE"
        elif local == "fbsource":
            next_context = "DFB"
        elif local in {"program", "fbprogram"}:
            next_context = "PROGRAM"
        parse_enum(node, relative)
        if local == "variables" and context not in {"TYPE", "DFB"}:
            add_global(node, relative)
        for child in list(node):
            walk(child, relative, next_context)

    for source, relative in files:
        try:
            root = ET.parse(source).getroot()
        except ET.ParseError:
            continue
        walk(root, relative, "GLOBAL")

    for name, rows in globals_by_name.items():
        types = {row.data_type.casefold() for row in rows if row.data_type != "UNKNOWN"}
        addresses = {_normalize_address(row.address) for row in rows if row.address}
        if len(types) > 1:
            conflicts.append(f"TYPE:{name}:{'|'.join(sorted(types))}")
        if len(addresses) > 1:
            conflicts.append(f"ADDRESS:{name}:{'|'.join(sorted(addresses))}")
    return globals_by_name, raw_types, tuple(sorted(set(conflicts), key=str.casefold))


def _build_types(project, files) -> tuple[SchneiderV8TypeIdentity, ...]:
    _globals, raw_types, _conflicts = _raw_inventory(files)
    result = dict(raw_types)
    for dtype in project.data_types:
        name = _clean(dtype.name)
        members = tuple((member.name, _type_name(member.data_type)) for member in dtype.members)[:_MAX_MEMBERS]
        result.setdefault(
            name.casefold(),
            SchneiderV8TypeIdentity(
                f"SCHNEIDER-TYPE8:DDT:{hashlib.sha1(name.casefold().encode()).hexdigest()[:16]}",
                name,
                "DDT",
                members=members,
                origin=dtype.id,
            ),
        )
    v3facts = _v3._facts(project)
    if v3facts is not None:
        for dfb in v3facts.dfb_types:
            members = tuple((parameter.name, parameter.data_type) for parameter in dfb.parameters) + tuple(dfb.local_symbols)
            result.setdefault(
                dfb.name.casefold(),
                SchneiderV8TypeIdentity(
                    f"SCHNEIDER-TYPE8:DFB:{hashlib.sha1(dfb.name.casefold().encode()).hexdigest()[:16]}",
                    dfb.name,
                    "DFB",
                    members=members[:_MAX_MEMBERS],
                    origin=dfb.id,
                ),
            )
    referenced = [tag.data_type for tag in project.tags]
    referenced.extend(member_type for item in result.values() for _name, member_type in item.members)
    for raw in referenced:
        name = _type_name(raw)
        array = _array(name)
        if array:
            dims, element = array
            key = name.casefold()
            result.setdefault(
                key,
                SchneiderV8TypeIdentity(
                    f"SCHNEIDER-TYPE8:ARRAY:{hashlib.sha1(key.encode()).hexdigest()[:16]}",
                    name,
                    "ARRAY",
                    element_type=element,
                    dimensions=dims,
                    origin="REFERENCED_TYPE",
                ),
            )
        elif name.upper() in _SIMPLE_TYPES:
            result.setdefault(
                name.casefold(),
                SchneiderV8TypeIdentity(f"SCHNEIDER-TYPE8:SIMPLE:{name.upper()}", name.upper(), "SIMPLE", origin="BUILTIN"),
            )
    return tuple(sorted(result.values(), key=lambda item: (item.kind, item.name.casefold())))


def _symbol_id(scope: str, parts: tuple[str, ...]) -> str:
    key = f"{scope.casefold()}::" + ".".join(item.casefold() for item in parts)
    return f"SCHNEIDER-SYM8:{hashlib.sha1(key.encode()).hexdigest()[:18]}"


def _add_symbol(store, conflicts, *, scope, parts, dtype, type_id, origin, address=None, synthetic=False):
    if not parts:
        return None
    canonical = tuple(item.casefold() for item in parts)
    key = (scope.casefold(), canonical)
    symbol = SchneiderV8SymbolIdentity(
        _symbol_id(scope, parts), scope, ".".join(parts), canonical, _type_name(dtype), type_id, origin,
        _normalize_address(address), _address_kind(address), _io_area(address) if address else None, synthetic,
    )
    existing = store.get(key)
    if existing is None:
        store[key] = symbol
        return symbol
    same_type = existing.data_type.casefold() == symbol.data_type.casefold() or "UNKNOWN" in {existing.data_type.upper(), symbol.data_type.upper()}
    same_address = not existing.address or not symbol.address or existing.address == symbol.address
    if not same_type or not same_address:
        conflicts.append(
            f"SYMBOL:{existing.id}|{symbol.id}|{existing.data_type}|{symbol.data_type}|{existing.address or ''}|{symbol.address or ''}"
        )
    if existing.data_type == "UNKNOWN" and symbol.data_type != "UNKNOWN":
        store[key] = replace(existing, data_type=symbol.data_type, type_id=symbol.type_id or existing.type_id, origin=f"{existing.origin}|{symbol.origin}")
    if not store[key].address and symbol.address:
        store[key] = replace(
            store[key], address=symbol.address, address_kind=symbol.address_kind, io_area=symbol.io_area,
            origin=f"{store[key].origin}|{symbol.origin}",
        )
    return store[key]


def _expand(store, conflicts, type_map, *, scope, prefix, dtype, origin, depth=0, seen=()):
    if depth >= _MAX_DEPTH:
        return
    array = _array(dtype)
    if array:
        _dims, element = array
        wildcard = prefix[:-1] + (prefix[-1] + "[*]",)
        info = type_map.get(element.casefold())
        _add_symbol(store, conflicts, scope=scope, parts=wildcard, dtype=element, type_id=info.id if info else None, origin=origin, synthetic=True)
        _expand(store, conflicts, type_map, scope=scope, prefix=wildcard, dtype=element, origin=origin, depth=depth + 1, seen=seen)
        return
    info = type_map.get(_type_name(dtype).casefold())
    if not info or info.kind not in {"DDT", "DFB"} or info.name.casefold() in seen:
        return
    next_seen = (*seen, info.name.casefold())
    for member, member_type in info.members:
        child = (*prefix, member)
        child_info = type_map.get(_type_name(member_type).casefold())
        _add_symbol(store, conflicts, scope=scope, parts=child, dtype=member_type, type_id=child_info.id if child_info else None, origin=origin, synthetic=True)
        _expand(store, conflicts, type_map, scope=scope, prefix=child, dtype=member_type, origin=origin, depth=depth + 1, seen=next_seen)


def _raw_for_tag(raw_globals, tag):
    rows = list(raw_globals.get(tag.name.casefold(), ()))
    if not rows:
        return None
    exact = [row for row in rows if row.data_type.casefold() == tag.data_type.casefold()]
    rows = exact or rows
    addresses = {_normalize_address(row.address) for row in rows if row.address}
    types = {row.data_type.casefold() for row in rows if row.data_type != "UNKNOWN"}
    return rows[0] if len(addresses) <= 1 and len(types) <= 1 else None


def _direct_address_refs(project):
    result = {}
    for statement in project.logic_statements:
        for ref in (*statement.reads, *statement.writes):
            text = str(ref)
            if text.startswith("%"):
                result.setdefault(text.casefold(), text)
        for match in re.finditer(r"%[A-Za-z]+[A-Za-z0-9_.]*", statement.text):
            result.setdefault(match.group(0).casefold(), match.group(0))
    return tuple(result.values())


def _build_symbols(project, types, files):
    type_map = {item.name.casefold(): item for item in types}
    raw_globals, _raw_types, raw_conflicts = _raw_inventory(files)
    store, conflicts = {}, list(raw_conflicts)
    for tag in project.tags:
        parts = _split_ref(tag.name)
        raw = _raw_for_tag(raw_globals, tag)
        dtype = raw.data_type if raw and raw.data_type != "UNKNOWN" else tag.data_type
        address = raw.address if raw else None
        info = type_map.get(_type_name(dtype).casefold())
        _add_symbol(store, conflicts, scope="controller", parts=parts, dtype=dtype, type_id=info.id if info else None, origin=tag.id, address=address)
        _expand(store, conflicts, type_map, scope="controller", prefix=parts, dtype=dtype, origin=tag.id)

    v3facts = _v3._facts(project)
    if v3facts is not None:
        types_by_name = {item.name.casefold(): item for item in v3facts.dfb_types}
        for instance in v3facts.instances:
            scope = "controller" if instance.owner_kind.casefold() in {"global", "section"} else f"dfb:{instance.owner_name}"
            parts = _split_ref(instance.name)
            info = type_map.get(instance.block_type.casefold())
            _add_symbol(store, conflicts, scope=scope, parts=parts, dtype=instance.block_type, type_id=info.id if info else None, origin=instance.id)
            dfb_type = types_by_name.get(instance.block_type.casefold())
            if dfb_type:
                for parameter in dfb_type.parameters:
                    child = (*parts, parameter.name)
                    pinfo = type_map.get(parameter.data_type.casefold())
                    _add_symbol(
                        store, conflicts, scope=scope, parts=child, dtype=parameter.data_type,
                        type_id=pinfo.id if pinfo else None, origin=f"{instance.id}:{parameter.direction}", synthetic=True,
                    )
            _expand(store, conflicts, type_map, scope=scope, prefix=parts, dtype=instance.block_type, origin=instance.id)
        for dfb_type in v3facts.dfb_types:
            scope = f"dfb-type:{dfb_type.name}"
            for parameter in dfb_type.parameters:
                info = type_map.get(parameter.data_type.casefold())
                _add_symbol(store, conflicts, scope=scope, parts=(parameter.name,), dtype=parameter.data_type, type_id=info.id if info else None, origin=dfb_type.id, synthetic=True)
            for name, dtype in dfb_type.local_symbols:
                info = type_map.get(dtype.casefold())
                _add_symbol(store, conflicts, scope=scope, parts=(name,), dtype=dtype, type_id=info.id if info else None, origin=dfb_type.id, synthetic=True)

    for address in _direct_address_refs(project):
        dtype = _address_dtype(address)
        info = type_map.get(dtype.casefold())
        _add_symbol(store, conflicts, scope="address", parts=(address,), dtype=dtype, type_id=info.id if info else None, origin="DIRECT_ADDRESS", address=address, synthetic=True)
    return tuple(sorted(store.values(), key=lambda item: (item.scope.casefold(), item.canonical_path))), tuple(sorted(set(conflicts), key=str.casefold))


def _resolve(raw_ref: str, symbols):
    parts = _split_ref(raw_ref)
    if not parts:
        return None, "EMPTY"
    exact = {(item.scope.casefold(), item.canonical_path): item for item in symbols}
    key = tuple(item.casefold() for item in parts)
    wildcard = _normalize_index(parts)
    if str(raw_ref).startswith("%"):
        symbol = exact.get(("address", key))
        return (symbol, "DIRECT_LOCATED_ADDRESS") if symbol else (None, "UNRESOLVED_ADDRESS")
    symbol = exact.get(("controller", key)) or exact.get(("controller", wildcard))
    if symbol:
        return symbol, "EXACT_CONTROLLER" if key == symbol.canonical_path else "ARRAY_WILDCARD"
    return None, "UNRESOLVED"


def _indexed_refs(statement):
    lhs = statement.text.split(":=", 1)[0] if ":=" in statement.text else ""
    return tuple(("WRITE" if match.group(0) in lhs else "READ", match.group(0)) for match in _INDEXED.finditer(statement.text))


def _bindings(project, symbols):
    result, seen = [], set()
    for statement in project.logic_statements:
        refs = [*(("READ", ref) for ref in statement.reads), *(("WRITE", ref) for ref in statement.writes), *_indexed_refs(statement)]
        for access, raw in refs:
            key = (statement.id, access, str(raw))
            if key in seen:
                continue
            seen.add(key)
            symbol, resolution = _resolve(str(raw), symbols)
            digest = hashlib.sha1(f"{statement.id}:{access}:{raw}:{resolution}".encode()).hexdigest()[:16]
            result.append(
                SchneiderV8ReferenceBinding(
                    f"SCHNEIDER-BIND8-{digest}", statement.id, access, str(raw),
                    symbol.id if symbol else None,
                    f"{symbol.scope}::{symbol.display_path}" if symbol else None,
                    resolution,
                    PLCSemanticState.FULL if symbol else PLCSemanticState.PARTIAL,
                )
            )
    return tuple(result)


def _overlap(left: SchneiderV8SymbolIdentity, right: SchneiderV8SymbolIdentity) -> bool:
    if left.scope.casefold() != right.scope.casefold():
        return False
    for x, y in zip(left.canonical_path, right.canonical_path):
        if re.sub(r"\[[^\]]+\]", "[*]", x) != re.sub(r"\[[^\]]+\]", "[*]", y):
            return False
    return True


def _writer_overlaps(bindings, symbols):
    by_id = {item.id: item for item in symbols}
    writers = defaultdict(set)
    for binding in bindings:
        if binding.access == "WRITE" and binding.canonical_symbol_id:
            writers[binding.canonical_symbol_id].add(binding.statement_id)
    ids, result = sorted(writers), []
    for index, left_id in enumerate(ids):
        for right_id in ids[index:]:
            if left_id in by_id and right_id in by_id and _overlap(by_id[left_id], by_id[right_id]) and len(writers[left_id] | writers[right_id]) > 1:
                pair = (left_id, right_id)
                if pair not in result:
                    result.append(pair)
    return tuple(result)


def _physical_aliases(symbols, bindings):
    referenced = {item.canonical_symbol_id for item in bindings if item.canonical_symbol_id}
    by_address = defaultdict(list)
    for symbol in symbols:
        if symbol.id in referenced and symbol.address and symbol.address_kind == "LOCATED":
            by_address[symbol.address].append(symbol.id)
    pairs = []
    for values in by_address.values():
        ids = sorted(set(values))
        for index, left in enumerate(ids):
            for right in ids[index + 1:]:
                pairs.append((left, right))
    return tuple(pairs)


def _io_points(symbols):
    result = []
    for symbol in symbols:
        if not symbol.address:
            continue
        digest = hashlib.sha1(f"{symbol.id}:{symbol.address}".encode()).hexdigest()[:16]
        result.append(
            SchneiderV8IOIdentity(
                f"SCHNEIDER-IO8-{digest}", symbol.id, f"{symbol.scope}::{symbol.display_path}", symbol.address,
                symbol.address_kind or "UNKNOWN", symbol.io_area or "UNKNOWN", symbol.origin,
            )
        )
    return tuple(result)


def _dfb_identities(project, symbols, types):
    v3facts = _v3._facts(project)
    if v3facts is None:
        return ()
    symbol_by_key = {(item.scope.casefold(), item.canonical_path): item for item in symbols}
    type_map = {item.name.casefold(): item for item in types}
    result = []
    for instance in v3facts.instances:
        scope = "controller" if instance.owner_kind.casefold() in {"global", "section"} else f"dfb:{instance.owner_name}"
        parts = _split_ref(instance.name)
        symbol = symbol_by_key.get((scope.casefold(), tuple(item.casefold() for item in parts)))
        type_info = type_map.get(instance.block_type.casefold())
        digest = hashlib.sha1(f"{instance.id}:{scope}:{instance.name}:{instance.block_type}".encode()).hexdigest()[:16]
        result.append(
            SchneiderV8DFBInstanceIdentity(
                f"SCHNEIDER-DFBI8-{digest}", instance.id, instance.owner_kind, instance.owner_name,
                instance.name, instance.block_type, symbol.id if symbol else None, type_info.id if type_info else None,
            )
        )
    return tuple(result)


def _project_identity(project) -> str:
    key = f"{project.metadata.controller_name}|{project.metadata.source_sha256}|{project.metadata.engineering_tool}"
    return f"SCHNEIDER-PROJECT8:{hashlib.sha256(key.encode()).hexdigest()[:24]}"


def schneider_capability_profile_v8(project) -> dict[str, object]:
    profile = dict(_PREVIOUS_CAPABILITY(project))
    facts = _facts(project)
    profile["schema"] = "devagent-schneider-control-expert-capability-v8"
    if facts is None:
        profile.update({"canonical_symbols": 0, "canonical_types": 0, "reference_bindings": 0, "io_identities": 0, "identity_contract": "NONE"})
        return profile
    profile.update(
        {
            "project_identity": facts.project_identity,
            "canonical_symbols": len(facts.symbols),
            "canonical_types": len(facts.types),
            "ddt_types": sum(item.kind == "DDT" for item in facts.types),
            "dfb_identity_types": sum(item.kind == "DFB" for item in facts.types),
            "array_types": sum(item.kind == "ARRAY" for item in facts.types),
            "enum_types": sum(item.kind == "ENUM" for item in facts.types),
            "simple_types": sum(item.kind == "SIMPLE" for item in facts.types),
            "reference_bindings": len(facts.bindings),
            "canonical_read_bindings": sum(item.access == "READ" and item.canonical_symbol_id is not None for item in facts.bindings),
            "canonical_write_bindings": sum(item.access == "WRITE" and item.canonical_symbol_id is not None for item in facts.bindings),
            "ambiguous_references": len(facts.ambiguous_references),
            "unresolved_references": len(facts.unresolved_references),
            "identity_conflicts": len(facts.identity_conflicts),
            "io_identities": len(facts.io_points),
            "located_io_identities": sum(item.address_kind == "LOCATED" for item in facts.io_points),
            "topological_io_identities": sum(item.address_kind == "TOPOLOGICAL" for item in facts.io_points),
            "input_identities": sum(item.io_area == "INPUT" for item in facts.io_points),
            "output_identities": sum(item.io_area == "OUTPUT" for item in facts.io_points),
            "memory_identities": sum(item.io_area == "MEMORY" for item in facts.io_points),
            "dfb_instance_identities": len(facts.dfb_instances),
            "whole_member_writer_overlaps": len(facts.whole_member_overlaps),
            "physical_address_aliases": len(facts.physical_address_aliases),
            "address_conflicts": len(facts.address_conflicts),
            "identity_contract": (
                "COMPLETE"
                if not facts.ambiguous_references and not facts.unresolved_references and not facts.identity_conflicts
                and not facts.address_conflicts and not facts.whole_member_overlaps and not facts.physical_address_aliases
                else "PARTIAL_FAIL_CLOSED"
            ),
            "canonical_identity_contract": (
                "Control Expert controller symbols, DDT members, ARRAY wildcard members, DFB instance/type interfaces, "
                "located/topological addresses, and source read/write bindings are canonicalized deterministically; "
                "unresolved/conflicting identity and physical/writer overlap fail closed."
            ),
        }
    )
    return profile


def analyze_schneider_control_expert_v8(path: Path) -> PLCEngineeringResult:
    target = Path(path)
    _root, files, _total = _v1._preflight_sources(target)
    base = _PREVIOUS_ANALYZER(target)
    project = base.project
    types = _build_types(project, files)
    symbols, identity_conflicts = _build_symbols(project, types, files)
    bindings = _bindings(project, symbols)
    io_points = _io_points(symbols)
    dfb_instances = _dfb_identities(project, symbols, types)
    whole_member = _writer_overlaps(bindings, symbols)
    physical_aliases = _physical_aliases(symbols, bindings)
    ambiguous = tuple(item.id for item in bindings if item.resolution.startswith("AMBIGUOUS"))
    unresolved = tuple(item.id for item in bindings if item.canonical_symbol_id is None and not item.resolution.startswith("AMBIGUOUS"))
    address_conflicts = tuple(item for item in identity_conflicts if item.startswith("ADDRESS:"))
    facts = SchneiderV8Facts(
        _project_identity(project), types, symbols, bindings, io_points, dfb_instances, whole_member, physical_aliases,
        ambiguous, unresolved, identity_conflicts, address_conflicts,
    )
    setattr(project, "_schneider_v8_identity_facts", facts)
    project.metadata = replace(project.metadata, schema_revision="SCHNEIDER-CONTROL-EXPERT-EXPORT-V8")

    for binding in bindings:
        if binding.canonical_symbol_id:
            edge = PLCDependencyEdge(binding.statement_id, binding.canonical_symbol_id, f"CANONICAL_{binding.access}", binding.id)
            if edge not in base.graph.edges:
                base.graph.edges.append(edge)
    for point in io_points:
        edge = PLCDependencyEdge(point.symbol_id, point.id, "LOCATED_AT", point.id)
        if edge not in base.graph.edges:
            base.graph.edges.append(edge)

    full_ids = {statement.id for statement in project.logic_statements if statement.semantic_state is PLCSemanticState.FULL}
    identity_gap = any(item.statement_id in full_ids and item.canonical_symbol_id is None for item in bindings)
    outcome = base.outcome
    if outcome is PLCOutcome.STATICALLY_VERIFIED and (identity_gap or facts.identity_conflicts or facts.whole_member_overlaps or facts.physical_address_aliases):
        outcome = PLCOutcome.PARTIALLY_VERIFIED

    checks = [item for item in base.static_checks if not item.id.startswith("SCHNEIDER_V8_")]
    checks.extend(
        [
            StaticCheck(
                "SCHNEIDER_V8_CANONICAL_IDENTITY",
                StaticCheckStatus.PASS if not ambiguous and not unresolved and not identity_conflicts else StaticCheckStatus.NOT_PROVEN,
                f"Canonical symbols={len(symbols)}, bindings={len(bindings)}, ambiguous={len(ambiguous)}, unresolved={len(unresolved)}, identity conflicts={len(identity_conflicts)}.",
                tuple((*ambiguous, *unresolved, *identity_conflicts)),
            ),
            StaticCheck(
                "SCHNEIDER_V8_DDT_DFB_TYPE_IDENTITY",
                StaticCheckStatus.PASS if types else StaticCheckStatus.WARN,
                f"Canonical types={len(types)}: DDT={sum(item.kind == 'DDT' for item in types)}, DFB={sum(item.kind == 'DFB' for item in types)}, ARRAY={sum(item.kind == 'ARRAY' for item in types)}, ENUM={sum(item.kind == 'ENUM' for item in types)}.",
                tuple(item.id for item in types),
            ),
            StaticCheck(
                "SCHNEIDER_V8_IO_ADDRESS_IDENTITY",
                StaticCheckStatus.NOT_PROVEN if address_conflicts or physical_aliases else StaticCheckStatus.PASS,
                f"Located/topological identities={len(io_points)}, source metadata conflicts={len(address_conflicts)}, physical alias pairs={len(physical_aliases)}.",
                tuple([*(item.id for item in io_points), *address_conflicts, *(value for pair in physical_aliases for value in pair)]),
            ),
            StaticCheck(
                "SCHNEIDER_V8_WRITER_OWNERSHIP",
                StaticCheckStatus.NOT_PROVEN if whole_member else StaticCheckStatus.PASS,
                f"Whole-structure/member canonical writer overlap pairs={len(whole_member)}.",
                tuple(value for pair in whole_member for value in pair),
            ),
        ]
    )
    limitations = list(base.limitations)
    limitations.extend(
        [
            "Schneider V8 canonical identity/type/I/O analysis is fail-closed for unresolved or conflicting symbols, whole/member ownership overlap, duplicate physical located addresses, malformed XML metadata, and recursive type expansion.",
            "ARRAY wildcard identity proves ownership/traceability only; dynamic index values, physical module behavior, I/O update timing, Control Expert Simulator, HIL, SIL/PL, and real Modicon execution are not statically proven.",
            "Topological/located address identity records exported Control Expert engineering metadata; it does not certify wiring, channel health, force state, field device behavior, or that the downloaded controller image matches the export.",
        ]
    )
    return PLCEngineeringResult(outcome, project, base.graph, base.fat_tests, checks, list(dict.fromkeys(limitations)))


def _v8_evidence(previous, engineering):
    items = list(previous(engineering))
    facts = _facts(engineering.project)
    if facts is None:
        return items
    sha = engineering.project.metadata.source_sha256
    existing = {item.id for item in items}
    if facts.project_identity not in existing:
        items.append(
            EvidenceItem(
                facts.project_identity, "SCHNEIDER_PROJECT_IDENTITY_V8",
                f"Control Expert project identity for {engineering.project.metadata.controller_name}; source SHA256={sha}.",
                engineering.project.metadata.source_path, sha,
                {"controller": engineering.project.metadata.controller_name, "engineering_tool": engineering.project.metadata.engineering_tool, "source_sha256": sha},
            )
        )
    for item in facts.types:
        if item.id not in existing:
            items.append(EvidenceItem(item.id, "SCHNEIDER_TYPE_IDENTITY_V8", f"{item.kind} {item.name}", source_sha256=sha, payload={"kind": item.kind, "element_type": item.element_type, "dimensions": list(item.dimensions), "members": [{"name": name, "data_type": dtype} for name, dtype in item.members], "enum_literals": list(item.enum_literals), "origin": item.origin}))
    for item in facts.symbols:
        if item.id not in existing:
            items.append(EvidenceItem(item.id, "SCHNEIDER_SYMBOL_IDENTITY_V8", f"{item.scope}::{item.display_path}: {item.data_type}", source_sha256=sha, payload={"scope": item.scope, "canonical_path": list(item.canonical_path), "data_type": item.data_type, "type_id": item.type_id, "origin": item.origin, "address": item.address, "address_kind": item.address_kind, "io_area": item.io_area, "synthetic": item.synthetic}))
    for item in facts.bindings:
        if item.id not in existing:
            items.append(EvidenceItem(item.id, "SCHNEIDER_REFERENCE_BINDING_V8", f"{item.access} {item.raw_ref} -> {item.canonical_display or item.resolution}", payload={"statement_id": item.statement_id, "canonical_symbol_id": item.canonical_symbol_id, "resolution": item.resolution, "semantic_state": item.semantic_state.value}))
    for item in facts.io_points:
        if item.id not in existing:
            items.append(EvidenceItem(item.id, "SCHNEIDER_IO_IDENTITY_V8", f"{item.symbol_display} -> {item.address} ({item.io_area})", item.source, sha, {"symbol_id": item.symbol_id, "address": item.address, "address_kind": item.address_kind, "io_area": item.io_area}))
    for item in facts.dfb_instances:
        if item.id not in existing:
            items.append(EvidenceItem(item.id, "SCHNEIDER_DFB_INSTANCE_IDENTITY_V8", f"{item.owner_kind}:{item.owner_name}::{item.instance_name} -> {item.block_type}", payload={"instance_id": item.instance_id, "canonical_symbol_id": item.canonical_symbol_id, "type_id": item.type_id}))
    return items


def _v8_risks(previous, engineering, verifications, executions, engineering_findings):
    result = list(previous(engineering, verifications, executions, engineering_findings))
    facts = _facts(engineering.project)
    if facts is None:
        return result
    gaps = (*facts.ambiguous_references, *facts.unresolved_references, *facts.identity_conflicts)
    if gaps:
        result.append(RiskFinding(stable_id("RISK", "SCHNEIDER_IDENTITY_V8", engineering.project.metadata.source_sha256), "SYMBOL_IDENTITY", "Schneider references are not all canonically resolved", Severity.HIGH, f"Ambiguous={len(facts.ambiguous_references)}, unresolved={len(facts.unresolved_references)}, identity conflicts={len(facts.identity_conflicts)}.", "Cause/effect, writer ownership, requirement scope, or regression impact could be wrong if identity were guessed.", "Correct/export symbol, DDT/DFB type, or address metadata and retain affected logic as PARTIAL until identity closes.", tuple(gaps)))
    if facts.whole_member_overlaps:
        result.append(RiskFinding(stable_id("RISK", "SCHNEIDER_WHOLE_MEMBER_V8", *(f"{a}|{b}" for a, b in facts.whole_member_overlaps)), "MULTIPLE_WRITERS", "Schneider whole-structure and member writers overlap", Severity.HIGH, f"{len(facts.whole_member_overlaps)} canonical whole/member ownership overlap pair(s) were found.", "A whole DDT/DFB/ARRAY write can overwrite a separately written member depending on section/task execution order.", "Disposition writer ownership and order, then rerun requirements, FAT, and regression checks.", tuple(value for pair in facts.whole_member_overlaps for value in pair)))
    if facts.physical_address_aliases:
        result.append(RiskFinding(stable_id("RISK", "SCHNEIDER_PHYSICAL_ALIAS_V8", *(f"{a}|{b}" for a, b in facts.physical_address_aliases)), "IO_IDENTITY", "Distinct Schneider symbols resolve to the same located address", Severity.HIGH, f"{len(facts.physical_address_aliases)} referenced physical-address alias pair(s) were found.", "Logical writer analysis can miss a physical collision when two exported names address the same PLC memory/I/O location.", "Confirm intentional aliasing/address ownership in Control Expert and field I/O documentation; otherwise correct the mapping.", tuple(value for pair in facts.physical_address_aliases for value in pair)))
    return result


def _scope_requirement(previous, requirement, engineering, evidence, tests):
    result = previous(requirement, engineering, evidence, tests)
    project = engineering.project
    if not str(project.metadata.vendor).casefold().startswith("schneider") or result.status not in {RequirementStatus.STATICALLY_VERIFIED, RequirementStatus.CONFLICT}:
        return result
    facts = _facts(project)
    if facts is None:
        return result
    evidence_ids, matched = set(result.evidence_ids), {item.casefold() for item in result.matched_tags}
    gaps = []
    for binding in facts.bindings:
        if binding.canonical_symbol_id is not None:
            continue
        statement = next((item for item in project.logic_statements if item.id == binding.statement_id), None)
        if statement and (binding.statement_id in evidence_ids or any(ref.casefold() in matched for ref in statement.writes)):
            gaps.append(binding.id)
    if gaps:
        return RequirementVerification(result.requirement_id, RequirementStatus.TRACEABLE_NOT_PROVEN, "Schneider V8 withheld the static verdict because source statement(s) used by the requirement contain unresolved canonical symbol/address identity.", tuple(dict.fromkeys([*result.evidence_ids, *gaps])), result.matched_tags, result.linked_test_ids, result.confidence, result.ai_assisted)
    roots, exact_roots = defaultdict(set), set()
    for symbol in facts.symbols:
        if symbol.scope.casefold() == "controller" and symbol.canonical_path:
            roots[symbol.canonical_path[-1]].add(symbol.display_path.casefold())
            if len(symbol.canonical_path) == 1:
                exact_roots.add(symbol.canonical_path[0])
    ambiguous = []
    for tag in result.matched_tags:
        clean = _clean(tag).casefold()
        if "." not in clean and not clean.startswith("%") and clean not in exact_roots and len(roots.get(clean, ())) > 1:
            ambiguous.append(tag)
    if ambiguous:
        return RequirementVerification(result.requirement_id, RequirementStatus.TRACEABLE_NOT_PROVEN, "Schneider V8 withheld the static verdict because unqualified matched symbol(s) map to multiple canonical project members: " + ", ".join(ambiguous) + ".", result.evidence_ids, result.matched_tags, result.linked_test_ids, result.confidence, result.ai_assisted)
    return result


def _rewrite_v8(value: str) -> str:
    text = str(value)
    for old in (
        "Schneider Control Expert V1", "Schneider Control Expert V2", "Schneider Control Expert V3", "Schneider Control Expert V4",
        "Schneider Control Expert V5", "Schneider Control Expert V6", "Schneider Control Expert V7", "Schneider V1", "Schneider V2",
        "Schneider V3", "Schneider V4", "Schneider V5", "Schneider V6", "Schneider V7",
    ):
        text = text.replace(old, "Schneider V8")
    return text


def _v8_render(previous, project):
    base = previous(project)
    facts = _facts(project)
    if facts is None:
        return base
    profile = schneider_capability_profile_v8(project)
    text = (
        "### Schneider V8 Canonical Symbols / Types / I/O Identity\n\n"
        f"- Project identity: **{profile['project_identity']}**\n"
        f"- Canonical symbols/members: **{profile['canonical_symbols']}**\n"
        f"- Canonical types: **{profile['canonical_types']}** (DDT={profile['ddt_types']}, DFB={profile['dfb_identity_types']}, ARRAY={profile['array_types']}, ENUM={profile['enum_types']})\n"
        f"- Read/write bindings: **{profile['reference_bindings']}**; ambiguous: **{profile['ambiguous_references']}**; unresolved: **{profile['unresolved_references']}**\n"
        f"- DFB instance identities: **{profile['dfb_instance_identities']}**\n"
        f"- Located/topological identities: **{profile['io_identities']}** (input={profile['input_identities']}, output={profile['output_identities']}, memory={profile['memory_identities']})\n"
        f"- Whole/member writer overlaps: **{profile['whole_member_writer_overlaps']}**\n"
        f"- Referenced physical-address alias pairs: **{profile['physical_address_aliases']}**\n"
        f"- Identity contract: **{profile['identity_contract']}**\n"
        "- V8 canonicalizes engineering identity and ownership; it does not prove wiring, forces, I/O refresh, scan timing, or physical process behavior.\n\n"
    )
    marker = "### Schneider V7 Fault / Reset / Recovery / Restart"
    return base.replace(marker, text + marker, 1) if marker in base else base + "\n\n" + text


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import schneider_control_expert_v1 as _root
    from devagent.plc import schneider_integration_v1 as _integration
    from devagent.plc import schneider_report_install_v1 as _report

    previous_verify = _integration._verify_requirement
    previous_evidence = _integration._evidence_index
    previous_findings = _integration._findings
    previous_risks = _integration._detect_risks
    previous_render = _report._render

    _root.analyze_schneider_control_expert = analyze_schneider_control_expert_v8
    _root.schneider_capability_profile = schneider_capability_profile_v8
    _dispatch.analyze_schneider_control_expert = analyze_schneider_control_expert_v8
    _integration.schneider_capability_profile = schneider_capability_profile_v8

    def verify_requirement(requirement, engineering, evidence, tests):
        return _scope_requirement(previous_verify, requirement, engineering, evidence, tests)

    def evidence_index(engineering):
        items = _v8_evidence(previous_evidence, engineering)
        if _facts(engineering.project) is None:
            return items
        return [replace(item, summary=_rewrite_v8(item.summary)) if item.kind == "SCHNEIDER_CONTROL_EXPERT_CAPABILITY_PROFILE" else item for item in items]

    def findings(engineering, valid_evidence_ids):
        items = list(previous_findings(engineering, valid_evidence_ids))
        if _facts(engineering.project) is None:
            return items
        return [replace(item, title=_rewrite_v8(item.title), summary=_rewrite_v8(item.summary), recommendation=_rewrite_v8(item.recommendation)) for item in items]

    def detect_risks(engineering, verifications, executions, engineering_findings):
        return _v8_risks(previous_risks, engineering, verifications, executions, engineering_findings)

    def render(project):
        return _v8_render(previous_render, project)

    _integration._verify_requirement = verify_requirement
    _integration._evidence_index = evidence_index
    _integration._findings = findings
    _integration._detect_risks = detect_risks
    _report._render = render
    _INSTALLED = True


__all__ = [
    "SchneiderV8DFBInstanceIdentity", "SchneiderV8Facts", "SchneiderV8IOIdentity", "SchneiderV8ReferenceBinding",
    "SchneiderV8SymbolIdentity", "SchneiderV8TypeIdentity", "analyze_schneider_control_expert_v8", "install",
    "schneider_capability_profile_v8",
]
