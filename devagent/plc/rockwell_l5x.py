from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from devagent.plc.models import (
    CanonicalPLCProject,
    PLCAddOnInstruction,
    PLCDataType,
    PLCInstruction,
    PLCModule,
    PLCAOIParameter,
    PLCProgram,
    PLCProjectMetadata,
    PLCRoutine,
    PLCRung,
    PLCSourceRef,
    PLCTag,
    PLCTask,
)


_MAX_L5X_BYTES = 128 * 1024 * 1024
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_:.\[\]-]*(?:\.[A-Za-z_][A-Za-z0-9_:.\[\]-]*)*")
_INSTRUCTION_START = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_LITERAL_WORDS = {"true", "false", "and", "or", "not"}

_READ_FIRST = {"XIC", "XIO"}
_WRITE_FIRST = {"OTE", "OTL", "OTU", "RES", "CLR"}
_READ_WRITE_FIRST = {"ONS", "OSR", "OSF", "TON", "TOF", "RTO", "CTU", "CTD"}
_MOVE = {"MOV", "COP", "CPS"}
_BINARY_WRITE_LAST = {"ADD", "SUB", "MUL", "DIV", "MOD", "AND", "OR", "XOR"}
_UNARY_WRITE_LAST = {"SQR", "SQRT", "ABS", "NEG"}
_COMPARE = {"EQU", "NEQ", "LES", "LEQ", "GRT", "GEQ", "MEQ", "LIM"}
_FLOW = {"NOP", "RET", "SBR", "AFI"}


class L5XError(ValueError):
    """Raised when an L5X artifact cannot be safely treated as a full project."""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(element) if _local_name(child.tag) == name]


def _child(element: ET.Element, name: str) -> ET.Element | None:
    for child in list(element):
        if _local_name(child.tag) == name:
            return child
    return None


def _descendants(element: ET.Element, name: str) -> list[ET.Element]:
    return [item for item in element.iter() if _local_name(item.tag) == name]


def _text_child(element: ET.Element, name: str) -> str | None:
    found = _child(element, name)
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def _bool_attr(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes"}


def _contains_encoded(element: ET.Element) -> bool:
    for item in element.iter():
        local = _local_name(item.tag).lower()
        if "encoded" in local or "encrypted" in local:
            return True
        for key, value in item.attrib.items():
            lowered_key = key.lower()
            lowered_value = str(value).lower()
            if ("encoded" in lowered_key or "encrypted" in lowered_key) and lowered_value not in {"", "0", "false", "no"}:
                return True
    return False


def _split_args(argument_text: str) -> tuple[str, ...]:
    args: list[str] = []
    current: list[str] = []
    quote: str | None = None
    depth = 0
    for char in argument_text:
        if quote is not None:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            current.append(char)
            continue
        if char in "([{" :
            depth += 1
            current.append(char)
            continue
        if char in ")]}" and depth:
            depth -= 1
            current.append(char)
            continue
        if char == "," and depth == 0:
            args.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    if current or argument_text.strip():
        args.append("".join(current).strip())
    return tuple(args)


def _instructions(text: str) -> tuple[PLCInstruction, ...]:
    result: list[PLCInstruction] = []
    position = 0
    while True:
        match = _INSTRUCTION_START.search(text, position)
        if match is None:
            break
        name = match.group(1)
        opening = match.end() - 1
        depth = 1
        quote: str | None = None
        index = opening + 1
        while index < len(text) and depth:
            char = text[index]
            if quote is not None:
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in {'"', "'"}:
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1
        if depth:
            break
        result.append(PLCInstruction(name=name, arguments=_split_args(text[opening + 1 : index - 1])))
        position = index
    return tuple(result)


def _identifier_tokens(value: str) -> tuple[str, ...]:
    stripped = value.strip()
    if not stripped or stripped[0:1] in {'"', "'"}:
        return ()
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", stripped):
        return ()
    if re.fullmatch(r"\d+#(?:[0-9A-Fa-f_]+)", stripped):
        return ()
    result: list[str] = []
    for token in _IDENTIFIER.findall(stripped):
        if token.lower() in _LITERAL_WORDS:
            continue
        if token not in result:
            result.append(token)
    return tuple(result)


def _first_reference(arguments: tuple[str, ...], index: int = 0) -> str | None:
    if len(arguments) <= index:
        return None
    tokens = _identifier_tokens(arguments[index])
    return tokens[0] if tokens else None


def _instruction_semantics(
    instruction: PLCInstruction,
    aoi_parameters: dict[str, tuple[PLCAOIParameter, ...]],
) -> tuple[set[str], set[str], set[str], set[str], bool]:
    name = instruction.name.upper()
    args = instruction.arguments
    reads: set[str] = set()
    writes: set[str] = set()
    calls: set[str] = set()
    references: set[str] = set()

    def add_tokens(target: set[str], value: str) -> None:
        target.update(_identifier_tokens(value))

    if name in _READ_FIRST:
        ref = _first_reference(args)
        if ref:
            reads.add(ref)
    elif name in _WRITE_FIRST:
        ref = _first_reference(args)
        if ref:
            writes.add(ref)
    elif name in _READ_WRITE_FIRST:
        ref = _first_reference(args)
        if ref:
            reads.add(ref)
            writes.add(ref)
    elif name in _MOVE:
        if args:
            add_tokens(reads, args[0])
        if len(args) > 1:
            ref = _first_reference(args, 1)
            if ref:
                writes.add(ref)
        if name in {"COP", "CPS"} and len(args) > 2:
            add_tokens(reads, args[2])
    elif name in _BINARY_WRITE_LAST:
        if len(args) >= 2:
            for value in args[:-1]:
                add_tokens(reads, value)
            ref = _first_reference(args, len(args) - 1)
            if ref:
                writes.add(ref)
    elif name in _UNARY_WRITE_LAST:
        if len(args) >= 2:
            for value in args[:-1]:
                add_tokens(reads, value)
            ref = _first_reference(args, len(args) - 1)
            if ref:
                writes.add(ref)
    elif name in _COMPARE:
        for value in args:
            add_tokens(reads, value)
    elif name == "CPT":
        ref = _first_reference(args)
        if ref:
            writes.add(ref)
        for value in args[1:]:
            add_tokens(reads, value)
    elif name == "JSR":
        routine = _first_reference(args)
        if routine:
            calls.add(routine)
        for value in args[1:]:
            add_tokens(references, value)
    elif name in _FLOW:
        pass
    elif instruction.name in aoi_parameters:
        calls.add(instruction.name)
        parameters = aoi_parameters[instruction.name]
        for parameter, value in zip(parameters, args):
            usage = parameter.usage.strip().lower()
            if usage in {"input", "inout"}:
                add_tokens(reads, value)
            if usage in {"output", "inout"}:
                add_tokens(writes, value)
    else:
        for value in args:
            add_tokens(references, value)
        return reads, writes, calls, references, False

    references.update(reads)
    references.update(writes)
    return reads, writes, calls, references, True


def _parse_tag(element: ET.Element, *, controller: str, program: str | None) -> PLCTag:
    name = element.attrib.get("Name", "").strip()
    scope = "controller" if program is None else f"program:{program}"
    tag_id = f"rockwell://{controller}/{scope}/tag/{name}"
    return PLCTag(
        id=tag_id,
        name=name,
        scope=scope,
        data_type=element.attrib.get("DataType", "UNKNOWN"),
        tag_type=element.attrib.get("TagType"),
        alias_for=element.attrib.get("AliasFor"),
        external_access=element.attrib.get("ExternalAccess"),
        constant=_bool_attr(element.attrib.get("Constant")),
        description=_text_child(element, "Description"),
    )


def _parse_aoi(element: ET.Element, controller: str) -> PLCAddOnInstruction:
    name = element.attrib.get("Name", "").strip()
    parameters_element = _child(element, "Parameters")
    parameters: list[PLCAOIParameter] = []
    if parameters_element is not None:
        for parameter in _children(parameters_element, "Parameter"):
            parameters.append(
                PLCAOIParameter(
                    name=parameter.attrib.get("Name", "").strip(),
                    usage=parameter.attrib.get("Usage", "Input"),
                    data_type=parameter.attrib.get("DataType"),
                )
            )
    return PLCAddOnInstruction(
        id=f"rockwell://{controller}/aoi/{name}",
        name=name,
        parameters=tuple(parameters),
        source_protected=_contains_encoded(element),
    )


def parse_full_project_l5x(path: Path) -> CanonicalPLCProject:
    candidate = path.expanduser().resolve(strict=False)
    if candidate.suffix.lower() != ".l5x":
        raise L5XError("Rockwell PLC V1 accepts a full-project .L5X export")
    if not candidate.is_file():
        raise L5XError(f"L5X project does not exist: {candidate}")

    with candidate.open("rb") as handle:
        payload = handle.read(_MAX_L5X_BYTES + 1)
    if len(payload) > _MAX_L5X_BYTES:
        raise L5XError(f"L5X project exceeds {_MAX_L5X_BYTES} bytes")
    if b"\x00" in payload:
        raise L5XError("L5X contains binary NUL bytes")
    lowered = payload.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise L5XError("L5X XML declarations with DTD/entities are not accepted")

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise L5XError(f"Invalid L5X XML: {exc}") from exc
    if _local_name(root.tag) != "RSLogix5000Content":
        raise L5XError("Artifact is not a Rockwell RSLogix5000Content L5X document")

    target_type = root.attrib.get("TargetType")
    controller = _child(root, "Controller")
    if target_type != "Controller" or controller is None:
        detail = target_type or "unknown component"
        raise L5XError(
            f"L5X is not a full-project Controller export (TargetType={detail}); "
            "export the whole project from Studio 5000"
        )

    controller_name = controller.attrib.get("Name", root.attrib.get("TargetName", "Controller"))
    metadata = PLCProjectMetadata(
        vendor="Rockwell Automation",
        engineering_tool="Studio 5000 Logix Designer",
        source_path=str(candidate),
        source_sha256=hashlib.sha256(payload).hexdigest(),
        schema_revision=root.attrib.get("SchemaRevision"),
        software_revision=root.attrib.get("SoftwareRevision"),
        target_type=target_type,
        controller_name=controller_name,
        processor_type=controller.attrib.get("ProcessorType"),
        major_revision=controller.attrib.get("MajorRev"),
        minor_revision=controller.attrib.get("MinorRev"),
        full_project=True,
    )
    project = CanonicalPLCProject(metadata=metadata)

    data_types_element = _child(controller, "DataTypes")
    if data_types_element is not None:
        for data_type in _children(data_types_element, "DataType"):
            name = data_type.attrib.get("Name", "").strip()
            project.data_types.append(
                PLCDataType(
                    id=f"rockwell://{controller_name}/datatype/{name}",
                    name=name,
                    family=data_type.attrib.get("Family"),
                )
            )

    modules_element = _child(controller, "Modules")
    if modules_element is not None:
        for module in _children(modules_element, "Module"):
            name = module.attrib.get("Name", "").strip()
            project.modules.append(
                PLCModule(
                    id=f"rockwell://{controller_name}/module/{name}",
                    name=name,
                    catalog_number=module.attrib.get("CatalogNumber"),
                    vendor=module.attrib.get("Vendor"),
                )
            )

    tasks_element = _child(controller, "Tasks")
    if tasks_element is not None:
        for task in _children(tasks_element, "Task"):
            name = task.attrib.get("Name", "").strip()
            project.tasks.append(
                PLCTask(
                    id=f"rockwell://{controller_name}/task/{name}",
                    name=name,
                    task_type=task.attrib.get("Type"),
                    priority=task.attrib.get("Priority"),
                    rate=task.attrib.get("Rate"),
                )
            )

    tags_element = _child(controller, "Tags")
    if tags_element is not None:
        project.tags.extend(_parse_tag(tag, controller=controller_name, program=None) for tag in _children(tags_element, "Tag"))

    aoi_element = _child(controller, "AddOnInstructionDefinitions")
    if aoi_element is not None:
        project.aois.extend(_parse_aoi(item, controller_name) for item in _children(aoi_element, "AddOnInstructionDefinition"))
    aoi_parameters = {item.name: item.parameters for item in project.aois}

    unknown_instructions: set[str] = set()
    programs_element = _child(controller, "Programs")
    if programs_element is not None:
        for program_element in _children(programs_element, "Program"):
            program_name = program_element.attrib.get("Name", "").strip()
            program_tag_ids: list[str] = []
            program_routine_ids: list[str] = []

            program_tags = _child(program_element, "Tags")
            if program_tags is not None:
                for tag_element in _children(program_tags, "Tag"):
                    tag = _parse_tag(tag_element, controller=controller_name, program=program_name)
                    project.tags.append(tag)
                    program_tag_ids.append(tag.id)

            routines_element = _child(program_element, "Routines")
            if routines_element is not None:
                for routine_element in _children(routines_element, "Routine"):
                    routine_name = routine_element.attrib.get("Name", "").strip()
                    routine_type = routine_element.attrib.get("Type", "UNKNOWN")
                    routine_id = f"rockwell://{controller_name}/program/{program_name}/routine/{routine_name}"
                    protected = _contains_encoded(routine_element)
                    rung_ids: list[str] = []
                    if routine_type == "RLL" and not protected:
                        rll = _child(routine_element, "RLLContent")
                        if rll is not None:
                            for ordinal, rung_element in enumerate(_children(rll, "Rung")):
                                number = rung_element.attrib.get("Number", str(ordinal))
                                text = _text_child(rung_element, "Text") or ""
                                instruction_list = _instructions(text)
                                reads: set[str] = set()
                                writes: set[str] = set()
                                calls: set[str] = set()
                                references: set[str] = set()
                                for instruction in instruction_list:
                                    project.instruction_total += 1
                                    ins_reads, ins_writes, ins_calls, ins_refs, known = _instruction_semantics(
                                        instruction, aoi_parameters
                                    )
                                    reads.update(ins_reads)
                                    writes.update(ins_writes)
                                    calls.update(ins_calls)
                                    references.update(ins_refs)
                                    if known:
                                        project.instruction_semantic_count += 1
                                    else:
                                        unknown_instructions.add(instruction.name)
                                rung_id = f"{routine_id}/rung/{number}"
                                source = PLCSourceRef(
                                    artifact=str(candidate),
                                    controller=controller_name,
                                    program=program_name,
                                    routine=routine_name,
                                    rung=number,
                                )
                                project.rungs.append(
                                    PLCRung(
                                        id=rung_id,
                                        program=program_name,
                                        routine=routine_name,
                                        number=number,
                                        text=text,
                                        comment=_text_child(rung_element, "Comment"),
                                        instructions=instruction_list,
                                        reads=tuple(sorted(reads)),
                                        writes=tuple(sorted(writes)),
                                        calls=tuple(sorted(calls)),
                                        references=tuple(sorted(references)),
                                        source=source,
                                    )
                                )
                                rung_ids.append(rung_id)
                    project.routines.append(
                        PLCRoutine(
                            id=routine_id,
                            program=program_name,
                            name=routine_name,
                            routine_type=routine_type,
                            source_protected=protected,
                            rung_ids=tuple(rung_ids),
                        )
                    )
                    program_routine_ids.append(routine_id)

            project.programs.append(
                PLCProgram(
                    id=f"rockwell://{controller_name}/program/{program_name}",
                    name=program_name,
                    tag_ids=tuple(program_tag_ids),
                    routine_ids=tuple(program_routine_ids),
                )
            )

    if unknown_instructions:
        project.warnings.append(
            "Instruction semantics not modeled for: " + ", ".join(sorted(unknown_instructions))
        )
    protected_routines = [routine for routine in project.routines if routine.source_protected]
    if protected_routines:
        project.warnings.append(
            f"{len(protected_routines)} routine(s) contain encoded/protected content and were not semantically parsed"
        )
    unsupported = sorted({routine.routine_type for routine in project.routines if routine.routine_type != "RLL"})
    if unsupported:
        project.warnings.append(
            "PLC V1 dependency semantics cover RLL routines only; unsupported routine types: "
            + ", ".join(unsupported)
        )

    return project
