from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from devagent.live.errors import LiveConfigurationError
from devagent.live.manager import ManagedPlcStatus, PlcReadResult, PlcSessionState
from devagent.live.models import BrowseNode, Quality, RuntimeValue
from devagent.live.reconciled_evidence import build_reconciled_live_agent_evidence
from devagent.live.tag_reconciliation import (
    LiveTagMappingStatus,
    reconcile_engineering_tags,
)

NOW = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)


def _tag(tag_id: str, name: str, data_type: str = "BOOL"):
    return SimpleNamespace(
        id=tag_id,
        name=name,
        scope="controller",
        data_type=data_type,
        alias_for=None,
        external_access=None,
    )


def _node(node_id: str, name: str, data_type: str = "Boolean") -> BrowseNode:
    return BrowseNode(
        path=f"Objects.Warehouse.{name}",
        node_id=node_id,
        browse_name=name,
        display_name=name,
        node_class="Variable",
        data_type=data_type,
        user_access=("CurrentRead",),
        readable=True,
        writable=False,
    )


def _value(node_id: str, value, *, stale: bool = False) -> RuntimeValue:
    return RuntimeValue(
        node_id=node_id,
        value=value,
        variant_type="Boolean",
        status_code="Good",
        quality=Quality.GOOD,
        source_timestamp=NOW,
        server_timestamp=NOW,
        received_at=NOW,
        age_seconds=99.0 if stale else 0.0,
        stale=stale,
        replayed=False,
    )


class _Manager:
    def __init__(self, values_by_node):
        self.values_by_node = values_by_node
        self.requests = None

    def statuses(self):
        return {
            "plc-a": ManagedPlcStatus(
                plc_id="plc-a",
                plc_name="Warehouse PLC",
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
        values = tuple(
            self.values_by_node[node_id]
            for node_id in requests["plc-a"]
            if node_id in self.values_by_node
        )
        return {
            "plc-a": PlcReadResult(
                plc_id="plc-a",
                values=values,
                state=PlcSessionState.CONNECTED,
                error=None,
            )
        }


def test_reconciled_evidence_reads_selected_node_and_includes_mapping_provenance() -> None:
    async def scenario() -> None:
        reconciliation = reconcile_engineering_tags(
            "plc-a",
            [_tag("TAG:RunCmd", "RunCmd")],
            [_node("ns=2;s=RunCmd", "RunCmd")],
        )
        manager = _Manager(
            {"ns=2;s=RunCmd": _value("ns=2;s=RunCmd", True)}
        )
        result = await build_reconciled_live_agent_evidence(
            manager,
            reconciliation,
            required_tag_ids=["TAG:RunCmd"],
        )
        assert manager.requests == {"plc-a": ("ns=2;s=RunCmd",)}
        assert result.reconciliation.mappings[0].status is LiveTagMappingStatus.AUTO_BOUND
        kinds = [item.kind for item in result.evidence_for_agent()]
        assert "LIVE_TAG_MAPPING" in kinds
        assert "LIVE_OPCUA_CURRENT" in kinds

    asyncio.run(scenario())


def test_unresolved_required_tag_fails_before_any_live_read() -> None:
    async def scenario() -> None:
        reconciliation = reconcile_engineering_tags(
            "plc-a",
            [_tag("TAG:Missing", "Missing")],
            [],
        )
        manager = _Manager({})
        with pytest.raises(LiveConfigurationError, match="TAG:Missing=UNMATCHED"):
            await build_reconciled_live_agent_evidence(
                manager,
                reconciliation,
                required_tag_ids=["TAG:Missing"],
            )
        assert manager.requests is None

    asyncio.run(scenario())


def test_partial_mode_reads_only_safe_mappings_and_keeps_mapping_limitation() -> None:
    async def scenario() -> None:
        reconciliation = reconcile_engineering_tags(
            "plc-a",
            [_tag("TAG:RunCmd", "RunCmd"), _tag("TAG:Missing", "Missing")],
            [_node("ns=2;s=RunCmd", "RunCmd")],
        )
        manager = _Manager(
            {"ns=2;s=RunCmd": _value("ns=2;s=RunCmd", True)}
        )
        result = await build_reconciled_live_agent_evidence(
            manager,
            reconciliation,
            required_tag_ids=["TAG:RunCmd", "TAG:Missing"],
            require_all=False,
        )
        assert manager.requests == {"plc-a": ("ns=2;s=RunCmd",)}
        kinds = [item.kind for item in result.evidence_for_agent()]
        assert "LIVE_TAG_MAPPING" in kinds
        assert "LIVE_TAG_MAPPING_LIMITATION" in kinds
        assert "LIVE_OPCUA_CURRENT" in kinds

    asyncio.run(scenario())


def test_stale_value_remains_excluded_even_after_exact_tag_reconciliation() -> None:
    async def scenario() -> None:
        reconciliation = reconcile_engineering_tags(
            "plc-a",
            [_tag("TAG:RunCmd", "RunCmd")],
            [_node("ns=2;s=RunCmd", "RunCmd")],
        )
        manager = _Manager(
            {"ns=2;s=RunCmd": _value("ns=2;s=RunCmd", True, stale=True)}
        )
        result = await build_reconciled_live_agent_evidence(manager, reconciliation)
        kinds = [item.kind for item in result.evidence_for_agent()]
        assert "LIVE_TAG_MAPPING" in kinds
        assert "LIVE_OPCUA_STALE" not in kinds
        assert "LIVE_OPCUA_LIMITATION" in kinds
        assert len(result.live_pack.excluded_raw_evidence_ids) == 1

    asyncio.run(scenario())


def test_no_safely_reconciled_node_fails_even_in_partial_mode() -> None:
    async def scenario() -> None:
        reconciliation = reconcile_engineering_tags(
            "plc-a",
            [_tag("TAG:Missing", "Missing")],
            [],
        )
        manager = _Manager({})
        with pytest.raises(LiveConfigurationError, match="No safely reconciled live nodes"):
            await build_reconciled_live_agent_evidence(
                manager,
                reconciliation,
                require_all=False,
            )
        assert manager.requests is None

    asyncio.run(scenario())
