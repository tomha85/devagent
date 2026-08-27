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
    PLCOutputLogic,
    PLCOutcome,
    PLCSemanticState,
    StaticCheck,
    StaticCheckStatus,
)
from devagent.plc.production_models import RiskFinding, Severity
from devagent.plc.production_utils import stable_id
from devagent.plc import siemens_call_graph_v3 as _v3
from devagent.plc import siemens_tia_v1 as _v1


_INSTALLED = False
_PREVIOUS_ANALYZER = _v3.analyze_siemens_tia_v3
_PREVIOUS_CAPABILITY = _v3.siemens_capability_profile_v3
_MAX_XML_CHARS = 8192
_MAX_PARTS = 160
_MAX_WIRES = 320
_MAX_PATHS = 128
_MAX_TERMS = 32
_MAX_COILS = 16
_ALLOWED_SCOPES = {"localvariable", "globalvariable"}
_BOOL_TYPES = {"BOOL", "BOOLEAN"}
_INPUT_PORT = re.compile(r"^in(?:\d+)?$", re.IGNORECASE)


@dataclass(frozen=True)
class SiemensFlgNetNetwork:
    statement_id: str
    block: str
    locator: str
    language: str
    semantic_state: PLCSemanticState
    reason: str
    parts: int
    wires: int
    outputs: tuple[str, ...] = ()
    logic_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SiemensV4Facts:
    networks: tuple[SiemensFlgNetNetwork, ...]
    modeled_logic_ids: tuple[str, ...]

    @property
    def modeled(self) -> tuple[SiemensFlgNetNetwork, ...]:
        return tuple(
            item for item in self.networks
            if item.semantic_state is PLCSemanticState.FULL
        )

    @property
    def withheld(self) -> tuple[SiemensFlgNetNetwork, ...]:
        return tuple(
            item for item in self.networks
            if item.semantic_state is not PLCSemanticState.FULL
        )


@dataclass(frozen=True)
class _Access:
    uid: str
    scope: str
    symbol: str | None
    constant: bool | None


@dataclass(frozen=True)
class _Part:
    uid: str
    name: str
    negated: tuple[str, ...]
    cardinality: int | None


class _Unsupported(ValueError):
    pass


def _lname(tag: str) -> str:
    return str(tag).split("}", 1)[-1]


def _attr(element, name: str) -> str:
    wanted = name.casefold()
    for key, value in element.attrib.items():
        if str(key).casefold() == wanted:
            return str(value)
    return ""


def _first_desc(parent, name: str):
    wanted = name.casefold()
    for child in parent.iter():
        if _lname(child.tag).casefold() == wanted:
            return child
    return None


def _symbol_from_access(element) -> str | None:
    symbol = _first_desc(element, "Symbol")
    if symbol is None:
        return None
    names = []
    for component in symbol.iter():
        if _lname(component.tag).casefold() != "component":
            continue
        name = _attr(component, "Name").strip()
        if name:
            names.append(name)
    return ".".join(names) or None


def _constant_from_access(element) -> bool | None:
    value = _first_desc(element, "ConstantValue")
    if value is None:
        return None
    text = (value.text or "").strip().upper()
    if text == "TRUE":
        return True
    if text == "FALSE":
        return False
    return None


def _bool_tag_type(project, block: str, symbol: str) -> str | None:
    folded = symbol.casefold()
    scope = f"program:{block}".casefold()
    local = [
        item for item in project.tags
        if item.scope.casefold() == scope and item.name.casefold() == folded
    ]
    if len(local) == 1:
        return local[0].data_type
    controller = [
        item for item in project.tags
        if item.scope.casefold() == "controller" and item.name.casefold() == folded
    ]
    if len(controller) == 1:
        return controller[0].data_type
    return None


def _require_bool_symbol(project, block: str, access: _Access) -> str:
    if access.symbol is None:
        raise _Unsupported("symbol_access_required")
    if access.scope.casefold() not in _ALLOWED_SCOPES:
        raise _Unsupported(
            f"unsupported_access_scope:{access.scope or 'unknown'}"
        )
    dtype = _bool_tag_type(project, block, access.symbol)
    if dtype is None:
        raise _Unsupported(f"unresolved_symbol:{access.symbol}")
    normalized = re.sub(r'["\s]', "", str(dtype)).upper()
    if normalized not in _BOOL_TYPES:
        raise _Unsupported(
            f"non_boolean_symbol:{access.symbol}:{dtype}"
        )
    return access.symbol


def _var(name: str, required: bool = True):
    return ("VAR", name, required)


def _const(value: bool):
    return ("CONST", value)


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


def _merge_paths(
    left: dict[str, bool],
    right: dict[str, bool],
) -> dict[str, bool] | None:
    result = dict(left)
    folded = {key.casefold(): key for key in result}
    for key, value in right.items():
        existing = folded.get(key.casefold())
        if existing is not None and result[existing] != value:
            return None
        if existing is None:
            result[key] = value
            folded[key.casefold()] = key
    if len(result) > _MAX_TERMS:
        raise _Unsupported("boolean_term_limit")
    return result


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
            if len(result) > _MAX_PATHS:
                raise _Unsupported("boolean_path_limit")
    elif kind == "AND":
        result = [{}]
        for child in expr[1]:
            child_paths = _dnf(child)
            merged_paths: list[dict[str, bool]] = []
            for left in result:
                for right in child_paths:
                    merged = _merge_paths(left, right)
                    if merged is not None:
                        merged_paths.append(merged)
                        if len(merged_paths) > _MAX_PATHS:
                            raise _Unsupported("boolean_path_limit")
            result = merged_paths
    else:
        raise _Unsupported("unsupported_boolean_expression")

    unique: dict[
        tuple[tuple[str, bool], ...], dict[str, bool]
    ] = {}
    for path in result:
        key = tuple(
            sorted(
                (name.casefold(), value)
                for name, value in path.items()
            )
        )
        unique.setdefault(key, path)
    return list(unique.values())


def _parse_parts(flgnet):
    parts_parent = _first_desc(flgnet, "Parts")
    wires_parent = _first_desc(flgnet, "Wires")
    if parts_parent is None or wires_parent is None:
        raise _Unsupported("missing_parts_or_wires")

    accesses: dict[str, _Access] = {}
    parts: dict[str, _Part] = {}
    direct_parts = list(parts_parent)
    if len(direct_parts) > _MAX_PARTS:
        raise _Unsupported("part_limit")
    for element in direct_parts:
        local = _lname(element.tag)
        uid = _attr(element, "UId") or _attr(element, "Uid")
        if not uid:
            raise _Unsupported("missing_uid")
        if local == "Access":
            accesses[uid] = _Access(
                uid,
                _attr(element, "Scope"),
                _symbol_from_access(element),
                _constant_from_access(element),
            )
            continue
        if local != "Part":
            raise _Unsupported(f"unsupported_parts_element:{local}")
        name = _attr(element, "Name")
        negated = tuple(
            _attr(child, "Name").casefold()
            for child in element.iter()
            if _lname(child.tag).casefold() == "negated"
            and _attr(child, "Name")
        )
        cardinality = None
        for child in element.iter():
            if _lname(child.tag).casefold() != "templatevalue":
                continue
            if _attr(child, "Name").casefold() != "card":
                continue
            try:
                cardinality = int((child.text or "").strip())
            except ValueError as exc:
                raise _Unsupported("invalid_cardinality") from exc
        parts[uid] = _Part(uid, name, negated, cardinality)
    wires = list(wires_parent)
    if len(wires) > _MAX_WIRES:
        raise _Unsupported("wire_limit")
    return accesses, parts, wires


def _operand_bindings(
    accesses: dict[str, _Access],
    parts: dict[str, _Part],
    wires,
):
    bindings: dict[str, _Access] = {}
    signal_wires = []
    for wire in wires:
        children = list(wire)
        operand_targets = [
            item for item in children
            if _lname(item.tag).casefold() == "namecon"
            and _attr(item, "Name").casefold() == "operand"
        ]
        if not operand_targets:
            signal_wires.append(wire)
            continue
        if any(
            _lname(item.tag).casefold() in {"powerrail", "opencon"}
            for item in children
        ):
            raise _Unsupported("invalid_operand_wire")
        ident = [
            item for item in children
            if _lname(item.tag).casefold() == "identcon"
        ]
        other_ports = [
            item for item in children
            if _lname(item.tag).casefold() == "namecon"
            and _attr(item, "Name").casefold() != "operand"
        ]
        if len(ident) != 1 or other_ports:
            raise _Unsupported("ambiguous_operand_wire")
        access = accesses.get(
            _attr(ident[0], "UId") or _attr(ident[0], "Uid")
        )
        if access is None:
            raise _Unsupported("operand_access_missing")
        for target in operand_targets:
            part_uid = _attr(target, "UId") or _attr(target, "Uid")
            if part_uid not in parts:
                raise _Unsupported("operand_target_missing")
            if part_uid in bindings and bindings[part_uid] != access:
                raise _Unsupported("multiple_operand_bindings")
            bindings[part_uid] = access
    return bindings, signal_wires


def _signal_graph(
    accesses: dict[str, _Access],
    parts: dict[str, _Part],
    wires,
    language: str,
):
    inputs: dict[
        tuple[str, str], tuple[tuple[str, str | None], ...]
    ] = {}
    for wire in wires:
        children = list(wire)
        if any(
            _lname(item.tag).casefold() == "opencon"
            for item in children
        ):
            raise _Unsupported("open_connection")
        sources: list[tuple[str, str | None]] = []
        targets: list[tuple[str, str]] = []
        power = [
            item for item in children
            if _lname(item.tag).casefold() == "powerrail"
        ]
        if len(power) > 1:
            raise _Unsupported("multiple_powerrails_on_wire")
        if power:
            sources.append(("POWER", None))
        unknown_ports = []
        for item in children:
            local = _lname(item.tag).casefold()
            if local == "identcon":
                if language == "LAD":
                    raise _Unsupported("unsupported_lad_direct_signal")
                uid = _attr(item, "UId") or _attr(item, "Uid")
                if uid not in accesses:
                    raise _Unsupported("signal_access_missing")
                sources.append(("ACCESS", uid))
            elif local == "namecon":
                uid = _attr(item, "UId") or _attr(item, "Uid")
                port = _attr(item, "Name")
                folded = port.casefold()
                if uid not in parts:
                    raise _Unsupported("signal_part_missing")
                if folded == "out":
                    sources.append(("PART", uid))
                elif _INPUT_PORT.fullmatch(port):
                    targets.append((uid, folded))
                else:
                    unknown_ports.append(port or "unknown")
            elif local != "powerrail":
                raise _Unsupported(
                    f"unsupported_wire_endpoint:{_lname(item.tag)}"
                )
        if unknown_ports:
            raise _Unsupported(
                "unsupported_signal_port:"
                + ",".join(sorted(set(unknown_ports)))
            )
        if not targets:
            if sources:
                raise _Unsupported("dangling_signal_wire")
            continue
        if not sources:
            raise _Unsupported("undriven_signal_input")
        if language == "FBD" and len(sources) != 1:
            raise _Unsupported("multiple_fbd_drivers")
        if power and len(sources) != 1:
            raise _Unsupported("mixed_powerrail_driver")
        for target in targets:
            if target in inputs:
                raise _Unsupported("multiple_input_wires")
            inputs[target] = tuple(sources)
    return inputs


def _evaluate_network(project, statement, flgnet, language: str):
    accesses, parts, wires = _parse_parts(flgnet)
    allowed = {
        "LAD": {"Contact", "Coil"},
        "FBD": {"A", "O", "Coil"},
    }[language]
    unsupported = sorted(
        {item.name for item in parts.values() if item.name not in allowed}
    )
    if unsupported:
        raise _Unsupported(
            "unsupported_part:" + ",".join(unsupported)
        )

    operands, signal_wires = _operand_bindings(
        accesses, parts, wires
    )
    signals = _signal_graph(
        accesses, parts, signal_wires, language
    )
    block = statement.source.program or statement.owner_name
    visiting: set[str] = set()
    memo: dict[str, tuple] = {}

    def access_expr(uid: str):
        access = accesses[uid]
        if access.constant is not None:
            return _const(access.constant)
        return _var(_require_bool_symbol(project, block, access))

    def input_expr(part_uid: str, port: str):
        sources = signals.get((part_uid, port))
        if not sources:
            raise _Unsupported(
                f"missing_signal_input:{part_uid}:{port}"
            )
        expressions = []
        for kind, uid in sources:
            if kind == "POWER":
                expressions.append(_const(True))
            elif kind == "ACCESS":
                assert uid is not None
                expressions.append(access_expr(uid))
            elif kind == "PART":
                assert uid is not None
                expressions.append(part_expr(uid))
            else:
                raise _Unsupported("unknown_signal_source")
        return (
            _or(*expressions)
            if language == "LAD"
            else expressions[0]
        )

    def part_expr(uid: str):
        if uid in memo:
            return memo[uid]
        if uid in visiting:
            raise _Unsupported("signal_cycle")
        visiting.add(uid)
        part = parts[uid]
        if part.name == "Contact":
            if any(name != "operand" for name in part.negated):
                raise _Unsupported("unsupported_contact_negation")
            access = operands.get(uid)
            if access is None:
                raise _Unsupported("contact_operand_missing")
            symbol = _require_bool_symbol(project, block, access)
            condition = _var(
                symbol,
                "operand" not in part.negated,
            )
            result = _and(input_expr(uid, "in"), condition)
        elif part.name in {"A", "O"}:
            ports = sorted(
                (port for owner, port in signals if owner == uid),
                key=lambda value: (len(value), value),
            )
            if not ports:
                raise _Unsupported(f"{part.name}_inputs_missing")
            if (
                part.cardinality is not None
                and len(ports) != part.cardinality
            ):
                raise _Unsupported(
                    f"{part.name}_cardinality_mismatch"
                )
            values = []
            for port in ports:
                value = input_expr(uid, port)
                if port.casefold() in part.negated:
                    value = _not(value)
                values.append(value)
            unknown = [
                name for name in part.negated
                if name not in {item.casefold() for item in ports}
            ]
            if unknown:
                raise _Unsupported(
                    "unsupported_gate_negation:"
                    + ",".join(unknown)
                )
            result = (
                _and(*values)
                if part.name == "A"
                else _or(*values)
            )
        else:
            raise _Unsupported(f"non_signal_part:{part.name}")
        visiting.remove(uid)
        memo[uid] = result
        return result

    coils = [
        item for item in parts.values()
        if item.name == "Coil"
    ]
    if not coils:
        raise _Unsupported("output_coil_missing")
    if len(coils) > _MAX_COILS:
        raise _Unsupported("coil_limit")

    logic: list[PLCOutputLogic] = []
    all_reads: list[str] = []
    all_writes: list[str] = []
    for coil in coils:
        if any(name != "operand" for name in coil.negated):
            raise _Unsupported("unsupported_coil_negation")
        access = operands.get(coil.uid)
        if access is None:
            raise _Unsupported("coil_operand_missing")
        output = _require_bool_symbol(project, block, access)
        expression = input_expr(coil.uid, "in")
        if "operand" in coil.negated:
            expression = _not(expression)
        paths = _dnf(expression)
        if any(
            name.casefold() == output.casefold()
            for path in paths
            for name in path
        ):
            raise _Unsupported(f"self_reference:{output}")
        reads = sorted(
            {name for path in paths for name in path},
            key=str.casefold,
        )
        all_reads.extend(reads)
        all_writes.append(output)
        digest = hashlib.sha1(
            (
                f"{statement.id}:{coil.uid}:{output}:"
                f"{repr(paths)}"
            ).encode("utf-8")
        ).hexdigest()[:14]
        logic.append(
            PLCOutputLogic(
                id=f"SIEMENS-FLG4-{digest}",
                output_tag=output,
                instruction="ASSIGN_BOOL",
                paths=tuple(
                    PLCLogicPath(
                        tuple(
                            PLCBooleanTerm(tag, required)
                            for tag, required in sorted(
                                path.items(),
                                key=lambda item: item[0].casefold(),
                            )
                        )
                    )
                    for path in paths
                ),
                source=statement.source,
                language=language,
                origin=(
                    f"SIEMENS_FLGNET_V4:"
                    f"{statement.id}:{coil.uid}"
                ),
                semantic_state=PLCSemanticState.FULL,
            )
        )
    return (
        tuple(dict.fromkeys(all_reads)),
        tuple(dict.fromkeys(all_writes)),
        tuple(logic),
        len(parts),
        len(wires),
    )


def _parse_statement(project, statement):
    if (
        statement.language not in {"LAD", "FBD"}
        or statement.semantic_state is not PLCSemanticState.OPAQUE
    ):
        return statement, (), None
    block = statement.source.program or statement.owner_name
    locator = statement.source.locator or statement.locator
    if len(statement.text) >= _MAX_XML_CHARS:
        return statement, (), SiemensFlgNetNetwork(
            statement.id,
            block,
            locator,
            statement.language,
            PLCSemanticState.OPAQUE,
            "network_serialization_limit_or_truncation",
            0,
            0,
        )
    try:
        root = ET.fromstring(statement.text)
    except ET.ParseError:
        return statement, (), SiemensFlgNetNetwork(
            statement.id,
            block,
            locator,
            statement.language,
            PLCSemanticState.OPAQUE,
            "invalid_or_truncated_compile_unit_xml",
            0,
            0,
        )
    flgnet = _first_desc(root, "FlgNet")
    if flgnet is None:
        return statement, (), SiemensFlgNetNetwork(
            statement.id,
            block,
            locator,
            statement.language,
            PLCSemanticState.OPAQUE,
            "flgnet_missing",
            0,
            0,
        )
    try:
        reads, writes, logic, parts, wires = _evaluate_network(
            project,
            statement,
            flgnet,
            statement.language,
        )
    except _Unsupported as exc:
        parts_node = _first_desc(flgnet, "Parts")
        wires_node = _first_desc(flgnet, "Wires")
        return statement, (), SiemensFlgNetNetwork(
            statement.id,
            block,
            locator,
            statement.language,
            PLCSemanticState.OPAQUE,
            str(exc),
            len(list(parts_node)) if parts_node is not None else 0,
            len(list(wires_node)) if wires_node is not None else 0,
        )
    updated = replace(
        statement,
        reads=reads,
        writes=writes,
        semantic_state=PLCSemanticState.FULL,
    )
    fact = SiemensFlgNetNetwork(
        statement.id,
        block,
        locator,
        statement.language,
        PLCSemanticState.FULL,
        "bounded_flgnet_boolean_network",
        parts,
        wires,
        writes,
        tuple(item.id for item in logic),
    )
    return updated, logic, fact


def _legacy_warning_matches(statement, warning: str) -> bool:
    prefix = (
        f"TIA XML {statement.language} network "
        f"{statement.source.program or statement.owner_name}/"
        f"{statement.source.locator or statement.locator} "
    )
    return (
        warning.startswith(prefix)
        and "V1 withholds executable behavior proof" in warning
    )


def _refresh_counts(project) -> None:
    project.instruction_total = len(project.logic_statements)
    project.instruction_semantic_count = sum(
        item.semantic_state is PLCSemanticState.FULL
        for item in project.logic_statements
    )
    scl = [
        item for item in project.logic_statements
        if item.language == "SCL"
    ]
    project.st_statement_total = len(scl)
    project.st_statement_semantic_count = sum(
        item.semantic_state is PLCSemanticState.FULL
        for item in scl
    )
    project.partially_modeled_instruction_names = sorted(
        {
            item.language
            for item in project.logic_statements
            if item.semantic_state is not PLCSemanticState.FULL
        }
    )


def _rebuild_v3_projection(project):
    facts = _v3._facts(project)
    if facts is None:
        return None
    previous_projected = set(facts.projected_logic_ids)
    if previous_projected:
        project.output_logic = [
            item for item in project.output_logic
            if item.id not in previous_projected
        ]
    calls = list(facts.calls)
    reachable = {item.casefold() for item in facts.reachable_blocks}
    blocked, conflicts = _v3._writer_conflicts(
        project, calls, reachable
    )
    if blocked:
        calls = [
            replace(
                call,
                semantic_state=PLCSemanticState.PARTIAL,
                resolution="competing_output_writer",
            )
            if call.id in blocked
            else call
            for call in calls
        ]
    reachable_names, unreachable, active_gaps = _v3._reachability(
        facts.blocks, calls
    )
    reachable = {item.casefold() for item in reachable_names}
    _v3._update_call_statements(project, calls)
    projected = _v3._project_logic(
        project,
        calls,
        facts.blocks,
        reachable,
        blocked,
    )
    updated = replace(
        facts,
        calls=tuple(calls),
        reachable_blocks=reachable_names,
        unreachable_blocks=unreachable,
        active_call_gaps=active_gaps,
        writer_conflicts=conflicts,
        projected_logic_ids=projected,
    )
    setattr(project, "_siemens_v3_facts", updated)
    return updated


def _normalize_flgnet_fat(
    project,
    tests: list[FATTestCase],
) -> list[FATTestCase]:
    flgnet = {
        (
            logic.source.program or "",
            logic.source.locator or "",
            logic.output_tag.casefold(),
        ): logic
        for logic in project.output_logic
        if logic.origin.startswith("SIEMENS_FLGNET_V4:")
    }
    result = []
    for test in tests:
        logic = flgnet.get(
            (
                test.source.program or "",
                test.source.locator or "",
                test.output_tag.casefold(),
            )
        )
        if logic is None:
            result.append(test)
            continue
        result.append(
            replace(
                test,
                title=(
                    f"Verify Siemens {logic.language} FlgNet Boolean network "
                    f"for {logic.output_tag} at {logic.source.locator}"
                ),
                limitations=(
                    (
                        f"Generated from bounded Siemens {logic.language} FlgNet "
                        "Boolean contact/gate/coil topology; no PLC scan was executed."
                    ),
                    (
                        "Timers/counters, calls, stateful coils, comparisons, "
                        "unsupported parts, scheduling, I/O update behavior, and "
                        "process physics remain outside this theorem."
                    ),
                ),
            )
        )
    return result


def _flgnet_gap_fat(
    project,
    facts: SiemensV4Facts,
) -> list[FATTestCase]:
    tests = []
    by_id = {item.id: item for item in project.logic_statements}
    for item in facts.withheld:
        statement = by_id.get(item.statement_id)
        if statement is None:
            continue
        digest = hashlib.sha1(
            f"{item.statement_id}:{item.reason}".encode("utf-8")
        ).hexdigest()[:10]
        tests.append(
            FATTestCase(
                id=f"FAT-SIEMENS-FLGNET-{digest}",
                title=(
                    f"Verify withheld Siemens {item.language} network "
                    f"{item.block}/{item.locator}"
                ),
                source=statement.source,
                output_tag=f"{item.block}:{item.locator}",
                preconditions={},
                expected=(
                    "Engineer-executed evidence must exercise the source-linked "
                    "network and confirm intended outputs/interlocks for all "
                    "relevant branches."
                ),
                method="RUNTIME_FAT_REQUIRED",
                scenario="SIEMENS_FLGNET_RUNTIME",
                limitations=(
                    f"Static FlgNet proof withheld: {item.reason}.",
                    "DevAgent does not execute PLCSIM, HIL, or a real PLC.",
                ),
            )
        )
    return enrich_fat_procedures(project, tests)


def _v4_checks(facts: SiemensV4Facts) -> list[StaticCheck]:
    modeled = len(facts.modeled)
    withheld = len(facts.withheld)
    return [
        StaticCheck(
            "SIEMENS_V4_FLGNET_SEMANTICS",
            (
                StaticCheckStatus.PASS
                if not withheld
                else StaticCheckStatus.NOT_PROVEN
            ),
            (
                f"Deterministically modeled {modeled}/{len(facts.networks)} "
                f"LAD/FBD FlgNet network(s); withheld={withheld}."
            ),
            tuple(item.statement_id for item in facts.networks),
        ),
        StaticCheck(
            "SIEMENS_V4_FLGNET_FAIL_CLOSED",
            StaticCheckStatus.PASS,
            (
                "Only bounded Boolean Access/Contact/Coil and FBD A/O topology "
                "is eligible for FULL semantics; unsupported/stateful/ambiguous "
                "networks remain OPAQUE with engineer FAT."
            ),
            tuple(item.statement_id for item in facts.withheld),
        ),
    ]


def _facts(project) -> SiemensV4Facts | None:
    return getattr(project, "_siemens_v4_facts", None)


def siemens_capability_profile_v4(project) -> dict[str, object]:
    facts = _facts(project)
    if facts is None:
        return dict(_PREVIOUS_CAPABILITY(project))
    profile = dict(_PREVIOUS_CAPABILITY(project))
    reasons: dict[str, int] = defaultdict(int)
    for item in facts.withheld:
        reasons[item.reason] += 1
    profile.update(
        {
            "schema": "devagent-siemens-tia-capability-v4",
            "flgnet_networks": len(facts.networks),
            "flgnet_modeled": len(facts.modeled),
            "flgnet_withheld": len(facts.withheld),
            "lad_modeled": sum(
                item.language == "LAD" for item in facts.modeled
            ),
            "fbd_modeled": sum(
                item.language == "FBD" for item in facts.modeled
            ),
            "flgnet_output_theorems": len(facts.modeled_logic_ids),
            "flgnet_withheld_reasons": dict(sorted(reasons.items())),
            "bounded_flgnet_semantics": (
                "LAD Powerrail/contact/normal-coil series-parallel Boolean "
                "topology and FBD A/O Boolean gates feeding normal coils; exact "
                "Access UId, Boolean type, and Wire binding required"
            ),
        }
    )
    return profile


def analyze_siemens_tia_v4(path: Path) -> PLCEngineeringResult:
    base = _PREVIOUS_ANALYZER(path)
    project = base.project
    candidates = [
        item for item in project.logic_statements
        if item.language in {"LAD", "FBD"}
        and item.semantic_state is PLCSemanticState.OPAQUE
    ]
    if not candidates:
        return base

    updated_statements = []
    new_logic: list[PLCOutputLogic] = []
    network_facts: list[SiemensFlgNetNetwork] = []
    modeled_ids: set[str] = set()
    for statement in project.logic_statements:
        updated, logic, fact = _parse_statement(project, statement)
        updated_statements.append(updated)
        if fact is None:
            continue
        network_facts.append(fact)
        new_logic.extend(logic)
        if fact.semantic_state is PLCSemanticState.FULL:
            modeled_ids.add(statement.id)

    project.logic_statements = updated_statements
    existing = {item.id for item in project.output_logic}
    for item in new_logic:
        if item.id not in existing:
            project.output_logic.append(item)
            existing.add(item.id)

    project.warnings = [
        warning for warning in project.warnings
        if not any(
            statement.id in modeled_ids
            and _legacy_warning_matches(statement, warning)
            for statement in candidates
        )
    ]
    candidate_by_id = {item.id: item for item in candidates}
    for fact in network_facts:
        if fact.semantic_state is PLCSemanticState.FULL:
            continue
        statement = candidate_by_id[fact.statement_id]
        project.warnings = [
            warning for warning in project.warnings
            if not _legacy_warning_matches(statement, warning)
        ]
        project.warnings.append(
            f"Siemens V4 withholds {fact.language} FlgNet "
            f"{fact.block}/{fact.locator}: {fact.reason}."
        )

    facts = SiemensV4Facts(
        tuple(network_facts),
        tuple(item.id for item in new_logic),
    )
    setattr(project, "_siemens_v4_facts", facts)
    project.metadata = replace(
        project.metadata,
        schema_revision="SIEMENS-TIA-EXPORT-V4",
    )
    _refresh_counts(project)

    v3facts = _rebuild_v3_projection(project)
    graph = build_dependency_graph(project)
    if v3facts is not None:
        graph = _v3._augment_graph(graph, v3facts)

    fat_tests = _normalize_flgnet_fat(
        project,
        _v1._siemens_fat_tests(project),
    )
    if v3facts is not None:
        fat_tests.extend(_v3._call_gap_fat(project, v3facts))
    fat_tests.extend(_flgnet_gap_fat(project, facts))
    fat_tests = list({item.id: item for item in fat_tests}.values())

    checks = _v1._siemens_checks(project, graph, fat_tests)
    if v3facts is not None:
        checks.extend(_v3._v3_checks(project, v3facts))
    checks.extend(_v4_checks(facts))

    profile = siemens_capability_profile_v4(project)
    closure_complete = (
        v3facts is None
        or (
            profile.get("execution_closure") == "COMPLETE"
            and not v3facts.writer_conflicts
        )
    )
    outcome = (
        PLCOutcome.STATICALLY_VERIFIED
        if profile["static_contract"] == "COMPLETE"
        and closure_complete
        else PLCOutcome.PARTIALLY_VERIFIED
    )
    if profile["static_contract"] == "NO_EXECUTABLE_LOGIC":
        outcome = PLCOutcome.BLOCKED

    limitations = [
        item.replace("Siemens V3", "Siemens V4")
        for item in base.limitations
    ]
    limitations.append(
        "Siemens V4 adds bounded SimaticML FlgNet proof for normal Boolean LAD "
        "Powerrail/contact/coil series-parallel topology and FBD Boolean A/O/"
        "normal-coil topology with exact UId/symbol/wire binding."
    )
    limitations.append(
        "S/R/edge coils, timers/counters, comparisons, MOVE/arithmetic, Call "
        "parts, OpenCon/unknown ports, unresolved/non-Boolean symbols, oversized/"
        "truncated networks, and cyclic/ambiguous wiring remain OPAQUE and require "
        "engineer FAT."
    )
    limitations.append(
        "V4 remains offline source analysis only; DevAgent does not execute "
        "PLCSIM, HIL, a real Siemens PLC, or process physics."
    )
    return PLCEngineeringResult(
        outcome,
        project,
        graph,
        fat_tests,
        checks,
        list(dict.fromkeys(limitations)),
    )


def _v4_verify_requirement(
    previous,
    requirement,
    engineering,
    evidence,
    tests,
):
    result = previous(requirement, engineering, evidence, tests)
    facts = _facts(engineering.project)
    if facts is None:
        return result
    local_ids = set(facts.modeled_logic_ids)
    v4_evidence = set(result.evidence_ids).intersection(local_ids)
    if not v4_evidence:
        v4_outputs = {
            logic.output_tag.casefold()
            for logic in engineering.project.output_logic
            if logic.id in local_ids
        }
        if not any(
            tag.casefold() in v4_outputs
            for tag in result.matched_tags
        ):
            return result
    return replace(
        result,
        summary=(
            result.summary
            .replace(
                "bounded Siemens SCL assignment theorem",
                "bounded Siemens LAD/FBD FlgNet Boolean theorem",
            )
            .replace(
                "modeled SCL dependencies",
                "modeled FlgNet Boolean dependencies",
            )
        ),
    )


def _v4_risks(
    previous,
    engineering,
    verifications,
    executions,
    engineering_findings,
):
    result = list(
        previous(
            engineering,
            verifications,
            executions,
            engineering_findings,
        )
    )
    facts = _facts(engineering.project)
    if facts is None or not facts.withheld:
        return result
    reasons = sorted({item.reason for item in facts.withheld})
    result.append(
        RiskFinding(
            stable_id(
                "RISK",
                "SIEMENS_FLGNET_V4",
                engineering.project.metadata.source_sha256,
                *reasons,
            ),
            "SEMANTIC_COVERAGE",
            "Siemens LAD/FBD networks remain outside bounded V4 proof",
            Severity.HIGH,
            (
                f"{len(facts.withheld)} FlgNet network(s) remain OPAQUE: "
                f"{'; '.join(reasons[:8])}."
            ),
            (
                "Unmodeled stateful/instruction/wiring behavior can affect "
                "commissioning and requirement outcomes beyond the static theorem."
            ),
            (
                "Review the exact source-linked networks and execute their generated "
                "engineer FAT procedures or export a deeper supported representation."
            ),
            tuple(item.statement_id for item in facts.withheld),
        )
    )
    return result


def _v4_semantic_section(previous, project) -> str:
    base = previous(project)
    facts = _facts(project)
    if facts is None:
        return base
    profile = siemens_capability_profile_v4(project)
    insertion = (
        "### Siemens V4 LAD/FBD FlgNet Boolean Theorem\n\n"
        f"- FlgNet networks discovered: **{profile['flgnet_networks']}**\n"
        f"- Deterministically modeled: **{profile['flgnet_modeled']}**\n"
        f"- Withheld / engineer FAT required: **{profile['flgnet_withheld']}**\n"
        f"- LAD modeled: **{profile['lad_modeled']}**\n"
        f"- FBD modeled: **{profile['fbd_modeled']}**\n"
        f"- FlgNet Boolean output theorems: **{profile['flgnet_output_theorems']}**\n"
        "- Proof requires exact SimaticML Access UId, Boolean symbol/type, supported "
        "Part names, complete Wire topology, acyclic signal flow, and normal-coil "
        "writer identity.\n"
        "- Supported V4 subset: LAD Powerrail/Contact/Coil series-parallel Boolean "
        "logic and FBD A/O Boolean gates feeding normal coils.\n"
        "- Stateful S/R/edge coils, timers/counters, comparisons, calls, OpenCon, "
        "unknown ports/parts, unresolved symbols, and oversized/truncated networks "
        "stay OPAQUE and receive engineer-executed FAT rather than static PASS.\n"
        "- V4 does not execute PLCSIM, HIL, or a real PLC.\n\n"
    )
    marker = "### Siemens V3 Call/Interface Execution Closure"
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
    previous_risks = _integration._siemens_detect_risks
    previous_section = _integration._siemens_semantic_section

    _v1.analyze_siemens_tia = analyze_siemens_tia_v4
    _v1.siemens_capability_profile = siemens_capability_profile_v4
    _dispatch.analyze_siemens_tia = analyze_siemens_tia_v4
    _integration.siemens_capability_profile = siemens_capability_profile_v4

    def verify_requirement(requirement, engineering, evidence, tests):
        return _v4_verify_requirement(
            previous_verify,
            requirement,
            engineering,
            evidence,
            tests,
        )

    def detect_risks(
        engineering,
        verifications,
        executions,
        engineering_findings,
    ):
        return _v4_risks(
            previous_risks,
            engineering,
            verifications,
            executions,
            engineering_findings,
        )

    def semantic_section(project):
        return _v4_semantic_section(previous_section, project)

    _integration._siemens_verify_requirement = verify_requirement
    _integration._siemens_detect_risks = detect_risks
    _integration._siemens_semantic_section = semantic_section
    _INSTALLED = True


__all__ = [
    "SiemensFlgNetNetwork",
    "SiemensV4Facts",
    "analyze_siemens_tia_v4",
    "install",
    "siemens_capability_profile_v4",
]
