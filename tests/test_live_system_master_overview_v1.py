from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from devagent.live.assistant import LiveCommissioningAssistant
from devagent.live.engineering_context import load_live_engineering_context
from devagent.live.manager import ManagedPlcStatus, PlcConnectionSpec, PlcSessionState
from devagent.live.security import LiveSecurityConfig
from devagent.live.tag_reconciliation import (
    LiveTagMapping,
    LiveTagMappingStatus,
    LiveTagReconciliation,
)


def _loaded():
    metadata = SimpleNamespace(
        vendor="ROCKWELL",
        engineering_tool="Studio 5000",
        source_path="/tmp/master.L5X",
        source_sha256="a" * 64,
        controller_name="WarehouseMaster",
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
        for name in ("AutoMode", "DownstreamReady", "RunCmd")
    ]
    source = SimpleNamespace(locator="WarehouseMaster / Conveyor / Main / Rung 0")
    rule = SimpleNamespace(
        id="logic-run",
        output_tag="RunCmd",
        instruction="OTE",
        paths=(
            SimpleNamespace(
                terms=(
                    SimpleNamespace(tag="AutoMode", required=True),
                    SimpleNamespace(tag="DownstreamReady", required=True),
                )
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
        Path("/tmp/master.L5X"),
        project_loader=lambda _path: engineering,
    )


class _StatusOnlyManager:
    plc_ids = ("plc1",)

    def status(self, plc_id: str) -> ManagedPlcStatus:
        assert plc_id == "plc1"
        return ManagedPlcStatus(
            plc_id="plc1",
            plc_name="WarehouseMaster",
            endpoint="opc.tcp://127.0.0.1:4841/",
            state=PlcSessionState.CONNECTED,
            connected=True,
            authentication_mode="ANONYMOUS",
            security_summary="NONE",
            successful_connections=1,
            last_error=None,
            changed_at=None,
        )


def test_system_master_overview_exposes_general_to_specific_knowledge() -> None:
    loaded = _loaded()
    assistant = LiveCommissioningAssistant(
        loaded,
        PlcConnectionSpec(
            plc_id="plc1",
            plc_name="WarehouseMaster",
            endpoint="opc.tcp://127.0.0.1:4841/",
            security=LiveSecurityConfig(),
        ),
        manager=_StatusOnlyManager(),
    )
    assistant.reconciliation = LiveTagReconciliation(
        plc_id="plc1",
        mappings=tuple(
            LiveTagMapping(
                tag_id=tag.id,
                tag_name=tag.name,
                tag_scope=tag.scope,
                tag_data_type=tag.data_type,
                status=LiveTagMappingStatus.AUTO_BOUND,
                reason="exact",
                candidates=(),
                selected_node_id=f"ns=1;s={tag.name}",
                selected_path=f"Objects.Warehouse.{tag.name}",
                evidence_id=f"MAP-{tag.id}",
            )
            for tag in loaded.context.tags
        ),
    )

    assert assistant._is_overview_question("What do you know about this system?")
    assert assistant._is_overview_question("What signals are available?")

    text = assistant.system_overview().render_text()
    assert text.startswith("DEVAGENT LIVE SYSTEM MASTER")
    assert "Controller: WarehouseMaster" in text
    assert "Full project model: YES" in text
    assert "Known outputs:" in text
    assert "- RunCmd" in text
    assert "Mapped engineering/live signals:" in text
    assert "- DownstreamReady" in text
    assert "Ask from general to specific" in text
    assert "Mode: READ ONLY" in text
