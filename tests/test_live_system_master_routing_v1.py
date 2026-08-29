from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from devagent.live.assistant import LiveCommissioningAssistant
from devagent.live.engineering_context import load_live_engineering_context
from devagent.live.manager import ManagedPlcStatus, PlcConnectionSpec, PlcSessionState
from devagent.live.security import LiveSecurityConfig


def _loaded():
    metadata = SimpleNamespace(
        vendor="ROCKWELL",
        engineering_tool="Studio 5000",
        source_path="/tmp/routing.L5X",
        source_sha256="a" * 64,
        controller_name="RoutingDemo",
        full_project=True,
    )
    tags = [
        SimpleNamespace(
            id=f"tag-{name.casefold()}",
            name=name,
            scope="Controller",
            data_type="BOOL",
            description=None,
            external_access="Read Only",
            alias_for=None,
        )
        for name in ("DownstreamReady", "RunCmd")
    ]
    source = SimpleNamespace(locator="RoutingDemo / Main / Rung 0")
    rule = SimpleNamespace(
        id="logic-run",
        output_tag="RunCmd",
        instruction="OTE",
        paths=(
            SimpleNamespace(
                terms=(SimpleNamespace(tag="DownstreamReady", required=True),)
            ),
        ),
        source=source,
        language="RLL",
        origin="RUNG",
        semantic_state=SimpleNamespace(value="FULL"),
    )
    engineering = SimpleNamespace(
        project=SimpleNamespace(
            metadata=metadata,
            tags=tags,
            output_logic=[rule],
            logic_statements=[],
            warnings=[],
        )
    )
    return load_live_engineering_context(
        Path("/tmp/routing.L5X"),
        project_loader=lambda _path: engineering,
    )


class _Manager:
    plc_ids = ("plc1",)

    def status(self, plc_id: str) -> ManagedPlcStatus:
        assert plc_id == "plc1"
        return ManagedPlcStatus(
            plc_id="plc1",
            plc_name="RoutingDemo",
            endpoint="opc.tcp://127.0.0.1:4841/",
            state=PlcSessionState.CONNECTED,
            connected=True,
            authentication_mode="ANONYMOUS",
            security_summary="NONE",
            successful_connections=1,
            last_error=None,
            changed_at=None,
        )


def _assistant() -> LiveCommissioningAssistant:
    return LiveCommissioningAssistant(
        _loaded(),
        PlcConnectionSpec(
            plc_id="plc1",
            plc_name="RoutingDemo",
            endpoint="opc.tcp://127.0.0.1:4841/",
            security=LiveSecurityConfig(),
        ),
        manager=_Manager(),
    )


def test_health_intent_is_not_consumed_by_broad_overview_phrase() -> None:
    assistant = _assistant()
    assert assistant._is_overview_question("Tell me about the system health") is False
    assert assistant._is_overview_question("Does the system have any faults?") is False


def test_explicit_engineering_target_is_not_consumed_by_signal_overview_phrase() -> None:
    assistant = _assistant()
    assert assistant._is_overview_question("What are the signals blocking RunCmd?") is False
    assert assistant._is_overview_question("Tell me about DownstreamReady") is False


def test_genuinely_general_signal_question_still_routes_to_system_master() -> None:
    assistant = _assistant()
    assert assistant._is_overview_question("What signals are available?") is True
    assert assistant._is_overview_question("What do you know about this system?") is True
