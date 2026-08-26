from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.production_regression import analyze_regression


def _write_project(
    tmp_path: Path,
    *,
    filename: str,
    rung_text: str,
    tag_names: tuple[str, ...],
) -> Path:
    tags = "\n".join(
        f'<Tag Name="{name}" TagType="Base" DataType="BOOL" />'
        for name in tag_names
    )
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="ReviewRegression" TargetType="Controller">
  <Controller Use="Target" Name="ReviewRegression" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <Modules><Module Name="Local" CatalogNumber="1756-L85E" Vendor="1" /></Modules>
    <AddOnInstructionDefinitions />
    <Tags>{tags}</Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="MainRoutine"><Routines>
      <Routine Name="MainRoutine" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[{rung_text}]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms>
      <ScheduledProgram Name="MainProgram" />
    </ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / filename
    path.write_text(payload, encoding="utf-8")
    return path


def test_v9_case_only_contact_spelling_cannot_reorder_canonical_path(tmp_path: Path) -> None:
    baseline = _write_project(
        tmp_path,
        filename="baseline-case-path.L5X",
        rung_text="XIC(A)XIC(b)OTE(Out);",
        tag_names=("A", "b", "Out"),
    )
    current_path = _write_project(
        tmp_path,
        filename="current-case-path.L5X",
        rung_text="XIC(a)XIC(B)OTE(out);",
        tag_names=("a", "B", "out"),
    )
    current = analyze_rockwell_l5x(current_path)
    changes, _ = analyze_regression(baseline, current, [])
    assert changes == []


def test_v9_multi_output_change_does_not_mark_unchanged_output_affected(tmp_path: Path) -> None:
    baseline = _write_project(
        tmp_path,
        filename="baseline-multi-impact.L5X",
        rung_text="XIC(Start)OTE(Fan)OTE(Fan2);",
        tag_names=("Start", "Fan", "Fan2", "Fan3"),
    )
    current_path = _write_project(
        tmp_path,
        filename="current-multi-impact.L5X",
        rung_text="XIC(Start)OTE(Fan)OTE(Fan3);",
        tag_names=("Start", "Fan", "Fan2", "Fan3"),
    )
    current = analyze_rockwell_l5x(current_path)
    changes, _ = analyze_regression(baseline, current, [])
    logic_changes = [change for change in changes if change.change_type == "LOGIC_CHANGED"]
    assert len(logic_changes) == 1
    affected = {tag.casefold() for tag in logic_changes[0].affected_tags}
    assert "fan" not in affected
    assert affected == {"fan2", "fan3"}
    assert logic_changes[0].subject.endswith("::Fan2, Fan3")
