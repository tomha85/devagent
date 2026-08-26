from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.models import PLCOutcome, PLCSemanticState


def _write_project(tmp_path: Path) -> Path:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="STConversions" TargetType="Controller">
  <Controller Name="STConversions" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="InputDint" TagType="Base" DataType="DINT" />
      <Tag Name="OutputReal" TagType="Base" DataType="REAL" />
      <Tag Name="Enable" TagType="Base" DataType="BOOL" />
      <Tag Name="OutputBool" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="Main"><Routines>
      <Routine Name="Main" Type="ST"><STContent>
        <Line Number="0"><![CDATA[OutputReal := REAL(InputDint);]]></Line>
        <Line Number="1"><![CDATA[OutputBool := BOOL(Enable);]]></Line>
      </STContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "STConversions.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def test_elementary_st_conversions_remain_expression_semantics_not_unknown_calls(tmp_path: Path) -> None:
    result = analyze_rockwell_l5x(_write_project(tmp_path))

    assert result.outcome is PLCOutcome.STATICALLY_VERIFIED
    assert result.project.st_statement_semantic_count == 2
    by_write = {statement.writes[0]: statement for statement in result.project.logic_statements}
    real = by_write["OutputReal"]
    boolean = by_write["OutputBool"]
    assert real.semantic_state is PLCSemanticState.FULL
    assert real.reads == ("InputDint",)
    assert real.calls == ()
    assert boolean.semantic_state is PLCSemanticState.FULL
    assert boolean.reads == ("Enable",)
    assert boolean.calls == ()
