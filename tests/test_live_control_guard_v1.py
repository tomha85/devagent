from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from devagent.live.assistant import LiveAssistantReplyKind, LiveCommissioningAssistant
from devagent.live.control_guard import is_plc_control_request
from devagent.live.engineering_context import load_live_engineering_context
from devagent.live.manager import PlcConnectionSpec


def test_control_guard_blocks_explicit_control_but_not_diagnostic_wording():
    assert is_plc_control_request("Force Conveyor7_Run true") is True
    assert is_plc_control_request("How do I bypass SafetyOK?") is True
    assert is_plc_control_request("Please reset the drive fault") is True
    assert is_plc_control_request("Stop the conveyor") is True
    assert is_plc_control_request("Why did the conveyor stop?") is False
    assert is_plc_control_request("Why is Conveyor7_Run not starting?") is False


class _NeverConnectManager:
    plc_ids = ("plc1",)

    def status(self, plc_id):
        raise AssertionError("control refusal must happen before any OPC UA connection/status access")



def _loaded():
    project = SimpleNamespace(
        metadata=SimpleNamespace(
            vendor="ROCKWELL",
            engineering_tool="Studio 5000",
            source_path="project.L5X",
            source_sha256="a" * 64,
            controller_name="PLC",
            full_project=True,
        ),
        tags=[],
        output_logic=[],
        logic_statements=[],
        warnings=[],
    )
    return load_live_engineering_context(
        Path("project.L5X"),
        project_loader=lambda _path: SimpleNamespace(project=project),
    )


def test_assistant_refuses_control_request_before_connect_or_ai():
    async def scenario():
        assistant = LiveCommissioningAssistant(
            _loaded(),
            PlcConnectionSpec("plc1", "opc.tcp://127.0.0.1:4840/"),
            manager=_NeverConnectManager(),
        )
        reply = await assistant.answer("Force Conveyor7_Run true")
        assert reply.kind is LiveAssistantReplyKind.LIMITATION
        assert "read-only" in reply.render_text().casefold()
        assert "will not execute or instruct" in reply.render_text().casefold()

    asyncio.run(scenario())
