from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from devagent.live.errors import LiveConfigurationError, LiveConnectionError
from devagent.live.manager import MultiPlcConnectionManager, PlcConnectionSpec, PlcSessionState
from devagent.live.models import BrowseNode
from devagent.live.security import LiveSecurityConfig
from devagent.live.tag_reconciliation import (
    LiveTagMappingStatus,
    LiveTagMatchKind,
    LiveTypeCompatibility,
    reconcile_connected_plc_tags,
    reconcile_engineering_tags,
)


def _tag(
    tag_id: str,
    name: str,
    *,
    scope: str = "Controller",
    data_type: str = "BOOL",
    alias_for: str | None = None,
    external_access: str | None = None,
):
    return SimpleNamespace(
        id=tag_id,
        name=name,
        scope=scope,
        data_type=data_type,
        alias_for=alias_for,
        external_access=external_access,
    )


def _node(
    node_id: str,
    name: str,
    *,
    path: str | None = None,
    data_type: str = "Boolean",
    readable: bool = True,
    node_class: str = "Variable",
) -> BrowseNode:
    return BrowseNode(
        path=path or f"Objects.{name}",
        node_id=node_id,
        browse_name=name,
        display_name=name,
        node_class=node_class,
        data_type=data_type,
        user_access=("CurrentRead",) if readable else (),
        readable=readable,
        writable=False,
    )


def test_unique_exact_compatible_match_auto_binds() -> None:
    result = reconcile_engineering_tags(
        "plc-a",
        [_tag("T1", "RunCmd", data_type="BOOL")],
        [_node("ns=2;s=RunCmd", "RunCmd", data_type="Boolean")],
    )
    mapping = result.mappings[0]
    assert mapping.status is LiveTagMappingStatus.AUTO_BOUND
    assert mapping.selected_node_id == "ns=2;s=RunCmd"
    assert mapping.candidates[0].type_compatibility is LiveTypeCompatibility.COMPATIBLE
    assert mapping.accepted is True


def test_normalization_handles_case_and_separator_variation_without_fuzzy_matching() -> None:
    result = reconcile_engineering_tags(
        "plc-a",
        [_tag("T1", "Motor_Run_1", data_type="DINT")],
        [_node("n1", "motor-run1", data_type="Int32")],
    )
    assert result.mappings[0].status is LiveTagMappingStatus.AUTO_BOUND

    unmatched = reconcile_engineering_tags(
        "plc-a",
        [_tag("T2", "Motor_Run_01", data_type="DINT")],
        [_node("n1", "motor-run1", data_type="Int32")],
    )
    assert unmatched.mappings[0].status is LiveTagMappingStatus.UNMATCHED


def test_multiple_exact_candidates_are_ambiguous() -> None:
    result = reconcile_engineering_tags(
        "plc-a",
        [_tag("T1", "Start", data_type="BOOL")],
        [
            _node("n1", "Start", path="Objects.ProgramA.Start"),
            _node("n2", "Start", path="Objects.ProgramB.Start"),
        ],
    )
    mapping = result.mappings[0]
    assert mapping.status is LiveTagMappingStatus.AMBIGUOUS
    assert mapping.selected_node_id is None
    assert len(mapping.candidates) == 2


def test_exact_scope_qualification_resolves_duplicate_names() -> None:
    result = reconcile_engineering_tags(
        "plc-a",
        [_tag("T1", "Start", scope="Program:ProgramB", data_type="BOOL")],
        [
            _node("n1", "Start", path="Objects.ProgramA.Start"),
            _node("n2", "Start", path="Objects.ProgramB.Start"),
        ],
    )
    mapping = result.mappings[0]
    assert mapping.status is LiveTagMappingStatus.AUTO_BOUND
    assert mapping.selected_node_id == "n2"
    selected = next(item for item in mapping.candidates if item.node_id == "n2")
    assert selected.match_kind is LiveTagMatchKind.EXACT_QUALIFIED


def test_deterministic_type_conflict_blocks_binding() -> None:
    result = reconcile_engineering_tags(
        "plc-a",
        [_tag("T1", "Speed", data_type="DINT")],
        [_node("n1", "Speed", data_type="Double")],
    )
    assert result.mappings[0].status is LiveTagMappingStatus.TYPE_CONFLICT


def test_unknown_type_requires_manual_mapping() -> None:
    result = reconcile_engineering_tags(
        "plc-a",
        [_tag("T1", "Recipe", data_type="MyRecipeUDT")],
        [_node("n1", "Recipe", data_type="ExtensionObject")],
    )
    assert result.mappings[0].status is LiveTagMappingStatus.MANUAL_REQUIRED


def test_non_readable_exact_node_is_not_bound() -> None:
    result = reconcile_engineering_tags(
        "plc-a",
        [_tag("T1", "RunCmd")],
        [_node("n1", "RunCmd", readable=False)],
    )
    assert result.mappings[0].status is LiveTagMappingStatus.NON_READABLE


def test_engineering_external_access_block_is_respected() -> None:
    result = reconcile_engineering_tags(
        "plc-a",
        [_tag("T1", "RunCmd", external_access="None")],
        [_node("n1", "RunCmd")],
    )
    assert result.mappings[0].status is LiveTagMappingStatus.EXTERNAL_ACCESS_BLOCKED


def test_alias_requires_explicit_mapping_even_when_unique() -> None:
    result = reconcile_engineering_tags(
        "plc-a",
        [_tag("T1", "AliasRun", alias_for="RunCmd")],
        [_node("n1", "AliasRun")],
    )
    assert result.mappings[0].status is LiveTagMappingStatus.MANUAL_REQUIRED


def test_explicit_node_map_resolves_ambiguity_and_allows_unknown_type() -> None:
    result = reconcile_engineering_tags(
        "plc-a",
        [_tag("T1", "Recipe", scope="Program:X", data_type="MyRecipeUDT")],
        [
            _node("n1", "Recipe", path="Objects.X.Recipe", data_type="ExtensionObject"),
            _node("n2", "Recipe", path="Objects.Y.Recipe", data_type="ExtensionObject"),
        ],
        explicit_node_map={"T1": "n2"},
    )
    mapping = result.mappings[0]
    assert mapping.status is LiveTagMappingStatus.EXPLICIT_BOUND
    assert mapping.selected_node_id == "n2"
    explicit = next(item for item in mapping.candidates if item.node_id == "n2")
    assert explicit.type_compatibility is LiveTypeCompatibility.UNKNOWN


def test_explicit_mapping_cannot_override_type_conflict_or_missing_target() -> None:
    conflict = reconcile_engineering_tags(
        "plc-a",
        [_tag("T1", "Speed", data_type="DINT")],
        [_node("n1", "Speed", data_type="Double")],
        explicit_node_map={"T1": "n1"},
    )
    assert conflict.mappings[0].status is LiveTagMappingStatus.TYPE_CONFLICT

    missing = reconcile_engineering_tags(
        "plc-a",
        [_tag("T1", "Speed", data_type="DINT")],
        [_node("n1", "Speed", data_type="Int32")],
        explicit_node_map={"T1": "missing"},
    )
    assert missing.mappings[0].status is LiveTagMappingStatus.UNMATCHED


def test_conflicting_explicit_id_and_name_mapping_fails_closed() -> None:
    with pytest.raises(LiveConfigurationError, match="Conflicting explicit"):
        reconcile_engineering_tags(
            "plc-a",
            [_tag("T1", "RunCmd")],
            [_node("n1", "RunCmd"), _node("n2", "Other")],
            explicit_node_map={"T1": "n1", "RunCmd": "n2"},
        )


def test_duplicate_engineering_tag_id_is_rejected() -> None:
    with pytest.raises(LiveConfigurationError, match="Duplicate engineering tag id"):
        reconcile_engineering_tags(
            "plc-a",
            [_tag("T1", "A"), _tag("T1", "B")],
            [_node("n1", "A"), _node("n2", "B")],
        )


def test_two_engineering_tags_cannot_silently_bind_same_live_node() -> None:
    result = reconcile_engineering_tags(
        "plc-a",
        [_tag("T1", "RunCmd"), _tag("T2", "Run_Cmd")],
        [_node("n1", "RunCmd")],
    )
    assert all(
        mapping.status is LiveTagMappingStatus.NODE_COLLISION
        for mapping in result.mappings
    )
    assert all(mapping.selected_node_id is None for mapping in result.mappings)


def test_request_map_fails_closed_for_unresolved_required_tags() -> None:
    result = reconcile_engineering_tags(
        "plc-a",
        [_tag("T1", "Good", data_type="BOOL"), _tag("T2", "Missing", data_type="BOOL")],
        [_node("n1", "Good")],
    )
    with pytest.raises(LiveConfigurationError, match="T2=UNMATCHED"):
        result.node_request_map(required_tag_ids=["T1", "T2"])
    assert result.node_request_map(
        required_tag_ids=["T1", "T2"],
        require_all=False,
    ) == {"plc-a": ("n1",)}


def test_mapping_evidence_distinguishes_accepted_and_limitations() -> None:
    result = reconcile_engineering_tags(
        "plc-a",
        [_tag("T1", "Good"), _tag("T2", "Missing")],
        [_node("n1", "Good")],
    )
    items = result.evidence_items()
    by_tag = {item.payload["tag_id"]: item for item in items}
    assert by_tag["T1"].kind == "LIVE_TAG_MAPPING"
    assert by_tag["T1"].payload["accepted"] is True
    assert by_tag["T2"].kind == "LIVE_TAG_MAPPING_LIMITATION"
    assert by_tag["T2"].payload["accepted"] is False
    assert "value" not in repr(by_tag["T1"].payload)


def test_mapping_evidence_id_is_deterministic_for_same_inputs() -> None:
    tags = [_tag("T1", "RunCmd")]
    nodes = [_node("n1", "RunCmd")]
    first = reconcile_engineering_tags("plc-a", tags, nodes)
    second = reconcile_engineering_tags("plc-a", tags, nodes)
    assert first.mappings[0].evidence_id == second.mappings[0].evidence_id


def test_reconcile_connected_plc_tags_uses_managed_browse() -> None:
    class Manager:
        def __init__(self) -> None:
            self.call = None

        async def browse(self, plc_id, *, max_depth, max_nodes):
            self.call = (plc_id, max_depth, max_nodes)
            return [_node("n1", "RunCmd")]

    async def scenario() -> None:
        manager = Manager()
        result = await reconcile_connected_plc_tags(
            manager,
            "plc-a",
            [_tag("T1", "RunCmd")],
            max_depth=7,
            max_nodes=321,
        )
        assert manager.call == ("plc-a", 7, 321)
        assert result.mappings[0].status is LiveTagMappingStatus.AUTO_BOUND

    asyncio.run(scenario())


def test_multi_plc_manager_browse_uses_owned_session_and_clears_degraded_state() -> None:
    class Client:
        def __init__(self, endpoint, **kwargs) -> None:
            self.connected = False
            self.connection_state = "DISCONNECTED"
            self.browse_call = None

        async def connect(self) -> None:
            self.connected = True
            self.connection_state = "CONNECTED"

        async def disconnect(self) -> None:
            self.connected = False
            self.connection_state = "DISCONNECTED"

        async def browse(self, *, max_depth, max_nodes):
            self.browse_call = (max_depth, max_nodes)
            return [_node("n1", "RunCmd")]

    async def scenario() -> None:
        clients = []

        def factory(endpoint, **kwargs):
            client = Client(endpoint, **kwargs)
            clients.append(client)
            return client

        manager = MultiPlcConnectionManager(
            [PlcConnectionSpec("plc-a", "opc.tcp://plc-a:4840/")],
            client_factory=factory,
        )
        await manager.connect_all()
        nodes = await manager.browse("plc-a", max_depth=6, max_nodes=123)
        assert clients[0].browse_call == (6, 123)
        assert nodes[0].node_id == "n1"
        assert manager.status("plc-a").state is PlcSessionState.CONNECTED
        await manager.disconnect_all()

    asyncio.run(scenario())


def test_multi_plc_manager_browse_failure_is_redacted_and_isolated() -> None:
    class Client:
        def __init__(self, endpoint, **kwargs) -> None:
            self.connected = False
            self.connection_state = "DISCONNECTED"

        async def connect(self) -> None:
            self.connected = True
            self.connection_state = "CONNECTED"

        async def disconnect(self) -> None:
            self.connected = False
            self.connection_state = "DISCONNECTED"

        async def browse(self, *, max_depth, max_nodes):
            raise RuntimeError("browse rejected plc-secret")

    async def scenario() -> None:
        security = LiveSecurityConfig(
            username="operator",
            password="plc-secret",
            security_policy="Basic256Sha256",
            security_mode="SignAndEncrypt",
            client_certificate="client.der",
            client_private_key="client.pem",
            server_certificate="server.der",
        )
        manager = MultiPlcConnectionManager(
            [
                PlcConnectionSpec(
                    "plc-a",
                    "opc.tcp://plc-a:4840/",
                    security=security,
                )
            ],
            client_factory=lambda endpoint, **kwargs: Client(endpoint, **kwargs),
        )
        await manager.connect_all()
        with pytest.raises(LiveConnectionError) as info:
            await manager.browse("plc-a")
        assert "plc-secret" not in str(info.value)
        assert "<redacted>" in str(info.value)
        assert manager.status("plc-a").state is PlcSessionState.DEGRADED
        await manager.disconnect_all()

    asyncio.run(scenario())


def test_tag_reconciliation_surface_has_no_control_operations() -> None:
    result = reconcile_engineering_tags(
        "plc-a",
        [_tag("T1", "RunCmd")],
        [_node("n1", "RunCmd")],
    )
    for prohibited in (
        "write",
        "write_value",
        "set_value",
        "call_method",
        "force",
        "reset",
        "download",
    ):
        assert not hasattr(result, prohibited)
