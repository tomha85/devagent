from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.production_regression import analyze_regression
from devagent.plc.rockwell_closeout import rockwell_capability_profile


def _write_project(
    tmp_path: Path,
    *,
    filename: str,
    tags: tuple[str, ...],
    rung_text: str,
    main_routine_name: str = "Logic",
) -> Path:
    tag_xml = "\n".join(
        f'<Tag Name="{name}" TagType="Base" DataType="BOOL" />'
        for name in tags
    )
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="MergeGate" TargetType="Controller">
  <Controller Use="Target" Name="MergeGate" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <Modules><Module Name="Local" CatalogNumber="1756-L85E" Vendor="1" /></Modules>
    <AddOnInstructionDefinitions />
    <Tags>{tag_xml}</Tags>
    <Programs>
      <Program Name="Main" MainRoutineName="{main_routine_name}"><Routines>
        <Routine Name="Logic" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[{rung_text}]]></Text></Rung>
        </RLLContent></Routine>
      </Routines></Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms>
      <ScheduledProgram Name="Main" />
    </ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / filename
    path.write_text(payload, encoding="utf-8")
    return path


def test_v9_structure_warning_forces_partial_support_contract(tmp_path: Path) -> None:
    project_path = _write_project(
        tmp_path,
        filename="missing-main.L5X",
        tags=("Start", "Fan"),
        rung_text="XIC(Start)OTE(Fan);",
        main_routine_name="MissingRoutine",
    )
    engineering = analyze_rockwell_l5x(project_path)
    profile = rockwell_capability_profile(engineering.project)

    assert engineering.outcome.value == "PARTIALLY_VERIFIED"
    assert profile["static_contract"] == "PARTIAL_FAIL_CLOSED"
    assert profile["static_gaps"]["execution_structure_warnings"] >= 1
    assert profile["execution_structure"]["warnings"] >= 1


def test_v9_regression_never_emits_unpacked_baseline_evidence_ids(tmp_path: Path) -> None:
    baseline_path = _write_project(
        tmp_path,
        filename="baseline.L5X",
        tags=("Start", "OldOutput"),
        rung_text="XIC(Start)OTE(OldOutput);",
    )
    current_path = _write_project(
        tmp_path,
        filename="current.L5X",
        tags=("Start", "NewOutput"),
        rung_text="XIC(Start)OTE(NewOutput);",
    )
    current = analyze_rockwell_l5x(current_path)
    changes, _ = analyze_regression(baseline_path, current, [])

    current_ids = {tag.id for tag in current.project.tags}
    current_ids.update(rung.id for rung in current.project.rungs)
    current_ids.update(statement.id for statement in current.project.logic_statements)
    current_ids.update(logic.id for logic in current.project.output_logic)
    current_ids.update(program.id for program in current.project.programs)
    current_ids.update(routine.id for routine in current.project.routines)
    current_ids.update(task.id for task in current.project.tasks)
    current_ids.update(aoi.id for aoi in current.project.aois)
    current_ids.update(data_type.id for data_type in current.project.data_types)

    assert changes
    assert all(set(change.evidence_ids) <= current_ids for change in changes)
    removed = next(change for change in changes if change.change_type == "TAG_REMOVED")
    assert removed.evidence_ids == ()


def test_release_triggering_production_ci_contains_rockwell_v9_gate() -> None:
    root = Path(__file__).resolve().parents[1]
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (root / ".github" / "workflows" / "release-on-ci.yml").read_text(encoding="utf-8")

    assert "name: Production CI" in ci
    assert "scripts/qualify_rockwell_official.py" in ci
    assert ".devagent/rockwell-official-qualification-v9.json" in ci
    assert 'workflows: ["Production CI"]' in release
