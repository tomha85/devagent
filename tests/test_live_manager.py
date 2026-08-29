from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from devagent.live.errors import LiveConnectionError
from devagent.live.manager import (
    MultiPlcConnectionManager,
    PlcConnectionSpec,
    PlcSessionState,
)
from devagent.live.models import Quality, RuntimeValue
from devagent.live.security import LiveSecurityConfig


def _value(node_id: str, value: int = 1) -> RuntimeValue:
    return RuntimeValue(
        node_id=node_id,
        value=value,
        variant_type="Int32",
        status_code="Good",
        quality=Quality.GOOD,
        source_timestamp=None,
        server_timestamp=None,
        received_at=datetime.now(timezone.utc),
        age_seconds=0.0,
        stale=False,
    )


class _Behavior:
    global_active_connects = 0
    global_max_active_connects = 0

    def __init__(
        self,
        *,
        connect_failures: int = 0,
        read_fail_nodes: set[str] | None = None,
        disconnect_fail: bool = False,
    ) -> None:
        self.connect_failures = connect_failures
        self.read_fail_nodes = set(read_fail_nodes or set())
        self.disconnect_fail = disconnect_fail
        self.instances: list[_FakeClient] = []
        self.read_started: asyncio.Event | None = None
        self.read_release: asyncio.Event | None = None
        self.disconnect_called = False


class _FakeClient:
    def __init__(self, endpoint: str, behavior: _Behavior, **kwargs) -> None:
        self.endpoint = endpoint
        self.behavior = behavior
        self.kwargs = kwargs
        self.state = "DISCONNECTED"
        behavior.instances.append(self)

    @property
    def connected(self) -> bool:
        return self.state == "CONNECTED"

    @property
    def connection_state(self) -> str:
        return self.state

    async def connect(self) -> None:
        _Behavior.global_active_connects += 1
        _Behavior.global_max_active_connects = max(
            _Behavior.global_max_active_connects,
            _Behavior.global_active_connects,
        )
        await asyncio.sleep(0)
        _Behavior.global_active_connects -= 1
        if self.behavior.connect_failures:
            self.behavior.connect_failures -= 1
            raise RuntimeError("connect failed")
        self.state = "CONNECTED"

    async def disconnect(self) -> None:
        self.behavior.disconnect_called = True
        if self.behavior.disconnect_fail:
            raise RuntimeError("disconnect failed")
        self.state = "DISCONNECTED"

    async def read(self, node_id: str) -> RuntimeValue:
        if self.behavior.read_started is not None:
            self.behavior.read_started.set()
        if self.behavior.read_release is not None:
            await self.behavior.read_release.wait()
        if node_id in self.behavior.read_fail_nodes:
            raise RuntimeError(f"read failed {node_id}")
        return _value(node_id)


def _factory(behaviors: dict[str, _Behavior], factory_fail: dict[str, str] | None = None):
    def create(endpoint: str, **kwargs):
        if factory_fail and endpoint in factory_fail:
            raise RuntimeError(factory_fail[endpoint])
        return _FakeClient(endpoint, behaviors[endpoint], **kwargs)

    return create


def _spec(plc_id: str) -> PlcConnectionSpec:
    return PlcConnectionSpec(plc_id, f"opc.tcp://{plc_id}:4840/")


def test_manager_requires_nonempty_unique_plcs() -> None:
    with pytest.raises(ValueError, match="At least one"):
        MultiPlcConnectionManager([])
    with pytest.raises(ValueError, match="Duplicate PLC id"):
        MultiPlcConnectionManager(
            [
                PlcConnectionSpec("a", "opc.tcp://a:4840/"),
                PlcConnectionSpec("a", "opc.tcp://b:4840/"),
            ]
        )
    with pytest.raises(ValueError, match="Duplicate OPC UA endpoint"):
        MultiPlcConnectionManager(
            [
                PlcConnectionSpec("a", "opc.tcp://same:4840/"),
                PlcConnectionSpec("b", "opc.tcp://same:4840/"),
            ]
        )


def test_three_plcs_connect_concurrently() -> None:
    async def scenario() -> None:
        _Behavior.global_active_connects = 0
        _Behavior.global_max_active_connects = 0
        behaviors = {f"opc.tcp://{plc}:4840/": _Behavior() for plc in ("a", "b", "c")}
        manager = MultiPlcConnectionManager(
            [_spec("a"), _spec("b"), _spec("c")],
            client_factory=_factory(behaviors),
        )
        statuses = await manager.connect_all()
        assert all(status.connected for status in statuses.values())
        assert _Behavior.global_max_active_connects == 3
        await manager.disconnect_all()

    asyncio.run(scenario())


def test_one_connection_failure_does_not_cancel_healthy_plcs() -> None:
    async def scenario() -> None:
        behaviors = {
            "opc.tcp://a:4840/": _Behavior(),
            "opc.tcp://b:4840/": _Behavior(connect_failures=1),
            "opc.tcp://c:4840/": _Behavior(),
        }
        manager = MultiPlcConnectionManager(
            [_spec("a"), _spec("b"), _spec("c")],
            client_factory=_factory(behaviors),
        )
        statuses = await manager.connect_all()
        assert statuses["a"].state is PlcSessionState.CONNECTED
        assert statuses["b"].state is PlcSessionState.FAILED
        assert statuses["c"].state is PlcSessionState.CONNECTED
        await manager.disconnect_all()

    asyncio.run(scenario())


def test_client_factory_failure_becomes_failed_not_connecting() -> None:
    async def scenario() -> None:
        behaviors = {
            "opc.tcp://a:4840/": _Behavior(),
            "opc.tcp://b:4840/": _Behavior(),
        }
        manager = MultiPlcConnectionManager(
            [_spec("a"), _spec("b")],
            client_factory=_factory(
                behaviors,
                {"opc.tcp://b:4840/": "factory boom"},
            ),
        )
        statuses = await manager.connect_all()
        assert statuses["a"].state is PlcSessionState.CONNECTED
        assert statuses["b"].state is PlcSessionState.FAILED
        assert "factory boom" in (statuses["b"].last_error or "")
        await manager.disconnect_all()

    asyncio.run(scenario())


def test_failed_connect_can_retry_with_fresh_client() -> None:
    async def scenario() -> None:
        behavior = _Behavior(connect_failures=1)
        endpoint = "opc.tcp://a:4840/"
        manager = MultiPlcConnectionManager(
            [_spec("a")],
            client_factory=_factory({endpoint: behavior}),
        )
        assert (await manager.connect_all())["a"].state is PlcSessionState.FAILED
        recovered = await manager.connect("a")
        assert recovered.state is PlcSessionState.CONNECTED
        assert recovered.successful_connections == 1
        assert len(behavior.instances) == 2
        await manager.disconnect_all()

    asyncio.run(scenario())


def test_underlying_reconnect_state_is_exposed_without_false_connected() -> None:
    async def scenario() -> None:
        behavior = _Behavior()
        endpoint = "opc.tcp://a:4840/"
        manager = MultiPlcConnectionManager(
            [_spec("a")],
            client_factory=_factory({endpoint: behavior}),
        )
        await manager.connect_all()
        behavior.instances[-1].state = "RECONNECTING"
        status = manager.status("a")
        assert status.state is PlcSessionState.RECONNECTING
        assert status.connected is False
        behavior.instances[-1].state = "CONNECTED"
        await manager.disconnect_all()

    asyncio.run(scenario())


def test_read_many_isolates_one_plc_failure_and_keeps_degraded_state() -> None:
    async def scenario() -> None:
        behaviors = {
            "opc.tcp://a:4840/": _Behavior(),
            "opc.tcp://b:4840/": _Behavior(read_fail_nodes={"bad"}),
            "opc.tcp://c:4840/": _Behavior(),
        }
        manager = MultiPlcConnectionManager(
            [_spec("a"), _spec("b"), _spec("c")],
            client_factory=_factory(behaviors),
        )
        await manager.connect_all()
        results = await manager.read_many(
            {"a": ["x"], "b": ["bad"], "c": ["z"]}
        )
        assert results["a"].succeeded is True
        assert results["b"].succeeded is False
        assert results["c"].succeeded is True
        assert results["b"].state is PlcSessionState.DEGRADED
        assert manager.status("b").connected is True
        assert manager.status("b").state is PlcSessionState.DEGRADED
        await manager.disconnect_all()

    asyncio.run(scenario())


def test_successful_read_clears_previous_degraded_state() -> None:
    async def scenario() -> None:
        behavior = _Behavior(read_fail_nodes={"bad"})
        endpoint = "opc.tcp://a:4840/"
        manager = MultiPlcConnectionManager(
            [_spec("a")],
            client_factory=_factory({endpoint: behavior}),
        )
        await manager.connect_all()
        with pytest.raises(LiveConnectionError):
            await manager.read("a", "bad")
        assert manager.status("a").state is PlcSessionState.DEGRADED
        await manager.read("a", "good")
        status = manager.status("a")
        assert status.state is PlcSessionState.CONNECTED
        assert status.last_error is None
        await manager.disconnect_all()

    asyncio.run(scenario())


def test_secret_bearing_connection_failure_is_redacted() -> None:
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
        endpoint = "opc.tcp://a:4840/"
        manager = MultiPlcConnectionManager(
            [PlcConnectionSpec("a", endpoint, security=security)],
            client_factory=_factory(
                {endpoint: _Behavior()},
                {endpoint: "login rejected plc-secret"},
            ),
        )
        status = (await manager.connect_all())["a"]
        assert "plc-secret" not in (status.last_error or "")
        assert "<redacted>" in (status.last_error or "")

    asyncio.run(scenario())


def test_disconnect_failure_is_isolated() -> None:
    async def scenario() -> None:
        behaviors = {
            "opc.tcp://a:4840/": _Behavior(),
            "opc.tcp://b:4840/": _Behavior(disconnect_fail=True),
            "opc.tcp://c:4840/": _Behavior(),
        }
        manager = MultiPlcConnectionManager(
            [_spec("a"), _spec("b"), _spec("c")],
            client_factory=_factory(behaviors),
        )
        await manager.connect_all()
        statuses = await manager.disconnect_all()
        assert statuses["a"].state is PlcSessionState.DISCONNECTED
        assert statuses["b"].state is PlcSessionState.FAILED
        assert statuses["c"].state is PlcSessionState.DISCONNECTED

    asyncio.run(scenario())


def test_read_and_disconnect_are_serialized_per_plc() -> None:
    async def scenario() -> None:
        behavior = _Behavior()
        behavior.read_started = asyncio.Event()
        behavior.read_release = asyncio.Event()
        endpoint = "opc.tcp://a:4840/"
        manager = MultiPlcConnectionManager(
            [_spec("a")],
            client_factory=_factory({endpoint: behavior}),
        )
        await manager.connect_all()

        read_task = asyncio.create_task(manager.read("a", "x"))
        await behavior.read_started.wait()
        disconnect_task = asyncio.create_task(manager.disconnect("a"))
        await asyncio.sleep(0)
        assert behavior.disconnect_called is False

        behavior.read_release.set()
        await read_task
        await disconnect_task
        assert behavior.disconnect_called is True

    asyncio.run(scenario())


def test_unknown_plc_batch_fails_before_starting_work() -> None:
    async def scenario() -> None:
        endpoint = "opc.tcp://a:4840/"
        manager = MultiPlcConnectionManager(
            [_spec("a")],
            client_factory=_factory({endpoint: _Behavior()}),
        )
        with pytest.raises(KeyError, match="Unknown PLC"):
            await manager.read_many({"missing": ["x"]})

    asyncio.run(scenario())


def test_manager_status_never_exposes_security_secret_fields() -> None:
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
        [PlcConnectionSpec("a", "opc.tcp://a:4840/", security=security)],
        client_factory=lambda *args, **kwargs: None,
    )
    rendered = repr(manager.status("a"))
    assert "plc-secret" not in rendered
    assert "client.pem" not in rendered


def test_manager_public_api_remains_read_only() -> None:
    manager = MultiPlcConnectionManager(
        [_spec("a")],
        client_factory=lambda *args, **kwargs: None,
    )
    for prohibited in (
        "write",
        "write_value",
        "set_value",
        "call_method",
        "force",
        "reset",
    ):
        assert not hasattr(manager, prohibited)
