from __future__ import annotations

import asyncio
import socket

import pytest

pytest.importorskip("asyncua")

from devagent.live.engineering_context import load_live_engineering_context
from devagent.live.manager import PlcConnectionSpec
from devagent.live.recursive_assistant import RecursiveLiveCommissioningAssistant
from devagent.live.security import LiveSecurityConfig
from devagent.live.simulator import OpcUaSimulator


def _free_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"opc.tcp://127.0.0.1:{port}/devagent/simulator/"


def _write_project(tmp_path):
    payload = '''<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="WarehouseDemo" TargetType="Controller">
  <Controller Use="Target" Name="WarehouseDemo" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <DataTypes />
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="AutoMode" TagType="Base" DataType="BOOL" ExternalAccess="Read Only" />
      <Tag Name="StartRequest" TagType="Base" DataType="BOOL" ExternalAccess="Read Only" />
      <Tag Name="DriveReady" TagType="Base" DataType="BOOL" ExternalAccess="Read Only" />
      <Tag Name="DownstreamReady" TagType="Base" DataType="BOOL" ExternalAccess="Read Only" />
      <Tag Name="RunCmd" TagType="Base" DataType="BOOL" ExternalAccess="Read Only" />
      <Tag Name="FaultCode" TagType="Base" DataType="DINT" ExternalAccess="Read Only" />
    </Tags>
    <Programs>
      <Program Name="ConveyorProgram" MainRoutineName="MainRoutine">
        <Routines>
          <Routine Name="MainRoutine" Type="RLL"><RLLContent>
            <Rung Number="0"><Text><![CDATA[XIC(AutoMode)XIC(StartRequest)XIC(DriveReady)XIC(DownstreamReady)OTE(RunCmd);]]></Text></Rung>
          </RLLContent></Routine>
        </Routines>
      </Program>
    </Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS"><ScheduledPrograms><ScheduledProgram Name="ConveyorProgram" /></ScheduledPrograms></Task></Tasks>
  </Controller>
</RSLogix5000Content>'''
    path = tmp_path / "WarehouseDemo.L5X"
    path.write_text(payload, encoding="utf-8")
    return path


def test_system_master_can_go_general_to_health_to_specific_signal(tmp_path) -> None:
    async def scenario() -> None:
        endpoint = _free_endpoint()
        project_path = _write_project(tmp_path)
        loaded = load_live_engineering_context(project_path)

        async with OpcUaSimulator(endpoint, scenario="blocker"):
            assistant = RecursiveLiveCommissioningAssistant(
                loaded,
                PlcConnectionSpec(
                    plc_id="plc1",
                    plc_name="WarehouseDemo",
                    endpoint=endpoint,
                    security=LiveSecurityConfig(),
                ),
                browse_max_depth=5,
                browse_max_nodes=500,
                history_seconds=0,
            )
            try:
                await assistant.start()
                assert assistant.reconciliation is not None
                assert len(assistant.reconciliation.accepted_mappings()) == 6
                assert len(assistant.reconciliation.unresolved_mappings()) == 0

                overview = await assistant.answer("What do you know about this system?")
                overview_text = overview.render_text()
                assert "DEVAGENT LIVE SYSTEM MASTER" in overview_text
                assert "Controller: WarehouseDemo" in overview_text
                assert "Known outputs:" in overview_text
                assert "RunCmd" in overview_text
                assert "DownstreamReady" in overview_text

                health = await assistant.answer("Does the system have any faults?")
                health_text = health.render_text()
                assert "DEVAGENT LIVE SYSTEM HEALTH" in health_text
                assert "Status: ATTENTION_REQUIRED" in health_text
                assert "[BLOCKER] RunCmd" in health_text
                assert "DownstreamReady" in health_text
                assert "FaultCode = 0" in health_text

                specific = await assistant.answer("Why is RunCmd false?")
                assert specific.target_output == "RunCmd"
                assert "Blocking condition(s): DownstreamReady" in specific.render_text()

                upstream = await assistant.answer("Why is DownstreamReady false?")
                upstream_text = upstream.render_text()
                assert upstream.target_output == "DownstreamReady"
                assert "Current value: False" in upstream_text
                assert "Deterministic writer: NOT FOUND" in upstream_text
                assert "physical/process root cause: NOT PROVEN" in upstream_text
            finally:
                await assistant.close()

    asyncio.run(scenario())
