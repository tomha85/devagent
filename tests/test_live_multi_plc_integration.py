from __future__ import annotations

import asyncio
import socket

import pytest

pytest.importorskip("asyncua")

from devagent.live.manager import (
    MultiPlcConnectionManager,
    PlcConnectionSpec,
    PlcSessionState,
)
from devagent.live.models import Quality, TrustState
from devagent.live.simulator import OpcUaSimulator


def _free_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"opc.tcp://127.0.0.1:{port}/devagent/simulator/"


def test_three_real_plcs_one_runtime_failure_is_isolated() -> None:
    async def scenario() -> None:
        endpoint_a = _free_endpoint()
        endpoint_b = _free_endpoint()
        endpoint_c = _free_endpoint()
        sim_a = OpcUaSimulator(endpoint_a, scenario="normal", update_interval_seconds=0.05)
        sim_b = OpcUaSimulator(endpoint_b, scenario="blocker", update_interval_seconds=0.05)
        sim_c = OpcUaSimulator(endpoint_c, scenario="normal", update_interval_seconds=0.05)

        await asyncio.gather(sim_a.start(), sim_b.start(), sim_c.start())
        assert sim_a.node_ids is not None
        assert sim_b.node_ids is not None
        assert sim_c.node_ids is not None

        manager = MultiPlcConnectionManager(
            [
                PlcConnectionSpec("plc-a", endpoint_a, plc_name="Conveyor PLC A", auto_reconnect=False),
                PlcConnectionSpec("plc-b", endpoint_b, plc_name="Sorter PLC B", auto_reconnect=False),
                PlcConnectionSpec("plc-c", endpoint_c, plc_name="Conveyor PLC C", auto_reconnect=False),
            ]
        )

        try:
            statuses = await manager.connect_all()
            assert all(status.state is PlcSessionState.CONNECTED for status in statuses.values())
            assert all(status.connected for status in statuses.values())

            initial = await manager.read_many(
                {
                    "plc-a": [sim_a.node_ids.machine_state],
                    "plc-b": [sim_b.node_ids.machine_state],
                    "plc-c": [sim_c.node_ids.machine_state],
                }
            )
            assert all(result.succeeded for result in initial.values())
            assert initial["plc-a"].values[0].quality is Quality.GOOD
            assert initial["plc-a"].values[0].trust is TrustState.CURRENT
            assert initial["plc-b"].values[0].value == "BLOCKED"
            assert initial["plc-c"].values[0].quality is Quality.GOOD

            await sim_b.stop()
            deadline = asyncio.get_running_loop().time() + 3.0
            while manager.status("plc-b").connected:
                if asyncio.get_running_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.05)
            assert manager.status("plc-b").connected is False

            after_failure = await manager.read_many(
                {
                    "plc-a": [sim_a.node_ids.machine_state],
                    "plc-b": [sim_b.node_ids.machine_state],
                    "plc-c": [sim_c.node_ids.machine_state],
                }
            )
            assert after_failure["plc-a"].succeeded is True
            assert after_failure["plc-c"].succeeded is True
            assert after_failure["plc-b"].succeeded is False
            assert after_failure["plc-b"].state is PlcSessionState.DEGRADED
            assert manager.status("plc-a").state is PlcSessionState.CONNECTED
            assert manager.status("plc-c").state is PlcSessionState.CONNECTED
        finally:
            await manager.disconnect_all()
            await asyncio.gather(
                sim_a.stop(),
                sim_b.stop(),
                sim_c.stop(),
                return_exceptions=True,
            )

    asyncio.run(scenario())
