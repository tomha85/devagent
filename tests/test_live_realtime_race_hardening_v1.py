from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import devagent.live.realtime_manager as realtime_module
from devagent.live.manager import PlcConnectionSpec, PlcSessionState
from devagent.live.models import Quality, RuntimeValue
from devagent.live.realtime_manager import RealtimeMultiPlcConnectionManager


def _runtime(
    node_id: str,
    value: object,
    *,
    source_timestamp: datetime | None = None,
    server_timestamp: datetime | None = None,
    received_at: datetime | None = None,
) -> RuntimeValue:
    received = received_at or datetime.now(timezone.utc)
    return RuntimeValue(
        node_id=node_id,
        value=value,
        variant_type="Boolean",
        status_code="Good",
        quality=Quality.GOOD,
        source_timestamp=source_timestamp,
        server_timestamp=server_timestamp,
        received_at=received,
        age_seconds=0.0,
        stale=False,
        replayed=False,
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
    def __init__(self, value: object, stamp: datetime | None) -> None:
        self.StatusCode = _Status()
        self.Value = SimpleNamespace(Value=value, VariantType=_VariantType())
        self.SourceTimestamp = stamp
        self.ServerTimestamp = stamp


class _FakeUaClient:
    def __init__(self) -> None:
        self.values: dict[str, object] = {"n1": True, "n2": True}
        self.hook = None

    async def read_attributes(self, node_ids, attribute):
        if self.hook is not None:
            self.hook()
        stamp = datetime.now(timezone.utc)
        return [_DataValue(self.values[str(node_id)], stamp) for node_id in node_ids]


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


def _manager() -> RealtimeMultiPlcConnectionManager:
    return RealtimeMultiPlcConnectionManager(
        [PlcConnectionSpec("plc1", "opc.tcp://plc1:4840/")],
        client_factory=lambda endpoint, **kwargs: _FakeOuterClient(endpoint, **kwargs),
        subscription_enabled=False,
        cache_fresh_seconds=0.25,
        max_snapshot_skew_seconds=0.05,
    )


def _fake_asyncua(monkeypatch) -> None:
    ua = SimpleNamespace(AttributeIds=SimpleNamespace(Value="Value"))
    monkeypatch.setattr(realtime_module, "_require_asyncua", lambda: (object, ua))


def test_timestamp_less_subscription_cannot_override_question_time_batch() -> None:
    manager = _manager()
    state = manager._state("plc1")
    now = datetime.now(timezone.utc)

    batch = _runtime("n1", True, received_at=now)
    queued_subscription = _runtime(
        "n1",
        False,
        received_at=now + timedelta(seconds=1),
    )

    assert manager._update_cache(state, batch, source="BATCH_READ") is True
    assert manager._update_cache(
        state, queued_subscription, source="SUBSCRIPTION"
    ) is False
    assert state.cache["n1"].value is True
    assert state.cache_sources["n1"] == "BATCH_READ"


def test_timestamp_less_batch_replaces_subscription_when_order_is_unknowable() -> None:
    manager = _manager()
    state = manager._state("plc1")
    now = datetime.now(timezone.utc)

    queued_subscription = _runtime("n1", False, received_at=now)
    batch = _runtime("n1", True, received_at=now + timedelta(milliseconds=1))

    assert manager._update_cache(
        state, queued_subscription, source="SUBSCRIPTION"
    ) is True
    assert manager._update_cache(state, batch, source="BATCH_READ") is True
    assert state.cache["n1"].value is True
    assert state.cache_sources["n1"] == "BATCH_READ"


def test_incoherent_realtime_merge_falls_back_to_single_batch(monkeypatch) -> None:
    async def scenario() -> None:
        _fake_asyncua(monkeypatch)
        manager = _manager()
        await manager.connect("plc1")
        outer = manager._entry("plc1").client
        assert outer is not None

        now = datetime.now(timezone.utc)

        def inject_subscription() -> None:
            subscription = _runtime(
                "n1",
                False,
                source_timestamp=now + timedelta(seconds=1),
                server_timestamp=now + timedelta(seconds=1),
                received_at=now - timedelta(seconds=1),
            )
            manager._update_cache(
                manager._state("plc1"), subscription, source="SUBSCRIPTION"
            )

        outer.inner.uaclient.hook = inject_subscription
        result = await manager.read_many({"plc1": ("n1", "n2")})

        assert result["plc1"].succeeded is True
        assert [item.value for item in result["plc1"].values] == [True, True]
        status = manager.realtime_status("plc1")
        assert status.source == "OPCUA_BATCH_READ"
        assert status.max_timestamp_skew_seconds is not None
        assert status.max_timestamp_skew_seconds <= manager.max_snapshot_skew_seconds
        await manager.disconnect("plc1")

    asyncio.run(scenario())


def test_batch_response_is_rejected_when_connection_epoch_changes(monkeypatch) -> None:
    async def scenario() -> None:
        _fake_asyncua(monkeypatch)
        manager = _manager()
        await manager.connect("plc1")
        outer = manager._entry("plc1").client
        assert outer is not None
        before = manager._state("plc1").epoch

        def lose_continuity() -> None:
            manager._invalidate_realtime("plc1", reason="test continuity loss")

        outer.inner.uaclient.hook = lose_continuity
        result = await manager.read_many({"plc1": ("n1", "n2")})

        assert result["plc1"].succeeded is False
        assert result["plc1"].state is PlcSessionState.RECONNECTING
        state = manager._state("plc1")
        assert state.epoch > before
        assert state.cache == {}
        assert state.cache_sources == {}
        assert state.last_snapshot is None
        await manager.disconnect("plc1")

    asyncio.run(scenario())
