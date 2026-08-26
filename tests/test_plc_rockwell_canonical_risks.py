from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.production_review import detect_risks


def _analyze(tmp_path: Path, tags: str, programs: str) -> object:
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="CanonicalRisk" TargetType="Controller">
  <Controller Use="Target" Name="CanonicalRisk" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>{tags}</Tags>
    <Programs>{programs}</Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms>
      <ScheduledProgram Name="MainProgram" /><ScheduledProgram Name="OtherProgram" />
    </ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "CanonicalRisk.L5X"
    path.write_text(payload, encoding="utf-8")
    return analyze_rockwell_l5x(path)


def _risks(engineering):
    return detect_risks(engineering, [], [], [])


def test_alias_writer_is_reported_as_one_canonical_multi_writer_risk(tmp_path: Path) -> None:
    tags = '''
      <Tag Name="A" TagType="Base" DataType="BOOL" />
      <Tag Name="B" TagType="Base" DataType="BOOL" />
      <Tag Name="Fan" TagType="Base" DataType="BOOL" />
      <Tag Name="FanAlias" TagType="Alias" DataType="BOOL" AliasFor="Fan" />'''
    programs = '''<Program Name="MainProgram" MainRoutineName="Main"><Routines>
      <Routine Name="Main" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(A)OTE(Fan);]]></Text></Rung>
        <Rung Number="1"><Text><![CDATA[XIC(B)OTE(FanAlias);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program>
    <Program Name="OtherProgram" MainRoutineName="Other"><Routines><Routine Name="Other" Type="RLL"><RLLContent /></Routine></Routines></Program>'''
    risks = _risks(_analyze(tmp_path, tags, programs))
    multi = [item for item in risks if item.category == "MULTIPLE_WRITERS"]
    assert len(multi) == 1
    assert len(multi[0].evidence_ids) == 2
    assert "AliasFor normalization" in multi[0].summary


def test_alias_otu_satisfies_base_otl_reset_identity(tmp_path: Path) -> None:
    tags = '''
      <Tag Name="SetCmd" TagType="Base" DataType="BOOL" />
      <Tag Name="ResetCmd" TagType="Base" DataType="BOOL" />
      <Tag Name="Run" TagType="Base" DataType="BOOL" />
      <Tag Name="RunAlias" TagType="Alias" DataType="BOOL" AliasFor="Run" />'''
    programs = '''<Program Name="MainProgram" MainRoutineName="Main"><Routines>
      <Routine Name="Main" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(SetCmd)OTL(Run);]]></Text></Rung>
        <Rung Number="1"><Text><![CDATA[XIC(ResetCmd)OTU(RunAlias);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program>
    <Program Name="OtherProgram" MainRoutineName="Other"><Routines><Routine Name="Other" Type="RLL"><RLLContent /></Routine></Routines></Program>'''
    risks = _risks(_analyze(tmp_path, tags, programs))
    assert not any(item.category == "RETENTIVE_LOGIC" for item in risks)


def test_independent_program_local_same_name_tags_do_not_create_multi_writer_risk(tmp_path: Path) -> None:
    tags = '<Tag Name="A" TagType="Base" DataType="BOOL" /><Tag Name="B" TagType="Base" DataType="BOOL" />'
    programs = '''
    <Program Name="MainProgram" MainRoutineName="Main">
      <Tags><Tag Name="Fan" TagType="Base" DataType="BOOL" /></Tags>
      <Routines><Routine Name="Main" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(A)OTE(Fan);]]></Text></Rung>
      </RLLContent></Routine></Routines>
    </Program>
    <Program Name="OtherProgram" MainRoutineName="Other">
      <Tags><Tag Name="Fan" TagType="Base" DataType="BOOL" /></Tags>
      <Routines><Routine Name="Other" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(B)OTE(Fan);]]></Text></Rung>
      </RLLContent></Routine></Routines>
    </Program>'''
    risks = _risks(_analyze(tmp_path, tags, programs))
    assert not any(item.category == "MULTIPLE_WRITERS" for item in risks)
