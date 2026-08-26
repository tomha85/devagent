import json
from pathlib import Path

from devagent.entrypoint import main


def _write_project(tmp_path: Path) -> Path:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="Inspect" TargetType="Controller">
  <Controller Name="Inspect" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Run" TagType="Base" DataType="BOOL" />
      <Tag Name="Value" TagType="Base" DataType="DINT" />
      <Tag Name="Copy" TagType="Base" DataType="DINT" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="Main"><Routines>
      <Routine Name="Main" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Start)OTE(Run);]]></Text></Rung>
        <Rung Number="1"><Text><![CDATA[MOV(Value,Copy);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "Inspect.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def test_plc_inspect_prints_human_semantic_summary(tmp_path: Path, capsys) -> None:
    project = _write_project(tmp_path)

    assert main(["plc", "inspect", str(project)]) == 0
    output = capsys.readouterr().out

    assert "DEVAGENT PLC PROJECT INSPECTION" in output
    assert "Controller: Inspect" in output
    assert "PROJECT INVENTORY" in output
    assert "Tags: 4" in output
    assert "Tasks: 1" in output
    assert "Programs: 1" in output
    assert "Routines: 1" in output
    assert "Program RLL rungs: 2" in output
    assert "Analysis warnings:" in output
    assert "Program RLL deterministic instruction coverage:" in output
    assert "Bounded data/compute action rungs: 1" in output
    assert "Bounded action occurrences: 1" in output
    assert "XIC: 1 (DETERMINISTIC_PATH=1)" in output
    assert "MOV: 1 (BOUNDED_DETERMINISTIC=1)" in output
    assert "TRUST BOUNDARY" in output


def test_plc_inspect_json_is_machine_readable(tmp_path: Path, capsys) -> None:
    project = _write_project(tmp_path)

    assert main(["plc", "inspect", str(project), "--json"]) == 0
    manifest = json.loads(capsys.readouterr().out)

    assert manifest["schema"] == "devagent-plc-semantic-coverage-v1"
    assert manifest["project"]["controller"] == "Inspect"
    assert manifest["inventory"]["tags"] == 4
    assert manifest["inventory"]["programs"] == 1
    assert manifest["inventory"]["program_rll_rungs"] == 2
    assert manifest["instruction_summary"]["scope"] == "PROGRAM_RLL"
    assert manifest["action_semantics"]["modeled_actions"] == 1
    assert manifest["stateful_runtime_semantics"]["modeled_occurrences"] == 0
    assert isinstance(manifest["project_boundaries"]["warnings"], list)


def test_plc_inspect_writes_new_manifest_without_modifying_project(tmp_path: Path) -> None:
    project = _write_project(tmp_path)
    original = project.read_bytes()
    output = tmp_path / "coverage.json"

    assert main(["plc", "inspect", str(project), "--output", str(output)]) == 0
    assert project.read_bytes() == original
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["project"]["source_sha256"]
    assert manifest["inventory"]["tasks"] == 1


def test_plc_inspect_refuses_to_overwrite_existing_artifact(tmp_path: Path, capsys) -> None:
    project = _write_project(tmp_path)
    output = tmp_path / "coverage.json"
    output.write_text("do-not-overwrite", encoding="utf-8")

    assert main(["plc", "inspect", str(project), "--output", str(output)]) == 1
    assert output.read_text(encoding="utf-8") == "do-not-overwrite"
    assert "Refusing to overwrite" in capsys.readouterr().err
