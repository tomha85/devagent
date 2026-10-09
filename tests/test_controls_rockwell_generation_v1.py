from __future__ import annotations

import xml.etree.ElementTree as ET

from devagent.controls.ir import build_controls_ir
from devagent.controls.rockwell import render_rockwell_project
from devagent.controls.schema import parse_control_system_payload
from devagent.controls.verification import verify_rockwell_roundtrip
from devagent.plc.safe_analysis import analyze_rockwell_l5x


def _spec():
    return parse_control_system_payload(
        {
            "schema": "devagent-controls-spec-v2",
            "project_id": "PACK01",
            "controllers": [
                {"id": "PLC_PACK_01", "vendor": "ROCKWELL", "platform": "CONTROLLOGIX"}
            ],
            "equipment": [
                {
                    "id": "CONV_101",
                    "type": "CONVEYOR",
                    "standard": "conveyor-v1",
                    "controller": "PLC_PACK_01",
                    "signals": ["SAFETY_OK", "DOWNSTREAM_READY", "GUARD_OPEN", "DRIVE_FAULT"],
                    "commands": {"START": True, "STOP": True, "RESET": True},
                    "status": ["READY", "RUNNING", "FAULTED"],
                    "permissives": ["SAFETY_OK", "DOWNSTREAM_READY"],
                    "interlocks": ["GUARD_OPEN", "DRIVE_FAULT"],
                    "faults": ["DRIVE_FAULT"],
                    "alarms": [
                        {
                            "id": "ALM_DRIVE_FAULT",
                            "priority": "HIGH",
                            "operator_response": "Inspect drive fault.",
                            "source_signal": "DRIVE_FAULT",
                        }
                    ],
                    "hmi": {"faceplate": "conveyor-v1", "historian": True},
                    "requirements": [],
                }
            ],
        }
    )


def test_rockwell_generator_is_byte_deterministic_and_logix_bounded() -> None:
    ir = build_controls_ir(_spec())
    controller = ir.controllers[0]
    first = render_rockwell_project(ir, controller)
    second = render_rockwell_project(ir, controller)

    assert first.content == second.content
    assert first.sha256 == second.sha256
    assert all(len(name) <= 40 for name in first.tags)
    assert len(first.rungs) == 5


def test_generated_l5x_reimports_through_existing_production_analyzer(tmp_path) -> None:
    ir = build_controls_ir(_spec())
    artifact = render_rockwell_project(ir, ir.controllers[0])
    path = tmp_path / "PLC_PACK_01.L5X"
    path.write_bytes(artifact.content)

    result = analyze_rockwell_l5x(path)

    assert result.outcome.value == "STATICALLY_VERIFIED"
    assert not result.project.unknown_instruction_names
    assert sorted(tag.name for tag in result.project.tags) == sorted(artifact.tags)
    assert [rung.text for rung in result.project.rungs] == [
        rung.text for rung in artifact.rungs
    ]


def test_generated_external_access_only_allows_command_writes(tmp_path) -> None:
    ir = build_controls_ir(_spec())
    artifact = render_rockwell_project(ir, ir.controllers[0])
    path = tmp_path / "PLC_PACK_01.L5X"
    path.write_bytes(artifact.content)

    project = analyze_rockwell_l5x(path)
    by_name = {tag.name: tag for tag in project.project.tags}
    symbols = dict(artifact.symbol_map)["CONV_101"]

    for name in dict(symbols["commands"]).values():
        assert by_name[name].external_access == "Read/Write"
    for category in ("signals", "status", "outputs"):
        for name in dict(symbols[category]).values():
            assert by_name[name].external_access == "Read Only"


def test_required_status_tags_have_exactly_one_generated_writer(tmp_path) -> None:
    ir = build_controls_ir(_spec())
    artifact = render_rockwell_project(ir, ir.controllers[0])
    path = tmp_path / "PLC_PACK_01.L5X"
    path.write_bytes(artifact.content)
    project = analyze_rockwell_l5x(path).project

    symbols = dict(artifact.symbol_map)["CONV_101"]
    for status_tag in dict(symbols["status"]).values():
        writers = [rung.id for rung in project.rungs if status_tag in rung.writes]
        assert len(writers) == 1


def test_roundtrip_tolerates_studio_whitespace_rewrite(tmp_path) -> None:
    ir = build_controls_ir(_spec())
    controller = ir.controllers[0]
    artifact = render_rockwell_project(ir, controller)
    root = ET.fromstring(artifact.content)

    for node in root.findall(".//Rung/Text"):
        text = node.text or ""
        node.text = text.replace("XIC(", " XIC(").replace("XIO(", " XIO(").replace(
            "OTE(", " OTE("
        )

    path = tmp_path / "studio-reexport.L5X"
    path.write_bytes(
        ET.tostring(
            root,
            encoding="utf-8",
            xml_declaration=True,
            short_empty_elements=True,
        )
    )

    result = verify_rockwell_roundtrip(ir, controller, path)

    assert result["status"] == "PASS"
    assert result["semantic_projection_status"] == "PASS"


def test_faulted_rung_uses_only_explicit_fault_sources() -> None:
    ir = build_controls_ir(_spec())
    artifact = render_rockwell_project(ir, ir.controllers[0])
    fault_rungs = [rung for rung in artifact.rungs if rung.purpose == "STATUS_FAULTED"]

    assert len(fault_rungs) == 1
    symbols = dict(artifact.symbol_map)["CONV_101"]
    signal_tags = dict(symbols["signals"])
    assert signal_tags["DRIVE_FAULT"] in fault_rungs[0].text
    assert signal_tags["GUARD_OPEN"] not in fault_rungs[0].text
