from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from devagent.live.manager import ManagedPlcStatus, PlcConnectionSpec, PlcReadResult, PlcSessionState
from devagent.live.models import BrowseNode, Quality, RuntimeValue
from devagent.live.security import LiveSecurityConfig
from devagent.live.workflow import (
    LiveCommissioningPlcSpec,
    LiveCommissioningState,
    LiveCommissioningWorkflow,
)

NOW = datetime(2026, 8, 29, 3, 30, tzinfo=timezone.utc)


def _tag(tag_id: str, name: str, data_type: str = "BOOL"):
    return SimpleNamespace(
        id=tag_id,
        name=name,
        scope="controller",
        data_type=data_type,
        alias_for=None,
        external_access=None,
    )


def _project(*tags):
    return SimpleNamespace(tags=list(tags))


def _node(node_id: str, name: str, *, data_type: str = "Boolean", path: str | None = None):
    return BrowseNode(
        path=path or f"Objects.{name}",
        node_id=node_id,
        browse_name=name,
        display_name=name,
        node_class="Variable",
        data_type=data_type,
        user_access=("CurrentRead",),
        readable=True,
        writable=False,
    )


def _value(node_id: str, value=True, *, stale: bool = False):
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
    def __init__(
        self,
        plc_ids,
        *,
        nodes_by_plc=None,
        values_by_plc=None,
        failed_connect=None,
        browse_errors=None,
        read_errors=None,
    ) -> None:
        self.plc_ids = tuple(plc_ids)
        self.nodes_by_plc = dict(nodes_by_plc or {})
        self.values_by_plc = dict(values_by_plc or {})
        self.failed_connect = dict(failed_connect or {})
        self.browse_errors = dict(browse_errors or {})
        self.read_errors = dict(read_errors or {})
        self.connected = {plc_id: False for plc_id in self.plc_ids}
        self.read_requests = {}
        self.browse_calls = {}
        self.disconnect_calls = 0

    def _status(self, plc_id):
        failed = self.failed_connect.get(plc_id)
        if failed:
            state = PlcSessionState.FAILED
            connected = False
            error = failed
        else:
            state = PlcSessionState.CONNECTED if self.connected[plc_id] else PlcSessionState.DISCONNECTED
            connected = self.connected[plc_id]
            error = None
        return ManagedPlcStatus(
            plc_id=plc_id,
            plc_name=plc_id.upper(),
            endpoint=f"opc.tcp://{plc_id}:4840/",
            state=state,
            connected=connected,
            authentication_mode="ANONYMOUS",
            security_summary="NONE",
            successful_connections=1 if connected else 0,
            last_error=error,
            changed_at=NOW,
        )

    def status(self, plc_id):
        return self._status(plc_id)

    def statuses(self):
        return {plc_id: self._status(plc_id) for plc_id in self.plc_ids}

    async def connect_all(self):
        for plc_id in self.plc_ids:
            if plc_id not in self.failed_connect:
                self.connected[plc_id] = True
        return self.statuses()

    async def browse(self, plc_id, *, max_depth, max_nodes):
        self.browse_calls[plc_id] = (max_depth, max_nodes)
        if plc_id in self.browse_errors:
            raise RuntimeError(self.browse_errors[plc_id])
        return list(self.nodes_by_plc.get(plc_id, []))

    async def read_many(self, requests):
        results = {}
        for plc_id, node_ids in requests.items():
            self.read_requests[plc_id] = tuple(node_ids)
            if plc_id in self.read_errors:
                results[plc_id] = PlcReadResult(
                    plc_id=plc_id,
                    values=(),
                    state=PlcSessionState.DEGRADED,
                    error=self.read_errors[plc_id],
                )
                continue
            values = tuple(
                self.values_by_plc[plc_id][node_id]
                for node_id in node_ids
                if node_id in self.values_by_plc.get(plc_id, {})
            )
            results[plc_id] = PlcReadResult(
                plc_id=plc_id,
                values=values,
                state=PlcSessionState.CONNECTED,
            )
        return results

    async def disconnect_all(self):
        self.disconnect_calls += 1
        for plc_id in self.plc_ids:
            self.connected[plc_id] = False
        return self.statuses()


def _spec(plc_id: str, project, required, **kwargs):
    return LiveCommissioningPlcSpec(
        connection=PlcConnectionSpec(plc_id, f"opc.tcp://{plc_id}:4840/"),
        engineering_project=project,
        required_tag_ids=tuple(required),
        **kwargs,
    )


def test_complete_when_all_required_tags_map_and_are_current() -> None:
    async def scenario() -> None:
        manager = _Manager(
            ["a"],
            nodes_by_plc={"a": [_node("n1", "RunCmd")]},
            values_by_plc={"a": {"n1": _value("n1")}},
        )
        workflow = LiveCommissioningWorkflow(
            [_spec("a", _project(_tag("T1", "RunCmd")), ["T1"])],
            manager=manager,
        )
        result = await workflow.run()
        plc = result.plc_results["a"]
        assert plc.state is LiveCommissioningState.COMPLETE
        assert plc.definitive_current is True
        assert result.all_complete is True
        assert result.any_limited_or_failed is False
        assert manager.read_requests == {"a": ("n1",)}
        assert manager.disconnect_calls == 1
        assert set(result.live_packs()) == {"a"}

    asyncio.run(scenario())


def test_stale_required_value_yields_limited_not_complete() -> None:
    async def scenario() -> None:
        manager = _Manager(
            ["a"],
            nodes_by_plc={"a": [_node("n1", "RunCmd")]},
            values_by_plc={"a": {"n1": _value("n1", stale=True)}},
        )
        result = await LiveCommissioningWorkflow(
            [_spec("a", _project(_tag("T1", "RunCmd")), ["T1"])],
            manager=manager,
        ).run()
        plc = result.plc_results["a"]
        assert plc.state is LiveCommissioningState.LIMITED
        assert plc.definitive_current is False
        assert len(plc.evidence.live_pack.excluded_raw_evidence_ids) == 1

    asyncio.run(scenario())


def test_ambiguous_strict_mapping_blocks_before_read() -> None:
    async def scenario() -> None:
        manager = _Manager(
            ["a"],
            nodes_by_plc={
                "a": [
                    _node("n1", "Start", path="Objects.P1.Start"),
                    _node("n2", "Start", path="Objects.P2.Start"),
                ]
            },
        )
        result = await LiveCommissioningWorkflow(
            [_spec("a", _project(_tag("T1", "Start")), ["T1"])],
            manager=manager,
        ).run()
        plc = result.plc_results["a"]
        assert plc.state is LiveCommissioningState.MAPPING_BLOCKED
        assert "AMBIGUOUS" in (plc.error or "")
        assert manager.read_requests == {}

    asyncio.run(scenario())


def test_partial_mapping_reads_only_safe_tags_and_marks_limited() -> None:
    async def scenario() -> None:
        manager = _Manager(
            ["a"],
            nodes_by_plc={"a": [_node("n1", "RunCmd")]},
            values_by_plc={"a": {"n1": _value("n1")}},
        )
        result = await LiveCommissioningWorkflow(
            [
                _spec(
                    "a",
                    _project(_tag("T1", "RunCmd"), _tag("T2", "Missing")),
                    ["T1", "T2"],
                    require_all_mappings=False,
                )
            ],
            manager=manager,
        ).run()
        plc = result.plc_results["a"]
        assert plc.state is LiveCommissioningState.LIMITED
        assert manager.read_requests["a"] == ("n1",)
        assert any(
            item.kind == "LIVE_TAG_MAPPING_LIMITATION"
            for item in plc.evidence.mapping_evidence
        )

    asyncio.run(scenario())


def test_all_unresolved_partial_mode_becomes_capture_failed_not_false_complete() -> None:
    async def scenario() -> None:
        manager = _Manager(["a"], nodes_by_plc={"a": []})
        result = await LiveCommissioningWorkflow(
            [
                _spec(
                    "a",
                    _project(_tag("T1", "Missing")),
                    ["T1"],
                    require_all_mappings=False,
                )
            ],
            manager=manager,
        ).run()
        assert result.plc_results["a"].state is LiveCommissioningState.CAPTURE_FAILED
        assert "No safely reconciled live nodes" in (result.plc_results["a"].error or "")

    asyncio.run(scenario())


def test_three_plcs_isolate_one_connection_failure() -> None:
    async def scenario() -> None:
        manager = _Manager(
            ["a", "b", "c"],
            nodes_by_plc={
                "a": [_node("a1", "RunCmd")],
                "c": [_node("c1", "RunCmd")],
            },
            values_by_plc={
                "a": {"a1": _value("a1")},
                "c": {"c1": _value("c1")},
            },
            failed_connect={"b": "connection refused"},
        )
        specs = [
            _spec("a", _project(_tag("TA", "RunCmd")), ["TA"]),
            _spec("b", _project(_tag("TB", "RunCmd")), ["TB"]),
            _spec("c", _project(_tag("TC", "RunCmd")), ["TC"]),
        ]
        result = await LiveCommissioningWorkflow(specs, manager=manager).run()
        assert result.plc_results["a"].state is LiveCommissioningState.COMPLETE
        assert result.plc_results["b"].state is LiveCommissioningState.CONNECT_FAILED
        assert result.plc_results["c"].state is LiveCommissioningState.COMPLETE
        assert "b" not in manager.read_requests

    asyncio.run(scenario())


def test_browse_failure_is_isolated_and_secret_redacted() -> None:
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
        manager = _Manager(
            ["a", "b"],
            nodes_by_plc={"b": [_node("b1", "RunCmd")]},
            values_by_plc={"b": {"b1": _value("b1")}},
            browse_errors={"a": "browse failed plc-secret"},
        )
        spec_a = LiveCommissioningPlcSpec(
            connection=PlcConnectionSpec("a", "opc.tcp://a:4840/", security=security),
            engineering_project=_project(_tag("TA", "RunCmd")),
            required_tag_ids=("TA",),
        )
        spec_b = _spec("b", _project(_tag("TB", "RunCmd")), ["TB"])
        result = await LiveCommissioningWorkflow([spec_a, spec_b], manager=manager).run()
        a = result.plc_results["a"]
        assert a.state is LiveCommissioningState.MAPPING_BLOCKED
        assert "plc-secret" not in (a.error or "")
        assert "<redacted>" in (a.error or "")
        assert result.plc_results["b"].state is LiveCommissioningState.COMPLETE

    asyncio.run(scenario())


def test_unknown_required_engineering_tag_is_mapping_blocked() -> None:
    async def scenario() -> None:
        manager = _Manager(["a"], nodes_by_plc={"a": [_node("n1", "RunCmd")]})
        result = await LiveCommissioningWorkflow(
            [_spec("a", _project(_tag("T1", "RunCmd")), ["UNKNOWN"])],
            manager=manager,
        ).run()
        assert result.plc_results["a"].state is LiveCommissioningState.MAPPING_BLOCKED
        assert "Unknown engineering tag id" in (result.plc_results["a"].error or "")
        assert manager.read_requests == {}

    asyncio.run(scenario())


def test_spec_deduplicates_required_tags_and_bounds_count() -> None:
    spec = _spec("a", _project(_tag("T1", "RunCmd")), ["T1", "T1"])
    assert spec.required_tag_ids == ("T1",)
    with pytest.raises(ValueError, match="at most 200"):
        _spec(
            "a",
            _project(*[_tag(f"T{i}", f"Tag{i}") for i in range(201)]),
            [f"T{i}" for i in range(201)],
        )


def test_spec_rejects_blank_required_tags_and_bad_browse_bounds() -> None:
    with pytest.raises(ValueError, match="blank"):
        _spec("a", _project(_tag("T1", "RunCmd")), [" "])
    with pytest.raises(ValueError, match="browse_max_depth"):
        _spec("a", _project(_tag("T1", "RunCmd")), ["T1"], browse_max_depth=-1)
    with pytest.raises(ValueError, match="browse_max_nodes"):
        _spec("a", _project(_tag("T1", "RunCmd")), ["T1"], browse_max_nodes=0)


def test_manager_ids_must_exactly_match_workflow_specs() -> None:
    manager = _Manager(["other"])
    with pytest.raises(ValueError, match="exactly match"):
        LiveCommissioningWorkflow(
            [_spec("a", _project(_tag("T1", "RunCmd")), ["T1"])],
            manager=manager,
        )


def test_disconnect_can_be_intentionally_kept_open() -> None:
    async def scenario() -> None:
        manager = _Manager(
            ["a"],
            nodes_by_plc={"a": [_node("n1", "RunCmd")]},
            values_by_plc={"a": {"n1": _value("n1")}},
        )
        result = await LiveCommissioningWorkflow(
            [_spec("a", _project(_tag("T1", "RunCmd")), ["T1"])],
            manager=manager,
            disconnect_when_done=False,
        ).run()
        assert result.plc_results["a"].state is LiveCommissioningState.COMPLETE
        assert result.disconnect_statuses == {}
        assert manager.disconnect_calls == 0
        assert manager.connected["a"] is True

    asyncio.run(scenario())


def test_workflow_surface_remains_read_only() -> None:
    manager = _Manager(["a"])
    workflow = LiveCommissioningWorkflow(
        [_spec("a", _project(_tag("T1", "RunCmd")), ["T1"])],
        manager=manager,
        disconnect_when_done=False,
    )
    for prohibited in (
        "write",
        "write_value",
        "set_value",
        "call_method",
        "force",
        "reset",
        "download",
        "change_mode",
    ):
        assert not hasattr(workflow, prohibited)
