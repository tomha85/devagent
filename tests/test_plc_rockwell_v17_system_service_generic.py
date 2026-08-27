from __future__ import annotations

from pathlib import Path

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome
from devagent.plc.rockwell_system_service_v17 import system_service_profile


def _project(tmp_path: Path) -> Path:
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="GenericSystemService" TargetType="Controller">
  <Controller Name="GenericSystemService" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="ClockValue" TagType="Base" DataType="DINT" />
    </Tags>
    <Programs>
      <Program Name="MainProgram" MainRoutineName="Main"><Routines>
        <Routine Name="Main" Type="RLL"><RLLContent>
          <Rung Number="0"><Text><![CDATA[GSV(Controller,THIS,WallClockTime,ClockValue);]]></Text></Rung>
        </RLLContent></Routine>
      </Routines></Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "GenericSystemService.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def test_v17_generic_gsv_gets_runtime_fat_without_becoming_static_proof(tmp_path: Path) -> None:
    result = run_production_verification_v5(_project(tmp_path))
    profile = system_service_profile(result.engineering.project)
    tests = [test for test in result.engineering.fat_tests if test.scenario == "SYSTEM_SERVICE_RUNTIME"]
    risks = [risk for risk in result.risks if risk.category == "SYSTEM_SERVICE_RUNTIME"]

    assert profile["occurrences"] == 1
    assert profile["gsv_occurrences"] == 1
    assert profile["ssv_occurrences"] == 0
    assert profile["major_fault_record_occurrences"] == 0
    assert profile["runtime_fat_tests"] == 1

    assert len(tests) == 1
    assert tests[0].output_tag == "ClockValue"
    assert tests[0].method == "RUNTIME_FAT_REQUIRED"
    assert tests[0].execution_status == "NOT_RUN"
    assert "system attribute" in tests[0].expected.lower()
    assert any("controller/system state" in step.lower() for step in tests[0].setup_steps)
    assert any("before and after" in step.lower() for step in tests[0].action_steps)

    assert len(risks) == 1
    assert risks[0].severity.value == "MEDIUM"
    assert "WallClockTime" in risks[0].title
    assert "SEMANTIC_COVERAGE" not in {risk.category for risk in result.risks}

    assert result.engineering.outcome is PLCOutcome.PARTIALLY_VERIFIED
    assert result.readiness is not None
    assert result.readiness.status.value == "NOT_READY"
