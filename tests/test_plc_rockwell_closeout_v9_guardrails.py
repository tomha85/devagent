from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.production_regression import _logic_index
from devagent.plc.rockwell_closeout import rockwell_capability_profile


def _write_project(tmp_path: Path, *, filename: str, rung_text: str | None) -> Path:
    if rung_text is None:
        content = "<RLLContent />"
    else:
        content = f'<RLLContent><Rung Number="0"><Text><![CDATA[{rung_text}]]></Text></Rung></RLLContent>'
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="V9Guard" TargetType="Controller">
  <Controller Use="Target" Name="V9Guard" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <Modules><Module Name="Local" CatalogNumber="1756-L85E" Vendor="1" /></Modules>
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Index" TagType="Base" DataType="DINT" />
      <Tag Name="Inputs" TagType="Base" DataType="BOOL" Dimensions="10" />
      <Tag Name="Fan" TagType="Base" DataType="BOOL" />
      <Tag Name="Fan2" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="MainRoutine"><Routines>
        <Routine Name="MainRoutine" Type="RLL">{content}</Routine>
      </Routines></Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms>
      <ScheduledProgram Name="MainProgram" />
    </ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / filename
    path.write_text(payload, encoding="utf-8")
    return path


def _support(engineering):
    return next(item for item in engineering.static_checks if item.id == "ROCKWELL_PRODUCTION_SUPPORT_CONTRACT")


def test_v9_variable_array_subscript_is_explicit_support_gap(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(
        _write_project(
            tmp_path,
            filename="indirect.L5X",
            rung_text="XIC(Inputs[Index])OTE(Fan);",
        )
    )
    profile = rockwell_capability_profile(engineering.project)
    assert profile["static_gaps"]["indirect_rungs"] == 1
    assert profile["static_contract"] == "PARTIAL_FAIL_CLOSED"
    assert len(profile["indirect_addressing"]["evidence_ids"]) == 1
    assert _support(engineering).status.value == "WARN"
    assert engineering.outcome.value == "PARTIALLY_VERIFIED"
    assert not engineering.fat_tests


def test_v9_empty_rll_is_not_claimed_as_complete_behavior(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(
        _write_project(tmp_path, filename="empty.L5X", rung_text=None)
    )
    profile = rockwell_capability_profile(engineering.project)
    assert profile["static_gaps"]["no_supported_logic"] == 1
    assert profile["static_contract"] == "PARTIAL_FAIL_CLOSED"
    assert _support(engineering).status.value == "WARN"
    assert engineering.outcome.value == "PARTIALLY_VERIFIED"


def test_v9_regression_index_preserves_multiple_outputs_from_one_rung(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(
        _write_project(
            tmp_path,
            filename="multi-output.L5X",
            rung_text="XIC(Start)OTE(Fan)OTE(Fan2);",
        )
    )
    buckets = _logic_index(engineering.project)
    rung_buckets = [bucket for key, bucket in buckets.items() if key[1] == "mainprogram" and key[3] == "0"]
    assert len(rung_buckets) == 1
    assert len(rung_buckets[0]) == 2
    assert {logic.output_tag for _, logic in rung_buckets[0]} == {"Fan", "Fan2"}
