from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from devagent.live.history import LiveHistoryCollector
from devagent.live.manager import PlcConnectionSpec, PlcReadResult, PlcSessionState
from devagent.live.models import Quality, RuntimeValue
from devagent.live.realtime_manager import RealtimeMultiPlcConnectionManager


def _runtime(
    node_id: str,
    value: object,
    *,
    stamp: datetime | None = None,
    received_at: datetime | None = None,
    replayed: bool = False,
    stale: bool = False,
) -> RuntimeValue:
    timestamp = stamp or datetime.now(timezone.utc)
    received = received_at or timestamp
    return RuntimeValue(
        node_id=node_id,
        value=value,
        variant_type="Boolean" if isinstance(value, bool) else "Int32",
        status_code="Good",
        quality=Quality.GOOD,
        source_timestamp=timestamp,
        server_timestamp=timestamp,
        received_at=received,
        age_seconds=max(0.0, (received - timestamp).total_seconds()),
        stale=stale,
        replayed=replayed,
    )


class _Status:
    name = "Good"

    @staticmethod
    def is_good() -> bool:
        return True

    @staticmethod
    def is_bad() -> bool:
        return False


class _VariantType:
    name = "Boolean"


class _DataValue:
    def __init__(self, value: object, stamp: datetime) -> None:
        self.StatusCode = _Status()
        self.Value = SimpleNamespace(Value=value, VariantType=_VariantType())
        self.SourceTimestamp = stamp
        self.ServerTimestamp = stamp


class _FakeUaClient:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], object]] = []
        self.values: dict[str, object] = {"n1": True, "n2": False}

    async def read_attributes(self, node_ids, attribute):
        ids = tuple(str(item) for item in node_ids)
        self.calls.append((ids, attribute))
        stamp = datetime.now(timezone.utc)
        return [_DataValue(self.values[node_id], stamp) for node_id in ids]


class _FakeInnerClient:
    def __init__(self) -> None:
        self.uaclient = _FakeUaClient()

    @staticmethod
    def get_node(node_id: str):
        return SimpleNamespace(nodeid=node_id)


class _FakeOuterClient:
    def __init__(self, endpoint: str, **kwargs) -> None:
        self.endpoint = endpoint
        self.state = "DISCONNECTED"
        self.stale_after_seconds = float(kwargs.get("stale_after_seconds", 5.0))
        self.auto_reconnect = True
        self.inner = _FakeInnerClient()

    @property
    def connected(self) -> bool:
        return self.state == "CONNECTED"

    @property
    def connection_state(self) -> str:
        return self.state

    @property
    def authentication_mode(self) -> str:
        return "ANONYMOUS"

    @property
    def security_summary(self) -> str:
        return "None/None"

    async def connect(self) -> None:
        self.state = "CONNECTED"

    async def disconnect(self) -> None:
        self.state = "DISCONNECTED"

    def _require_connected(self):
        if not self.connected:
            raise RuntimeError("not connected")
        return self.inner


def _manager(**kwargs) -> RealtimeMultiPlcConnectionManager:
    return RealtimeMultiPlcConnectionManager(
        [PlcConnectionSpec("plc1", "opc.tcp://plc1:4840/")],
        client_factory=lambda endpoint, **client_kwargs: _FakeOuterClient(
            endpoint, **client_kwargs
        ),
        subscription_enabled=False,
        **kwargs,
    )


def test_current_question_uses_one_multi_node_read_then_fresh_cache() -> None:
    async def scenario() -> None:
        manager = _manager(cache_fresh_seconds=1.0)
        await manager.connect("plc1")
        first = await manager.read_many({"plc1": ("n1", "n2")})
        outer = manager._entry("plc1").client
        assert outer is not None
        assert len(outer.inner.uaclient.calls) == 1
        assert outer.inner.uaclient.calls[0][0] == ("n1", "n2")
        assert [item.value for item in first["plc1"].values] == [True, False]
        assert manager.realtime_status("plc1").source == "OPCUA_BATCH_READ"

        second = await manager.read_many({"plc1": ("n1", "n2")})
        assert len(outer.inner.uaclient.calls) == 1
        assert [item.value for item in second["plc1"].values] == [True, False]
        assert manager.realtime_status("plc1").source == "REALTIME_CACHE"
        await manager.disconnect("plc1")

    asyncio.run(scenario())


def test_cache_residency_uses_recent_receipt_not_old_source_change_time() -> None:
    async def scenario() -> None:
        manager = _manager(cache_fresh_seconds=0.5)
        await manager.connect("plc1")
        now = datetime.now(timezone.utc)
        state = manager._state("plc1")
        state.cache["n1"] = _runtime(
            "n1",
            True,
            stamp=now - timedelta(seconds=30),
            received_at=now,
            stale=False,
        )
        cached = manager._cached_snapshot("plc1", ("n1",))
        assert cached is not None
        assert cached[0].value is True
        assert manager.realtime_status("plc1").source == "REALTIME_CACHE"
        await manager.disconnect("plc1")

    asyncio.run(scenario())


def test_source_stale_value_is_rejected_even_when_received_recently() -> None:
    async def scenario() -> None:
        manager = _manager(cache_fresh_seconds=2.0)
        await manager.connect("plc1")
        now = datetime.now(timezone.utc)
        state = manager._state("plc1")
        state.cache["n1"] = _runtime(
            "n1",
            True,
            stamp=now - timedelta(seconds=30),
            received_at=now,
            stale=True,
        )
        assert manager._cached_snapshot("plc1", ("n1",)) is None
        await manager.disconnect("plc1")

    asyncio.run(scenario())


def test_old_or_incoherent_receipt_cache_forces_new_batch_read() -> None:
    async def scenario() -> None:
        manager = _manager(cache_fresh_seconds=2.0, max_snapshot_skew_seconds=0.05)
        await manager.connect("plc1")
        await manager.read_many({"plc1": ("n1", "n2")})
        outer = manager._entry("plc1").client
        assert outer is not None
        state = manager._state("plc1")
        now = datetime.now(timezone.utc)
        state.cache["n1"] = _runtime("n1", True, stamp=now, received_at=now)
        state.cache["n2"] = _runtime(
            "n2",
            False,
            stamp=now,
            received_at=now - timedelta(seconds=1),
        )

        await manager.read_many({"plc1": ("n1", "n2")})
        assert len(outer.inner.uaclient.calls) == 2
        assert manager.realtime_status("plc1").source == "OPCUA_BATCH_READ"
        await manager.disconnect("plc1")

    asyncio.run(scenario())


def test_cache_ordering_rejects_delayed_older_subscription_after_batch() -> None:
    manager = _manager()
    state = manager._state("plc1")
    now = datetime.now(timezone.utc)
    batch = _runtime(
        "n1",
        True,
        stamp=now + timedelta(milliseconds=100),
        received_at=now + timedelta(milliseconds=120),
    )
    delayed_old_subscription = _runtime(
        "n1",
        False,
        stamp=now,
        received_at=now + timedelta(milliseconds=200),
    )

    assert manager._update_cache(state, batch, source="BATCH_READ") is True
    assert (
        manager._update_cache(
            state,
            delayed_old_subscription,
            source="SUBSCRIPTION",
        )
        is False
    )
    assert state.cache["n1"].value is True
    assert state.cache_sources["n1"] == "BATCH_READ"


def test_batch_wins_exact_timestamp_tie_against_delayed_subscription() -> None:
    manager = _manager()
    state = manager._state("plc1")
    stamp = datetime.now(timezone.utc)
    subscription = _runtime("n1", False, stamp=stamp, received_at=stamp)
    batch = _runtime(
        "n1",
        True,
        stamp=stamp,
        received_at=stamp + timedelta(milliseconds=10),
    )
    delayed_subscription = _runtime(
        "n1",
        False,
        stamp=stamp,
        received_at=stamp + timedelta(milliseconds=20),
    )

    manager._update_cache(state, subscription, source="SUBSCRIPTION")
    manager._update_cache(state, batch, source="BATCH_READ")
    manager._update_cache(state, delayed_subscription, source="SUBSCRIPTION")

    assert state.cache["n1"].value is True
    assert state.cache_sources["n1"] == "BATCH_READ"


def test_replayed_cache_is_never_current_question_truth() -> None:
    async def scenario() -> None:
        manager = _manager(cache_fresh_seconds=2.0)
        await manager.connect("plc1")
        state = manager._state("plc1")
        state.cache["n1"] = _runtime("n1", False, replayed=True)
        outer = manager._entry("plc1").client
        assert outer is not None

        result = await manager.read_many({"plc1": ("n1",)})
        assert len(outer.inner.uaclient.calls) == 1
        assert result["plc1"].values[0].replayed is False
        await manager.disconnect("plc1")

    asyncio.run(scenario())


def test_read_observed_reconnect_invalidates_cache_and_advances_epoch() -> None:
    async def scenario() -> None:
        manager = _manager(cache_fresh_seconds=2.0)
        await manager.connect("plc1")
        state = manager._state("plc1")
        state.cache["n1"] = _runtime("n1", True)
        state.cache_sources["n1"] = "SUBSCRIPTION"
        state.events.append(_runtime("n1", True))
        outer = manager._entry("plc1").client
        assert outer is not None
        outer.state = "RECONNECTING"
        before_epoch = state.epoch

        assert manager._cached_snapshot("plc1", ("n1",)) is None
        result = await manager.read_many({"plc1": ("n1",)})

        assert result["plc1"].succeeded is False
        assert result["plc1"].state is PlcSessionState.RECONNECTING
        assert state.cache == {}
        assert state.cache_sources == {}
        assert not state.events
        assert state.epoch > before_epoch

    asyncio.run(scenario())


def test_disconnect_invalidates_cache_events_snapshot_and_epoch() -> None:
    async def scenario() -> None:
        manager = _manager()
        await manager.connect("plc1")
        state = manager._state("plc1")
        state.cache["n1"] = _runtime("n1", True)
        state.cache_sources["n1"] = "SUBSCRIPTION"
        state.events.append(_runtime("n1", True))
        state.last_snapshot = manager.realtime_status("plc1")
        before = state.epoch
        await manager.disconnect("plc1")
        assert state.cache == {}
        assert state.cache_sources == {}
        assert not state.events
        assert state.last_snapshot is None
        assert state.epoch > before

    asyncio.run(scenario())


def test_history_drain_discards_unrelated_backlog_and_keeps_requested_overflow() -> None:
    manager = _manager()
    state = manager._state("plc1")
    now = datetime.now(timezone.utc)
    for index in range(20):
        state.events.append(
            _runtime(
                "noise",
                index,
                stamp=now + timedelta(milliseconds=index),
            )
        )
    state.events.append(_runtime("n1", True, stamp=now + timedelta(milliseconds=30)))
    state.events.append(_runtime("n1", False, stamp=now + timedelta(milliseconds=40)))

    first = manager.drain_realtime_events("plc1", node_ids=("n1",), max_events=1)
    assert [item.value for item in first] == [True]
    assert [item.node_id for item in state.events] == ["n1"]

    second = manager.drain_realtime_events("plc1", node_ids=("n1",), max_events=10)
    assert [item.value for item in second] == [False]
    assert not state.events


def _history_reconciliation():
    mapping = SimpleNamespace(
        tag_id="tag-1",
        tag_name="RunCmd",
        selected_node_id="n1",
    )
    return SimpleNamespace(
        plc_id="plc1",
        accepted_mappings=lambda: (mapping,),
    )


def test_realtime_event_drain_preserves_out_of_order_transient_for_history() -> None:
    class _HistoryManager:
        def __init__(self) -> None:
            now = datetime.now(timezone.utc)
            self.events = [
                _runtime("n1", False, stamp=now + timedelta(milliseconds=100)),
                _runtime("n1", True, stamp=now),
            ]
            self.current = _runtime(
                "n1", False, stamp=now + timedelta(milliseconds=200)
            )

        @staticmethod
        def status(plc_id: str):
            return SimpleNamespace(plc_name="PLC 1")

        def drain_realtime_events(self, plc_id: str, *, node_ids, max_events: int):
            result, self.events = tuple(self.events), []
            return result

        async def read_many(self, requests):
            return {
                "plc1": PlcReadResult(
                    plc_id="plc1",
                    values=(self.current,),
                    state=PlcSessionState.CONNECTED,
                )
            }

    collector = LiveHistoryCollector(
        _HistoryManager(),
        _history_reconciliation(),
        poll_interval_seconds=1.0,
    )

    asyncio.run(collector._collect_once())

    transitions = collector.store.transitions()
    assert len(transitions) == 1
    assert transitions[0].old_value is True
    assert transitions[0].new_value is False
    latest = collector.store.latest_samples()[0]
    assert latest.value is False
    assert latest.timestamp >= transitions[0].timestamp


def test_history_merges_events_arriving_while_poll_read_is_in_flight() -> None:
    class _HistoryManager:
        def __init__(self) -> None:
            self.now = datetime.now(timezone.utc)
            self.events = [_runtime("n1", True, stamp=self.now)]

        @staticmethod
        def status(plc_id: str):
            return SimpleNamespace(plc_name="PLC 1")

        def drain_realtime_events(self, plc_id: str, *, node_ids, max_events: int):
            result, self.events = tuple(self.events), []
            return result

        async def read_many(self, requests):
            # Simulate a subscription DataChange delivered while the awaited Read
            # operation is in flight.
            self.events.append(
                _runtime(
                    "n1",
                    False,
                    stamp=self.now + timedelta(milliseconds=100),
                )
            )
            return {
                "plc1": PlcReadResult(
                    plc_id="plc1",
                    values=(
                        _runtime(
                            "n1",
                            False,
                            stamp=self.now + timedelta(milliseconds=200),
                        ),
                    ),
                    state=PlcSessionState.CONNECTED,
                )
            }

    collector = LiveHistoryCollector(
        _HistoryManager(),
        _history_reconciliation(),
        poll_interval_seconds=1.0,
    )

    asyncio.run(collector._collect_once())

    transitions = collector.store.transitions()
    assert len(transitions) == 1
    assert transitions[0].old_value is True
    assert transitions[0].new_value is False
    latest = collector.store.latest_samples()[0]
    assert latest.value is False
    assert latest.timestamp >= transitions[0].timestamp


def test_realtime_manager_does_not_expose_write_surface() -> None:
    manager = _manager()
    for prohibited in (
        "write",
        "write_value",
        "set_value",
        "call_method",
        "force",
        "reset",
    ):
        assert not hasattr(manager, prohibited)
