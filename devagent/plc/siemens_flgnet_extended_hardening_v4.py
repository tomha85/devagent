from __future__ import annotations

from dataclasses import replace

from devagent.plc import siemens_flgnet_extended_v4 as _ext


_INSTALLED = False
_PREVIOUS_VISUAL_CALL = _ext._visual_call


def _single_access_on_port(wires, accesses, uid: str, port: str):
    power, opened, idents, names = _ext._wire_for_port(wires, uid, port)
    others = [item for item in names if item != (uid, port)]
    if power or opened or others or len(idents) != 1:
        raise _ext._Unsupported(f"simple_access_binding_required:{uid}:{port}")
    access = accesses.get(idents[0])
    if access is None:
        raise _ext._Unsupported(f"access_missing:{uid}:{port}")
    return access


def _compare_fact(
    project,
    block,
    statement,
    part_uid,
    part,
    accesses,
    wires,
    constants,
):
    name = _ext._attr(part, "Name")
    operator = _ext._COMPARE_PARTS.get(name)
    if operator is None:
        raise _ext._Unsupported(f"unsupported_compare:{name}")

    left = _ext._access_value(
        project,
        block,
        _single_access_on_port(wires, accesses, part_uid, "in1"),
        constants,
    )
    right = _ext._access_value(
        project,
        block,
        _single_access_on_port(wires, accesses, part_uid, "in2"),
        constants,
    )
    if not _ext._same_type(left, right):
        raise _ext._Unsupported(
            f"comparison_type_mismatch:{left.data_type}:{right.data_type}"
        )

    pre_paths = (
        _ext._bool_paths(
            project,
            block,
            part_uid,
            "pre",
            accesses,
            {},
            wires,
        )
        if statement.language == "LAD"
        else ()
    )
    reads = tuple(item.ref for item in (left, right) if item.ref)
    description = f"{left.text} {operator} {right.text}"
    return left, right, reads, pre_paths, description


def _evaluate_eq_move(
    project,
    statement,
    accesses,
    parts,
    calls,
    wires,
    constants,
):
    if calls:
        raise _ext._Unsupported("mixed_call_and_move_unsupported")

    moves = [
        (uid, part)
        for uid, part in parts.items()
        if _ext._attr(part, "Name") == "Move"
    ]
    compares = [
        (uid, part)
        for uid, part in parts.items()
        if _ext._attr(part, "Name") in _ext._COMPARE_PARTS
    ]
    allowed = {"Contact", "A", "O", "Move", *_ext._COMPARE_PARTS.keys()}
    if (
        len(moves) != 1
        or len(compares) > 1
        or any(_ext._attr(part, "Name") not in allowed for part in parts.values())
    ):
        raise _ext._Unsupported("move_bounded_shape_required")

    move_uid, _move = moves[0]
    block = statement.source.program or statement.owner_name
    source = _ext._access_value(
        project,
        block,
        _single_access_on_port(wires, accesses, move_uid, "in"),
        constants,
    )

    output_matches = []
    for wire in wires:
        record = _ext._wire_nodes(wire)
        if (move_uid, "out1") in record[3]:
            output_matches.append(record)
    if len(output_matches) != 1:
        raise _ext._Unsupported(
            f"move_output_wire_count:{len(output_matches)}"
        )
    power, opened, idents, names = output_matches[0]
    others = [item for item in names if item != (move_uid, "out1")]
    if power or opened or others or len(idents) != 1:
        raise _ext._Unsupported("move_simple_destination_required")
    dest_access = accesses.get(idents[0])
    if dest_access is None:
        raise _ext._Unsupported("move_destination_access_missing")
    dest = _ext._access_value(project, block, dest_access, constants)
    if dest.ref is None or not _ext._same_type(source, dest):
        raise _ext._Unsupported(
            f"move_type_mismatch:{source.data_type}:{dest.data_type}"
        )

    comparison = None
    condition_paths = ()
    reads = [source.ref] if source.ref else []
    if compares:
        compare_uid, compare_part = compares[0]
        _left, _right, compare_reads, pre_paths, comparison = _compare_fact(
            project,
            block,
            statement,
            compare_uid,
            compare_part,
            accesses,
            wires,
            constants,
        )
        reads.extend(compare_reads)
        power, opened, idents, names = _ext._wire_for_port(
            wires,
            move_uid,
            "en",
        )
        others = [item for item in names if item != (move_uid, "en")]
        if (
            power
            or opened
            or idents
            or others != [(compare_uid, "out")]
        ):
            raise _ext._Unsupported("move_compare_enable_binding_required")
        condition_paths = pre_paths
    else:
        condition_paths = _ext._bool_paths(
            project,
            block,
            move_uid,
            "en",
            accesses,
            parts,
            wires,
        )
        reads.extend(
            key
            for path in condition_paths
            for key, _required in path
        )

    reads_tuple = tuple(dict.fromkeys(item for item in reads if item))
    action = _ext.SiemensV4ActionFact(
        statement.id,
        block,
        statement.locator,
        statement.language,
        "MOVE",
        dest.ref,
        reads_tuple,
        condition_paths,
        source.text,
        comparison,
    )
    updated = replace(
        statement,
        reads=reads_tuple,
        writes=(dest.ref,),
        semantic_state=_ext.PLCSemanticState.FULL,
    )
    return updated, (), action


def _visual_call(
    project,
    statement,
    accesses,
    parts,
    calls,
    wires,
    constants,
):
    updated, call = _PREVIOUS_VISUAL_CALL(
        project,
        statement,
        accesses,
        parts,
        calls,
        wires,
        constants,
    )
    # V3 writer-conflict discovery must see the visual invocation as a call,
    # not as both a direct writer and a call-output writer. V3's own
    # _update_call_statements step repopulates the normalized reads/writes after
    # conflict analysis from the exact parameter bindings.
    staged = replace(updated, reads=(), writes=())
    return staged, call


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _ext._compare_fact = _compare_fact
    _ext._evaluate_eq_move = _evaluate_eq_move
    _ext._visual_call = _visual_call
    _INSTALLED = True


__all__ = ["install"]
