from __future__ import annotations

import hashlib
from importlib.resources import files
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from devagent.controls.catalog import get_standard
from devagent.controls.ir import ControlsIR
from devagent.controls.models import ControllerSpec, EquipmentSpec
from devagent.controls.symbols import controller_symbol, equipment_symbol_map

ROCKWELL_GENERATOR_VERSION = "1.0.0"
ROCKWELL_SCHEMA_REVISION = "1.0"
ROCKWELL_SOFTWARE_REVISION = "36.00"
ROCKWELL_REFERENCE_REPOSITORY = "RockwellAutomation/ra-logix-cicd"
ROCKWELL_REFERENCE_PATH = "1-production-files/L5Xs/ExampleForCICD_L85E.L5X"
ROCKWELL_REFERENCE_BLOB_SHA = "ea3814f7d3657de569539228042903dc9ea8a908"
ROCKWELL_REFERENCE_LICENSE = "MIT"


class RockwellGenerationError(ValueError):
    pass


@dataclass(frozen=True)
class GeneratedRung:
    equipment_id: str
    purpose: str
    text: str
    output_tag: str


@dataclass(frozen=True)
class RockwellArtifact:
    controller_id: str
    controller_name: str
    content: bytes
    sha256: str
    tags: tuple[str, ...]
    rungs: tuple[GeneratedRung, ...]
    symbol_map: tuple[tuple[str, object], ...]


def rockwell_reference_provenance() -> dict[str, str]:
    return {
        "repository": ROCKWELL_REFERENCE_REPOSITORY,
        "path": ROCKWELL_REFERENCE_PATH,
        "blob_sha": ROCKWELL_REFERENCE_BLOB_SHA,
        "license": ROCKWELL_REFERENCE_LICENSE,
        "role": "CONTROLLOGIX_STUDIO5000_EXPORTED_DOCUMENT_SHELL",
        "studio5000_import_validation": "NOT_RUN_FOR_GENERATED_OUTPUT",
    }


def _base_document(controller_name: str, platform: str) -> tuple[ET.Element, ET.Element]:
    normalized = platform.strip().upper().replace(" ", "")
    if normalized in {"CONTROLLOGIX", "1756"}:
        resource = files("devagent.controls").joinpath(
            "templates/rockwell/ExampleForCICD_L85E.L5X"
        )
        root = ET.fromstring(resource.read_bytes())
        plc = root.find("Controller")
        if plc is None:
            raise RockwellGenerationError("packaged Rockwell reference template has no Controller")

        root.set("TargetName", controller_name)
        root.set("TargetType", "Controller")
        root.set("SoftwareRevision", ROCKWELL_SOFTWARE_REVISION)
        root.attrib.pop("ExportDate", None)

        plc.set("Name", controller_name)
        plc.set("ProcessorType", "1756-L85E")
        plc.set("MajorRev", "36")
        plc.set("MinorRev", "11")
        for stale_attribute in (
            "MajorFaultProgram",
            "ProjectCreationDate",
            "LastModifiedDate",
            "CommPath",
        ):
            plc.attrib.pop(stale_attribute, None)

        for child_name in ("Tags", "Programs", "Tasks"):
            existing = plc.find(child_name)
            if existing is None:
                raise RockwellGenerationError(
                    f"packaged Rockwell reference template has no {child_name}"
                )
            existing.clear()
        aois = plc.find("AddOnInstructionDefinitions")
        if aois is not None:
            aois.clear()
        return root, plc

    root = ET.Element(
        "RSLogix5000Content",
        {
            "SchemaRevision": ROCKWELL_SCHEMA_REVISION,
            "SoftwareRevision": ROCKWELL_SOFTWARE_REVISION,
            "TargetName": controller_name,
            "TargetType": "Controller",
            "ContainsContext": "false",
        },
    )
    plc = ET.SubElement(
        root,
        "Controller",
        {
            "Use": "Target",
            "Name": controller_name,
            "ProcessorType": _processor_type(platform),
            "MajorRev": "36",
            "MinorRev": "11",
        },
    )
    ET.SubElement(plc, "DataTypes")
    ET.SubElement(plc, "Modules")
    ET.SubElement(plc, "AddOnInstructionDefinitions")
    return root, plc


def _processor_type(platform: str) -> str:
    normalized = platform.strip().upper().replace(" ", "")
    if normalized in {"CONTROLLOGIX", "1756"}:
        return "1756-L85E"
    if normalized in {"COMPACTLOGIX", "5069"}:
        return "5069-L320ER"
    raise RockwellGenerationError(
        f"Rockwell platform {platform!r} is not qualified for deterministic generation; "
        "supported V1 platforms are CONTROLLOGIX and COMPACTLOGIX"
    )


def _enabled_commands(equipment: EquipmentSpec) -> dict[str, bool]:
    return {name: enabled for name, enabled in equipment.commands}


def _series(
    equipment: EquipmentSpec,
    *,
    command: str,
    inverse_command: str | None,
    output: str,
    symbols: dict[str, object],
    purpose: str,
) -> GeneratedRung:
    commands = dict(symbols["commands"])
    signals = dict(symbols["signals"])
    outputs = dict(symbols["outputs"])

    contacts: list[str] = [f"XIC({commands[command]})"]
    if inverse_command is not None:
        contacts.append(f"XIO({commands[inverse_command]})")
    contacts.extend(f"XIC({signals[name]})" for name in equipment.permissives)
    contacts.extend(f"XIO({signals[name]})" for name in equipment.interlocks)
    output_tag = str(outputs[output])
    return GeneratedRung(
        equipment_id=equipment.id,
        purpose=purpose,
        text="".join([*contacts, f"OTE({output_tag});"]),
        output_tag=output_tag,
    )


def _status_rungs(
    equipment: EquipmentSpec,
    symbols: dict[str, object],
) -> tuple[GeneratedRung, ...]:
    standard = get_standard(equipment.standard)
    signals = dict(symbols["signals"])
    status = dict(symbols["status"])
    outputs = dict(symbols["outputs"])
    rungs: list[GeneratedRung] = []

    if standard.equipment_type in {"MOTOR", "VFD", "CONVEYOR"}:
        ready_contacts = [
            *(f"XIC({signals[name]})" for name in equipment.permissives),
            *(f"XIO({signals[name]})" for name in equipment.interlocks),
        ]
        if not ready_contacts:
            raise RockwellGenerationError(
                f"{equipment.id} cannot derive READY without permissive/interlock inputs"
            )
        ready_tag = status["READY"]
        rungs.append(
            GeneratedRung(
                equipment_id=equipment.id,
                purpose="STATUS_READY",
                text="".join([*ready_contacts, f"OTE({ready_tag});"]),
                output_tag=str(ready_tag),
            )
        )

        running_tag = status["RUNNING"]
        rungs.append(
            GeneratedRung(
                equipment_id=equipment.id,
                purpose="STATUS_RUNNING",
                text=f"XIC({outputs['RUN']})OTE({running_tag});",
                output_tag=str(running_tag),
            )
        )

        if not equipment.interlocks:
            raise RockwellGenerationError(
                f"{equipment.id} cannot derive FAULTED without an interlock/fault source"
            )
        fault_contacts = [f"XIC({signals[name]})" for name in equipment.interlocks]
        fault_logic = (
            fault_contacts[0]
            if len(fault_contacts) == 1
            else "[" + ",".join(fault_contacts) + "]"
        )
        faulted_tag = status["FAULTED"]
        rungs.append(
            GeneratedRung(
                equipment_id=equipment.id,
                purpose="STATUS_FAULTED",
                text=f"{fault_logic}OTE({faulted_tag});",
                output_tag=str(faulted_tag),
            )
        )
    elif standard.equipment_type == "VALVE":
        for member in ("OPEN", "CLOSED"):
            output_name = "OPEN" if member == "OPEN" else "CLOSE"
            status_tag = status[member]
            rungs.append(
                GeneratedRung(
                    equipment_id=equipment.id,
                    purpose=f"STATUS_{member}",
                    text=f"XIC({outputs[output_name]})OTE({status_tag});",
                    output_tag=str(status_tag),
                )
            )
    return tuple(rungs)


def generated_rungs(equipment: EquipmentSpec) -> tuple[GeneratedRung, ...]:
    standard = get_standard(equipment.standard)
    commands = _enabled_commands(equipment)
    symbols = equipment_symbol_map(equipment)
    rungs: list[GeneratedRung] = []

    if standard.equipment_type in {"MOTOR", "VFD", "CONVEYOR"}:
        primary = standard.primary_action
        stop = standard.stop_action
        if not commands.get(primary):
            raise RockwellGenerationError(
                f"{equipment.id} requires enabled command {primary} for {standard.id}"
            )
        if stop is not None and not commands.get(stop):
            raise RockwellGenerationError(
                f"{equipment.id} requires enabled command {stop} for {standard.id}"
            )
        rungs.append(
            _series(
                equipment,
                command=primary,
                inverse_command=stop,
                output="RUN",
                symbols=symbols,
                purpose="PRIMARY_RUN",
            )
        )
        if "RESET" in dict(symbols["outputs"]):
            if not commands.get("RESET"):
                raise RockwellGenerationError(
                    f"{equipment.id} requires enabled command RESET for {standard.id}"
                )
            cmd_tag = dict(symbols["commands"])["RESET"]
            output_tag = dict(symbols["outputs"])["RESET"]
            rungs.append(
                GeneratedRung(
                    equipment_id=equipment.id,
                    purpose="RESET",
                    text=f"XIC({cmd_tag})OTE({output_tag});",
                    output_tag=str(output_tag),
                )
            )
    elif standard.equipment_type == "VALVE":
        for action, inverse in (("OPEN", "CLOSE"), ("CLOSE", "OPEN")):
            if not commands.get(action):
                raise RockwellGenerationError(
                    f"{equipment.id} requires enabled command {action} for {standard.id}"
                )
            rungs.append(
                _series(
                    equipment,
                    command=action,
                    inverse_command=inverse,
                    output=action,
                    symbols=symbols,
                    purpose=f"VALVE_{action}",
                )
            )
    else:  # pragma: no cover - catalog owns this invariant
        raise RockwellGenerationError(
            f"unsupported equipment type for Rockwell generation: {standard.equipment_type}"
        )

    rungs.extend(_status_rungs(equipment, symbols))
    return tuple(rungs)


def _equipment_for_controller(ir: ControlsIR, controller: ControllerSpec) -> tuple[EquipmentSpec, ...]:
    items = tuple(item for item in ir.equipment if item.controller == controller.id)
    if not items:
        raise RockwellGenerationError(
            f"controller {controller.id} has no equipment; empty generated PLCs are not supported"
        )
    return items


def _tag_names(equipment: tuple[EquipmentSpec, ...]) -> tuple[str, ...]:
    names: list[str] = []
    for item in equipment:
        symbols = equipment_symbol_map(item)
        for category in ("signals", "commands", "status", "outputs"):
            names.extend(str(value) for value in dict(symbols[category]).values())
    if len(names) != len(set(names)):
        raise RockwellGenerationError(
            "generated Rockwell tags collide across equipment after identifier normalization"
        )
    return tuple(sorted(names))


def render_rockwell_project(ir: ControlsIR, controller: ControllerSpec) -> RockwellArtifact:
    if controller.vendor != "ROCKWELL":
        raise RockwellGenerationError(
            f"controller {controller.id} vendor {controller.vendor} is not supported by the Rockwell V1 generator"
        )

    equipment = _equipment_for_controller(ir, controller)
    tags = _tag_names(equipment)
    rungs = tuple(
        rung
        for item in equipment
        for rung in generated_rungs(item)
    )
    controller_name = controller_symbol(controller.id)

    root, plc = _base_document(controller_name, controller.platform)

    externally_writable: set[str] = set()
    for item in equipment:
        symbols = equipment_symbol_map(item)
        externally_writable.update(
            str(value) for value in dict(symbols["commands"]).values()
        )

    tags_node = plc.find("Tags")
    if tags_node is None:
        tags_node = ET.SubElement(plc, "Tags")
    for name in tags:
        ET.SubElement(
            tags_node,
            "Tag",
            {
                "Name": name,
                "TagType": "Base",
                "DataType": "BOOL",
                # Standard commands are the only HMI/operator write surface.
                # Signals, status, and generated outputs are externally read-only.
                "ExternalAccess": (
                    "Read/Write" if name in externally_writable else "Read Only"
                ),
            },
        )

    programs = plc.find("Programs")
    if programs is None:
        programs = ET.SubElement(plc, "Programs")
    program = ET.SubElement(
        programs,
        "Program",
        {"Name": "DevAgentGenerated", "MainRoutineName": "MainLogic"},
    )
    routines = ET.SubElement(program, "Routines")
    routine = ET.SubElement(routines, "Routine", {"Name": "MainLogic", "Type": "RLL"})
    rll = ET.SubElement(routine, "RLLContent")
    for number, rung in enumerate(rungs):
        rung_node = ET.SubElement(rll, "Rung", {"Number": str(number), "Type": "N"})
        comment = ET.SubElement(rung_node, "Comment")
        comment.text = f"{rung.equipment_id}:{rung.purpose}"
        text = ET.SubElement(rung_node, "Text")
        text.text = rung.text

    tasks = plc.find("Tasks")
    if tasks is None:
        tasks = ET.SubElement(plc, "Tasks")
    task = ET.SubElement(tasks, "Task", {"Name": "MainTask", "Type": "CONTINUOUS"})
    scheduled = ET.SubElement(task, "ScheduledPrograms")
    ET.SubElement(scheduled, "ScheduledProgram", {"Name": "DevAgentGenerated"})

    ET.indent(root, space="  ")
    body = ET.tostring(
        root,
        encoding="utf-8",
        xml_declaration=True,
        short_empty_elements=True,
    )
    if not body.endswith(b"\n"):
        body += b"\n"

    symbol_pairs: list[tuple[str, object]] = []
    for item in equipment:
        symbol_pairs.append((item.id, equipment_symbol_map(item)))

    return RockwellArtifact(
        controller_id=controller.id,
        controller_name=controller_name,
        content=body,
        sha256=hashlib.sha256(body).hexdigest(),
        tags=tags,
        rungs=rungs,
        symbol_map=tuple(symbol_pairs),
    )


def write_rockwell_project(
    ir: ControlsIR,
    controller: ControllerSpec,
    path: Path,
) -> RockwellArtifact:
    artifact = render_rockwell_project(ir, controller)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(artifact.content)
    return artifact
