from __future__ import annotations

import asyncio
import socket
from datetime import datetime, timedelta, timezone

import pytest

from devagent.live.manager import PlcConnectionSpec
from devagent.live.production_realtime import (
    EvidenceIntegrityTimelineStore,
    LiveEvidenceGap,
    ProductionRealtimeMultiPlcConnectionManager,
)
from devagent.live.simulator import OpcUaSimulator


def _manager(**kwargs) -> ProductionRealtimeMultiPlcConnectionManager:
    return ProductionRealtimeMultiPlcConnectionManager(
        [PlcConnectionSpec("plc1", "opc.tcp://plc1:4840/")],
        subscription_enabled=False,
        **kwargs,
    )


def _context():
    class Context:
        @staticmethod
        def unique_tag_for_reference(reference: str):
            return type("Tag", (), {"id": "tag-1"})()

    return Context()


def test_open_connection_gap_marks_later_window_incomplete_until_closed() -> None:
    store = EvidenceIntegrityTimelineStore(retention_seconds=120.0)
    now = datetime.now(timezone.utc)
    start = now - timedelta(seconds=10)
    open_gap = LiveEvidenceGap(
        timestamp=start,
        end_timestamp=None,
        plc_id="plc1",
        source="CONNECTION_CONTINUITY",
        reason="transport lost",
    )
    store.record_gap(open_gap)

    diagnosis = store.diagnose_recent_transition(
        _context(),
        "RunCmd",
        lookback_seconds=2.0,
        now=now,
    )
    assert diagnosis.evidence_complete is False

    # Close the same interval before the requested window. The closing record must
    # replace the open marker rather than leaving an open-ended false positive.
    store.record_gap(
        LiveEvidenceGap(
            timestamp=start,
            end_timestamp=now - timedelta(seconds=5),
            plc_id="plc1",
            source="CONNECTION_CONTINUITY",
            reason="transport lost",
            dropped_count=0,
        )
    )
    diagnosis = store.diagnose_recent_transition(
        _context(),
        "RunCmd",
        lookback_seconds=2.0,
        now=now,
    )
    assert diagnosis.evidence_complete is True


def test_local_buffer_gap_uses_evicted_source_timestamp() -> None:
    from collections import deque
    from devagent.live.models import Quality, RuntimeValue

    manager = _manager()
    state = manager._state("plc1")
    state.events = deque(maxlen=1)
    now = datetime.now(timezone.utc)
    old_stamp = now - timedelta(seconds=8)
    old = RuntimeValue(
        node_id="n1",
        value=False,
        variant_type="Boolean",
        status_code="Good",
        quality=Quality.GOOD,
        source_timestamp=old_stamp,
        server_timestamp=old_stamp,
        received_at=now - timedelta(seconds=7),
        age_seconds=1.0,
        stale=False,
        replayed=False,
    )
    new = RuntimeValue(
        node_id="n1",
        value=True,
        variant_type="Boolean",
        status_code="Good",
        quality=Quality.GOOD,
        source_timestamp=now,
        server_timestamp=now,
        received_at=now,
        age_seconds=0.0,
        stale=False,
        replayed=False,
    )
    manager._append_subscription_event("plc1", old)
    manager._append_subscription_event("plc1", new)
    gaps = manager.drain_evidence_gaps("plc1")
    assert len(gaps) == 1
    assert gaps[0].source == "LOCAL_EVENT_BUFFER_OVERFLOW"
    assert gaps[0].timestamp == old_stamp


def test_gap_metadata_overflow_coalesces_instead_of_becoming_silent() -> None:
    manager = _manager(evidence_gap_maxsize=2)
    now = datetime.now(timezone.utc)
    for index in range(3):
        manager._record_gap(
            "plc1",
            source="TEST_GAP",
            reason=f"gap {index}",
            timestamp=now + timedelta(milliseconds=index),
        )
    status = manager.integrity_status("plc1")
    assert status.gap_metadata_overflows == 1
    assert status.evidence_complete_since_session_start is False
    gaps = manager.drain_evidence_gaps("plc1")
    assert len(gaps) == 2
    assert gaps[0].source == "EVIDENCE_GAP_METADATA_OVERFLOW"
    assert gaps[0].end_timestamp is not None


def test_additive_monitoring_preserves_deterministic_desired_order() -> None:
    async def scenario() -> None:
        manager = _manager(max_monitored_nodes=4)
        await manager.monitor_node_ids("plc1", ("A", "B"))
        await manager.ensure_monitored_node_ids("plc1", ("C", "A"))
        state = manager._integrity_state("plc1")
        assert state.desired_nodes == ("A", "B", "C")
        assert manager._state("plc1").monitored_nodes == {"A", "B", "C"}

    asyncio.run(scenario())


def test_monitored_item_setup_gap_is_interval_and_closes_on_recovery() -> None:
    manager = _manager()
    manager._sync_setup_gaps(
        "plc1",
        desired_nodes=("A",),
        failures=("A",),
    )
    state = manager._integrity_state("plc1")
    start = state.setup_gap_starts["A"]
    manager._sync_setup_gaps(
        "plc1",
        desired_nodes=("A",),
        failures=(),
    )
    gaps = manager.drain_evidence_gaps("plc1")
    assert len(gaps) == 2
    assert gaps[0].timestamp == start
    assert gaps[0].end_timestamp is None
    assert gaps[1].timestamp == start
    assert gaps[1].end_timestamp is not None

    store = EvidenceIntegrityTimelineStore(retention_seconds=60.0)
    for gap in gaps:
        store.record_gap(gap)
    matching = [gap for gap in store.evidence_gaps() if gap.source == "MONITORED_ITEM_SETUP"]
    assert len(matching) == 1
    assert matching[0].end_timestamp is not None


def test_equal_timestamp_conflict_is_explicit_integrity_gap() -> None:
    from devagent.live.history import LiveHistoricalSample

    store = EvidenceIntegrityTimelineStore(retention_seconds=60.0)
    now = datetime.now(timezone.utc)
    stamp = now - timedelta(seconds=1)

    def sample(value: bool) -> LiveHistoricalSample:
        return LiveHistoricalSample(
            timestamp=stamp,
            plc_id="plc1",
            tag_id="tag-1",
            tag_name="RunCmd",
            node_id="n1",
            value=value,
            definitive_current=True,
            quality="GOOD",
            trust="CURRENT",
        )

    store.append_many((sample(False), sample(True)))
    assert store.transitions() == ()
    gaps = store.evidence_gaps()
    assert len(gaps) == 1
    assert gaps[0].source == "TIMESTAMP_CONFLICT"
    diagnosis = store.diagnose_recent_transition(
        _context(),
        "RunCmd",
        lookback_seconds=5.0,
        now=now,
    )
    assert diagnosis.evidence_complete is False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_real_asyncua_idle_subscription_timeout_is_not_evidence_gap() -> None:
    pytest.importorskip("asyncua")

    async def scenario() -> None:
        port = _free_port()
        endpoint = f"opc.tcp://127.0.0.1:{port}/devagent/integrity-idle/"
        async with OpcUaSimulator(
            endpoint=endpoint,
            scenario="healthy_running",
            update_interval_seconds=0.20,
        ) as simulator:
            assert simulator.node_ids is not None
            # LaneCounts is initialized by the simulator and is not rewritten by
            # either fixed-state or dynamic update loops, so after the initial
            # publish this monitored item is intentionally idle for >1 second.
            node = simulator.node_ids.lane_counts
            manager = ProductionRealtimeMultiPlcConnectionManager(
                [PlcConnectionSpec("plc1", endpoint)],
                subscription_enabled=True,
                sampling_interval_ms=50.0,
                publishing_interval_ms=100.0,
                iterator_queue_maxsize=1000,
            )
            try:
                await manager.connect("plc1")
                await manager.monitor_node_ids("plc1", (node,))
                await asyncio.sleep(1.35)
                status = manager.integrity_status("plc1")
                assert status.active_monitored_nodes == 1
                assert status.evidence_gap_count == 0
                assert status.evidence_complete_since_session_start is True
                assert manager._state("plc1").task is not None
                assert manager._state("plc1").task.done() is False
            finally:
                await manager.disconnect("plc1")

    asyncio.run(scenario())
