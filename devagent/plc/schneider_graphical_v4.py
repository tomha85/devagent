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
    PLCEngineeringResult,
    PLCLogicPath,
    PLCLogicStatement,
    PLCOutcome,
    PLCOutputLogic,
    PLCSourceRef,
    PLCSemanticState,
    StaticCheck,
    StaticCheckStatus,
)
from devagent.plc.production_models import EvidenceItem, RiskFinding, Severity
from devagent.plc.production_utils import stable_id
from devagent.plc import schneider_call_graph_v3 as _v3
from devagent.plc import schneider_control_expert_v1 as _v1


_INSTALLED = False
_PREVIOUS_ANALYZER = _v1.analyze_schneider_control_expert
_PREVIOUS_CAPABILITY = _v1.schneider_capability_profile

_BOOL_TYPES = {"BOOL", "EBOOL", "BOOLEAN"}
_MAX_LD_COLUMNS = 64
_MAX_LD_ROWS = 512
_MAX_PATHS = 128
_MAX_TERMS = 32
_MAX_FBD_BLOCKS = 512
_MAX_FBD_LINKS = 1024
_MAX_REGION_XML = 32768
_SIMPLE_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_FBD_AND = {"AND", "AND_BOOL"}
_FBD_OR = {"OR", "OR_BOOL"}


@dataclass(frozen=True)
class SchneiderGraphicalRegion:
    id: str
    section: str
    language: str
    locator: str
    semantic_state: PLCSemanticState
    reason: str
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    logic_ids: tuple[str, ...]
    elements: int


@dataclass(frozen=True)
class SchneiderV4Facts:
    regions: tuple[SchneiderGraphicalRegion, ...]
    modeled_logic_ids: tuple[str, ...]
    writer_conflicts: tuple[str, ...]

    @property
    def modeled(self) -> tuple[SchneiderGraphicalRegion, ...]:
        return tuple(item for item in self.regions if item.semantic_state is PLCSemanticState.FULL)

    @property
    def partial(self) -> tuple[SchneiderGraphicalRegion, ...]:
        return tuple(item for item in self.regions if item.semantic_state is PLCSemanticState.PARTIAL)

    @property
    def withheld(self) -> tuple[SchneiderGraphicalRegion, ...]:
        return tuple(item for item in self.regions if item.semantic_state is PLCSemanticState.OPAQUE)


@dataclass(frozen=True)
class _FBPin:
    formal: str
    actual: str
    inverted: bool
    direction: str


@dataclass(frozen=True)
class _FBBlock:
    name: str
    block_type: str
    en_eno: bool
    pins: tuple[_FBPin, ...]


class _Unsupported(ValueError):
    pass


def _lname(tag: str) -> str:
    return _v1._local_name(tag)


def _bool_type(project, symbol: str) -> bool:
    matches = [
        tag for tag in project.tags
        if tag.name.casefold() == symbol.casefold()
        and tag.scope.casefold() == "controller"
    ]
    if len(matches) != 1:
        return False
    return str(matches[0].data_type).strip().upper() in _BOOL_TYPES


def _require_bool(project, symbol: str) -> str:
    if not _SIMPLE_SYMBOL.fullmatch(symbol):
        raise _Unsupported(f"unsupported_symbol:{symbol}")
    if not _bool_type(project, symbol):
        raise _Unsupported(f"unresolved_or_non_boolean_symbol:{symbol}")
    return symbol


def _var(name: str, required: bool = True):
    return ("VAR", name, required)


def _const(value: bool):
    return ("CONST", bool(value))


def _and(*items):
    flat = []
    for item in items:
        if item[0] == "CONST" and item[1] is False:
            return _const(False)
        if item[0] == "CONST" and item[1] is True:
            continue
        if item[0] == "AND":
            flat.extend(item[1])
        else:
            flat.append(item)
    if not flat:
        return _const(True)
    if len(flat) == 1:
        return flat[0]
    return ("AND", tuple(flat))


def _or(*items):
    flat = []
    for item in items:
        if item[0] == "CONST" and item[1] is True:
            return _const(True)
        if item[0] == "CONST" and item[1] is False:
            continue
        if item[0] == "OR":
            flat.extend(item[1])
        else:
            flat.append(item)
    if not flat:
        return _const(False)
    if len(flat) == 1:
        return flat[0]
    return ("OR", tuple(flat))


def _not(item):
    kind = item[0]
    if kind == "CONST":
        return _const(not item[1])
    if kind == "VAR":
        return _var(item[1], not item[2])
    if kind == "AND":
        return _or(*(_not(child) for child in item[1]))
    if kind == "OR":
        return _and(*(_not(child) for child in item[1]))
    raise _Unsupported("unsupported_boolean_negation")


def _merge_path(left: dict[str, bool], right: dict[str, bool]) -> dict[str, bool] | None:
    result = dict(left)
    folded = {key.casefold(): key for key in result}
    for key, value in right.items():
        old = folded.get(key.casefold())
        if old is not None and result[old] != value:
            return None
        if old is None:
            result[key] = value
            folded[key.casefold()] = key
    if len(result) > _MAX_TERMS:
        raise _Unsupported("boolean_term_limit")
    return result


def _dedupe_paths(paths: list[dict[str, bool]]) -> list[dict[str, bool]]:
    unique: dict[tuple[tuple[str, bool], ...], dict[str, bool]] = {}
    for path in paths:
        key = tuple(sorted(((name.casefold(), value) for name, value in path.items())))
        unique.setdefault(key, path)
        if len(unique) > _MAX_PATHS:
            raise _Unsupported("boolean_path_limit")
    return list(unique.values())


def _dnf(expr) -> list[dict[str, bool]]:
    kind = expr[0]
    if kind == "CONST":
        return [{}] if expr[1] else []
    if kind == "VAR":
        return [{expr[1]: expr[2]}]
    if kind == "OR":
        result: list[dict[str, bool]] = []
        for child in expr[1]:
            result.extend(_dnf(child))
            result = _dedupe_paths(result)
        return result
    if kind == "AND":
        result = [{}]
        for child in expr[1]:
            next_paths: list[dict[str, bool]] = []
            for left in result:
                for right in _dnf(child):
                    merged = _merge_path(left, right)
                    if merged is not None:
                        next_paths.append(merged)
            result = _dedupe_paths(next_paths)
        return result
    raise _Unsupported("unsupported_boolean_expression")


def _paths_to_logic(paths: list[dict[str, bool]]) -> tuple[PLCLogicPath, ...]:
    return tuple(
        PLCLogicPath(
            tuple(
                PLCBooleanTerm(tag, required)
                for tag, required in sorted(path.items(), key=lambda item: item[0].casefold())
            )
        )
        for path in _dedupe_paths(paths)
    )


def _source(project, section: str, locator: str) -> PLCSourceRef:
    return PLCSourceRef(
        project.metadata.source_path,
        project.metadata.controller_name,
        program=section,
        routine=section,
        line=locator,
    )


def _serialize(element: ET.Element) -> str:
    return ET.tostring(element, encoding="unicode")[:_MAX_REGION_XML]


def _int_attr(element: ET.Element, name: str, *, minimum: int = 1) -> int:
    raw = (element.attrib.get(name) or "").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise _Unsupported(f"invalid_{name}") from exc
    if value < minimum:
        raise _Unsupported(f"invalid_{name}")
    return value


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[tuple[int, int], tuple[int, int]] = {}

    def add(self, item: tuple[int, int]) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: tuple[int, int]) -> tuple[int, int]:
        self.add(item)
        parent = self.parent[item]
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, left: tuple[int, int], right: tuple[int, int]) -> None:
        a = self.find(left)
        b = self.find(right)
        if a != b:
            self.parent[b] = a


def _line_rows(network: ET.Element):
    y = 0
    current: list[tuple[int, ET.Element]] = []
    groups: list[list[tuple[int, ET.Element]]] = []
    for child in list(network):
        if _lname(child.tag) != "typeLine":
            continue
        empties = [item for item in list(child) if _lname(item.tag) == "emptyLine"]
        if empties:
            if len(empties) != 1 or len(list(child)) != 1:
                raise _Unsupported("mixed_empty_line")
            if current:
                groups.append(current)
                current = []
            y += _int_attr(empties[0], "nbRows")
            continue
        current.append((y, child))
        y += 1
        if y > _MAX_LD_ROWS:
            raise _Unsupported("ld_row_limit")
    if current:
        groups.append(current)
    return groups


def _ld_group(
    project,
    section: str,
    relative: str,
    network_index: int,
    group_index: int,
    rows: list[tuple[int, ET.Element]],
    nb_columns: int,
):
    if not 1 <= nb_columns <= _MAX_LD_COLUMNS:
        raise _Unsupported("ld_column_limit")

    uf = _UnionFind()
    horizontal: list[tuple[tuple[int, int], tuple[int, int], tuple[str, bool] | None]] = []
    vertical: list[tuple[tuple[int, int], tuple[int, int]]] = []
    coils: list[tuple[str, int, int]] = []
    reads: list[str] = []
    writes: list[str] = []
    row_ids = {y for y, _line in rows}
    element_count = 0

    for y, line in rows:
        col = 0
        for child in list(line):
            local = _lname(child.tag)
            element_count += 1
            if local == "HLink":
                span = _int_attr(child, "nbCells")
                for offset in range(span):
                    horizontal.append(((y, col + offset), (y, col + offset + 1), None))
                col += span
                continue
            if local == "emptyCell":
                col += _int_attr(child, "nbCells")
                continue
            if local == "VLink":
                if y + 1 not in row_ids:
                    raise _Unsupported("dangling_vertical_link")
                vertical.append(((y, col), (y + 1, col)))
                col += 1
                continue
            if local == "shortCircuit":
                links = [item for item in list(child) if _lname(item.tag) == "HLink"]
                vlinks = [item for item in list(child) if _lname(item.tag) == "VLink"]
                if len(links) != 1 or len(vlinks) != 1:
                    raise _Unsupported("unsupported_short_circuit")
                span = _int_attr(links[0], "nbCells")
                if y + 1 not in row_ids:
                    raise _Unsupported("dangling_short_circuit")
                vertical.append(((y, col), (y + 1, col)))
                for offset in range(span):
                    horizontal.append(((y, col + offset), (y, col + offset + 1), None))
                col += span
                continue
            if local == "contact":
                kind = (child.attrib.get("typeContact") or "").strip().casefold()
                name = (child.attrib.get("contactVariableName") or "").strip()
                if kind not in {"opencontact", "closedcontact"} or not name:
                    raise _Unsupported(f"unsupported_contact:{kind or 'unknown'}")
                symbol = _require_bool(project, name)
                reads.append(symbol)
                horizontal.append(((y, col), (y, col + 1), (symbol, kind == "opencontact")))
                col += 1
                continue
            if local == "coil":
                kind = (child.attrib.get("typeCoil") or "").strip().casefold()
                name = (child.attrib.get("coilVariableName") or "").strip()
                if kind != "coil" or not name:
                    raise _Unsupported(f"unsupported_coil:{kind or 'unknown'}")
                symbol = _require_bool(project, name)
                coils.append((symbol, y, col))
                writes.append(symbol)
                horizontal.append(((y, col), (y, col + 1), None))
                col += 1
                continue
            raise _Unsupported(f"unsupported_ld_element:{local}")
        if col != nb_columns:
            raise _Unsupported(f"ld_cell_law:{col}/{nb_columns}")

    if not coils:
        raise _Unsupported("ld_output_coil_missing")
    if len({name.casefold() for name, _y, _col in coils}) != len(coils):
        raise _Unsupported("duplicate_ld_output_writer")

    for left, right in vertical:
        uf.union(left, right)
    for left, right, _condition in horizontal:
        uf.add(left)
        uf.add(right)
    for y, _line in rows:
        for col in range(nb_columns + 1):
            uf.add((y, col))

    component_column: dict[tuple[int, int], int] = {}
    for node in list(uf.parent):
        root = uf.find(node)
        old = component_column.setdefault(root, node[1])
        if old != node[1]:
            raise _Unsupported("vertical_link_changes_column")

    edges: dict[int, list[tuple[tuple[int, int], tuple[int, int], tuple[str, bool] | None]]] = defaultdict(list)
    for left, right, condition in horizontal:
        src = uf.find(left)
        dst = uf.find(right)
        if component_column[src] >= component_column[dst]:
            raise _Unsupported("non_forward_ld_edge")
        edges[component_column[src]].append((src, dst, condition))

    values: dict[tuple[int, int], list[dict[str, bool]]] = defaultdict(list)
    for y, _line in rows:
        root = uf.find((y, 0))
        values[root].append({})
        values[root] = _dedupe_paths(values[root])

    for col in range(nb_columns):
        for src, dst, condition in edges.get(col, ()):
            if not values.get(src):
                continue
            for path in values[src]:
                addition = {} if condition is None else {condition[0]: condition[1]}
                merged = _merge_path(path, addition)
                if merged is not None:
                    values[dst].append(merged)
            values[dst] = _dedupe_paths(values[dst])

    logic: list[PLCOutputLogic] = []
    locator = f"LD Network {network_index} Group {group_index}"
    source = _source(project, section, f"{relative}:{locator}")
    statement_seed = f"{relative}:{section}:{locator}"
    statement_id = f"SCHNEIDER-LD4-{hashlib.sha1(statement_seed.encode()).hexdigest()[:14]}"
    for output, y, col in coils:
        paths = values.get(uf.find((y, col)), [])
        if not paths:
            raise _Unsupported(f"unpowered_ld_coil:{output}")
        if any(output.casefold() == tag.casefold() for path in paths for tag in path):
            raise _Unsupported(f"self_reference:{output}")
        digest = hashlib.sha1(f"{statement_id}:{output}:{repr(paths)}".encode()).hexdigest()[:14]
        logic.append(
            PLCOutputLogic(
                id=f"SCHNEIDER-GRAPH4-{digest}",
                output_tag=output,
                instruction="ASSIGN_BOOL",
                paths=_paths_to_logic(paths),
                source=source,
                language="LD",
                origin=f"SCHNEIDER_LD_V4:{statement_id}",
                semantic_state=PLCSemanticState.FULL,
            )
        )

    statement = PLCLogicStatement(
        id=statement_id,
        language="LD",
        owner_type="program",
        owner_name=section,
        routine=section,
        locator=locator,
        text="\n".join(_serialize(line) for _y, line in rows)[:_MAX_REGION_XML],
        reads=tuple(dict.fromkeys(reads)),
        writes=tuple(dict.fromkeys(writes)),
        calls=(),
        semantic_state=PLCSemanticState.FULL,
        source=source,
    )
    fact = SchneiderGraphicalRegion(
        statement_id,
        section,
        "LD",
        locator,
        PLCSemanticState.FULL,
        "bounded_ld_cell_graph_boolean",
        statement.reads,
        statement.writes,
        tuple(item.id for item in logic),
        element_count,
    )
    return statement, tuple(logic), fact


def _withheld_ld_group(
    project,
    section: str,
    relative: str,
    network_index: int,
    group_index: int,
    rows: list[tuple[int, ET.Element]],
    reason: str,
):
    locator = f"LD Network {network_index} Group {group_index}"
    source = _source(project, section, f"{relative}:{locator}")
    seed = f"{relative}:{section}:{locator}:{reason}"
    statement_id = f"SCHNEIDER-LD4-{hashlib.sha1(seed.encode()).hexdigest()[:14]}"
    reads: list[str] = []
    writes: list[str] = []
    calls: list[str] = []
    elements = 0
    for _y, line in rows:
        for child in line.iter():
            local = _lname(child.tag)
            if child is line:
                continue
            elements += 1
            if local == "contact":
                name = (child.attrib.get("contactVariableName") or "").strip()
                if name and name not in reads:
                    reads.append(name)
            elif local == "coil":
                name = (child.attrib.get("coilVariableName") or "").strip()
                if name and name not in writes:
                    writes.append(name)
            elif local == "FFBBlock":
                block = (child.attrib.get("typeName") or child.attrib.get("instanceName") or "FFB").strip()
                if block and block not in calls:
                    calls.append(block)
    statement = PLCLogicStatement(
        id=statement_id,
        language="LD",
        owner_type="program",
        owner_name=section,
        routine=section,
        locator=locator,
        text="\n".join(_serialize(line) for _y, line in rows)[:_MAX_REGION_XML],
        reads=tuple(reads),
        writes=tuple(writes),
        calls=tuple(calls),
        semantic_state=PLCSemanticState.OPAQUE,
        source=source,
    )
    fact = SchneiderGraphicalRegion(
        statement_id,
        section,
        "LD",
        locator,
        PLCSemanticState.OPAQUE,
        reason,
        statement.reads,
        statement.writes,
        (),
        elements,
    )
    return statement, (), fact


def _parse_fb_block(element: ET.Element) -> _FBBlock:
    name = (element.attrib.get("instanceName") or "").strip()
    block_type = (element.attrib.get("typeName") or "").strip().upper()
    if not name or not block_type:
        raise _Unsupported("fbd_block_identity_missing")
    en_eno = (element.attrib.get("enEnO") or "false").strip().casefold() == "true"
    desc = next((item for item in element.iter() if _lname(item.tag) == "descriptionFFB"), None)
    if desc is None:
        raise _Unsupported(f"fbd_description_missing:{name}")
    pins: list[_FBPin] = []
    for pin in list(desc):
        local = _lname(pin.tag)
        if local not in {"inputVariable", "outputVariable"}:
            continue
        formal = (pin.attrib.get("formalParameter") or "").strip()
        actual = (pin.attrib.get("effectiveParameter") or "").strip()
        inverted = (pin.attrib.get("invertedPin") or "false").strip().casefold() == "true"
        pins.append(_FBPin(formal, actual, inverted, "INPUT" if local == "inputVariable" else "OUTPUT"))
    return _FBBlock(name, block_type, en_eno, tuple(pins))


def _fbd_actual(project, text: str):
    value = text.strip()
    if value.upper() == "TRUE":
        return _const(True)
    if value.upper() == "FALSE":
        return _const(False)
    return _var(_require_bool(project, value), True)


def _fbd_network(
    project,
    section: str,
    relative: str,
    network_index: int,
    network: ET.Element,
):
    blocks: dict[str, _FBBlock] = {}
    links: dict[tuple[str, str], tuple[str, str]] = {}
    link_count = 0
    unsupported_objects: list[str] = []

    for child in list(network):
        local = _lname(child.tag)
        if local == "FFBBlock":
            block = _parse_fb_block(child)
            key = block.name.casefold()
            if key in blocks:
                raise _Unsupported(f"duplicate_fbd_instance:{block.name}")
            blocks[key] = block
            if len(blocks) > _MAX_FBD_BLOCKS:
                raise _Unsupported("fbd_block_limit")
            continue
        if local == "linkFB":
            link_count += 1
            if link_count > _MAX_FBD_LINKS:
                raise _Unsupported("fbd_link_limit")
            sources = [item for item in child.iter() if _lname(item.tag) == "linkSource"]
            destinations = [item for item in child.iter() if _lname(item.tag) == "linkDestination"]
            if len(sources) != 1 or len(destinations) != 1:
                raise _Unsupported("ambiguous_fbd_link")
            source = sources[0]
            dest = destinations[0]
            src = ((source.attrib.get("parentObjectName") or "").strip(), (source.attrib.get("pinName") or "").strip())
            dst = ((dest.attrib.get("parentObjectName") or "").strip(), (dest.attrib.get("pinName") or "").strip())
            if not all(src) or not all(dst):
                raise _Unsupported("fbd_link_identity_missing")
            key = (dst[0].casefold(), dst[1].casefold())
            if key in links:
                raise _Unsupported("multiple_fbd_input_drivers")
            links[key] = (src[0], src[1])
            continue
        if local in {"textBox"}:
            continue
        unsupported_objects.append(local)

    if not blocks:
        raise _Unsupported("fbd_blocks_missing")
    if unsupported_objects:
        raise _Unsupported("unsupported_fbd_object:" + ",".join(sorted(set(unsupported_objects))))

    memo: dict[tuple[str, str], object] = {}
    visiting: set[tuple[str, str]] = set()

    def output_expr(block_name: str, pin_name: str):
        key = (block_name.casefold(), pin_name.casefold())
        if key in memo:
            return memo[key]
        if key in visiting:
            raise _Unsupported("fbd_signal_cycle")
        visiting.add(key)
        block = blocks.get(block_name.casefold())
        if block is None:
            raise _Unsupported(f"fbd_source_block_missing:{block_name}")
        if block.en_eno:
            raise _Unsupported(f"fbd_en_eno_enabled:{block.name}")
        if block.block_type not in _FBD_AND | _FBD_OR:
            raise _Unsupported(f"stateful_or_unsupported_fbd_block:{block.block_type}")
        if pin_name.casefold() != "out":
            raise _Unsupported(f"unsupported_fbd_output_pin:{block.name}:{pin_name}")

        input_pins = [
            pin for pin in block.pins
            if pin.direction == "INPUT" and pin.formal.casefold() != "en"
        ]
        if len(input_pins) < 2:
            raise _Unsupported(f"fbd_gate_inputs_missing:{block.name}")
        values = []
        for pin in input_pins:
            direct = pin.actual.strip()
            link = links.get((block.name.casefold(), pin.formal.casefold()))
            if direct and link:
                raise _Unsupported(f"fbd_pin_has_direct_and_link:{block.name}:{pin.formal}")
            if direct:
                value = _fbd_actual(project, direct)
            elif link:
                value = output_expr(link[0], link[1])
            else:
                raise _Unsupported(f"fbd_input_unbound:{block.name}:{pin.formal}")
            if pin.inverted:
                value = _not(value)
            values.append(value)
        expression = _and(*values) if block.block_type in _FBD_AND else _or(*values)
        output_pin = next(
            (
                pin for pin in block.pins
                if pin.direction == "OUTPUT" and pin.formal.casefold() == "out"
            ),
            None,
        )
        if output_pin is None:
            raise _Unsupported(f"fbd_out_pin_missing:{block.name}")
        if output_pin.inverted:
            expression = _not(expression)
        visiting.remove(key)
        memo[key] = expression
        return expression

    logic: list[PLCOutputLogic] = []
    writes_all: list[str] = []
    reads_modeled: list[str] = []
    calls: list[str] = []
    reasons: list[str] = []
    per_output_writers: dict[str, int] = defaultdict(int)

    for block in blocks.values():
        if block.block_type not in _FBD_AND | _FBD_OR:
            calls.append(block.block_type)
        for pin in block.pins:
            if pin.direction != "OUTPUT" or pin.formal.casefold() in {"eno"} or not pin.actual:
                continue
            if _SIMPLE_SYMBOL.fullmatch(pin.actual):
                writes_all.append(pin.actual)
                per_output_writers[pin.actual.casefold()] += 1

    locator = f"FBD Network {network_index}"
    source = _source(project, section, f"{relative}:{locator}")
    statement_id = f"SCHNEIDER-FBD4-{hashlib.sha1(f'{relative}:{section}:{locator}'.encode()).hexdigest()[:14]}"

    for block in blocks.values():
        if block.block_type not in _FBD_AND | _FBD_OR:
            continue
        out_pin = next(
            (
                pin for pin in block.pins
                if pin.direction == "OUTPUT" and pin.formal.casefold() == "out"
            ),
            None,
        )
        if out_pin is None or not out_pin.actual:
            continue
        try:
            output = _require_bool(project, out_pin.actual)
            if per_output_writers[output.casefold()] != 1:
                raise _Unsupported(f"duplicate_fbd_output_writer:{output}")
            expr = output_expr(block.name, "OUT")
            paths = _dnf(expr)
            if any(output.casefold() == name.casefold() for path in paths for name in path):
                raise _Unsupported(f"self_reference:{output}")
            reads = sorted({name for path in paths for name in path}, key=str.casefold)
            reads_modeled.extend(reads)
            digest = hashlib.sha1(f"{statement_id}:{block.name}:{output}:{repr(paths)}".encode()).hexdigest()[:14]
            logic.append(
                PLCOutputLogic(
                    id=f"SCHNEIDER-GRAPH4-{digest}",
                    output_tag=output,
                    instruction="ASSIGN_BOOL",
                    paths=_paths_to_logic(paths),
                    source=source,
                    language="FBD",
                    origin=f"SCHNEIDER_FBD_V4:{statement_id}:{block.name}",
                    semantic_state=PLCSemanticState.FULL,
                )
            )
        except _Unsupported as exc:
            reasons.append(str(exc))

    supported_blocks = sum(block.block_type in _FBD_AND | _FBD_OR and not block.en_eno for block in blocks.values())
    if logic and supported_blocks == len(blocks) and not reasons:
        semantic = PLCSemanticState.FULL
        reason = "bounded_fbd_boolean_gate_graph"
    elif logic:
        semantic = PLCSemanticState.PARTIAL
        reason = "partial_fbd_boolean_projection:" + ",".join(sorted(set(reasons + ["unsupported_blocks_present"])))
    else:
        semantic = PLCSemanticState.OPAQUE
        if reasons:
            reason = ",".join(sorted(set(reasons)))
        else:
            unsupported = sorted({block.block_type for block in blocks.values() if block.block_type not in _FBD_AND | _FBD_OR})
            reason = "stateful_or_unsupported_fbd_blocks:" + ",".join(unsupported or ["no_boolean_output"])

    statement = PLCLogicStatement(
        id=statement_id,
        language="FBD",
        owner_type="program",
        owner_name=section,
        routine=section,
        locator=locator,
        text=_serialize(network),
        reads=tuple(dict.fromkeys(reads_modeled)),
        writes=tuple(dict.fromkeys(writes_all)),
        calls=tuple(dict.fromkeys(calls)),
        semantic_state=semantic,
        source=source,
    )
    fact = SchneiderGraphicalRegion(
        statement_id,
        section,
        "FBD",
        locator,
        semantic,
        reason,
        statement.reads,
        statement.writes,
        tuple(item.id for item in logic),
        len(blocks) + link_count,
    )
    return statement, tuple(logic), fact


def _graphical_sources(path: Path, project):
    _root, files, _total = _v1._preflight_sources(path)
    for source, relative in files:
        try:
            root = ET.parse(source).getroot()
        except ET.ParseError:
            continue
        for program in (item for item in root.iter() if _lname(item.tag) == "program"):
            ident = next((item for item in program.iter() if _lname(item.tag) == "identProgram"), None)
            if ident is None:
                continue
            section = (ident.attrib.get("name") or "").strip()
            if not section:
                continue
            source_node = next(
                (
                    item for item in program.iter()
                    if _lname(item.tag) in {"LDSource", "FBDSource"}
                ),
                None,
            )
            if source_node is not None:
                yield relative, section, _lname(source_node.tag), source_node


def _build_graphical(path: Path, project):
    statements: list[PLCLogicStatement] = []
    logic: list[PLCOutputLogic] = []
    facts: list[SchneiderGraphicalRegion] = []
    warnings: list[str] = []

    for relative, section, source_kind, source_node in _graphical_sources(path, project):
        if source_kind == "LDSource":
            try:
                nb_columns = _int_attr(source_node, "nbColumns")
            except _Unsupported as exc:
                warnings.append(f"Control Expert V4 LD {section} withheld: {exc}.")
                continue
            networks = [item for item in source_node.iter() if _lname(item.tag) == "networkLD"]
            for n_index, network in enumerate(networks, start=1):
                try:
                    groups = _line_rows(network)
                except _Unsupported as exc:
                    locator = f"LD Network {n_index}"
                    sid = f"SCHNEIDER-LD4-{hashlib.sha1(f'{relative}:{section}:{locator}:{exc}'.encode()).hexdigest()[:14]}"
                    src = _source(project, section, f"{relative}:{locator}")
                    statement = PLCLogicStatement(
                        sid, "LD", "program", section, section, locator, _serialize(network),
                        (), (), (), PLCSemanticState.OPAQUE, src,
                    )
                    fact = SchneiderGraphicalRegion(sid, section, "LD", locator, PLCSemanticState.OPAQUE, str(exc), (), (), (), len(list(network)))
                    statements.append(statement)
                    facts.append(fact)
                    warnings.append(f"Control Expert V4 LD {section}/{locator} withheld: {exc}.")
                    continue
                for g_index, rows in enumerate(groups, start=1):
                    try:
                        statement, outputs, fact = _ld_group(project, section, relative, n_index, g_index, rows, nb_columns)
                    except _Unsupported as exc:
                        statement, outputs, fact = _withheld_ld_group(project, section, relative, n_index, g_index, rows, str(exc))
                        warnings.append(f"Control Expert V4 LD {section}/{fact.locator} withheld: {exc}.")
                    statements.append(statement)
                    logic.extend(outputs)
                    facts.append(fact)
        else:
            networks = [item for item in source_node.iter() if _lname(item.tag) == "networkFBD"]
            for n_index, network in enumerate(networks, start=1):
                try:
                    statement, outputs, fact = _fbd_network(project, section, relative, n_index, network)
                except _Unsupported as exc:
                    locator = f"FBD Network {n_index}"
                    sid = f"SCHNEIDER-FBD4-{hashlib.sha1(f'{relative}:{section}:{locator}:{exc}'.encode()).hexdigest()[:14]}"
                    src = _source(project, section, f"{relative}:{locator}")
                    writes = []
                    calls = []
                    for block in (item for item in network.iter() if _lname(item.tag) == "FFBBlock"):
                        btype = (block.attrib.get("typeName") or "").strip()
                        if btype:
                            calls.append(btype)
                        for pin in block.iter():
                            if _lname(pin.tag) == "outputVariable":
                                actual = (pin.attrib.get("effectiveParameter") or "").strip()
                                if actual and actual not in writes:
                                    writes.append(actual)
                    statement = PLCLogicStatement(
                        sid, "FBD", "program", section, section, locator, _serialize(network),
                        (), tuple(writes), tuple(dict.fromkeys(calls)), PLCSemanticState.OPAQUE, src,
                    )
                    fact = SchneiderGraphicalRegion(sid, section, "FBD", locator, PLCSemanticState.OPAQUE, str(exc), (), statement.writes, (), len(list(network)))
                    outputs = ()
                statements.append(statement)
                logic.extend(outputs)
                facts.append(fact)
                if fact.semantic_state is not PLCSemanticState.FULL:
                    warnings.append(f"Control Expert V4 FBD {section}/{fact.locator} withheld or partial: {fact.reason}.")
    return statements, logic, facts, warnings


def _remove_legacy_graphical(project) -> None:
    project.logic_statements = [item for item in project.logic_statements if item.language not in {"LD", "FBD"}]
    project.output_logic = [
        item for item in project.output_logic
        if not (
            item.language in {"LD", "FBD"}
            and item.origin in {"CONTROL_EXPERT_LD", "CONTROL_EXPERT_FBD"}
        )
    ]
    project.warnings = [
        warning for warning in project.warnings
        if not warning.startswith("Control Expert LD ")
        and not warning.startswith("Control Expert FBD section ")
    ]


def _refresh_counts(project) -> None:
    project.instruction_total = len(project.logic_statements)
    project.instruction_semantic_count = sum(item.semantic_state is PLCSemanticState.FULL for item in project.logic_statements)
    st = [item for item in project.logic_statements if item.language == "ST"]
    project.st_statement_total = len(st)
    project.st_statement_semantic_count = sum(item.semantic_state is PLCSemanticState.FULL for item in st)
    project.partially_modeled_instruction_names = sorted(
        {item.language for item in project.logic_statements if item.semantic_state is not PLCSemanticState.FULL}
    )


def _rebuild_v3_projection(project):
    facts = _v3._facts(project)
    if facts is None:
        return None
    old_projected = set(facts.projected_logic_ids)
    if old_projected:
        project.output_logic = [item for item in project.output_logic if item.id not in old_projected]
    calls = list(facts.calls)
    reachable = {item.casefold() for item in facts.reachable_dfb_types}
    blocked, conflicts = _v3._writer_conflicts(project, calls, reachable)
    if blocked:
        calls = [
            replace(call, semantic_state=PLCSemanticState.PARTIAL, resolution="competing_output_writer")
            if call.id in blocked else call
            for call in calls
        ]
    reachable_names, unreachable, active_gaps = _v3._reachability(facts.dfb_types, calls)
    reachable = {item.casefold() for item in reachable_names}
    _v3._update_section_call_statements(project, calls)
    projected = _v3._project_logic(project, calls, facts.local_logic, facts.dfb_types, reachable, blocked)
    updated = replace(
        facts,
        calls=tuple(calls),
        reachable_dfb_types=reachable_names,
        unreachable_dfb_types=unreachable,
        active_call_gaps=active_gaps,
        writer_conflicts=conflicts,
        projected_logic_ids=projected,
    )
    setattr(project, "_schneider_v3_facts", updated)
    return updated


def _apply_writer_conflicts(project, modeled_ids: set[str]):
    logic_by_id = {item.id: item for item in project.output_logic if item.id in modeled_ids}
    writers: dict[str, set[str]] = defaultdict(set)
    labels: dict[str, str] = {}
    for statement in project.logic_statements:
        for ref in statement.writes:
            key = ref.casefold()
            labels.setdefault(key, ref)
            writers[key].add(statement.id)
    conflicts = {
        key for key in {logic.output_tag.casefold() for logic in logic_by_id.values()}
        if len(writers.get(key, set())) > 1
    }
    if not conflicts:
        return modeled_ids, ()
    removed = {
        logic.id for logic in logic_by_id.values()
        if logic.output_tag.casefold() in conflicts
    }
    project.output_logic = [item for item in project.output_logic if item.id not in removed]
    updated_statements = []
    for statement in project.logic_statements:
        if any(ref.casefold() in conflicts for ref in statement.writes) and statement.language in {"LD", "FBD"}:
            updated_statements.append(replace(statement, semantic_state=PLCSemanticState.PARTIAL))
        else:
            updated_statements.append(statement)
    project.logic_statements = updated_statements
    return modeled_ids - removed, tuple(sorted((labels[key] for key in conflicts), key=str.casefold))


def _normalize_fat(project, tests: list[FATTestCase], modeled_ids: set[str]) -> list[FATTestCase]:
    graphical = {
        (logic.source.locator, logic.output_tag.casefold()): logic
        for logic in project.output_logic
        if logic.id in modeled_ids
    }
    result = []
    for test in tests:
        logic = graphical.get((test.source.locator, test.output_tag.casefold()))
        if logic is None:
            result.append(test)
            continue
        result.append(
            replace(
                test,
                title=f"Verify Schneider V4 {logic.language} Boolean theorem for {logic.output_tag} at {logic.source.locator}",
                limitations=(
                    f"Generated from bounded Control Expert V4 {logic.language} graphical Boolean semantics; no PLC scan was executed.",
                    "Stateful coils/blocks, compare/operate instructions, jumps, unsupported geometry, task ordering, I/O refresh, and process physics remain outside this theorem.",
                ),
            )
        )
    return result


def _gap_fat(project, facts: SchneiderV4Facts) -> list[FATTestCase]:
    by_id = {item.id: item for item in project.logic_statements}
    tests: list[FATTestCase] = []
    for fact in facts.regions:
        if fact.semantic_state is PLCSemanticState.FULL:
            continue
        statement = by_id.get(fact.id)
        if statement is None:
            continue
        digest = hashlib.sha1(f"{fact.id}:{fact.reason}".encode()).hexdigest()[:10]
        tests.append(
            FATTestCase(
                id=f"FAT-SCHNEIDER-GRAPH4-{digest}",
                title=f"Verify withheld Schneider {fact.language} region {fact.section}/{fact.locator}",
                source=statement.source,
                output_tag=fact.writes[0] if fact.writes else f"{fact.section}:{fact.locator}",
                preconditions={},
                expected="Engineer-executed evidence must confirm intended outputs/interlocks for the exact source-linked graphical region.",
                method="RUNTIME_FAT_REQUIRED",
                scenario="SCHNEIDER_GRAPHICAL_RUNTIME",
                limitations=(
                    f"Static Schneider V4 graphical proof withheld or partial: {fact.reason}.",
                    "DevAgent does not execute Control Expert Simulator, HIL, or a real Modicon PLC.",
                ),
            )
        )
    return enrich_fat_procedures(project, tests)


def _facts(project) -> SchneiderV4Facts | None:
    return getattr(project, "_schneider_v4_facts", None)


def schneider_capability_profile_v4(project) -> dict[str, object]:
    facts = _facts(project)
    profile = dict(_PREVIOUS_CAPABILITY(project))
    if facts is None:
        return profile
    reasons: dict[str, int] = defaultdict(int)
    for item in facts.regions:
        if item.semantic_state is not PLCSemanticState.FULL:
            reasons[item.reason] += 1
    profile.update(
        {
            "schema": "devagent-schneider-control-expert-capability-v4",
            "graphical_regions": len(facts.regions),
            "graphical_full": len(facts.modeled),
            "graphical_partial": len(facts.partial),
            "graphical_opaque": len(facts.withheld),
            "ld_regions": sum(item.language == "LD" for item in facts.regions),
            "ld_modeled": sum(item.language == "LD" for item in facts.modeled),
            "fbd_regions": sum(item.language == "FBD" for item in facts.regions),
            "fbd_modeled": sum(item.language == "FBD" and item.semantic_state is PLCSemanticState.FULL for item in facts.regions),
            "graphical_output_theorems": len(facts.modeled_logic_ids),
            "graphical_writer_conflicts": list(facts.writer_conflicts),
            "graphical_withheld_reasons": dict(sorted(reasons.items())),
            "bounded_graphical_semantics": (
                "LD whole-network cell graph with open/closed BOOL/EBOOL contacts, HLink/VLink/shortCircuit series-parallel connectivity, and normal coils; "
                "FBD stateless AND/AND_BOOL/OR/OR_BOOL graphs with exact effectiveParameter/linkFB binding and optional pin inversion"
            ),
        }
    )
    return profile


def _v4_checks(facts: SchneiderV4Facts) -> list[StaticCheck]:
    gaps = len(facts.partial) + len(facts.withheld)
    return [
        StaticCheck(
            "SCHNEIDER_V4_GRAPHICAL_SEMANTICS",
            StaticCheckStatus.PASS if not gaps else StaticCheckStatus.NOT_PROVEN,
            f"Deterministically modeled {len(facts.modeled)}/{len(facts.regions)} Schneider LD/FBD graphical region(s); partial/opaque={gaps}.",
            tuple(item.id for item in facts.regions),
        ),
        StaticCheck(
            "SCHNEIDER_V4_GRAPHICAL_FAIL_CLOSED",
            StaticCheckStatus.PASS,
            "Only bounded combinational LD/FBD Boolean topology is eligible for FULL semantics; edge/stateful/control/compare/operate/call/ambiguous geometry remains withheld with engineer FAT.",
            tuple(item.id for item in facts.regions if item.semantic_state is not PLCSemanticState.FULL),
        ),
        StaticCheck(
            "SCHNEIDER_V4_GRAPHICAL_WRITERS",
            StaticCheckStatus.NOT_PROVEN if facts.writer_conflicts else StaticCheckStatus.PASS,
            (
                f"Competing source writers block {len(facts.writer_conflicts)} Schneider V4 graphical output theorem(s)."
                if facts.writer_conflicts
                else "No competing source writers were found for retained Schneider V4 graphical output theorems."
            ),
            facts.writer_conflicts,
        ),
    ]


def analyze_schneider_control_expert_v4(path: Path) -> PLCEngineeringResult:
    base = _PREVIOUS_ANALYZER(path)
    project = base.project
    sources = list(_graphical_sources(path, project))
    if not sources:
        return base

    _remove_legacy_graphical(project)
    statements, new_logic, region_facts, warnings = _build_graphical(path, project)
    project.logic_statements.extend(statements)
    project.output_logic.extend(new_logic)
    project.warnings.extend(warnings)
    modeled_ids = {item.id for item in new_logic}

    modeled_ids, conflicts = _apply_writer_conflicts(project, modeled_ids)
    _refresh_counts(project)
    v3facts = _rebuild_v3_projection(project)

    facts = SchneiderV4Facts(tuple(region_facts), tuple(sorted(modeled_ids)), conflicts)
    setattr(project, "_schneider_v4_facts", facts)
    _refresh_counts(project)

    graph = build_dependency_graph(project)
    if v3facts is not None:
        graph = _v3._augment_graph(graph, v3facts)

    fat_tests = _normalize_fat(project, _v1._fat_tests(project), modeled_ids)
    if v3facts is not None:
        fat_tests.extend(_v3._call_gap_fat(project, v3facts))
    fat_tests.extend(_gap_fat(project, facts))
    fat_tests = list({item.id: item for item in fat_tests}.values())

    checks = _v1._checks(project, graph, fat_tests)
    if v3facts is not None:
        checks.extend(_v3._v3_checks(project, v3facts))
    checks.extend(_v4_checks(facts))

    profile = schneider_capability_profile_v4(project)
    closure_complete = (
        v3facts is None
        or (profile.get("execution_closure") == "COMPLETE" and not v3facts.writer_conflicts)
    )
    if profile["static_contract"] == "NO_EXECUTABLE_LOGIC":
        outcome = PLCOutcome.BLOCKED
    elif profile["static_contract"] == "COMPLETE" and closure_complete and not conflicts:
        outcome = PLCOutcome.STATICALLY_VERIFIED
    else:
        outcome = PLCOutcome.PARTIALLY_VERIFIED

    limitations = [item.replace("Schneider V3", "Schneider V4") for item in base.limitations]
    limitations.append(
        "Schneider V4 retires V1 per-typeLine LD proof for graphical sections and re-evaluates complete Control Expert LD rung groups using the exported cell geometry. Only open/closed BOOL/EBOOL contacts, HLink/VLink/shortCircuit connectivity, and normal coils are statically modeled."
    )
    limitations.append(
        "Schneider V4 FBD proof is limited to stateless AND/AND_BOOL/OR/OR_BOOL Boolean graphs with exact effectiveParameter/linkFB binding and compatible Boolean symbols. Timers/counters, DFB/EFB state, EN/ENO execution gating, compare/operate blocks, edge/state coils, jumps/returns, ambiguous links, cycles, and unsupported objects remain PARTIAL/OPAQUE."
    )
    limitations.append(
        "A graphical Boolean theorem is local source proof only; task ordering, I/O refresh, retained state, external process behavior, Control Expert Simulator, HIL, and real Modicon execution remain outside static verification."
    )
    return PLCEngineeringResult(outcome, project, graph, fat_tests, checks, list(dict.fromkeys(limitations)))


def _v4_verify_requirement(previous, requirement, engineering, evidence, tests):
    result = previous(requirement, engineering, evidence, tests)
    facts = _facts(engineering.project)
    if facts is None:
        return result
    v4_ids = set(facts.modeled_logic_ids)
    outputs = {
        item.output_tag.casefold()
        for item in engineering.project.output_logic
        if item.id in v4_ids
    }
    if not set(result.evidence_ids).intersection(v4_ids) and not any(tag.casefold() in outputs for tag in result.matched_tags):
        return result
    return replace(
        result,
        summary=(
            result.summary
            .replace("bounded Schneider Control Expert V1 theorem", "bounded Schneider Control Expert V4 graphical Boolean theorem")
            .replace("bounded Schneider Boolean theorem", "bounded Schneider V4 graphical Boolean theorem")
        ),
    )


def _v4_evidence(previous, engineering):
    result = list(previous(engineering))
    facts = _facts(engineering.project)
    if facts is None:
        return result
    project = engineering.project
    existing = {item.id for item in result}
    for fact in facts.regions:
        evidence_id = f"SCHNEIDER-V4-REGION:{fact.id}"
        if evidence_id in existing:
            continue
        result.append(
            EvidenceItem(
                evidence_id,
                "SCHNEIDER_GRAPHICAL_REGION_V4",
                f"{fact.language} {fact.section}/{fact.locator}: {fact.semantic_state.value} ({fact.reason}).",
                f"{fact.section}:{fact.locator}",
                project.metadata.source_sha256,
                {
                    "statement_id": fact.id,
                    "language": fact.language,
                    "semantic_state": fact.semantic_state.value,
                    "reason": fact.reason,
                    "reads": list(fact.reads),
                    "writes": list(fact.writes),
                    "logic_ids": list(fact.logic_ids),
                    "elements": fact.elements,
                },
            )
        )
    return result


def _v4_risks(previous, engineering, verifications, executions, engineering_findings):
    result = list(previous(engineering, verifications, executions, engineering_findings))
    facts = _facts(engineering.project)
    if facts is None:
        return result
    gaps = [item for item in facts.regions if item.semantic_state is not PLCSemanticState.FULL]
    if gaps:
        reasons = sorted({item.reason for item in gaps})
        result.append(
            RiskFinding(
                stable_id("RISK", "SCHNEIDER_GRAPHICAL_V4", engineering.project.metadata.source_sha256, *reasons),
                "SEMANTIC_COVERAGE",
                "Schneider LD/FBD regions remain outside bounded V4 proof",
                Severity.HIGH,
                f"{len(gaps)} graphical region(s) remain PARTIAL/OPAQUE: {'; '.join(reasons[:8])}.",
                "Unmodeled stateful, control, block, or ambiguous graphical behavior can affect commissioning and requirement outcomes beyond the static Boolean theorem.",
                "Review the exact source-linked LD/FBD regions and execute their engineer FAT procedures; do not promote graphical traceability to static PASS.",
                tuple(item.id for item in gaps),
            )
        )
    if facts.writer_conflicts:
        result.append(
            RiskFinding(
                stable_id("RISK", "SCHNEIDER_GRAPHICAL_WRITERS_V4", *facts.writer_conflicts),
                "MULTIPLE_WRITERS",
                "Competing Schneider writers block graphical Boolean proof",
                Severity.HIGH,
                f"{len(facts.writer_conflicts)} Schneider V4 graphical output target(s) have competing source writers.",
                "Final value can depend on section/task order or arbitration outside the local graphical theorem.",
                "Disposition writer ownership and execution order, then rerun requirement verification and FAT.",
                facts.writer_conflicts,
            )
        )
    return result


def _v4_render(previous, project) -> str:
    base = previous(project)
    facts = _facts(project)
    if facts is None:
        return base
    profile = schneider_capability_profile_v4(project)
    insertion = (
        "### Schneider V4 LD/FBD Boolean Theorem\n\n"
        f"- Graphical regions discovered: **{profile['graphical_regions']}**\n"
        f"- Fully modeled graphical regions: **{profile['graphical_full']}**\n"
        f"- PARTIAL graphical regions: **{profile['graphical_partial']}**\n"
        f"- OPAQUE graphical regions: **{profile['graphical_opaque']}**\n"
        f"- LD modeled: **{profile['ld_modeled']}/{profile['ld_regions']}**\n"
        f"- FBD fully modeled: **{profile['fbd_modeled']}/{profile['fbd_regions']}**\n"
        f"- Retained graphical Boolean output theorems: **{profile['graphical_output_theorems']}**\n"
        f"- Writer conflicts withholding proof: **{len(profile['graphical_writer_conflicts'])}**\n"
        "- LD proof is rebuilt from complete Control Expert cell geometry rather than treating each typeLine as an independent rung. Supported topology is open/closed BOOL/EBOOL contacts, HLink/VLink/shortCircuit series-parallel wiring, and normal coils.\n"
        "- FBD proof requires stateless AND/AND_BOOL/OR/OR_BOOL blocks, exact effectiveParameter/linkFB binding, Boolean symbols, acyclic signal flow, and unambiguous writers. Inverted Boolean pins are modeled.\n"
        "- Edge/state coils, compare/operate blocks, timers/counters, DFB/EFB state, EN/ENO gating, jumps/returns, unresolved types, unsupported geometry, ambiguous links, and cycles remain PARTIAL/OPAQUE with engineer FAT.\n"
        "- DevAgent does not execute Control Expert Simulator, HIL, or a real Modicon PLC.\n\n"
    )
    marker = "### Schneider V3 DFB Call / Interface Closure"
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

    _v1.analyze_schneider_control_expert = analyze_schneider_control_expert_v4
    _v1.schneider_capability_profile = schneider_capability_profile_v4
    _dispatch.analyze_schneider_control_expert = analyze_schneider_control_expert_v4
    _integration.schneider_capability_profile = schneider_capability_profile_v4

    def verify_requirement(requirement, engineering, evidence, tests):
        return _v4_verify_requirement(previous_verify, requirement, engineering, evidence, tests)

    def evidence_index(engineering):
        return _v4_evidence(previous_evidence, engineering)

    def detect_risks(engineering, verifications, executions, engineering_findings):
        return _v4_risks(previous_risks, engineering, verifications, executions, engineering_findings)

    def render(project):
        return _v4_render(previous_render, project)

    _integration._verify_requirement = verify_requirement
    _integration._evidence_index = evidence_index
    _integration._detect_risks = detect_risks
    _report._render = render
    _INSTALLED = True


__all__ = [
    "SchneiderGraphicalRegion",
    "SchneiderV4Facts",
    "analyze_schneider_control_expert_v4",
    "install",
    "schneider_capability_profile_v4",
]
