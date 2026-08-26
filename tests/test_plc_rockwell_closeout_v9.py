from pathlib import Path

from devagent.plc import analyze_rockwell_l5x
from devagent.plc.production_evidence import evidence_index
from devagent.plc.production_regression import analyze_regression
from devagent.plc.rockwell_closeout import rockwell_capability_profile


def _write_project(
    tmp_path: Path,
    *,
    filename: str = "Closeout.L5X",
    routine_type: str = "RLL",
    rung_text: str = "XIC(Start)OTE(Fan);",
    tag_names: tuple[str, ...] = ("Start", "Fan"),
    alias: tuple[str, str] | None = None,
) -> Path:
    tags = "\n".join(
        f'<Tag Name="{name}" TagType="Base" DataType="BOOL" />'
        for name in tag_names
    )
    if alias is not None:
        tags += f'\n<Tag Name="{alias[0]}" TagType="Alias" DataType="BOOL" AliasFor="{alias[1]}" />'
    if routine_type == "RLL":
        body = f'''<RLLContent><Rung Number="0"><Text><![CDATA[{rung_text}]]></Text></Rung></RLLContent>'''
    elif routine_type == "ST":
        body = '<STContent><Line Number="0"><![CDATA[Fan := Start;]]></Line></STContent>'
    elif routine_type == "FBD":
        body = '<FBDContent />'
    else:
        body = '<SFCContent />'
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="Closeout" TargetType="Controller">
  <Controller Use="Target" Name="Closeout" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <Modules><Module Name="Local" CatalogNumber="1756-L85E" Vendor="1" /></Modules>
    <AddOnInstructionDefinitions />
    <Tags>{tags}</Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="MainRoutine"><Routines>
        <Routine Name="MainRoutine" Type="{routine_type}">{body}</Routine>
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


def _support_check(engineering):
    return next(
        item
        for item in engineering.static_checks
        if item.id == "ROCKWELL_PRODUCTION_SUPPORT_CONTRACT"
    )


def test_v9_simple_rll_has_complete_static_support_contract(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(_write_project(tmp_path))
    profile = rockwell_capability_profile(engineering.project)
    assert _support_check(engineering).status.value == "PASS"
    assert profile["static_contract"] == "COMPLETE"
    assert profile["routine_types"] == {"RLL": 1}
    assert engineering.outcome.value == "STATICALLY_VERIFIED"
    assert len(engineering.fat_tests) >= 2
    assert all(test.execution_status == "NOT_RUN" for test in engineering.fat_tests)


def test_v9_fbd_and_sfc_are_inventory_only_and_fail_closed(tmp_path: Path) -> None:
    for routine_type in ("FBD", "SFC"):
        engineering = analyze_rockwell_l5x(
            _write_project(
                tmp_path,
                filename=f"Closeout-{routine_type}.L5X",
                routine_type=routine_type,
            )
        )
        profile = rockwell_capability_profile(engineering.project)
        assert _support_check(engineering).status.value == "WARN"
        assert profile["routine_support"][routine_type] == "INVENTORY_ONLY_NOT_PROVEN"
        assert profile["static_gaps"]["unsupported_routines"] == 1
        assert engineering.outcome.value == "PARTIALLY_VERIFIED"
        assert not engineering.fat_tests


def test_v9_complex_instruction_families_are_partial_not_fake_full(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(
        _write_project(
            tmp_path,
            rung_text=(
                "MSG(MsgControl);"
                "GSV(WallClockTime,,DateTime,CurrentTime);"
                "PID(PIDLoop,ProcessVariable,ControlVariable,0,0,0);"
                "MAM(Axis1,1,0,1,1,1,0,0,0,0,0,0,0);"
            ),
        )
    )
    profile = rockwell_capability_profile(engineering.project)
    partial = {name.upper() for name in engineering.project.partially_modeled_instruction_names}
    assert {"MSG", "GSV", "PID", "MAM"} <= partial
    assert not ({"MSG", "GSV", "PID", "MAM"} & {name.upper() for name in engineering.project.unknown_instruction_names})
    assert profile["instruction_semantics"]["partial_families"]["COMMUNICATION"] == ["MSG"]
    assert profile["instruction_semantics"]["partial_families"]["SYSTEM"] == ["GSV"]
    assert profile["instruction_semantics"]["partial_families"]["PROCESS_CONTROL"] == ["PID"]
    assert profile["instruction_semantics"]["partial_families"]["MOTION"] == ["MAM"]
    assert _support_check(engineering).status.value == "WARN"
    assert engineering.outcome.value == "PARTIALLY_VERIFIED"
    assert not engineering.fat_tests


def test_v9_capability_profile_is_part_of_evidence_package(tmp_path: Path) -> None:
    engineering = analyze_rockwell_l5x(_write_project(tmp_path))
    capability = next(item for item in evidence_index(engineering) if item.kind == "ROCKWELL_CAPABILITY_PROFILE")
    assert capability.source_sha256 == engineering.project.metadata.source_sha256
    assert capability.payload["schema"] == "devagent-rockwell-capability-v9"
    assert capability.payload["dynamic_contract"]["qualified_backend_required_for_runtime_pass"] is True
    assert capability.payload["dynamic_contract"]["physical_controller_writes_default"] is False


def test_v9_regression_ignores_case_only_tag_and_logic_changes(tmp_path: Path) -> None:
    baseline = _write_project(
        tmp_path,
        filename="baseline-case.L5X",
        rung_text="XIC(Start)OTE(Fan);",
        tag_names=("Start", "Fan"),
    )
    current_path = _write_project(
        tmp_path,
        filename="current-case.L5X",
        rung_text="XIC(start)OTE(fan);",
        tag_names=("start", "fan"),
    )
    current = analyze_rockwell_l5x(current_path)
    changes, _ = analyze_regression(baseline, current, [])
    assert changes == []


def test_v9_regression_treats_alias_output_as_same_physical_logic(tmp_path: Path) -> None:
    baseline = _write_project(
        tmp_path,
        filename="baseline-alias.L5X",
        rung_text="XIC(Start)OTE(Fan);",
        tag_names=("Start", "Fan"),
    )
    current_path = _write_project(
        tmp_path,
        filename="current-alias.L5X",
        rung_text="XIC(Start)OTE(FanAlias);",
        tag_names=("Start", "Fan"),
        alias=("FanAlias", "Fan"),
    )
    current = analyze_rockwell_l5x(current_path)
    changes, _ = analyze_regression(baseline, current, [])
    assert any(change.change_type == "TAG_ADDED" and "FanAlias" in change.subject for change in changes)
    assert not any(change.change_type.startswith("LOGIC_") for change in changes)


def test_v9_regression_detects_alias_retarget_as_high_impact_metadata_change(tmp_path: Path) -> None:
    baseline = _write_project(
        tmp_path,
        filename="baseline-retarget.L5X",
        tag_names=("Start", "Fan", "Fan2"),
        alias=("CmdAlias", "Fan"),
    )
    current_path = _write_project(
        tmp_path,
        filename="current-retarget.L5X",
        tag_names=("Start", "Fan", "Fan2"),
        alias=("CmdAlias", "Fan2"),
    )
    current = analyze_rockwell_l5x(current_path)
    changes, _ = analyze_regression(baseline, current, [])
    changed = [change for change in changes if change.change_type == "TAG_CHANGED" and "CmdAlias" in change.subject]
    assert len(changed) == 1
    assert changed[0].severity.value == "HIGH"
    assert {name.casefold() for name in changed[0].affected_tags} >= {"cmdalias", "fan", "fan2"}
