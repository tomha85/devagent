from pathlib import Path

from devagent.plc import analyze_rockwell_l5x, run_production_verification_v5
from devagent.plc.rockwell_closeout import rockwell_capability_profile


def _write_project(
    tmp_path: Path,
    *,
    filename: str,
    tags: tuple[str, ...],
    rung_text: str,
    main_routine_name: str | None = "Logic",
) -> Path:
    tag_xml = "\n".join(
        f'<Tag Name="{name}" TagType="Base" DataType="BOOL" />'
        for name in tags
    )
    main_attr = f' MainRoutineName="{main_routine_name}"' if main_routine_name is not None else ""
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="MergeGate" TargetType="Controller">
  <Controller Use="Target" Name="MergeGate" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <Modules><Module Name="Local" CatalogNumber="1756-L85E" Vendor="1" /></Modules>
    <AddOnInstructionDefinitions />
    <Tags>{tag_xml}</Tags>
    <Programs>
      <Program Name="Main"{main_attr}><Routines>
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
        filename="missing-main-target.L5X",
        tags=("Start", "Fan"),
        rung_text="XIC(Start)OTE(Fan);",
        main_routine_name="MissingRoutine",
    )
    engineering = analyze_rockwell_l5x(project_path)
    profile = rockwell_capability_profile(engineering.project)

    assert engineering.outcome.value == "PARTIALLY_VERIFIED"
    assert profile["static_contract"] == "PARTIAL_FAIL_CLOSED"
    assert profile["static_gaps"]["execution_structure_warnings"] >= 1


def test_v9_scheduled_program_without_main_routine_is_fail_closed(tmp_path: Path) -> None:
    project_path = _write_project(
        tmp_path,
        filename="no-main-attribute.L5X",
        tags=("Start", "Fan"),
        rung_text="XIC(Start)OTE(Fan);",
        main_routine_name=None,
    )
    engineering = analyze_rockwell_l5x(project_path)
    profile = rockwell_capability_profile(engineering.project)

    assert profile["static_contract"] == "PARTIAL_FAIL_CLOSED"
    assert profile["static_gaps"]["scheduled_programs_without_main_routine"] == 1
    assert profile["execution_structure"]["scheduled_programs_without_main_routine"] == ["Main"]
    assert engineering.outcome.value == "PARTIALLY_VERIFIED"


def test_v9_regression_packages_baseline_and_current_evidence_without_id_collision(tmp_path: Path) -> None:
    baseline_path = _write_project(
        tmp_path,
        filename="baseline-path.L5X",
        tags=("A", "B", "Out"),
        rung_text="XIC(A)OTE(Out);",
    )
    current_path = _write_project(
        tmp_path,
        filename="current-path.L5X",
        tags=("A", "B", "Out"),
        rung_text="XIC(B)OTE(Out);",
    )
    result = run_production_verification_v5(current_path, baseline_path=baseline_path)
    logic_change = next(
        change for change in result.regression_changes
        if change.change_type == "LOGIC_CHANGED"
    )
    evidence_ids = {item.id for item in result.evidence}

    assert len(logic_change.evidence_ids) == 2
    assert any(item.startswith(f"BASELINE:{result.baseline_sha256}:") for item in logic_change.evidence_ids)
    assert any(not item.startswith("BASELINE:") for item in logic_change.evidence_ids)
    assert set(logic_change.evidence_ids) <= evidence_ids
    baseline_items = [
        item for item in result.evidence
        if item.id in logic_change.evidence_ids and item.id.startswith("BASELINE:")
    ]
    current_items = [
        item for item in result.evidence
        if item.id in logic_change.evidence_ids and not item.id.startswith("BASELINE:")
    ]
    assert len(baseline_items) == 1
    assert len(current_items) == 1
    assert baseline_items[0].source_sha256 == result.baseline_sha256
    assert baseline_items[0].payload["baseline"] is True
    assert baseline_items[0].payload["paths"] == [[{"tag": "A", "required": True}]]
    assert current_items[0].payload["paths"] == [[{"tag": "B", "required": True}]]


def test_release_triggering_production_ci_contains_rockwell_v9_gate() -> None:
    root = Path(__file__).resolve().parents[1]
    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (root / ".github" / "workflows" / "release-on-ci.yml").read_text(encoding="utf-8")

    assert "name: Production CI" in ci
    assert "scripts/qualify_rockwell_official.py" in ci
    assert ".devagent/rockwell-official-qualification-v9.json" in ci
    assert 'workflows: ["Production CI"]' in release
