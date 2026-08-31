from __future__ import annotations

import asyncio
import socket

import pytest

from devagent.live.manager import PlcConnectionSpec
from devagent.live.production_realtime import ProductionRealtimeMultiPlcConnectionManager
from devagent.live.simulator import OpcUaSimulator


pytest.importorskip("asyncua")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_real_asyncua_production_subscription_and_coherent_snapshot() -> None:
    async def scenario() -> None:
        port = _free_port()
        endpoint = f"opc.tcp://127.0.0.1:{port}/devagent/integrity-v2/"
        async with OpcUaSimulator(
            endpoint=endpoint,
            update_interval_seconds=0.05,
        ) as simulator:
            assert simulator.node_ids is not None
            nodes = (
                simulator.node_ids.run_cmd,
                simulator.node_ids.downstream_ready,
                simulator.node_ids.production_count,
            )
            manager = ProductionRealtimeMultiPlcConnectionManager(
                [PlcConnectionSpec("plc1", endpoint)],
                subscription_enabled=True,
                sampling_interval_ms=20.0,
                publishing_interval_ms=50.0,
                queue_size=100,
                cache_fresh_seconds=0.20,
                max_snapshot_skew_seconds=0.20,
                max_monitored_nodes=32,
                iterator_queue_maxsize=1000,
            )
            try:
                status = await manager.connect("plc1")
                assert status.connected is True
                await manager.monitor_node_ids("plc1", nodes)
                await asyncio.sleep(0.35)

                integrity = manager.integrity_status("plc1")
                assert integrity.desired_monitored_nodes == len(nodes)
                assert integrity.active_monitored_nodes == len(nodes)
                assert integrity.omitted_monitored_nodes == 0
                assert integrity.server_overflow_events == 0
                assert integrity.local_buffer_drops == 0

                result = await manager.read_many({"plc1": nodes})
                assert result["plc1"].succeeded is True
                assert len(result["plc1"].values) == len(nodes)
                assert all(item.node_id in nodes for item in result["plc1"].values)

                # The dynamic simulator must produce genuine subscription events.
                await asyncio.sleep(0.25)
                events = manager.drain_realtime_events(
                    "plc1",
                    node_ids=nodes,
                    max_events=5000,
                )
                assert events
                assert any(item.node_id == simulator.node_ids.production_count for item in events)

                integrity = manager.integrity_status("plc1")
                assert integrity.evidence_complete_since_session_start is True
                assert integrity.last_sequence_number is not None
            finally:
                await manager.disconnect("plc1")

    asyncio.run(scenario())


def test_real_asyncua_two_plc_production_manager_reads_both_sessions() -> None:
    async def scenario() -> None:
        port1 = _free_port()
        port2 = _free_port()
        endpoint1 = f"opc.tcp://127.0.0.1:{port1}/devagent/integrity-v2-a/"
        endpoint2 = f"opc.tcp://127.0.0.1:{port2}/devagent/integrity-v2-b/"
        async with OpcUaSimulator(
            endpoint=endpoint1,
            scenario="normal",
            update_interval_seconds=0.05,
        ) as sim1, OpcUaSimulator(
            endpoint=endpoint2,
            scenario="normal",
            update_interval_seconds=0.05,
        ) as sim2:
            assert sim1.node_ids is not None
            assert sim2.node_ids is not None
            manager = ProductionRealtimeMultiPlcConnectionManager(
                [
                    PlcConnectionSpec("plc1", endpoint1),
                    PlcConnectionSpec("plc2", endpoint2),
                ],
                subscription_enabled=True,
                sampling_interval_ms=20.0,
                publishing_interval_ms=50.0,
                cache_fresh_seconds=0.20,
                max_snapshot_skew_seconds=0.20,
                max_monitored_nodes=32,
                iterator_queue_maxsize=1000,
            )
            try:
                statuses = await manager.connect_all()
                assert statuses["plc1"].connected is True
                assert statuses["plc2"].connected is True

                nodes1 = (sim1.node_ids.run_cmd, sim1.node_ids.production_count)
                nodes2 = (sim2.node_ids.run_cmd, sim2.node_ids.production_count)
                await manager.monitor_node_ids("plc1", nodes1)
                await manager.monitor_node_ids("plc2", nodes2)
                await asyncio.sleep(0.30)

                results = await manager.read_many(
                    {
                        "plc1": nodes1,
                        "plc2": nodes2,
                    }
                )
                assert results["plc1"].succeeded is True
                assert results["plc2"].succeeded is True
                assert len(results["plc1"].values) == 2
                assert len(results["plc2"].values) == 2
                assert manager.integrity_status("plc1").active_monitored_nodes == 2
                assert manager.integrity_status("plc2").active_monitored_nodes == 2
            finally:
                await manager.disconnect_all()

    asyncio.run(scenario())


def test_monitor_capacity_contract_handles_256_nodes_deterministically() -> None:
    async def scenario() -> None:
        manager = ProductionRealtimeMultiPlcConnectionManager(
            [PlcConnectionSpec("plc1", "opc.tcp://plc1:4840/")],
            subscription_enabled=False,
            max_monitored_nodes=256,
        )
        requested = tuple(f"ns=2;s=Tag{index:03d}" for index in range(300))
        await manager.monitor_node_ids("plc1", requested)
        status = manager.integrity_status("plc1")
        assert status.desired_monitored_nodes == 300
        assert status.omitted_monitored_nodes == 44
        assert len(manager._state("plc1").monitored_nodes) == 256
        assert manager._state("plc1").monitored_nodes == set(requested[:256])

        replacement = tuple(f"ns=2;s=NewTag{index:03d}" for index in range(256))
        await manager.monitor_node_ids("plc1", replacement)
        assert manager._state("plc1").monitored_nodes == set(replacement)
        assert not manager._state("plc1").monitored_nodes.intersection(requested)

    asyncio.run(scenario())
