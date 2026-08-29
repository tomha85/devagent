from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from devagent.live.engineering_context import load_live_engineering_context
from devagent.live.manager import ManagedPlcStatus, PlcReadResult, PlcSessionState, PlcConnectionSpec
from devagent.live.models import BrowseNode, Quality, RuntimeValue
from devagent.live.question_resolution import resolve_explicit_tag_reference
from devagent.live.recursive_assistant import RecursiveLiveCommissioningAssistant
from devagent.live.security import LiveSecurityConfig
from devagent.live.system_health import is_system_health_question


def _engineering():
    metadata = SimpleNamespace(
        vendor="ROCKWELL",
        engineering_tool="Studio 5000",
        source_path="/tmp/ConveyorDemo.L5X",
        source_sha256="a" * 64,
        controller_name="WarehouseDemo",
        full_project=True,
    )
    tags = [
        SimpleNamespace(
            id=f"tag-{name.casefold()}",
            name=name,
            scope="Controller",
            data_type=data_type,
            description=None,
            external_access="Read Only",
            alias_for=None,
        )
        for name, data_type in (
            ("AutoMode", "BOOL"),
            ("StartRequest", "BOOL"),
            ("DriveReady", "BOOL"),
            ("DownstreamReady", "BOOL"),
            ("RunCmd", "BOOL"),
            ("FaultCode", "DINT"),
        )
    ]
    source = SimpleNamespace(locator="WarehouseDemo / ConveyorProgram / MainRoutine / Rung 0")
    path = SimpleNamespace(
        terms=tuple(
            SimpleNamespace(tag=name, required=True)
            for name in ("AutoMode", "StartRequest", "DriveReady", "DownstreamReady")
        )
    )
    rule = SimpleNamespace(
        id="logic-runcmd",
        output_tag="RunCmd",
        instruction="OTE",
        paths=(path,),
        source=source,
        language="RLL",
        origin="RUNG",
        semantic_state=SimpleNamespace(value="FULL"),
    )
    project = SimpleNamespace(
        metadata=metadata,
        tags=tags,
        output_logic=[rule],
        logic_statements=[],
        warnings=[],
    )
    return SimpleNamespace(project=project)


def _loaded():
    return load_live_engineering_context(
        Path("/tmp/ConveyorDemo.L5X"),
        project_loader=lambda _path: _engineering(),
    )


class _FakeManager:
    def __init__(self, values: dict[str, object]):
        self.plc_ids = ("plc1",)
        self._state = PlcSessionState.DISCONNECTED
        self._values = dict(values)
        data_types = {
            "AutoMode": "Boolean",
            "StartRequest": "Boolean",
            "DriveReady": "Boolean",
            "DownstreamReady": "Boolean",
            "RunCmd": "Boolean",
            "FaultCode": "Int32",
        }
        self._nodes = tuple(
            BrowseNode(
                path=f"Objects.Warehouse.Conveyor1.{name}",
                node_id=f"ns=1;s=Warehouse.Conveyor1.{name}",
                browse_name=name,
                display_name=name,
                node_class="Variable",
                data_type=data_types[name],
                user_access=("CurrentRead",),
                readable=True,
                writable=False,
            )
            for name in data_types
        )

    def status(self, plc_id: str) -> ManagedPlcStatus:
        assert plc_id == "plc1"
        return ManagedPlcStatus(
            plc_id="plc1",
            plc_name="WarehouseDemo",
            endpoint="opc.tcp://127.0.0.1:4841/devagent/simulator/",
            state=self._state,
            connected=self._state is PlcSessionState.CONNECTED,
            authentication_mode="ANONYMOUS",
            security_summary="NONE",
            successful_connections=int(self._state is PlcSessionState.CONNECTED),
            last_error=None,
            changed_at=datetime.now(timezone.utc),
        )

    def statuses(self):
        return {"plc1": self.status("plc1")}

    async def connect(self, plc_id: str):
        self._state = PlcSessionState.CONNECTED
        return self.status(plc_id)

    async def disconnect(self, plc_id: str):
        self._state = PlcSessionState.DISCONNECTED
        return self.status(plc_id)

    async def browse(self, plc_id: str, *, max_depth: int, max_nodes: int):
        assert plc_id == "plc1"
        return self._nodes[:max_nodes]

    async def read_many(self, requests):
        now = datetime.now(timezone.utc)
        values = []
        for node_id in requests["plc1"]:
            name = node_id.rsplit(".", 1)[-1]
            value = self._values[name]
            variant_type = "Boolean" if isinstance(value, bool) else "Int32"
            values.append(
                RuntimeValue(
                    node_id=node_id,
                    value=value,
                    variant_type=variant_type,
                    status_code="Good",
                    quality=Quality.GOOD,
                    source_timestamp=now,
                    server_timestamp=now,
                    received_at=now,
                    age_seconds=0.0,
                    stale=False,
                )
            )
        return {
            "plc1": PlcReadResult(
                plc_id="plc1",
                values=tuple(values),
                state=PlcSessionState.CONNECTED,
            )
        }


def _assistant(values: dict[str, object]):
    return RecursiveLiveCommissioningAssistant(
        _loaded(),
        PlcConnectionSpec(
            plc_id="plc1",
            plc_name="WarehouseDemo",
            endpoint="opc.tcp://127.0.0.1:4841/devagent/simulator/",
            security=LiveSecurityConfig(),
        ),
        manager=_FakeManager(values),
        history_seconds=0,
    )


def _normal_values(**overrides):
    values = {
        "AutoMode": True,
        "StartRequest": True,
        "DriveReady": True,
        "DownstreamReady": True,
        "RunCmd": True,
        "FaultCode": 0,
    }
    values.update(overrides)
    return values


def test_system_health_question_detection_includes_field_engineer_phrasing() -> None:
    assert is_system_health_question("Does the system have any faults?")
    assert is_system_health_question("What's wrong with this system?")
    assert is_system_health_question("Is the machine OK?")
    assert is_system_health_question("he thong co loi khong?")
    assert not is_system_health_question("Why is RunCmd false?")


def test_explicit_tag_resolver_finds_input_signal_not_only_outputs() -> None:
    context = _loaded().context
    tag = resolve_explicit_tag_reference(context, "Why is DownstreamReady false?")
    assert tag is not None
    assert tag.name == "DownstreamReady"


def test_system_health_reports_operational_blocker_without_calling_it_fault() -> None:
    async def scenario():
        assistant = _assistant(
            _normal_values(DownstreamReady=False, RunCmd=False)
        )
        reply = await assistant.answer("Does the system have any faults?")
        text = reply.render_text()
        assert "DEVAGENT LIVE SYSTEM HEALTH" in text
        assert "Status: ATTENTION_REQUIRED" in text
        assert "[BLOCKER] RunCmd" in text
        assert "DownstreamReady" in text
        assert "FaultCode = 0" in text
        assert "No explicit PLC fault signal is proven active" in text
        await assistant.close()

    asyncio.run(scenario())


def test_system_health_reports_no_current_proven_fault_for_consistent_normal_state() -> None:
    async def scenario():
        assistant = _assistant(_normal_values())
        reply = await assistant.answer("Is the system OK?")
        text = reply.render_text()
        assert "Status: NO_CURRENT_PROVEN_FAULT" in text
        assert "Current proven/observed issues: NONE" in text
        assert "FaultCode = 0" in text
        assert "does not prove the entire physical machine/process is healthy" in text
        await assistant.close()

    asyncio.run(scenario())


def test_system_health_reports_nonzero_fault_code_as_fault() -> None:
    async def scenario():
        assistant = _assistant(_normal_values(FaultCode=37))
        reply = await assistant.answer("Does the system have any faults?")
        text = reply.render_text()
        assert "Status: ATTENTION_REQUIRED" in text
        assert "[FAULT] FaultCode" in text
        assert "FaultCode=37" in text
        await assistant.close()

    asyncio.run(scenario())


def test_system_health_reports_logic_conflict_when_output_disagrees_with_model() -> None:
    async def scenario():
        assistant = _assistant(_normal_values(RunCmd=False))
        reply = await assistant.answer("What's wrong with the system?")
        text = reply.render_text()
        assert "Status: ATTENTION_REQUIRED" in text
        assert "[CONFLICT] RunCmd" in text
        assert "conditions for RunCmd are currently satisfied" in text
        await assistant.close()

    asyncio.run(scenario())


def test_command_not_requested_is_inactive_but_not_misclassified_as_system_fault() -> None:
    async def scenario():
        assistant = _assistant(_normal_values(StartRequest=False, RunCmd=False))
        reply = await assistant.answer("Does the system have any faults?")
        text = reply.render_text()
        assert "Status: NO_CURRENT_PROVEN_FAULT" in text
        assert "Modeled inactive outputs not classified as a fault:" in text
        assert "RunCmd: blocked by StartRequest" in text
        assert "[BLOCKER] RunCmd" not in text
        await assistant.close()

    asyncio.run(scenario())


def test_explicit_input_followup_overrides_previous_output_target_and_fails_closed() -> None:
    async def scenario():
        assistant = _assistant(_normal_values(DownstreamReady=False, RunCmd=False))
        first = await assistant.answer("Why is RunCmd false?")
        assert first.target_output == "RunCmd"
        assert "DownstreamReady" in first.render_text()

        second = await assistant.answer("Why is DownstreamReady false?")
        assert second.target_output == "DownstreamReady"
        text = second.render_text()
        assert "Target: DownstreamReady" in text
        assert "Current value: False" in text
        assert "Deterministic writer: NOT FOUND" in text
        assert "physical/process root cause: NOT PROVEN" in text

        third = await assistant.answer("Why?")
        assert third.target_output == "DownstreamReady"
        assert "Target: DownstreamReady" in third.render_text()
        await assistant.close()

    asyncio.run(scenario())


def test_system_health_question_never_reuses_previous_output_target() -> None:
    async def scenario():
        assistant = _assistant(_normal_values(DownstreamReady=False, RunCmd=False))
        targeted = await assistant.answer("Why is RunCmd false?")
        assert targeted.target_output == "RunCmd"

        health = await assistant.answer("Does the system have any faults?")
        assert health.target_output is None
        assert health.render_text().startswith("DEVAGENT LIVE SYSTEM HEALTH")
        await assistant.close()

    asyncio.run(scenario())
