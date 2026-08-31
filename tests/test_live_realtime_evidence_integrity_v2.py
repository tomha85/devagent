from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from devagent.live.manager import PlcConnectionSpec
from devagent.live.models import Quality, RuntimeValue
from devagent.live.production_realtime import (
    EvidenceIntegrityTimelineStore,
    LiveEvidenceGap,
    ProductionHistoricalDiagnosis,
    ProductionRealtimeMultiPlcConnectionManager,
    status_has_monitored_item_overflow,
)


def _runtime(
    node_id: str,
    value: object,
    *,
    stamp: datetime | None = None,
    received_at: datetime | None = None,
) -> RuntimeValue:
    source = stamp or datetime.now(timezone.utc)
    received = received_at or source
    return RuntimeValue(
        node_id=node_id,
        value=value,
        variant_type="Boolean" if isinstance(value, bool) else "Int32",
        status_code="Good",
        quality=Quality.GOOD,
        source_timestamp=source,
        server_timestamp=source,
        received_at=received,
        age_seconds=max(0.0, (received - source).total_seconds()),
        stale=False,
        replayed=False,
    )


def _manager(**kwargs) -> ProductionRealtimeMultiPlcConnectionManager:
    return ProductionRealtimeMultiPlcConnectionManager(
        [PlcConnectionSpec("plc1", "opc.tcp://plc1:4840/")],
        subscription_enabled=False,
        **kwargs,
    )


def test_opcua_monitored_item_overflow_infobit_is_detected() -> None:
    assert status_has_monitored_item_overflow(0x00000480) is True
    assert status_has_monitored_item_overflow(SimpleNamespace(value=0x00000480)) is True
    assert status_has_monitored_item_overflow(0x00000400) is False
    assert status_has_monitored_item_overflow(0x00000080) is False
    assert status_has_monitored_item_overflow(0) is False


def test_local_event_buffer_eviction_is_never_silent() -> None:
    manager = _manager()
    state = manager._state("plc1")
    state.events = deque(maxlen=2)
    now = datetime.now(timezone.utc)
    manager._append_subscription_event("plc1", _runtime("n1", 1, stamp=now))
    manager._append_subscription_event(
        "plc1", _runtime("n1", 2, stamp=now + timedelta(milliseconds=1))
    )
    manager._append_subscription_event(
        "plc1", _runtime("n1", 3, stamp=now + timedelta(milliseconds=2))
    )

    assert [item.value for item in state.events] == [2, 3]
    status = manager.integrity_status("plc1")
    assert status.evidence_complete_since_session_start is False
    assert status.local_buffer_drops == 1
    assert status.evidence_gap_count == 1
    gaps = manager.drain_evidence_gaps("plc1")
    assert len(gaps) == 1
    assert gaps[0].source == "LOCAL_EVENT_BUFFER_OVERFLOW"
    assert gaps[0].node_id == "n1"


def test_connection_continuity_gap_is_deduplicated_until_recovered() -> None:
    manager = _manager()
    manager._invalidate_realtime("plc1", reason="transport lost")
    manager._invalidate_realtime("plc1", reason="transport still lost")
    status = manager.integrity_status("plc1")
    assert status.evidence_gap_count == 1
    assert status.gap_backlog == 1


def test_exact_monitored_set_reconciliation_removes_stale_nodes() -> None:
    async def scenario() -> None:
        manager = _manager(max_monitored_nodes=4)
        await manager.monitor_node_ids("plc1", ("A", "B", "C"))
        assert manager._state("plc1").monitored_nodes == {"A", "B", "C"}

        await manager.monitor_node_ids("plc1", ("B", "C", "D"))
        assert manager._state("plc1").monitored_nodes == {"B", "C", "D"}
        assert "A" not in manager._state("plc1").monitored_nodes
        status = manager.integrity_status("plc1")
        assert status.desired_monitored_nodes == 3
        assert status.omitted_monitored_nodes == 0

    asyncio.run(scenario())


def test_monitored_set_limit_is_explicit_not_silent() -> None:
    async def scenario() -> None:
        manager = _manager(max_monitored_nodes=2)
        await manager.monitor_node_ids("plc1", ("A", "B", "C", "D"))
        assert manager._state("plc1").monitored_nodes == {"A", "B"}
        status = manager.integrity_status("plc1")
        assert status.desired_monitored_nodes == 4
        assert status.omitted_monitored_nodes == 2

    asyncio.run(scenario())


def _sample(
    tag_id: str,
    value: object,
    timestamp: datetime,
    *,
    tag_name: str = "RunCmd",
    node_id: str = "n1",
):
    from devagent.live.history import LiveHistoricalSample

    return LiveHistoricalSample(
        timestamp=timestamp,
        plc_id="plc1",
        tag_id=tag_id,
        tag_name=tag_name,
        node_id=node_id,
        value=value,
        definitive_current=True,
        quality="GOOD",
        trust="CURRENT",
    )


def test_late_cross_cycle_event_is_preserved_and_transitions_are_recomputed() -> None:
    store = EvidenceIntegrityTimelineStore(
        retention_seconds=60.0,
        continuity_seconds=5.0,
    )
    now = datetime.now(timezone.utc)
    t0 = now - timedelta(seconds=3)
    t1 = now - timedelta(seconds=2)
    t2 = now - timedelta(seconds=1)

    store.append_many((_sample("tag-1", False, t0), _sample("tag-1", False, t2)))
    assert store.transitions() == ()

    store.append(_sample("tag-1", True, t1))
    transitions = store.transitions()
    assert len(transitions) == 2
    assert transitions[0].timestamp == t1
    assert transitions[0].old_value is False
    assert transitions[0].new_value is True
    assert transitions[1].timestamp == t2
    assert transitions[1].old_value is True
    assert transitions[1].new_value is False
    assert store.latest_samples()[0].timestamp == t2
    assert store.latest_samples()[0].value is False


def test_equal_timestamp_conflict_does_not_invent_temporal_transition() -> None:
    store = EvidenceIntegrityTimelineStore(retention_seconds=60.0)
    now = datetime.now(timezone.utc)
    stamp = now - timedelta(seconds=1)
    store.append_many(
        (
            _sample("tag-1", False, stamp),
            _sample("tag-1", True, stamp),
        )
    )
    assert store.transitions() == ()


def test_historical_diagnosis_marks_overlapping_evidence_gap_incomplete() -> None:
    store = EvidenceIntegrityTimelineStore(retention_seconds=60.0)
    now = datetime.now(timezone.utc)
    store.append_many(
        (
            _sample("tag-1", False, now - timedelta(seconds=4)),
            _sample("tag-1", True, now - timedelta(seconds=3)),
        )
    )
    gap_time = now - timedelta(seconds=2)
    store.record_gap(
        LiveEvidenceGap(
            timestamp=gap_time,
            end_timestamp=gap_time,
            plc_id="plc1",
            source="SERVER_MONITORED_ITEM_OVERFLOW",
            reason="server queue purged changes",
            node_id="n1",
        )
    )
    context = SimpleNamespace(
        unique_tag_for_reference=lambda reference: SimpleNamespace(id="tag-1")
    )

    diagnosis = store.diagnose_recent_transition(
        context,
        "RunCmd",
        lookback_seconds=10.0,
        now=now,
    )
    assert isinstance(diagnosis, ProductionHistoricalDiagnosis)
    assert diagnosis.transition is not None
    assert diagnosis.evidence_complete is False
    assert len(diagnosis.evidence_gaps) == 1
    assert "Evidence integrity: INCOMPLETE" in diagnosis.render_text()


def test_absence_of_transition_is_not_proof_when_window_has_gap() -> None:
    store = EvidenceIntegrityTimelineStore(retention_seconds=60.0)
    now = datetime.now(timezone.utc)
    gap_time = now - timedelta(seconds=1)
    store.record_gap(
        LiveEvidenceGap(
            timestamp=gap_time,
            end_timestamp=gap_time,
            plc_id="plc1",
            source="CONNECTION_CONTINUITY",
            reason="session reconnect",
        )
    )
    context = SimpleNamespace(
        unique_tag_for_reference=lambda reference: SimpleNamespace(id="tag-1")
    )
    diagnosis = store.diagnose_recent_transition(
        context,
        "RunCmd",
        lookback_seconds=10.0,
        now=now,
    )
    assert diagnosis.transition is None
    assert diagnosis.evidence_complete is False
    assert any("absence" in item.casefold() for item in diagnosis.limitations)


def test_gap_outside_requested_window_does_not_poison_current_window() -> None:
    store = EvidenceIntegrityTimelineStore(retention_seconds=120.0)
    now = datetime.now(timezone.utc)
    gap_time = now - timedelta(seconds=90)
    store.record_gap(
        LiveEvidenceGap(
            timestamp=gap_time,
            end_timestamp=gap_time,
            plc_id="plc1",
            source="CONNECTION_CONTINUITY",
            reason="old reconnect",
        )
    )
    store.append_many(
        (
            _sample("tag-1", False, now - timedelta(seconds=4)),
            _sample("tag-1", True, now - timedelta(seconds=3)),
        )
    )
    context = SimpleNamespace(
        unique_tag_for_reference=lambda reference: SimpleNamespace(id="tag-1")
    )
    diagnosis = store.diagnose_recent_transition(
        context,
        "RunCmd",
        lookback_seconds=10.0,
        now=now,
    )
    assert diagnosis.evidence_complete is True
    assert diagnosis.evidence_gaps == ()


def test_timeline_stress_stays_bounded_and_keeps_latest_truth() -> None:
    store = EvidenceIntegrityTimelineStore(
        retention_seconds=300.0,
        max_samples=500,
        max_transitions=300,
        continuity_seconds=5.0,
    )
    now = datetime.now(timezone.utc)
    samples = [
        _sample(
            "tag-1",
            bool(index % 2),
            now - timedelta(milliseconds=1000 - index),
        )
        for index in range(1000)
    ]
    store.append_many(reversed(samples))
    assert len(store._samples) <= 500
    assert len(store.transitions()) <= 300
    latest = store.latest_samples()[0]
    assert latest.timestamp == max(item.timestamp for item in samples)


def test_production_cli_installs_integrity_runtime_without_rewriting_v1_modules() -> None:
    from devagent.live import assist_cli
    from devagent.live import assist_production_cli
    from devagent.live import recursive_assistant
    from devagent.live.history import LiveHistoryCollector
    from devagent.live.realtime_manager import RealtimeMultiPlcConnectionManager
    from devagent.live.production_realtime import ProductionLiveHistoryCollector

    old_manager = assist_cli.RealtimeMultiPlcConnectionManager
    old_history = recursive_assistant.LiveHistoryCollector
    old_print_status = assist_cli._print_status
    try:
        assist_production_cli._install_production_runtime()
        assert (
            assist_cli.RealtimeMultiPlcConnectionManager
            is ProductionRealtimeMultiPlcConnectionManager
        )
        assert recursive_assistant.LiveHistoryCollector is ProductionLiveHistoryCollector
    finally:
        assist_cli.RealtimeMultiPlcConnectionManager = old_manager
        recursive_assistant.LiveHistoryCollector = old_history
        assist_cli._print_status = old_print_status

    assert RealtimeMultiPlcConnectionManager is not ProductionRealtimeMultiPlcConnectionManager
    assert LiveHistoryCollector is not ProductionLiveHistoryCollector
