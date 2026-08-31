from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace

import pytest

from devagent.live.assistant import LiveCommissioningAssistant
from devagent.live.commercial_assistant import (
    CommercialRealtimeSemanticLiveCommissioningAssistant,
)
from devagent.live.manager import PlcConnectionSpec
from devagent.live.production_realtime import (
    ProductionRealtimeMultiPlcConnectionManager,
)
from devagent.live.realtime_assistant import RealtimeSemanticLiveCommissioningAssistant


class _FakeSubscription:
    def __init__(self, *, setup_gate: asyncio.Event | None = None) -> None:
        self.setup_gate = setup_gate
        self.subscription_id = 1
        self.last_sequence_number = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def subscribe_data_change(self, nodes, **kwargs):
        if self.setup_gate is not None:
            await self.setup_gate.wait()
        return [index + 1 for index, _node in enumerate(nodes)]

    async def next_event(self, timeout: float | None = None):
        await asyncio.sleep(0)
        raise asyncio.TimeoutError


class _FakeAsyncuaClient:
    def __init__(self, subscription: _FakeSubscription) -> None:
        self.subscription = subscription

    def get_node(self, node_id: str):
        return SimpleNamespace(nodeid=node_id)

    async def create_subscription(self, *args, **kwargs):
        return self.subscription


class _FakeOuterClient:
    def __init__(self, subscription: _FakeSubscription) -> None:
        self.connected = True
        self.auto_reconnect = True
        self.connection_state = "CONNECTED"
        self.stale_after_seconds = 1.0
        self._client = _FakeAsyncuaClient(subscription)

    def _require_connected(self):
        return self._client

    async def wait_until_connected(self, timeout_seconds: float = 5.0):
        return None


def _production_manager() -> ProductionRealtimeMultiPlcConnectionManager:
    return ProductionRealtimeMultiPlcConnectionManager(
        [PlcConnectionSpec("plc1", "opc.tcp://plc1:4840/")],
        subscription_enabled=True,
        max_monitored_nodes=8,
        iterator_queue_maxsize=1000,
    )


def _install_fake_transport(
    manager: ProductionRealtimeMultiPlcConnectionManager,
    subscription: _FakeSubscription,
) -> None:
    outer = _FakeOuterClient(subscription)

    def fake_entry(self, plc_id: str):
        assert plc_id == "plc1"
        return SimpleNamespace(client=outer)

    manager._entry = MethodType(fake_entry, manager)


def test_idle_asyncua_timeout_is_health_check_not_evidence_gap() -> None:
    pytest.importorskip("asyncua")

    async def scenario() -> None:
        manager = _production_manager()
        _install_fake_transport(manager, _FakeSubscription())
        realtime = manager._state("plc1")
        integrity = manager._integrity_state("plc1")
        realtime.monitored_nodes = {"A"}
        realtime.generation = 1
        integrity.desired_nodes = ("A",)

        task = asyncio.create_task(manager._subscription_loop("plc1", 1))
        await asyncio.sleep(0.03)
        assert task.done() is False
        status = manager.integrity_status("plc1")
        assert status.active_monitored_nodes == 1
        assert status.evidence_gap_count == 0
        assert manager.open_evidence_gaps("plc1") == ()

        realtime.generation += 1
        await task

    asyncio.run(scenario())


def test_startup_exact_replace_stays_incomplete_until_monitoring_is_armed() -> None:
    pytest.importorskip("asyncua")

    async def scenario() -> None:
        manager = _production_manager()
        setup_gate = asyncio.Event()
        _install_fake_transport(manager, _FakeSubscription(setup_gate=setup_gate))

        await manager.replace_monitored_node_ids("plc1", ("A",))
        assert any(
            gap.source == "MONITOR_SET_RECONFIGURATION"
            for gap in manager.open_evidence_gaps("plc1")
        )

        ready_task = asyncio.create_task(
            manager.wait_for_active_monitoring("plc1", timeout_seconds=0.5)
        )
        await asyncio.sleep(0.03)
        assert ready_task.done() is False
        assert manager.integrity_status("plc1").evidence_complete_since_session_start is False

        setup_gate.set()
        assert await ready_task is True
        status = manager.integrity_status("plc1")
        assert status.active_monitored_nodes == 1
        assert not any(
            gap.source == "MONITOR_SET_RECONFIGURATION"
            for gap in manager.open_evidence_gaps("plc1")
        )
        assert any(
            gap.source == "MONITOR_SET_RECONFIGURATION"
            for gap in manager.drain_evidence_gaps("plc1")
        )

        realtime = manager._state("plc1")
        realtime.generation += 1
        if realtime.task is not None:
            await realtime.task

    asyncio.run(scenario())


def test_refresh_stops_old_history_before_authoritative_mapping_replace(monkeypatch) -> None:
    async def scenario() -> None:
        events: list[str] = []

        class OldCollector:
            async def stop(self):
                events.append("old-history-stop")

        class NewCollector:
            def sync_integrity_gaps(self):
                events.append("new-history-sync")

        class Reconciliation:
            @staticmethod
            def accepted_mappings():
                return (SimpleNamespace(selected_node_id="NEW"),)

        class Manager:
            async def replace_monitored_node_ids(self, plc_id, node_ids):
                events.append(f"replace:{plc_id}:{','.join(node_ids)}")

            async def wait_for_active_monitoring(self, plc_id, timeout_seconds=5.0):
                events.append(f"wait:{plc_id}")
                return True

        async def fake_base_refresh(self):
            events.append("base-refresh")
            self.reconciliation = Reconciliation()
            return self.reconciliation

        monkeypatch.setattr(
            LiveCommissioningAssistant,
            "refresh_mapping",
            fake_base_refresh,
        )

        assistant = object.__new__(CommercialRealtimeSemanticLiveCommissioningAssistant)
        assistant.connection = SimpleNamespace(plc_id="plc1")
        assistant.manager = Manager()
        assistant.history_collector = OldCollector()
        assistant.reconciliation = SimpleNamespace()

        async def fake_start_history(self):
            events.append("new-history-start")
            self.history_collector = NewCollector()

        assistant._start_history = MethodType(fake_start_history, assistant)
        await assistant.refresh_mapping()

        assert events == [
            "old-history-stop",
            "base-refresh",
            "replace:plc1:NEW",
            "wait:plc1",
            "new-history-start",
            "new-history-sync",
        ]

    asyncio.run(scenario())


def test_ai_historical_route_reconnects_before_integrity_sync(monkeypatch) -> None:
    async def scenario() -> None:
        events: list[str] = []

        class Collector:
            def sync_integrity_gaps(self):
                events.append("sync")

        class TestAssistant(CommercialRealtimeSemanticLiveCommissioningAssistant):
            def __init__(self):
                self._connected_for_test = False
                self.reconciliation = None
                self.history_collector = None

            @property
            def connected(self):
                return self._connected_for_test

            async def start(self):
                events.append("start")
                self._connected_for_test = True
                self.reconciliation = object()
                self.history_collector = Collector()
                return SimpleNamespace(connected=True)

        async def fake_super_dispatch(self, original, route):
            events.append("dispatch")
            return SimpleNamespace(text="ok")

        monkeypatch.setattr(
            RealtimeSemanticLiveCommissioningAssistant,
            "_dispatch_historical_route",
            fake_super_dispatch,
            raising=False,
        )

        assistant = TestAssistant()
        await assistant._dispatch_historical_route("why earlier", object())
        assert events == ["start", "sync", "dispatch"]

    asyncio.run(scenario())
