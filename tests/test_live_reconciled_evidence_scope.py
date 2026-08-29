from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from devagent.live.manager import ManagedPlcStatus, PlcReadResult, PlcSessionState
from devagent.live.models import BrowseNode, Quality, RuntimeValue
from devagent.live.reconciled_evidence import build_reconciled_live_agent_evidence
from devagent.live.tag_reconciliation import reconcile_engineering_tags

NOW = datetime(2026, 8, 29, 3, 10, tzinfo=timezone.utc)


def _tag(tag_id: str, name: str):
    return SimpleNamespace(
        id=tag_id,
        name=name,
        scope="controller",
        data_type="BOOL",
        alias_for=None,
        external_access=None,
    )


def _node(node_id: str, name: str) -> BrowseNode:
    return BrowseNode(
        path=f"Objects.{name}",
        node_id=node_id,
        browse_name=name,
        display_name=name,
        node_class="Variable",
        data_type="Boolean",
        user_access=("CurrentRead",),
        readable=True,
        writable=False,
    )


def _value(node_id: str) -> RuntimeValue:
    return RuntimeValue(
        node_id=node_id,
        value=True,
        variant_type="Boolean",
        status_code="Good",
        quality=Quality.GOOD,
        source_timestamp=NOW,
        server_timestamp=NOW,
        received_at=NOW,
        age_seconds=0.0,
        stale=False,
        replayed=False,
    )


class _Manager:
    def __init__(self) -> None:
        self.requests = None

    def statuses(self):
        return {
            "plc-a": ManagedPlcStatus(
                plc_id="plc-a",
                plc_name="PLC A",
                endpoint="opc.tcp://plc-a:4840/",
                state=PlcSessionState.CONNECTED,
                connected=True,
                authentication_mode="ANONYMOUS",
                security_summary="NONE",
                successful_connections=1,
                last_error=None,
                changed_at=NOW,
            )
        }

    async def read_many(self, requests):
        self.requests = requests
        values = tuple(_value(node_id) for node_id in requests["plc-a"])
        return {
            "plc-a": PlcReadResult(
                plc_id="plc-a",
                values=values,
                state=PlcSessionState.CONNECTED,
            )
        }


def test_requested_tag_subset_excludes_unrelated_mapping_evidence() -> None:
    async def scenario() -> None:
        reconciliation = reconcile_engineering_tags(
            "plc-a",
            [_tag("T1", "RunCmd"), _tag("T2", "Other")],
            [_node("n1", "RunCmd"), _node("n2", "Other")],
        )
        manager = _Manager()
        result = await build_reconciled_live_agent_evidence(
            manager,
            reconciliation,
            required_tag_ids=["T1"],
        )
        assert manager.requests == {"plc-a": ("n1",)}
        mapping_tag_ids = [item.payload["tag_id"] for item in result.mapping_evidence]
        assert mapping_tag_ids == ["T1"]
        rendered = " ".join(item.summary for item in result.evidence_for_agent())
        assert "(T1)" in rendered
        assert "(T2)" not in rendered

    asyncio.run(scenario())
