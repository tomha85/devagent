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


def test_real_exact_monitor_refresh_records_closed_reconfiguration_interval() -> None:
    async def scenario() -> None:
        port = _free_port()
        endpoint = f"opc.tcp://127.0.0.1:{port}/devagent/reconfiguration-integrity/"
        async with OpcUaSimulator(
            endpoint=endpoint,
            scenario="normal",
            update_interval_seconds=0.05,
        ) as simulator:
            assert simulator.node_ids is not None
            manager = ProductionRealtimeMultiPlcConnectionManager(
                [PlcConnectionSpec("plc1", endpoint)],
                subscription_enabled=True,
                sampling_interval_ms=20.0,
                publishing_interval_ms=50.0,
                iterator_queue_maxsize=1000,
            )
            try:
                await manager.connect("plc1")
                first = (simulator.node_ids.run_cmd,)
                second = (simulator.node_ids.production_count,)

                await manager.replace_monitored_node_ids("plc1", first)
                assert await manager.wait_for_active_monitoring(
                    "plc1",
                    timeout_seconds=5.0,
                )
                assert manager.integrity_status("plc1").active_monitored_nodes == 1
                initial_gaps = manager.drain_evidence_gaps("plc1")
                initial_reconfiguration = [
                    gap
                    for gap in initial_gaps
                    if gap.source == "MONITOR_SET_RECONFIGURATION"
                ]
                assert len(initial_reconfiguration) == 1
                assert initial_reconfiguration[0].end_timestamp is not None
                # Initial arming is preserved as bounded startup metadata but does
                # not poison the session-wide evidence-loss counter.
                assert manager.integrity_status("plc1").evidence_gap_count == 0

                await manager.replace_monitored_node_ids("plc1", second)
                assert await manager.wait_for_active_monitoring(
                    "plc1",
                    timeout_seconds=5.0,
                )
                assert manager.integrity_status("plc1").active_monitored_nodes == 1
                assert manager._state("plc1").monitored_nodes == set(second)
                assert not any(
                    gap.source == "MONITOR_SET_RECONFIGURATION"
                    for gap in manager.open_evidence_gaps("plc1")
                )

                gaps = manager.drain_evidence_gaps("plc1")
                reconfiguration = [
                    gap for gap in gaps if gap.source == "MONITOR_SET_RECONFIGURATION"
                ]
                assert len(reconfiguration) == 1
                assert reconfiguration[0].end_timestamp is not None
                assert reconfiguration[0].end_timestamp >= reconfiguration[0].timestamp
                assert manager.integrity_status("plc1").evidence_gap_count == 1
            finally:
                await manager.disconnect("plc1")

    asyncio.run(scenario())
