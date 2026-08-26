from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.project_test_planner import BehaviorKind, TestIntentMethod, TestIntentTrust


def _write_boolean_project(
    tmp_path: Path,
    *,
    filename: str,
    controller: str,
    program: str,
    routine: str,
    input_a: str,
    input_b: str,
    output: str,
) -> Path:
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="{controller}" TargetType="Controller">
  <Controller Use="Target" Name="{controller}" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes /><Modules /><AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="{input_a}" TagType="Base" DataType="BOOL" />
      <Tag Name="{input_b}" TagType="Base" DataType="BOOL" />
      <Tag Name="{output}" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs><Program Name="{program}" MainRoutineName="{routine}"><Routines>
      <Routine Name="{routine}" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC({input_a})XIO({input_b})OTE({output});]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="{program}" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / filename
    path.write_text(payload, encoding="utf-8")
    return path


def _shape(plan) -> list[tuple[str, str, str, str, int, bool]]:
    return sorted(
        (
            item.kind.value,
            item.scenario,
            item.method.value,
            item.trust.value,
            len(item.preconditions),
            item.expected is not None,
        )
        for item in plan.test_intents
    )


def test_semantic_rename_produces_equivalent_test_shape(tmp_path: Path) -> None:
    first = _write_boolean_project(
        tmp_path,
        filename="alpha.L5X",
        controller="AlphaMachine",
        program="ProgramAlpha",
        routine="RoutineAlpha",
        input_a="PermitOne",
        input_b="BlockOne",
        output="CommandOne",
    )
    second = _write_boolean_project(
        tmp_path,
        filename="omega.L5X",
        controller="OmegaPlant",
        program="SequenceZeta",
        routine="LogicBeta",
        input_a="SignalGreen",
        input_b="SignalRed",
        output="ActuatorQ",
    )

    result_a = run_production_verification_v5(first)
    result_b = run_production_verification_v5(second)

    assert result_a.project_test_plan is not None
    assert result_b.project_test_plan is not None
    assert _shape(result_a.project_test_plan) == _shape(result_b.project_test_plan)
    assert result_a.project_test_plan.summary["hardcoded_domain_rules"] == 0
    assert result_b.project_test_plan.summary["hardcoded_domain_rules"] == 0
    assert {item.scenario for item in result_a.project_test_plan.test_intents} == {
        "POSITIVE_PATH",
        "NEGATIVE_BLOCK",
    }


def _write_timer_project(tmp_path: Path, *, variable_preset: bool = False) -> Path:
    preset = "TimerPreset" if variable_preset else "2000"
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="NeutralProcess" TargetType="Controller">
  <Controller Use="Target" Name="NeutralProcess" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes /><Modules /><AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="InputX" TagType="Base" DataType="BOOL" />
      <Tag Name="TimerX" TagType="Base" DataType="TIMER" />
      <Tag Name="TimerPreset" TagType="Base" DataType="DINT" />
      <Tag Name="OutputY" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs><Program Name="Process" MainRoutineName="Logic"><Routines>
      <Routine Name="Logic" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(InputX)TON(TimerX,{preset},0);]]></Text></Rung>
        <Rung Number="1"><Text><![CDATA[XIC(TimerX.DN)OTE(OutputY);]]></Text></Rung>
        <Rung Number="2"><Text><![CDATA[XIO(InputX)RES(TimerX);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="Process" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / ("variable-timer.L5X" if variable_preset else "literal-timer.L5X")
    path.write_text(payload, encoding="utf-8")
    return path


def test_different_project_semantics_create_different_test_matrix(tmp_path: Path) -> None:
    boolean_project = _write_boolean_project(
        tmp_path,
        filename="boolean.L5X",
        controller="BooleanOnly",
        program="ProgramA",
        routine="Logic",
        input_a="A",
        input_b="B",
        output="C",
    )
    timer_project = _write_timer_project(tmp_path)

    boolean_result = run_production_verification_v5(boolean_project)
    timer_result = run_production_verification_v5(timer_project)

    boolean_kinds = {item.kind for item in boolean_result.project_test_plan.test_intents}
    timer_kinds = {item.kind for item in timer_result.project_test_plan.test_intents}
    timer_scenarios = {item.scenario for item in timer_result.project_test_plan.test_intents}

    assert BehaviorKind.TIMER not in boolean_kinds
    assert BehaviorKind.TIMER in timer_kinds
    assert {"TIMER_NOT_EARLY", "TIMER_AT_PRESET", "RESET_PATH"} <= timer_scenarios
    assert _shape(boolean_result.project_test_plan) != _shape(timer_result.project_test_plan)


def test_variable_timer_preset_is_not_given_an_invented_expected_time(tmp_path: Path) -> None:
    result = run_production_verification_v5(_write_timer_project(tmp_path, variable_preset=True))
    timer_intents = [item for item in result.project_test_plan.test_intents if item.kind is BehaviorKind.TIMER]

    assert timer_intents
    dynamic = next(item for item in timer_intents if item.scenario == "TIMER_DYNAMIC_BEHAVIOR")
    assert dynamic.method is TestIntentMethod.SIMULATOR
    assert dynamic.trust is TestIntentTrust.NOT_PROVEN
    assert dynamic.expected is None


def test_v5_stage8_and_evidence_expose_project_specific_plan(tmp_path: Path) -> None:
    project = _write_boolean_project(
        tmp_path,
        filename="evidence.L5X",
        controller="EvidenceController",
        program="UnitProgram",
        routine="UnitLogic",
        input_a="ReadyInput",
        input_b="StopInput",
        output="RunOutput",
    )
    result = run_production_verification_v5(project)

    assert result.project_test_plan is not None
    assert result.stages[7].number == 8
    assert "project-specific test intent" in result.stages[7].summary
    plan_items = [item for item in result.evidence if item.kind == "PROJECT_TEST_PLAN"]
    assert len(plan_items) == 1
    assert plan_items[0].payload["summary"]["hardcoded_domain_rules"] == 0
    assert plan_items[0].payload["test_intents"]


def test_core_planner_contains_no_warehouse_equipment_name_rules() -> None:
    source = (Path(__file__).resolve().parents[1] / "devagent" / "plc" / "project_test_planner.py").read_text(encoding="utf-8").casefold()
    forbidden = (
        "warehouse",
        "conveyor",
        "diverter",
        "chute",
        "barcode",
        "sortation",
        "palletizer",
        "hvac",
    )
    assert not any(token in source for token in forbidden)
