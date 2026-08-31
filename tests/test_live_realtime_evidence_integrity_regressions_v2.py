from __future__ import annotations

import asyncio
import socket
from datetime import datetime, timedelta, timezone
from types import MethodType, SimpleNamespace

import pytest

from devagent.live.manager import PlcConnectionSpec, PlcReadResult, PlcSessionState
from devagent.live.models import Quality, RuntimeValue
from devagent.live.production_realtime import (
    EvidenceIntegrityTimelineStore,
    LiveEvidenceGap,
    ProductionLiveHistoryCollector,
    ProductionRealtimeMultiPlcConnectionManager,
)
from devagent.live.simulator import OpcUaSimulator


def _manager(*, subscription_enabled: bool = False, **kwargs) -> ProductionRealtimeMultiPlcConnectionManager:
    return ProductionRealtimeMultiPlcConnectionManager(
        [PlcConnectionSpec("plc1", "opc.tcp://plc1:4840/")],
        subscription_enabled=subscription_enabled,
        **kwargs,
    )


def _runtime(node_id: str, value: object, stamp: datetime) -> RuntimeValue:
    return RuntimeValue(
        node_id=node_id,
        value=value,
        variant_type="Boolean" if isinstance(value, bool) else "Int32",
        status_code="Good",
        quality=Quality.GOOD,
        source_timestamp=stamp,
        server_timestamp=stamp,
        received_at=stamp,
        age_seconds=0.0,
        stale=False,
        replayed=False,
    )


def _context():
    class Context:
        @staticmethod
        def unique_tag_for_reference(reference: str):
            return type("Tag", (), {"id": "tag-1"})()

    return Context()


def test_question_read_many_is_additive_and_does_not_replace_history_scope() -> None:
    async def scenario() -> None:
        manager = _manager(max_monitored_nodes=8)
        await manager.replace_monitored_node_ids("plc1", ("A", "B", "C"))

        async def fake_batch(self, plc_id: str, node_ids: tuple[str, ...]):
            return PlcReadResult(
                plc_id=plc_id,
                values=(),
                state=PlcSessionState.CONNECTED,
            )

        manager._batch_read_isolated = MethodType(fake_batch, manager)
        await manager.read_many({"plc1": ("B", "D")})
        assert manager._integrity_state("plc1").desired_nodes == ("A", "B", "C", "D")
        assert manager._state("plc1").monitored_nodes == {"A", "B", "C", "D"}

    asyncio.run(scenario())


def test_open_connection_gap_marks_every_overlapping_later_window_incomplete_until_closed() -> None:
    store = EvidenceIntegrityTimelineStore(retention_seconds=120.0)
    now = datetime.now(timezone.utc)
    start = now - timedelta(seconds=30)
    gap = LiveEvidenceGap(
        timestamp=start,
        end_timestamp=None,
        plc_id="plc1",
        source="CONNECTION_CONTINUITY",
        reason="transport lost",
    )
    store.sync_open_gaps((gap,), observed_at=now)
    diagnosis = store.diagnose_recent_transition(
        _context(), "RunCmd", lookback_seconds=2.0, now=now
    )
    assert diagnosis.evidence_complete is False

    store.sync_open_gaps((), observed_at=now + timedelta(seconds=1))
    later = now + timedelta(seconds=10)
    diagnosis = store.diagnose_recent_transition(
        _context(), "RunCmd", lookback_seconds=2.0, now=later
    )
    assert diagnosis.evidence_complete is True


def test_omitted_monitored_nodes_open_capacity_gap_and_never_claim_complete() -> None:
    async def scenario() -> None:
        manager = _manager(max_monitored_nodes=2)
        await manager.replace_monitored_node_ids("plc1", ("A", "B", "C"))
        status = manager.integrity_status("plc1")
        assert status.omitted_monitored_nodes == 1
        assert status.monitored_coverage_complete is False
        assert status.evidence_complete_since_session_start is False
        assert any(
            gap.source == "MONITOR_CAPACITY"
            for gap in manager.open_evidence_gaps("plc1")
        )

    asyncio.run(scenario())


def test_bounded_closed_gap_metadata_never_evicts_authoritative_open_gap() -> None:
    manager = _manager(evidence_gap_maxsize=16)
    now = datetime.now(timezone.utc)
    for index in range(24):
        stamp = now + timedelta(milliseconds=index)
        manager._record_gap(
            "plc1",
            source="TEST_CLOSED",
            reason=f"closed {index}",
            timestamp=stamp,
            end_timestamp=stamp,
        )
    manager._invalidate_realtime("plc1", reason="transport lost")
    status = manager.integrity_status("plc1")
    assert status.gap_metadata_overflows > 0
    assert status.evidence_complete_since_session_start is False
    assert len(manager.open_evidence_gaps("plc1")) == 1
    assert manager.open_evidence_gaps("plc1")[0].source == "CONNECTION_CONTINUITY"


def test_server_overflow_gap_spans_previous_defensible_observation_to_retained_value() -> None:
    manager = _manager()
    now = datetime.now(timezone.utc)
    previous = now - timedelta(seconds=2)
    manager._integrity_state("plc1").last_observed_at_by_node["n1"] = previous
    manager._record_gap(
        "plc1",
        source="SERVER_MONITORED_ITEM_OVERFLOW",
        reason="server queue purged changes",
        node_id="n1",
        timestamp=now,
    )
    gaps = manager.drain_evidence_gaps("plc1")
    assert len(gaps) == 1
    assert gaps[0].timestamp == previous
    assert gaps[0].end_timestamp == now


def _sample(value: bool, timestamp: datetime):
    from devagent.live.history import LiveHistoricalSample

    return LiveHistoricalSample(
        timestamp=timestamp,
        plc_id="plc1",
        tag_id="tag-1",
        tag_name="RunCmd",
        node_id="n1",
        value=value,
        definitive_current=True,
        quality="GOOD",
        trust="CURRENT",
    )


def test_timeline_sample_and_transition_capacity_eviction_are_explicit_gaps() -> None:
    store = EvidenceIntegrityTimelineStore(
        retention_seconds=120.0,
        max_samples=10,
        max_transitions=5,
        continuity_seconds=5.0,
    )
    now = datetime.now(timezone.utc)
    samples = tuple(
        _sample(bool(index % 2), now - timedelta(milliseconds=30 - index))
        for index in range(30)
    )
    store.append_many(samples)
    sources = {gap.source for gap in store.evidence_gaps()}
    assert "TIMELINE_SAMPLE_CAPACITY" in sources
    assert "TIMELINE_TRANSITION_CAPACITY" in sources
    diagnosis = store.diagnose_recent_transition(
        _context(), "RunCmd", lookback_seconds=1.0, now=now
    )
    assert diagnosis.evidence_complete is False


def test_pending_manager_gap_is_synchronized_immediately_before_history_diagnosis() -> None:
    manager = _manager()

    class Reconciliation:
        plc_id = "plc1"

        @staticmethod
        def accepted_mappings():
            return ()

    collector = ProductionLiveHistoryCollector(
        manager,
        Reconciliation(),
        retention_seconds=60.0,
    )
    now = datetime.now(timezone.utc)
    manager._record_gap(
        "plc1",
        source="LOCAL_EVENT_BUFFER_OVERFLOW",
        reason="history consumer fell behind",
        node_id="n1",
        timestamp=now - timedelta(seconds=1),
    )
    assert collector.store.evidence_gaps() == ()
    collector.sync_integrity_gaps()
    diagnosis = collector.store.diagnose_recent_transition(
        _context(), "RunCmd", lookback_seconds=5.0, now=now
    )
    assert diagnosis.evidence_complete is False
    assert any(gap.source == "LOCAL_EVENT_BUFFER_OVERFLOW" for gap in diagnosis.evidence_gaps)


def test_monitored_item_setup_open_gap_closes_without_losing_interval_identity() -> None:
    manager = _manager()
    manager._sync_setup_gaps("plc1", desired_nodes=("A",), failures=("A",))
    open_gaps = manager.open_evidence_gaps("plc1")
    assert len(open_gaps) == 1
    start = open_gaps[0].timestamp
    manager._sync_setup_gaps("plc1", desired_nodes=("A",), failures=())
    assert manager.open_evidence_gaps("plc1") == ()
    closed = manager.drain_evidence_gaps("plc1")
    assert len(closed) == 1
    assert closed[0].timestamp == start
    assert closed[0].end_timestamp is not None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_real_asyncua_idle_subscription_stays_healthy_past_one_second() -> None:
    pytest.importorskip("asyncua")

    async def scenario() -> None:
        port = _free_port()
        endpoint = f"opc.tcp://127.0.0.1:{port}/devagent/integrity-idle/"
        async with OpcUaSimulator(
            endpoint=endpoint,
            scenario="healthy",
            update_interval_seconds=0.20,
        ) as simulator:
            assert simulator.node_ids is not None
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
                await manager.replace_monitored_node_ids("plc1", (node,))
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
