from __future__ import annotations

import asyncio
import socket

import pytest
from cryptography.x509.oid import ExtendedKeyUsageOID

asyncua = pytest.importorskip("asyncua")

from asyncua import Client
from asyncua.crypto.cert_gen import setup_self_signed_certificate
from devagent.live.errors import LiveConnectionError
from devagent.live.models import Quality, TrustState
from devagent.live.opcua_client import ReadOnlyOpcUaClient
from devagent.live.security import LiveSecurityConfig
from devagent.live.simulator import OpcUaSimulator


def _free_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"opc.tcp://127.0.0.1:{port}/devagent/simulator/"


async def _create_test_certificate(
    private_key,
    certificate,
    application_uri: str,
    *,
    server: bool,
) -> None:
    extended = [ExtendedKeyUsageOID.CLIENT_AUTH]
    if server:
        extended.append(ExtendedKeyUsageOID.SERVER_AUTH)
    await setup_self_signed_certificate(
        private_key,
        certificate,
        application_uri,
        socket.gethostname(),
        extended,
        {"organizationName": "DevAgent Qualification"},
    )


def test_unreachable_endpoint_fails_closed() -> None:
    async def scenario() -> None:
        endpoint = _free_endpoint()
        client = ReadOnlyOpcUaClient(endpoint, timeout_seconds=0.25, auto_reconnect=False)
        with pytest.raises(LiveConnectionError):
            await client.discover_endpoints()

    asyncio.run(scenario())


def test_simulator_probe_browse_and_typed_value_loading() -> None:
    async def scenario() -> None:
        endpoint = _free_endpoint()
        async with OpcUaSimulator(endpoint, scenario="blocker") as simulator:
            client = ReadOnlyOpcUaClient(endpoint, auto_reconnect=False)
            endpoints = await client.discover_endpoints()
            assert endpoints
            await client.connect()
            try:
                nodes = await client.browse(max_depth=4, max_nodes=100)
                names = {node.browse_name for node in nodes}
                assert {"AutoMode", "RunCmd", "Speed", "FaultCode", "MachineState", "LaneCounts"} <= names

                assert simulator.node_ids is not None
                run_cmd = await client.read(simulator.node_ids.run_cmd)
                speed = await client.read(simulator.node_ids.speed)
                fault_code = await client.read(simulator.node_ids.fault_code)
                machine_state = await client.read(simulator.node_ids.machine_state)
                lane_counts = await client.read(simulator.node_ids.lane_counts)

                assert run_cmd.value is False
                assert run_cmd.variant_type == "Boolean"
                assert run_cmd.quality is Quality.GOOD
                assert run_cmd.trust is TrustState.CURRENT

                assert isinstance(speed.value, float)
                assert speed.variant_type == "Double"
                assert isinstance(fault_code.value, int)
                assert fault_code.variant_type == "Int32"
                assert machine_state.value == "BLOCKED"
                assert machine_state.variant_type == "String"
                assert lane_counts.value == [1, 2, 3]
                assert lane_counts.variant_type == "Int32"

                values = await client.load_values(nodes, max_values=50)
                assert len(values) >= 10
                assert all(value.loaded_successfully for value in values)

                missing = await client.read(
                    f"ns={simulator.namespace_index};s=Warehouse.Conveyor1.DoesNotExist"
                )
                assert missing.quality is Quality.BAD
                assert missing.trust is TrustState.UNTRUSTED
                assert missing.loaded_successfully is False
            finally:
                await client.disconnect()

    asyncio.run(scenario())


def test_simulator_subscription_returns_initial_and_changed_values() -> None:
    async def scenario() -> None:
        endpoint = _free_endpoint()
        async with OpcUaSimulator(endpoint, scenario="normal", update_interval_seconds=0.05) as simulator:
            assert simulator.node_ids is not None
            client = ReadOnlyOpcUaClient(endpoint, auto_reconnect=False)
            await client.connect()
            try:
                changes = await client.collect_changes(
                    [simulator.node_ids.run_cmd],
                    count=2,
                    timeout_seconds=2.0,
                    publishing_interval_ms=50.0,
                    sampling_interval_ms=20.0,
                )
                assert len(changes) == 2
                assert all(change.quality is Quality.GOOD for change in changes)
                assert all(change.node_id == simulator.node_ids.run_cmd for change in changes)
                assert len({change.value for change in changes}) >= 2
            finally:
                await client.disconnect()

    asyncio.run(scenario())


def test_simulator_variables_reject_client_writes() -> None:
    async def scenario() -> None:
        endpoint = _free_endpoint()
        async with OpcUaSimulator(endpoint, scenario="blocker") as simulator:
            assert simulator.node_ids is not None
            raw_client = Client(endpoint)
            await raw_client.connect()
            try:
                node = raw_client.get_node(simulator.node_ids.run_cmd)
                original = await node.read_value()
                with pytest.raises(Exception):
                    await node.write_value(not original)
                assert await node.read_value() is original
            finally:
                await raw_client.disconnect()

    asyncio.run(scenario())


def test_read_only_client_has_no_write_or_method_call_surface() -> None:
    client = ReadOnlyOpcUaClient("opc.tcp://127.0.0.1:4840/")
    for prohibited in ("write", "write_value", "set_value", "call_method", "force", "reset"):
        assert not hasattr(client, prohibited)


def test_simulator_reconnect_restores_subscription_after_server_restart() -> None:
    async def scenario() -> None:
        endpoint = _free_endpoint()
        first = OpcUaSimulator(endpoint, scenario="normal", update_interval_seconds=1.0)
        replacement: OpcUaSimulator | None = None
        collect_task: asyncio.Task[list] | None = None
        client = ReadOnlyOpcUaClient(
            endpoint,
            timeout_seconds=0.25,
            auto_reconnect=True,
            reconnect_max_delay_seconds=0.25,
            reconnect_request_timeout_seconds=5.0,
        )

        await first.start()
        assert first.node_ids is not None
        production_count_id = first.node_ids.production_count
        await client.connect()
        try:
            collect_task = asyncio.create_task(
                client.collect_changes(
                    [production_count_id],
                    count=3,
                    timeout_seconds=10.0,
                    publishing_interval_ms=50.0,
                    sampling_interval_ms=20.0,
                )
            )

            # ProductionCount changes only once per second on the first server.
            # The subscription must still be incomplete when the outage begins.
            await asyncio.sleep(0.40)
            assert collect_task.done() is False

            await first.stop()

            deadline = asyncio.get_running_loop().time() + 3.0
            while client.connection_state == "CONNECTED":
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.05)
            assert client.connection_state in {"DISCONNECTED", "RECONNECTING"}

            replacement = OpcUaSimulator(endpoint, scenario="normal", update_interval_seconds=0.05)
            await replacement.start()
            assert replacement.node_ids is not None
            assert replacement.node_ids.production_count == production_count_id

            await client.wait_until_connected(timeout_seconds=5.0)
            assert client.connected is True

            changes = await asyncio.wait_for(collect_task, timeout=5.0)
            assert len(changes) == 3
            assert all(change.node_id == production_count_id for change in changes)
            assert all(change.quality is Quality.GOOD for change in changes)
            assert any(isinstance(change.value, int) and change.value >= 1 for change in changes)

            recovered = await client.read(production_count_id)
            assert isinstance(recovered.value, int)
            assert recovered.quality is Quality.GOOD
            assert recovered.trust is TrustState.CURRENT
        finally:
            if collect_task is not None and not collect_task.done():
                collect_task.cancel()
                try:
                    await collect_task
                except asyncio.CancelledError:
                    pass
            await client.disconnect()
            if replacement is not None:
                await replacement.stop()
            await first.stop()

    asyncio.run(scenario())


def test_secure_username_password_and_server_certificate_pinning(tmp_path) -> None:
    async def scenario() -> None:
        endpoint = _free_endpoint()
        server_key = tmp_path / "server-key.pem"
        server_cert = tmp_path / "server-cert.der"
        client_key = tmp_path / "client-key.pem"
        client_cert = tmp_path / "client-cert.der"
        wrong_server_key = tmp_path / "wrong-server-key.pem"
        wrong_server_cert = tmp_path / "wrong-server-cert.der"
        client_uri = "urn:devagent:qualification:secure-client"

        await _create_test_certificate(
            server_key,
            server_cert,
            OpcUaSimulator.APPLICATION_URI,
            server=True,
        )
        await _create_test_certificate(
            client_key,
            client_cert,
            client_uri,
            server=False,
        )
        await _create_test_certificate(
            wrong_server_key,
            wrong_server_cert,
            "urn:devagent:qualification:wrong-server",
            server=True,
        )

        async with OpcUaSimulator(
            endpoint,
            scenario="blocker",
            username="operator",
            password="correct-password",
            server_certificate=str(server_cert),
            server_private_key=str(server_key),
            security_policy="Basic256Sha256",
            security_mode="SignAndEncrypt",
        ) as simulator:
            assert simulator.node_ids is not None

            good_security = LiveSecurityConfig(
                username="operator",
                password="correct-password",
                security_policy="Basic256Sha256",
                security_mode="SignAndEncrypt",
                client_certificate=str(client_cert),
                client_private_key=str(client_key),
                server_certificate=str(server_cert),
                application_uri=client_uri,
            )
            client = ReadOnlyOpcUaClient(
                endpoint,
                auto_reconnect=False,
                security=good_security,
            )
            await client.connect()
            try:
                state = await client.read(simulator.node_ids.machine_state)
                assert state.value == "BLOCKED"
                assert state.quality is Quality.GOOD
                assert state.trust is TrustState.CURRENT
                assert client.authentication_mode == "USERNAME_PASSWORD"
                assert client.security_summary == "Basic256Sha256/SignAndEncrypt"
            finally:
                await client.disconnect()

            bad_password = LiveSecurityConfig(
                username="operator",
                password="wrong-password",
                security_policy="Basic256Sha256",
                security_mode="SignAndEncrypt",
                client_certificate=str(client_cert),
                client_private_key=str(client_key),
                server_certificate=str(server_cert),
                application_uri=client_uri,
            )
            rejected = ReadOnlyOpcUaClient(
                endpoint,
                auto_reconnect=False,
                security=bad_password,
            )
            with pytest.raises(LiveConnectionError) as bad_password_error:
                await rejected.connect()
            assert "wrong-password" not in str(bad_password_error.value)

            wrong_pin = LiveSecurityConfig(
                username="operator",
                password="correct-password",
                security_policy="Basic256Sha256",
                security_mode="SignAndEncrypt",
                client_certificate=str(client_cert),
                client_private_key=str(client_key),
                server_certificate=str(wrong_server_cert),
                application_uri=client_uri,
            )
            untrusted = ReadOnlyOpcUaClient(
                endpoint,
                auto_reconnect=False,
                security=wrong_pin,
            )
            with pytest.raises(LiveConnectionError):
                await untrusted.connect()

    asyncio.run(scenario())
