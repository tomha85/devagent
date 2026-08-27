from __future__ import annotations

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
from devagent.plc import siemens_flgnet_v4 as _v4
from devagent.plc import siemens_tia_v1 as _v1


_INSTALLED = False
_PREVIOUS_ANALYZER = _v4.analyze_siemens_tia_v4
_PREVIOUS_CAPABILITY = _v4.siemens_capability_profile_v4
_RUNTIME_PARTS = {"TON", "TOF", "TP", "CTU", "CTD"}
_COMPARE_PARTS = {"Eq": "EQ", "Ne": "NE", "Gt": "GT", "Ge": "GE", "Lt": "LT", "Le": "LE"}
_SIMPLE_TYPES = {
    "BOOL", "BYTE", "WORD", "DWORD", "LWORD",
    "SINT", "USINT", "INT", "UINT", "DINT", "UDINT", "LINT", "ULINT",
    "REAL", "LREAL", "TIME",
}
_MAX_EXTENDED_PARTS = 192
_MAX_EXTENDED_WIRES = 384


@dataclass(frozen=True)
class SiemensV4ActionFact:
    statement_id: str
    block: str
    locator: str
    language: str
    instruction: str
    target: str
    reads: tuple[str, ...]
    condition_paths: tuple[tuple[tuple[str, bool], ...], ...] = ()
    source_value: str | None = None
    comparison: str | None = None
    semantic_state: PLCSemanticState = PLCSemanticState.FULL
    reason: str = "bounded_local_action"


@dataclass(frozen=True)
class SiemensV4RuntimeFact:
    statement_id: str
    block: str
    locator: str
    language: str
    instruction: str
    reason: str


@dataclass(frozen=True)
class SiemensV4ExtendedFacts:
    actions: tuple[SiemensV4ActionFact, ...]
    runtime_contracts: tuple[SiemensV4RuntimeFact, ...]
    visual_call_ids: tuple[str, ...]


@dataclass(frozen=True)
class _Value:
    text: str
    data_type: str
    ref: str | None = None


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


def _first(parent, name: str):
    wanted = name.casefold()
    for item in parent.iter():
        if _lname(item.tag).casefold() == wanted:
            return item
    return None


def _components(element) -> str | None:
    names = [
        _attr(item, "Name").strip()
        for item in element.iter()
        if _lname(item.tag).casefold() == "component" and _attr(item, "Name").strip()
    ]
    return ".".join(names) or None


def _type_name(value: str) -> str:
    return _v3._type_name(value).upper()


def _block_constants(path: Path) -> dict[str, dict[str, tuple[str, str]]]:
    result: dict[str, dict[str, tuple[str, str]]] = {}
    try:
        _, files = _v1._supported_sources(path)
    except Exception:
        return result
    for source, _relative in files:
        if source.suffix.lower() != ".xml":
            continue
        try:
            root = ET.parse(source).getroot()
        except (OSError, ET.ParseError):
            continue
        for block in root.iter():
            kind = _lname(block.tag)
            if kind not in {"SW.Blocks.OB", "SW.Blocks.FB", "SW.Blocks.FC"}:
                continue
            name = _v1._clean_name(_v1._child_text(block, "Name") or "")
            if not name:
                continue
            constants = result.setdefault(name.casefold(), {})
            for section in block.iter():
                if _lname(section.tag).casefold() != "section":
                    continue
                if _attr(section, "Name").casefold() not in {"constant", "const"}:
                    continue
                for member in list(section):
                    if _lname(member.tag).casefold() != "member":
                        continue
                    mname = _attr(member, "Name").strip()
                    dtype = _attr(member, "Datatype").strip()
                    start = _first(member, "StartValue")
                    if mname and dtype and start is not None and (start.text or "").strip():
                        constants[mname.casefold()] = (dtype, (start.text or "").strip())
    return result


def _access_value(project, block: str, element, constants) -> _Value:
    scope = _attr(element, "Scope").casefold()
    if scope in {"localvariable", "globalvariable"}:
        ref = _components(element)
        if not ref:
            raise _Unsupported("symbol_access_required")
        dtype = _v3._symbol_type(project, block, ref)
        if dtype is None:
            raise _Unsupported(f"unresolved_symbol:{ref}")
        return _Value(ref, _type_name(dtype), ref)
    if scope in {"literalconstant", "typedconstant"}:
        constant = _first(element, "Constant")
        if constant is None:
            raise _Unsupported("constant_payload_missing")
        dtype_node = _first(constant, "ConstantType")
        value_node = _first(constant, "ConstantValue")
        if value_node is None or not (value_node.text or "").strip():
            raise _Unsupported("constant_value_missing")
        dtype = (dtype_node.text or "").strip() if dtype_node is not None else ""
        return _Value((value_node.text or "").strip(), _type_name(dtype) if dtype else "UNKNOWN", None)
    if scope == "localconstant":
        constant = _first(element, "Constant")
        name = _attr(constant, "Name").strip() if constant is not None else ""
        entry = constants.get(block.casefold(), {}).get(name.casefold())
        if not name or entry is None:
            raise _Unsupported(f"unresolved_local_constant:{name or 'unknown'}")
        return _Value(entry[1], _type_name(entry[0]), None)
    raise _Unsupported(f"unsupported_access_scope:{scope or 'unknown'}")


def _parse_flgnet(statement):
    if statement.semantic_state is not PLCSemanticState.OPAQUE or statement.language not in {"LAD", "FBD"}:
        return None
    try:
        root = ET.fromstring(statement.text)
    except ET.ParseError:
        return None
    flgnet = _first(root, "FlgNet")
    if flgnet is None:
        return None
    parts_parent = _first(flgnet, "Parts")
    wires_parent = _first(flgnet, "Wires")
    if parts_parent is None or wires_parent is None:
        return None
    if len(list(parts_parent)) > _MAX_EXTENDED_PARTS or len(list(wires_parent)) > _MAX_EXTENDED_WIRES:
        return None
    accesses = {}
    parts = {}
    calls = {}
    for item in list(parts_parent):
        uid = _attr(item, "UId") or _attr(item, "Uid")
        if not uid:
            raise _Unsupported("missing_uid")
        kind = _lname(item.tag)
        if kind == "Access":
            accesses[uid] = item
        elif kind == "Part":
            parts[uid] = item
        elif kind == "Call":
            calls[uid] = item
        else:
            raise _Unsupported(f"unsupported_parts_element:{kind}")
    wires = list(wires_parent)
    return flgnet, accesses, parts, calls, wires


def _wire_nodes(wire):
    power = False
    opened = False
    idents: list[str] = []
    names: list[tuple[str, str]] = []
    for item in list(wire):
        kind = _lname(item.tag).casefold()
        if kind == "powerrail":
            power = True
        elif kind == "opencon":
            opened = True
        elif kind == "identcon":
            idents.append(_attr(item, "UId") or _attr(item, "Uid"))
        elif kind == "namecon":
            names.append((_attr(item, "UId") or _attr(item, "Uid"), _attr(item, "Name")))
        else:
            raise _Unsupported(f"unsupported_wire_endpoint:{_lname(item.tag)}")
    return power, opened, idents, names


def _wire_for_port(wires, uid: str, port: str):
    matches = []
    for wire in wires:
        power, opened, idents, names = _wire_nodes(wire)
        if (uid, port) in names:
            matches.append((power, opened, idents, names))
    if len(matches) != 1:
        raise _Unsupported(f"port_wire_count:{uid}:{port}:{len(matches)}")
    return matches[0]


def _operand_access(wires, accesses, uid: str):
    power, opened, idents, names = _wire_for_port(wires, uid, "operand")
    if power or opened or len(idents) != 1:
        raise _Unsupported(f"invalid_operand_binding:{uid}")
    other = [(a, b) for a, b in names if (a, b) != (uid, "operand")]
    if other:
        raise _Unsupported(f"ambiguous_operand_binding:{uid}")
    access = accesses.get(idents[0])
    if access is None:
        raise _Unsupported(f"operand_access_missing:{uid}")
    return access


def _bool_paths(project, block: str, uid: str, port: str, accesses, parts, wires):
    memo = {}
    visiting = set()

    def source_expr(owner: str, p: str):
        power, opened, idents, names = _wire_for_port(wires, owner, p)
        if opened:
            raise _Unsupported("open_connection")
        exprs = []
        if power:
            exprs.append(_v4._const(True))
        for access_uid in idents:
            access = accesses.get(access_uid)
            if access is None:
                raise _Unsupported("signal_access_missing")
            value = _access_value(project, block, access, {})
            if value.data_type not in {"BOOL", "BOOLEAN"} or value.ref is None:
                raise _Unsupported("boolean_symbol_required")
            exprs.append(_v4._var(value.ref))
        for other_uid, other_port in names:
            if other_uid == owner and other_port == p:
                continue
            if other_port.casefold() != "out":
                continue
            exprs.append(part_expr(other_uid))
        if not exprs:
            raise _Unsupported(f"undriven_signal_input:{owner}:{p}")
        return _v4._or(*exprs) if len(exprs) > 1 else exprs[0]

    def part_expr(part_uid: str):
        if part_uid in memo:
            return memo[part_uid]
        if part_uid in visiting:
            raise _Unsupported("signal_cycle")
        part = parts.get(part_uid)
        if part is None:
            raise _Unsupported("signal_part_missing")
        visiting.add(part_uid)
        name = _attr(part, "Name")
        if name == "Contact":
            access = _operand_access(wires, accesses, part_uid)
            value = _access_value(project, block, access, {})
            if value.ref is None or value.data_type not in {"BOOL", "BOOLEAN"}:
                raise _Unsupported("contact_boolean_operand_required")
            negated = any(
                _lname(item.tag).casefold() == "negated"
                and _attr(item, "Name").casefold() == "operand"
                for item in part.iter()
            )
            result = _v4._and(source_expr(part_uid, "in"), _v4._var(value.ref, not negated))
        elif name in {"A", "O"}:
            input_ports = sorted(
                {
                    port for wire in wires for _power, _opened, _idents, names in [_wire_nodes(wire)]
                    for node_uid, port in names
                    if node_uid == part_uid and re.fullmatch(r"in\d*", port, flags=re.IGNORECASE)
                }
            )
            if not input_ports:
                raise _Unsupported(f"{name}_inputs_missing")
            vals = [source_expr(part_uid, p) for p in input_ports]
            result = _v4._and(*vals) if name == "A" else _v4._or(*vals)
        else:
            raise _Unsupported(f"unsupported_boolean_part:{name}")
        visiting.remove(part_uid)
        memo[part_uid] = result
        return result

    expr = source_expr(uid, port)
    raw = _v4._dnf(expr)
    return tuple(
        tuple(sorted(path.items(), key=lambda item: item[0].casefold()))
        for path in raw
    )


def _same_type(left: _Value, right: _Value) -> bool:
    if left.data_type == "UNKNOWN" or right.data_type == "UNKNOWN":
        return False
    return left.data_type == right.data_type and left.data_type in _SIMPLE_TYPES


def _compare_fact(project, block, statement, part_uid, part, accesses, wires, constants):
    name = _attr(part, "Name")
    operator = _COMPARE_PARTS.get(name)
    if operator is None:
        raise _Unsupported(f"unsupported_compare:{name}")
    left_wire = _wire_for_port(wires, part_uid, "in1")
    right_wire = _wire_for_port(wires, part_uid, "in2")
    vals = []
    for record in (left_wire, right_wire):
        power, opened, idents, names = record
        if power or opened or names or len(idents) != 1:
            raise _Unsupported("comparison_simple_operands_required")
        access = accesses.get(idents[0])
        if access is None:
            raise _Unsupported("comparison_access_missing")
        vals.append(_access_value(project, block, access, constants))
    if not _same_type(vals[0], vals[1]):
        raise _Unsupported(f"comparison_type_mismatch:{vals[0].data_type}:{vals[1].data_type}")
    pre_paths = _bool_paths(project, block, part_uid, "pre", accesses, {}, wires) if statement.language == "LAD" else ()
    reads = tuple(item.ref for item in vals if item.ref)
    description = f"{vals[0].text} {operator} {vals[1].text}"
    return vals[0], vals[1], reads, pre_paths, description


def _evaluate_sr(project, statement, accesses, parts, calls, wires, constants):
    if calls:
        raise _Unsupported("mixed_call_and_sr_unsupported")
    action_parts = [(uid, p) for uid, p in parts.items() if _attr(p, "Name") in {"SCoil", "RCoil"}]
    other = {_attr(p, "Name") for p in parts.values()} - {"Contact", "A", "O", "SCoil", "RCoil"}
    if len(action_parts) != 1 or other:
        raise _Unsupported("sr_bounded_shape_required")
    uid, part = action_parts[0]
    block = statement.source.program or statement.owner_name
    access = _operand_access(wires, accesses, uid)
    target = _access_value(project, block, access, constants)
    if target.ref is None or target.data_type not in {"BOOL", "BOOLEAN"}:
        raise _Unsupported("sr_boolean_target_required")
    paths = _bool_paths(project, block, uid, "in", accesses, parts, wires)
    if any(key.casefold() == target.ref.casefold() for path in paths for key, _ in path):
        raise _Unsupported(f"self_reference:{target.ref}")
    reads = tuple(sorted({key for path in paths for key, _ in path}, key=str.casefold))
    instruction = "SET_BOOL" if _attr(part, "Name") == "SCoil" else "RESET_BOOL"
    action = SiemensV4ActionFact(
        statement.id, block, statement.locator, statement.language, instruction,
        target.ref, reads, paths,
    )
    logic = PLCOutputLogic(
        id=f"SIEMENS-FLG4-ACT-{hashlib.sha1((statement.id+uid+instruction+target.ref).encode()).hexdigest()[:14]}",
        output_tag=target.ref,
        instruction=instruction,
        paths=tuple(
            PLCLogicPath(tuple(PLCBooleanTerm(tag, required) for tag, required in path))
            for path in paths
        ),
        source=statement.source,
        language=statement.language,
        origin=f"SIEMENS_FLGNET_ACTION_V4:{statement.id}:{uid}",
        semantic_state=PLCSemanticState.FULL,
    )
    updated = replace(statement, reads=reads, writes=(target.ref,), semantic_state=PLCSemanticState.FULL)
    return updated, (logic,), action


def _evaluate_eq_move(project, statement, accesses, parts, calls, wires, constants):
    if calls:
        raise _Unsupported("mixed_call_and_move_unsupported")
    moves = [(uid, p) for uid, p in parts.items() if _attr(p, "Name") == "Move"]
    compares = [(uid, p) for uid, p in parts.items() if _attr(p, "Name") in _COMPARE_PARTS]
    allowed = {"Contact", "A", "O", "Move", *_COMPARE_PARTS.keys()}
    if len(moves) != 1 or len(compares) > 1 or any(_attr(p, "Name") not in allowed for p in parts.values()):
        raise _Unsupported("move_bounded_shape_required")
    move_uid, move = moves[0]
    block = statement.source.program or statement.owner_name
    in_wire = _wire_for_port(wires, move_uid, "in")
    power, opened, idents, names = in_wire
    if power or opened or names or len(idents) != 1:
        raise _Unsupported("move_simple_source_required")
    source_access = accesses.get(idents[0])
    if source_access is None:
        raise _Unsupported("move_source_access_missing")
    source = _access_value(project, block, source_access, constants)

    out_wire = None
    for wire in wires:
        record = _wire_nodes(wire)
        if (move_uid, "out1") in record[3]:
            if out_wire is not None:
                raise _Unsupported("move_multiple_output_wires")
            out_wire = record
    if out_wire is None:
        raise _Unsupported("move_output_missing")
    power, opened, idents, names = out_wire
    if power or opened or len(idents) != 1 or any(x != (move_uid, "out1") for x in names):
        raise _Unsupported("move_simple_destination_required")
    dest_access = accesses.get(idents[0])
    if dest_access is None:
        raise _Unsupported("move_destination_access_missing")
    dest = _access_value(project, block, dest_access, constants)
    if dest.ref is None or not _same_type(source, dest):
        raise _Unsupported(f"move_type_mismatch:{source.data_type}:{dest.data_type}")

    comparison = None
    condition_paths = ()
    reads = [source.ref] if source.ref else []
    if compares:
        cmp_uid, cmp_part = compares[0]
        _left, _right, cmp_reads, pre_paths, comparison = _compare_fact(
            project, block, statement, cmp_uid, cmp_part, accesses, wires, constants
        )
        reads.extend(cmp_reads)
        en_wire = _wire_for_port(wires, move_uid, "en")
        if (cmp_uid, "out") not in en_wire[3]:
            raise _Unsupported("move_compare_enable_binding_required")
        condition_paths = pre_paths
    else:
        condition_paths = _bool_paths(project, block, move_uid, "en", accesses, parts, wires)
        reads.extend(key for path in condition_paths for key, _ in path)
    reads = tuple(dict.fromkeys(item for item in reads if item))
    action = SiemensV4ActionFact(
        statement.id, block, statement.locator, statement.language, "MOVE",
        dest.ref, reads, condition_paths, source.text, comparison,
    )
    updated = replace(statement, reads=reads, writes=(dest.ref,), semantic_state=PLCSemanticState.FULL)
    return updated, (), action


def _visual_call(project, statement, accesses, parts, calls, wires, constants):
    if len(calls) != 1 or parts:
        raise _Unsupported("visual_call_single_call_shape_required")
    facts = _v3._facts(project)
    if facts is None:
        raise _Unsupported("v3_call_inventory_required")
    uid, call = next(iter(calls.items()))
    info = _first(call, "CallInfo")
    if info is None:
        raise _Unsupported("callinfo_missing")
    callee_name = _v1._clean_name(_attr(info, "Name"))
    block_type = _attr(info, "BlockType").upper()
    matches = [b for b in facts.blocks if b.name.casefold() == callee_name.casefold() and b.kind == block_type]
    if len(matches) != 1:
        raise _Unsupported(f"ambiguous_or_missing_call_target:{callee_name}")
    callee = matches[0]
    caller_name = statement.source.program or statement.owner_name
    caller_matches = [b for b in facts.blocks if b.name.casefold() == caller_name.casefold()]
    if len(caller_matches) != 1:
        raise _Unsupported("caller_block_missing")
    caller = caller_matches[0]

    instance_name = None
    instance = _first(info, "Instance")
    if callee.kind == "FB":
        if instance is None:
            raise _Unsupported("fb_instance_missing")
        instance_name = _components(instance)
        if not instance_name:
            raise _Unsupported("fb_instance_name_missing")
        scope = _attr(instance, "Scope").casefold()
        if scope == "globalvariable":
            if not any(
                db.name.casefold() == instance_name.casefold()
                and db.block_type.casefold() == callee.name.casefold()
                for db in facts.instance_dbs
            ):
                raise _Unsupported(f"instance_db_type_unproven:{instance_name}")
        elif scope == "localvariable":
            dtype = _v3._multi_instance_type(caller, instance_name)
            if dtype is None or dtype.casefold() != callee.name.casefold():
                raise _Unsupported(f"multi_instance_type_unproven:{instance_name}")
        else:
            raise _Unsupported(f"unsupported_instance_scope:{scope}")
    elif instance is not None:
        raise _Unsupported("fc_instance_not_allowed")

    en_wire = _wire_for_port(wires, uid, "en")
    if not en_wire[0] or en_wire[1] or en_wire[2] or any(x != (uid, "en") for x in en_wire[3]):
        raise _Unsupported("guarded_or_ambiguous_call_enable")

    declared = {}
    for param in info.iter():
        if _lname(param.tag).casefold() != "parameter":
            continue
        name = _attr(param, "Name")
        section = _attr(param, "Section")
        dtype = _type_name(_attr(param, "Type"))
        if name:
            declared[name.casefold()] = (name, section, dtype)

    pmap = _v3._param_map(callee)
    bindings = []
    reads = []
    writes = []
    for key, (formal_name, section, exported_type) in declared.items():
        param = pmap.get(key)
        if param is None:
            raise _Unsupported(f"call_unknown_formal:{formal_name}")
        direction = {
            "input": "VAR_INPUT", "output": "VAR_OUTPUT", "inout": "VAR_IN_OUT",
        }.get(section.casefold())
        if direction != param.direction or exported_type != _type_name(param.data_type):
            raise _Unsupported(f"call_interface_mismatch:{formal_name}")
        record = _wire_for_port(wires, uid, formal_name)
        power, opened, idents, names = record
        if power or opened or len(idents) != 1 or any(x != (uid, formal_name) for x in names):
            raise _Unsupported(f"call_simple_binding_required:{formal_name}")
        access = accesses.get(idents[0])
        if access is None:
            raise _Unsupported(f"call_access_missing:{formal_name}")
        actual = _access_value(project, caller.name, access, constants)
        if actual.ref is None or _type_name(actual.data_type) != _type_name(param.data_type):
            raise _Unsupported(f"call_actual_type_unproven:{formal_name}")
        operator = "=>" if direction == "VAR_OUTPUT" else ":="
        bindings.append(_v3.PLCParameterBinding(formal_name, actual.ref, direction, operator))
        if direction in {"VAR_INPUT", "VAR_IN_OUT"}:
            reads.append(actual.ref)
        if direction in {"VAR_OUTPUT", "VAR_IN_OUT"}:
            writes.append(actual.ref)

    seen = {b.formal.casefold() for b in bindings}
    required = {
        p.name.casefold() for p in callee.parameters
        if p.direction == "VAR_IN_OUT" or (p.direction == "VAR_INPUT" and not p.has_default)
    }
    missing = sorted(required - seen)
    if missing:
        raise _Unsupported("call_missing_required_binding:" + ",".join(missing))

    call_fact = _v3.PLCCallBinding(
        id=f"SIEMENS-CALL:{statement.id}",
        caller_block=caller.name,
        call_symbol=callee.name,
        callee_block=callee.name,
        instance_db=instance_name,
        bindings=tuple(bindings),
        source=statement.source,
        semantic_state=PLCSemanticState.FULL,
        resolution="bound_visual_flgnet_call",
    )
    updated = replace(
        statement,
        reads=tuple(dict.fromkeys(reads)),
        writes=tuple(dict.fromkeys(writes)),
        calls=(callee.name,),
        semantic_state=PLCSemanticState.FULL,
    )
    return updated, call_fact


def _runtime_facts(statement, parts):
    result = []
    for part in parts.values():
        name = _attr(part, "Name").upper()
        if name in _RUNTIME_PARTS:
            result.append(SiemensV4RuntimeFact(
                statement.id,
                statement.source.program or statement.owner_name,
                statement.locator,
                statement.language,
                name,
                "stateful_timer_counter_runtime_required",
            ))
    return result


def _action_fat(project, actions):
    tests = []
    for action in actions:
        pre = dict(action.condition_paths[0]) if len(action.condition_paths) == 1 else {}
        digest = hashlib.sha1(
            f"{action.statement_id}:{action.instruction}:{action.target}:{action.source_value}:{action.comparison}".encode()
        ).hexdigest()[:10]
        expected = (
            f"When the source-linked network is enabled, {action.target} receives {action.source_value}."
            if action.instruction == "MOVE"
            else f"When the source-linked network condition is TRUE, {action.target} is {'set' if action.instruction == 'SET_BOOL' else 'reset'} by the local Siemens action."
        )
        if action.comparison:
            expected += f" Enable comparison: {action.comparison}."
        tests.append(FATTestCase(
            id=f"FAT-SIEMENS-ACTION-{digest}",
            title=f"Verify Siemens {action.instruction} action for {action.target} at {action.locator}",
            source=next(s.source for s in project.logic_statements if s.id == action.statement_id),
            output_tag=action.target,
            preconditions=pre,
            expected=expected,
            method="ENGINEER_FAT_REQUIRED",
            scenario="SIEMENS_LOCAL_ACTION",
            limitations=(
                "Static proof covers the local source action and exact bindings only; final scan/process state, prior retentive state, scheduling, and other writers are not proven.",
                "DevAgent does not execute PLCSIM, HIL, or a real PLC.",
            ),
        ))
    return enrich_fat_procedures(project, tests)


def _runtime_fat(project, runtime_facts):
    tests = []
    by_id = {s.id: s for s in project.logic_statements}
    for fact in runtime_facts:
        statement = by_id.get(fact.statement_id)
        if statement is None:
            continue
        digest = hashlib.sha1(f"{fact.statement_id}:{fact.instruction}".encode()).hexdigest()[:10]
        tests.append(FATTestCase(
            id=f"FAT-SIEMENS-RUNTIME4-{digest}",
            title=f"Verify Siemens {fact.instruction} runtime behavior at {fact.block}/{fact.locator}",
            source=statement.source,
            output_tag=f"{fact.block}:{fact.locator}",
            preconditions={},
            expected=(
                f"Engineer evidence must exercise {fact.instruction} across its relevant state/time/count transitions and confirm outputs, reset behavior, and boundary conditions."
            ),
            method="RUNTIME_FAT_REQUIRED",
            scenario="SIEMENS_TIMER_COUNTER_RUNTIME",
            limitations=(
                f"{fact.instruction} is normalized as a runtime-dependent Siemens instruction; timing/counting state is not statically PASSed.",
                "DevAgent does not execute PLCSIM, HIL, or a real PLC.",
            ),
        ))
    return enrich_fat_procedures(project, tests)


def _facts(project):
    return getattr(project, "_siemens_v4_extended_facts", None)


def siemens_capability_profile_v4_extended(project):
    profile = dict(_PREVIOUS_CAPABILITY(project))
    facts = _facts(project)
    if facts is None:
        return profile
    profile.update({
        "schema": "devagent-siemens-tia-capability-v4",
        "flgnet_local_actions": len(facts.actions),
        "flgnet_set_actions": sum(a.instruction == "SET_BOOL" for a in facts.actions),
        "flgnet_reset_actions": sum(a.instruction == "RESET_BOOL" for a in facts.actions),
        "flgnet_move_actions": sum(a.instruction == "MOVE" for a in facts.actions),
        "flgnet_typed_comparisons": sum(bool(a.comparison) for a in facts.actions),
        "flgnet_visual_calls_bound": len(facts.visual_call_ids),
        "flgnet_runtime_contracts": len(facts.runtime_contracts),
        "flgnet_runtime_instructions": sorted({f.instruction for f in facts.runtime_contracts}),
    })
    return profile


def analyze_siemens_tia_v4_extended(path: Path) -> PLCEngineeringResult:
    base = _PREVIOUS_ANALYZER(path)
    project = base.project
    constants = _block_constants(path)
    actions = []
    runtime = []
    visual_calls = []
    new_logic = []
    updated_statements = []
    changed_ids = set()

    for statement in project.logic_statements:
        parsed = _parse_flgnet(statement)
        if parsed is None:
            updated_statements.append(statement)
            continue
        _flgnet, accesses, parts, calls, wires = parsed
        runtime.extend(_runtime_facts(statement, parts))
        updated = statement
        try:
            names = {_attr(p, "Name") for p in parts.values()}
            if names.intersection({"SCoil", "RCoil"}):
                updated, logic, action = _evaluate_sr(project, statement, accesses, parts, calls, wires, constants)
                new_logic.extend(logic)
                actions.append(action)
                changed_ids.add(statement.id)
            elif "Move" in names:
                updated, logic, action = _evaluate_eq_move(project, statement, accesses, parts, calls, wires, constants)
                new_logic.extend(logic)
                actions.append(action)
                changed_ids.add(statement.id)
            elif calls:
                updated, call_fact = _visual_call(project, statement, accesses, parts, calls, wires, constants)
                visual_calls.append(call_fact)
                changed_ids.add(statement.id)
        except _Unsupported:
            pass
        updated_statements.append(updated)

    if not actions and not runtime and not visual_calls:
        return base

    project.logic_statements = updated_statements
    existing = {x.id for x in project.output_logic}
    for logic in new_logic:
        if logic.id not in existing:
            project.output_logic.append(logic)
            existing.add(logic.id)

    v3facts = _v3._facts(project)
    if visual_calls and v3facts is not None:
        old = [c for c in v3facts.calls if c.id not in {v.id for v in visual_calls}]
        setattr(project, "_siemens_v3_facts", replace(v3facts, calls=tuple([*old, *visual_calls])))
    v3facts = _v4._rebuild_v3_projection(project)

    v4facts = _v4._facts(project)
    if v4facts is not None and changed_ids:
        networks = tuple(
            replace(
                n,
                semantic_state=PLCSemanticState.FULL,
                reason="bounded_extended_flgnet_semantics",
            ) if n.statement_id in changed_ids else n
            for n in v4facts.networks
        )
        setattr(project, "_siemens_v4_facts", replace(v4facts, networks=networks))

        changed_statements = {
            item.id: item for item in project.logic_statements if item.id in changed_ids
        }
        retained_warnings = []
        for warning in project.warnings:
            drop = False
            for statement in changed_statements.values():
                block_name = statement.source.program or statement.owner_name
                if warning.startswith(
                    f"TIA XML {statement.language} network {block_name}/{statement.locator} "
                ):
                    drop = True
                if warning.startswith(
                    f"Siemens V4 withholds {statement.language} FlgNet "
                    f"{block_name}/{statement.locator}:"
                ):
                    drop = True
            if not drop:
                retained_warnings.append(warning)
        project.warnings = list(dict.fromkeys(retained_warnings))

    ext = SiemensV4ExtendedFacts(tuple(actions), tuple(runtime), tuple(c.id for c in visual_calls))
    setattr(project, "_siemens_v4_extended_facts", ext)
    project.metadata = replace(project.metadata, schema_revision="SIEMENS-TIA-EXPORT-V4")

    _v4._refresh_counts(project)
    graph = build_dependency_graph(project)
    if v3facts is not None:
        graph = _v3._augment_graph(graph, v3facts)

    fat_tests = _v4._normalize_flgnet_fat(project, _v1._siemens_fat_tests(project))
    if v3facts is not None:
        fat_tests.extend(_v3._call_gap_fat(project, v3facts))
    current_v4 = _v4._facts(project)
    if current_v4 is not None:
        fat_tests.extend(_v4._flgnet_gap_fat(project, current_v4))
    fat_tests.extend(_action_fat(project, actions))
    fat_tests.extend(_runtime_fat(project, runtime))
    fat_tests = list({t.id: t for t in fat_tests}.values())

    checks = _v1._siemens_checks(project, graph, fat_tests)
    if v3facts is not None:
        checks.extend(_v3._v3_checks(project, v3facts))
    if current_v4 is not None:
        checks.extend(_v4._v4_checks(current_v4))
    checks.append(StaticCheck(
        "SIEMENS_V4_EXTENDED_FLGNET",
        StaticCheckStatus.PASS,
        (
            f"Normalized bounded Siemens visual local actions={len(actions)}, "
            f"bound visual calls={len(visual_calls)}, runtime contracts={len(runtime)}; "
            "unsupported variants remain fail-closed."
        ),
        tuple([*(a.statement_id for a in actions), *(c.id for c in visual_calls), *(r.statement_id for r in runtime)]),
    ))

    profile = siemens_capability_profile_v4_extended(project)
    closure_complete = (
        v3facts is None
        or (profile.get("execution_closure") == "COMPLETE" and not v3facts.writer_conflicts)
    )
    outcome = (
        PLCOutcome.STATICALLY_VERIFIED
        if profile["static_contract"] == "COMPLETE" and closure_complete
        else PLCOutcome.PARTIALLY_VERIFIED
    )
    if profile["static_contract"] == "NO_EXECUTABLE_LOGIC":
        outcome = PLCOutcome.BLOCKED

    limitations = list(base.limitations)
    limitations.append(
        "Siemens V4 also normalizes bounded SCoil/RCoil local actions, exact-type Move actions, Eq/Ne/Gt/Ge/Lt/Le comparison-gated Move patterns, and unguarded FlgNet FB/FC calls with exact interface/instance binding. Local action proof is not final scan/process-state proof."
    )
    limitations.append(
        "TON/TOF/TP/CTU/CTD are explicitly classified as runtime-dependent and receive engineer FAT; their timing/counting state never receives static PASS."
    )
    return PLCEngineeringResult(
        outcome, project, graph, fat_tests, checks, list(dict.fromkeys(limitations))
    )


def _semantic_section(previous, project):
    base = previous(project)
    facts = _facts(project)
    if facts is None:
        return base
    profile = siemens_capability_profile_v4_extended(project)
    text = (
        "### Siemens V4 Extended FlgNet Actions / Calls\n\n"
        f"- Stateful S/R local actions normalized: **{profile['flgnet_set_actions'] + profile['flgnet_reset_actions']}**\n"
        f"- MOVE local actions normalized: **{profile['flgnet_move_actions']}**\n"
        f"- Typed comparison-gated actions: **{profile['flgnet_typed_comparisons']}**\n"
        f"- Deterministically bound visual FB/FC calls: **{profile['flgnet_visual_calls_bound']}**\n"
        f"- Runtime timer/counter contracts: **{profile['flgnet_runtime_contracts']}**\n"
        "- S/R and MOVE are local source-action facts, not final retained/process-state PASS claims.\n"
        "- Visual calls require exact target/interface/type/instance binding and unconditional enable before entering V3 execution closure.\n"
        "- TON/TOF/TP/CTU/CTD remain runtime-dependent and require engineer-executed FAT.\n\n"
    )
    marker = "### Siemens V4 LAD/FBD FlgNet Boolean Theorem"
    return base.replace(marker, text + marker, 1) if marker in base else base + "\n\n" + text


def _risks(previous, engineering, verifications, executions, engineering_findings):
    result = list(previous(engineering, verifications, executions, engineering_findings))
    facts = _facts(engineering.project)
    if facts is None:
        return result
    if any(a.instruction in {"SET_BOOL", "RESET_BOOL"} for a in facts.actions):
        result.append(RiskFinding(
            stable_id("RISK", "SIEMENS_RETENTIVE_ACTION_V4", engineering.project.metadata.source_sha256),
            "STATEFUL_LOGIC",
            "Siemens S/R actions require final-state and writer-order FAT",
            Severity.MEDIUM,
            "Bounded SCoil/RCoil activation and target identity are normalized, but retained final state depends on prior state and execution order.",
            "A local set/reset action can be deterministic while final scan/process state remains runtime/order dependent.",
            "Review all writers and execute linked FAT across set, reset, power-cycle/initial-state, and competing-writer scenarios.",
            tuple(a.statement_id for a in facts.actions if a.instruction in {"SET_BOOL", "RESET_BOOL"}),
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

    _v4.analyze_siemens_tia_v4 = analyze_siemens_tia_v4_extended
    _v4.siemens_capability_profile_v4 = siemens_capability_profile_v4_extended
    _v1.analyze_siemens_tia = analyze_siemens_tia_v4_extended
    _v1.siemens_capability_profile = siemens_capability_profile_v4_extended
    _dispatch.analyze_siemens_tia = analyze_siemens_tia_v4_extended
    _integration.siemens_capability_profile = siemens_capability_profile_v4_extended

    def semantic_section(project):
        return _semantic_section(previous_section, project)

    def detect_risks(engineering, verifications, executions, engineering_findings):
        return _risks(previous_risks, engineering, verifications, executions, engineering_findings)

    _integration._siemens_semantic_section = semantic_section
    _integration._siemens_detect_risks = detect_risks
    _INSTALLED = True


__all__ = [
    "SiemensV4ActionFact",
    "SiemensV4ExtendedFacts",
    "SiemensV4RuntimeFact",
    "analyze_siemens_tia_v4_extended",
    "install",
    "siemens_capability_profile_v4_extended",
]
