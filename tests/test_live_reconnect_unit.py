from __future__ import annotations

import asyncio
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from devagent.live.errors import LiveConnectionError
from devagent.live.models import Quality
from devagent.live.opcua_client import ReadOnlyOpcUaClient
import devagent.live.opcua_client as opcua_client


class FakeState:
    def __init__(self, value: str) -> None:
        self.value = value


class FakeStateSubscription:
    def __init__(self, client: "FakeClient") -> None:
        self.client = client

    async def __aenter__(self) -> "FakeStateSubscription":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def next_change(self, timeout: float | None = None) -> FakeState:
        await asyncio.sleep(0)
        self.client.state = FakeState("connected")
        return self.client.state


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, **kwargs) -> None:
        self.init_kwargs = kwargs
        self.connect_kwargs: dict[str, object] | None = None
        self.state = FakeState("disconnected")
        self.subscription = None
        type(self).instances.append(self)

    async def connect(self, **kwargs) -> None:
        self.connect_kwargs = kwargs
        self.state = FakeState("connected")

    async def disconnect(self) -> None:
        self.state = FakeState("disconnected")

    def subscribe_state(self) -> FakeStateSubscription:
        return FakeStateSubscription(self)

    def get_node(self, node_id: str):
        return SimpleNamespace(nodeid=FakeNodeId(node_id))

    async def create_subscription(self, publishing_interval_ms: float):
        assert self.subscription is not None
        return self.subscription


class FakeNodeId:
    def __init__(self, text: str) -> None:
        self.text = text

    def to_string(self) -> str:
        return self.text


class FakeStatus:
    def __init__(self, name: str, *, good: bool = False, bad: bool = False) -> None:
        self.name = name
        self._good = good
        self._bad = bad

    def is_good(self) -> bool:
        return self._good

    def is_bad(self) -> bool:
        return self._bad


class FakeVariantType:
    name = "Boolean"


class FakeVariant:
    def __init__(self, value: object) -> None:
        self.Value = value
        self.VariantType = FakeVariantType()


class FakeDataValue:
    def __init__(self, value: object) -> None:
        self.Value = FakeVariant(value)
        self.StatusCode = FakeStatus("Good", good=True)
        self.SourceTimestamp = datetime.now(timezone.utc)
        self.ServerTimestamp = self.SourceTimestamp


class StatusChangeEvent:
    def __init__(self, status: FakeStatus) -> None:
        self.notification = SimpleNamespace(Status=status)


class DataChangeEvent:
    def __init__(self, node_id: str, value: object, *, replayed: bool = False) -> None:
        self.node = SimpleNamespace(nodeid=FakeNodeId(node_id))
        self.data = SimpleNamespace(monitored_item=SimpleNamespace(Value=FakeDataValue(value)))
        self.replayed = replayed


class FakeSubscription:
    def __init__(self, client: FakeClient, events: list[tuple[str, object]]) -> None:
        self.client = client
        self.events = list(events)
        self.subscribed_nodes = None

    async def __aenter__(self) -> "FakeSubscription":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def subscribe_data_change(self, nodes, *, queuesize: int, sampling_interval: float) -> None:
        self.subscribed_nodes = list(nodes)

    async def next_event(self, timeout: float | None = None):
        if not self.events:
            raise asyncio.TimeoutError
        state, event = self.events.pop(0)
        self.client.state = FakeState(state)
        await asyncio.sleep(0)
        return event


@pytest.fixture
def fake_subscription_module(monkeypatch: pytest.MonkeyPatch):
    asyncua_module = types.ModuleType("asyncua")
    common_module = types.ModuleType("asyncua.common")
    subscription_module = types.ModuleType("asyncua.common.subscription")
    subscription_module.DataChangeEvent = DataChangeEvent
    subscription_module.StatusChangeEvent = StatusChangeEvent
    common_module.subscription = subscription_module
    asyncua_module.common = common_module

    monkeypatch.setitem(sys.modules, "asyncua", asyncua_module)
    monkeypatch.setitem(sys.modules, "asyncua.common", common_module)
    monkeypatch.setitem(sys.modules, "asyncua.common.subscription", subscription_module)
    return subscription_module


def test_connect_keeps_asyncua_2_0_constructor_compatibility(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeClient.instances.clear()
    monkeypatch.setattr(opcua_client, "_require_asyncua", lambda: (FakeClient, SimpleNamespace()))

    async def scenario() -> None:
        client = ReadOnlyOpcUaClient(
            "opc.tcp://127.0.0.1:4840/",
            timeout_seconds=1.25,
            auto_reconnect=True,
            reconnect_max_delay_seconds=2.5,
            reconnect_request_timeout_seconds=7.5,
        )
        assert client.connection_state == "DISCONNECTED"
        assert client.connected is False

        await client.connect()
        raw = FakeClient.instances[-1]
        assert raw.init_kwargs == {
            "url": "opc.tcp://127.0.0.1:4840/",
            "timeout": 1.25,
        }
        assert raw.connect_kwargs == {
            "auto_reconnect": True,
            "reconnect_max_delay": 2.5,
            "reconnect_request_timeout": 7.5,
        }
        assert client.connection_state == "CONNECTED"
        assert client.connected is True

        await client.disconnect()
        assert client.connection_state == "DISCONNECTED"
        assert client.connected is False

    asyncio.run(scenario())


def test_wait_until_connected_observes_library_reconnect_state(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        wrapper = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/", auto_reconnect=True)
        raw = FakeClient(url=wrapper.endpoint, timeout=wrapper.timeout_seconds)
        raw.state = FakeState("reconnecting")
        wrapper._client = raw

        assert wrapper.connection_state == "RECONNECTING"
        await wrapper.wait_until_connected(timeout_seconds=0.5)
        assert wrapper.connected is True

    asyncio.run(scenario())


def test_wait_until_connected_fails_closed_without_auto_reconnect() -> None:
    async def scenario() -> None:
        wrapper = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/", auto_reconnect=False)
        raw = FakeClient(url=wrapper.endpoint, timeout=wrapper.timeout_seconds)
        raw.state = FakeState("disconnected")
        wrapper._client = raw

        with pytest.raises(LiveConnectionError, match="disconnected"):
            await wrapper.wait_until_connected(timeout_seconds=0.5)

    asyncio.run(scenario())


def test_subscription_survives_transient_bad_status_during_reconnect(
    monkeypatch: pytest.MonkeyPatch,
    fake_subscription_module,
) -> None:
    monkeypatch.setattr(opcua_client, "_require_asyncua", lambda: (FakeClient, SimpleNamespace()))

    async def scenario() -> None:
        wrapper = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/", auto_reconnect=True)
        raw = FakeClient(url=wrapper.endpoint, timeout=wrapper.timeout_seconds)
        wrapper._client = raw
        raw.subscription = FakeSubscription(
            raw,
            [
                # A graceful server shutdown can notify BadShutdown before the
                # client's public state has transitioned away from CONNECTED.
                ("connected", StatusChangeEvent(FakeStatus("BadShutdown", bad=True))),
                ("connected", DataChangeEvent("ns=2;s=RunCmd", True, replayed=True)),
            ],
        )

        changes = await wrapper.collect_changes(["ns=2;s=RunCmd"], count=1, timeout_seconds=0.5)
        assert len(changes) == 1
        assert changes[0].value is True
        assert changes[0].quality is Quality.GOOD
        assert changes[0].replayed is True
        assert wrapper.connected is True

    asyncio.run(scenario())


def test_subscription_bad_status_is_not_hidden_when_session_is_connected(
    monkeypatch: pytest.MonkeyPatch,
    fake_subscription_module,
) -> None:
    monkeypatch.setattr(opcua_client, "_require_asyncua", lambda: (FakeClient, SimpleNamespace()))

    async def scenario() -> None:
        wrapper = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/", auto_reconnect=True)
        raw = FakeClient(url=wrapper.endpoint, timeout=wrapper.timeout_seconds)
        raw.state = FakeState("connected")
        wrapper._client = raw
        raw.subscription = FakeSubscription(
            raw,
            [("connected", StatusChangeEvent(FakeStatus("BadSubscriptionIdInvalid", bad=True)))],
        )

        with pytest.raises(LiveConnectionError, match="BadSubscriptionIdInvalid"):
            await wrapper.collect_changes(["ns=2;s=RunCmd"], count=1, timeout_seconds=0.5)

    asyncio.run(scenario())


def test_connected_bad_timeout_remains_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    fake_subscription_module,
) -> None:
    monkeypatch.setattr(opcua_client, "_require_asyncua", lambda: (FakeClient, SimpleNamespace()))

    async def scenario() -> None:
        wrapper = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/", auto_reconnect=True)
        raw = FakeClient(url=wrapper.endpoint, timeout=wrapper.timeout_seconds)
        raw.state = FakeState("connected")
        wrapper._client = raw
        raw.subscription = FakeSubscription(
            raw,
            [("connected", StatusChangeEvent(FakeStatus("BadTimeout", bad=True)))],
        )

        with pytest.raises(LiveConnectionError, match="BadTimeout"):
            await wrapper.collect_changes(["ns=2;s=RunCmd"], count=1, timeout_seconds=0.5)

    asyncio.run(scenario())


def test_wait_until_connected_timeout_fails_closed() -> None:
    class NeverConnectStateSubscription:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def next_change(self, timeout: float | None = None):
            raise asyncio.TimeoutError

    async def scenario() -> None:
        wrapper = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/", auto_reconnect=True)
        raw = FakeClient(url=wrapper.endpoint, timeout=wrapper.timeout_seconds)
        raw.state = FakeState("reconnecting")
        raw.subscribe_state = lambda: NeverConnectStateSubscription()
        wrapper._client = raw

        with pytest.raises(LiveConnectionError, match="Timed out waiting for OPC UA reconnect"):
            await wrapper.wait_until_connected(timeout_seconds=0.05)

    asyncio.run(scenario())


def test_reconnect_changes_preserve_read_only_surface() -> None:
    client = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/")
    for prohibited in ("write", "write_value", "set_value", "call_method", "force", "reset"):
        assert not hasattr(client, prohibited)
