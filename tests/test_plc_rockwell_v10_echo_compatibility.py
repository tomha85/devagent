import hashlib
from pathlib import Path

import pytest

from devagent.plc.production_v5 import run_production_verification_v5
from devagent.plc.production_verification import compute_test_plan_sha256
from devagent.plc.rockwell_echo import (
    EchoRunnerDescriptor,
    RockwellRuntimeBinding,
    build_echo_execution_request,
)


def _write_project(tmp_path: Path, *, boolean: bool = True) -> Path:
    boolean_rung = '<Rung Number="0"><Text><![CDATA[XIC(Enable)OTE(Run);]]></Text></Rung>' if boolean else ''
    payload = f'''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="EchoRichPlan" TargetType="Controller">
  <Controller Name="EchoRichPlan" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes /><AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Enable" TagType="Base" DataType="BOOL" />
      <Tag Name="Run" TagType="Base" DataType="BOOL" />
      <Tag Name="Source" TagType="Base" DataType="DINT" />
      <Tag Name="Dest" TagType="Base" DataType="DINT" />
      <Tag Name="Timer1" TagType="Base" DataType="TIMER" />
    </Tags>
    <Programs><Program Name="MainProgram" MainRoutineName="Main"><Routines>
      <Routine Name="Main" Type="RLL"><RLLContent>
        {boolean_rung}
        <Rung Number="1"><Text><![CDATA[XIC(Enable)MOV(Source,Dest);]]></Text></Rung>
        <Rung Number="2"><Text><![CDATA[XIC(Enable)TON(Timer1,1000,0);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="MainProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "EchoRichPlan.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def _descriptor() -> EchoRunnerDescriptor:
    return EchoRunnerDescriptor(
        adapter_id="echo-qualified",
        adapter_version="1",
        vendor="Rockwell Automation",
        product="FactoryTalk Logix Echo",
        backend_kind="SIMULATOR",
        capabilities=("DOWNLOAD", "SNAPSHOT", "DATA_EXCHANGE", "COSIMULATION"),
        supported_controller_families=("ControlLogix 5580",),
        runner_path="/qualified/runner",
        runner_sha256="a" * 64,
        default_time_quantum_us=1000,
    )


def _binding(project_sha: str, runtime_sha: str) -> RockwellRuntimeBinding:
    return RockwellRuntimeBinding(
        analysis_project_sha256=project_sha,
        runtime_project_sha256=runtime_sha,
        controller_name="EchoRichPlan",
        adapter_id="echo-qualified",
        runner_sha256="a" * 64,
        approved_by="Controls",
        approved_at="2026-08-26T20:00:00Z",
        source_path="binding.json",
        source_sha256="b" * 64,
    )


def test_echo_v6_executes_boolean_subset_without_dropping_full_plan_binding(tmp_path: Path) -> None:
    result = run_production_verification_v5(_write_project(tmp_path))
    runtime = tmp_path / "runtime.ACD"
    runtime.write_bytes(b"runtime")
    runtime_sha = hashlib.sha256(runtime.read_bytes()).hexdigest()

    assert any(item.id.startswith("FAT-ACTION-") for item in result.engineering.fat_tests)
    assert any(item.id.startswith("FAT-STATEFUL-") for item in result.engineering.fat_tests)

    request, _ = build_echo_execution_request(
        result,
        runtime_project_path=runtime,
        descriptor=_descriptor(),
        binding=_binding(result.engineering.project.metadata.source_sha256, runtime_sha),
        backend_registry_sha256="c" * 64,
    )

    assert request["test_plan_sha256"] == compute_test_plan_sha256(result.engineering.fat_tests)
    # Preserve the established Echo V6 request schema: subset selection is an
    # adapter implementation detail, not a new field that could break a strict
    # already-qualified runner.
    assert "adapter_test_scope" not in request["execution_policy"]
    assert "test_selection" not in request
    assert all(item["assertion"]["type"] == "BOOL" for item in request["tests"])

    full_ids = {item.id for item in result.engineering.fat_tests}
    selected_ids = {item["test_id"] for item in request["tests"]}
    excluded_ids = full_ids - selected_ids
    assert selected_ids
    assert selected_ids < full_ids
    assert any(test_id.startswith("FAT-ACTION-") for test_id in excluded_ids)
    assert any(test_id.startswith("FAT-STATEFUL-") for test_id in excluded_ids)


def test_echo_v6_refuses_plan_with_no_boolean_compatible_tests(tmp_path: Path) -> None:
    result = run_production_verification_v5(_write_project(tmp_path, boolean=False))
    runtime = tmp_path / "runtime.ACD"
    runtime.write_bytes(b"runtime")
    runtime_sha = hashlib.sha256(runtime.read_bytes()).hexdigest()

    with pytest.raises(ValueError, match="no compatible typed-Boolean FAT tests"):
        build_echo_execution_request(
            result,
            runtime_project_path=runtime,
            descriptor=_descriptor(),
            binding=_binding(result.engineering.project.metadata.source_sha256, runtime_sha),
            backend_registry_sha256="c" * 64,
        )
