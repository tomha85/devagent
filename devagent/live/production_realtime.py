from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .history import (
    LiveHistoricalDiagnosis,
    LiveHistoricalSample,
    LiveHistoryCollector,
    LiveSignalTransition,
    LiveTimelineStore,
)
from .manager import PlcSessionState
from .models import RuntimeValue
from .opcua_client import (
    _is_graceful_shutdown_status,
    _node_id_text,
    _runtime_value_from_datavalue,
    _status_name,
)
from .realtime_manager import RealtimeMultiPlcConnectionManager


# OPC UA Part 4 StatusCode DataValue InfoBits:
# InfoType=DataValue (bit 10) + Overflow (bit 7).
_OPCUA_DATAVALUE_OVERFLOW_MASK = 0x00000480


@dataclass(frozen=True)
class LiveEvidenceGap:
    timestamp: datetime
    plc_id: str
    source: str
    reason: str
    node_id: str | None = None
    dropped_count: int = 1

    def render_text(self) -> str:
        node = f" node={self.node_id}" if self.node_id else ""
        return (
            f"{self.timestamp.isoformat()} source={self.source}{node} "
            f"count={self.dropped_count} reason={self.reason}"
        )


@dataclass(frozen=True)
class LiveEvidenceIntegrityStatus:
    plc_id: str
    evidence_complete_since_session_start: bool
    evidence_gap_count: int
    gap_backlog: int
    server_overflow_events: int
    local_buffer_drops: int
    replayed_events: int
    subscription_recreations: int
    desired_monitored_nodes: int
    active_monitored_nodes: int
    omitted_monitored_nodes: int
    last_sequence_number: int | None
    last_gap_at: datetime | None
    last_gap_reason: str | None


@dataclass
class _IntegrityState:
    gaps: deque[LiveEvidenceGap]
    evidence_gap_count: int = 0
    server_overflow_events: int = 0
    local_buffer_drops: int = 0
    replayed_events: int = 0
    subscription_recreations: int = 0
    desired_nodes: tuple[str, ...] = ()
    active_nodes: set[str] | None = None
    omitted_nodes: tuple[str, ...] = ()
    last_sequence_number: int | None = None
    last_gap_at: datetime | None = None
    last_gap_reason: str | None = None
    continuity_gap_open: bool = False

    def __post_init__(self) -> None:
        if self.active_nodes is None:
            self.active_nodes = set()


def _status_code_value(status: object) -> int | None:
    raw = getattr(status, "value", status)
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def status_has_monitored_item_overflow(status: object) -> bool:
    value = _status_code_value(status)
    if value is None:
        return False
    return (value & _OPCUA_DATAVALUE_OVERFLOW_MASK) == _OPCUA_DATAVALUE_OVERFLOW_MASK


class ProductionRealtimeMultiPlcConnectionManager(RealtimeMultiPlcConnectionManager):
    """Commercial read-only realtime manager with explicit evidence-integrity accounting.

    This class keeps the V1 current-state correctness model and adds the properties
    needed for defensible commissioning evidence:

    * exact desired monitored-node reconciliation instead of an add-only union;
    * asyncua iterator overflow policy DISCONNECT (supported by asyncua 2.0+);
    * explicit OPC UA MonitoredItem Overflow InfoBit detection;
    * explicit local event-buffer loss accounting before bounded deque eviction;
    * subscription recreation and connection-continuity gap accounting;
    * replay telemetry and bounded evidence-gap drain for the historical layer.

    No PLC write/control API is added.
    """

    def __init__(
        self,
        *args: Any,
        iterator_queue_maxsize: int = 20000,
        evidence_gap_maxsize: int = 5000,
        **kwargs: Any,
    ) -> None:
        if iterator_queue_maxsize < 100:
            raise ValueError("iterator_queue_maxsize must be >= 100")
        if evidence_gap_maxsize < 1:
            raise ValueError("evidence_gap_maxsize must be >= 1")
        super().__init__(*args, **kwargs)
        self.iterator_queue_maxsize = int(iterator_queue_maxsize)
        self.evidence_gap_maxsize = int(evidence_gap_maxsize)
        self._integrity: dict[str, _IntegrityState] = {
            plc_id: _IntegrityState(deque(maxlen=self.evidence_gap_maxsize))
            for plc_id in self.plc_ids
        }

    def _integrity_state(self, plc_id: str) -> _IntegrityState:
        try:
            return self._integrity[plc_id]
        except KeyError as exc:
            raise KeyError(f"Unknown PLC id: {plc_id}") from exc

    def _record_gap(
        self,
        plc_id: str,
        *,
        source: str,
        reason: str,
        node_id: str | None = None,
        dropped_count: int = 1,
    ) -> None:
        state = self._integrity_state(plc_id)
        now = datetime.now(timezone.utc)
        gap = LiveEvidenceGap(
            timestamp=now,
            plc_id=plc_id,
            source=source,
            reason=str(reason),
            node_id=node_id,
            dropped_count=max(1, int(dropped_count)),
        )
        state.gaps.append(gap)
        state.evidence_gap_count += gap.dropped_count
        state.last_gap_at = now
        state.last_gap_reason = gap.reason
        if source == "SERVER_MONITORED_ITEM_OVERFLOW":
            state.server_overflow_events += gap.dropped_count
        elif source == "LOCAL_EVENT_BUFFER_OVERFLOW":
            state.local_buffer_drops += gap.dropped_count
        elif source == "SUBSCRIPTION_RECREATED":
            state.subscription_recreations += 1

    def _invalidate_realtime(self, plc_id: str, *, reason: str | None = None) -> None:
        integrity = getattr(self, "_integrity", {}).get(plc_id)
        if (
            integrity is not None
            and reason
            and reason != "session disconnected"
            and not integrity.continuity_gap_open
        ):
            self._record_gap(
                plc_id,
                source="CONNECTION_CONTINUITY",
                reason=reason,
            )
            integrity.continuity_gap_open = True
        super()._invalidate_realtime(plc_id, reason=reason)

    async def connect(self, plc_id: str):
        status = await super().connect(plc_id)
        if status.connected:
            self._integrity_state(plc_id).continuity_gap_open = False
        return status

    def integrity_status(self, plc_id: str) -> LiveEvidenceIntegrityStatus:
        state = self._integrity_state(plc_id)
        return LiveEvidenceIntegrityStatus(
            plc_id=plc_id,
            evidence_complete_since_session_start=state.evidence_gap_count == 0,
            evidence_gap_count=state.evidence_gap_count,
            gap_backlog=len(state.gaps),
            server_overflow_events=state.server_overflow_events,
            local_buffer_drops=state.local_buffer_drops,
            replayed_events=state.replayed_events,
            subscription_recreations=state.subscription_recreations,
            desired_monitored_nodes=len(state.desired_nodes),
            active_monitored_nodes=len(state.active_nodes or ()),
            omitted_monitored_nodes=len(state.omitted_nodes),
            last_sequence_number=state.last_sequence_number,
            last_gap_at=state.last_gap_at,
            last_gap_reason=state.last_gap_reason,
        )

    def drain_evidence_gaps(
        self,
        plc_id: str,
        *,
        max_gaps: int = 5000,
    ) -> tuple[LiveEvidenceGap, ...]:
        if max_gaps < 1:
            raise ValueError("max_gaps must be >= 1")
        state = self._integrity_state(plc_id)
        result: list[LiveEvidenceGap] = []
        while state.gaps and len(result) < max_gaps:
            result.append(state.gaps.popleft())
        return tuple(result)

    def _append_subscription_event(self, plc_id: str, value: RuntimeValue) -> None:
        realtime = self._state(plc_id)
        maxlen = realtime.events.maxlen
        if maxlen is not None and len(realtime.events) >= maxlen:
            dropped = realtime.events[0]
            self._record_gap(
                plc_id,
                source="LOCAL_EVENT_BUFFER_OVERFLOW",
                reason="DevAgent bounded realtime event buffer evicted the oldest event before history consumed it",
                node_id=dropped.node_id,
            )
        realtime.events.append(value)

    async def _reconcile_monitored_node_ids(
        self,
        plc_id: str,
        node_ids: Iterable[str],
        *,
        exact: bool,
    ) -> None:
        requested = tuple(
            dict.fromkeys(str(item).strip() for item in node_ids if str(item).strip())
        )
        realtime = self._state(plc_id)
        integrity = self._integrity_state(plc_id)
        if exact:
            desired = requested
        else:
            desired = tuple(dict.fromkeys((*realtime.monitored_nodes, *requested)))

        selected = desired[: self.max_monitored_nodes]
        omitted = desired[self.max_monitored_nodes :]
        selected_set = set(selected)
        integrity.desired_nodes = desired
        integrity.omitted_nodes = omitted

        async with realtime.lock:
            before = set(realtime.monitored_nodes)
            healthy_task = realtime.task is not None and not realtime.task.done()
            realtime.monitored_nodes = selected_set
            if before == selected_set and (
                not self.subscription_enabled or healthy_task or not selected_set
            ):
                return
            realtime.generation += 1
            generation = realtime.generation
            old_task, realtime.task = realtime.task, None

        if old_task is not None:
            old_task.cancel()
            try:
                await old_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

        if not self.subscription_enabled or not selected_set:
            integrity.active_nodes = set()
            return

        async with realtime.lock:
            if generation != realtime.generation or not realtime.monitored_nodes:
                return
            realtime.task = asyncio.create_task(
                self._subscription_loop(plc_id, generation),
                name=f"devagent-live-production-realtime-{plc_id}",
            )

    async def monitor_node_ids(self, plc_id: str, node_ids: Iterable[str]) -> None:
        """Reconcile to the exact desired monitored set used by the Live session."""
        await self._reconcile_monitored_node_ids(plc_id, node_ids, exact=True)

    async def ensure_monitored_node_ids(self, plc_id: str, node_ids: Iterable[str]) -> None:
        """Add a bounded subset without removing the session's desired monitored set."""
        await self._reconcile_monitored_node_ids(plc_id, node_ids, exact=False)

    @staticmethod
    def _subscription_setup_failures(
        node_ids: tuple[str, ...],
        handles: object,
    ) -> tuple[str, ...]:
        if not isinstance(handles, list):
            return ()
        failed: list[str] = []
        for node_id, handle in zip(node_ids, handles, strict=False):
            if isinstance(handle, int):
                continue
            is_good = getattr(handle, "is_good", None)
            try:
                good = bool(is_good()) if callable(is_good) else False
            except Exception:
                good = False
            if not good:
                failed.append(node_id)
        return tuple(failed)

    async def _subscription_loop(self, plc_id: str, generation: int) -> None:
        realtime = self._state(plc_id)
        integrity = self._integrity_state(plc_id)
        while generation == realtime.generation:
            try:
                entry = self._entry(plc_id)
                outer_client = entry.client
                if outer_client is None:
                    self._invalidate_realtime(
                        plc_id, reason="subscription has no OPC UA client"
                    )
                    await asyncio.sleep(0.1)
                    continue
                if not bool(getattr(outer_client, "connected", False)):
                    self._invalidate_realtime(
                        plc_id, reason="OPC UA session is reconnecting"
                    )
                    try:
                        await outer_client.wait_until_connected(timeout_seconds=5.0)
                    except Exception:
                        await asyncio.sleep(0.1)
                    continue

                client = outer_client._require_connected()
                from asyncua.common.subscription import (
                    DataChangeEvent,
                    OverflowPolicy,
                    StatusChangeEvent,
                )

                node_ids = tuple(realtime.monitored_nodes)
                nodes = [client.get_node(node_id) for node_id in node_ids]
                async with await client.create_subscription(
                    self.publishing_interval_ms,
                    queue_maxsize=self.iterator_queue_maxsize,
                    overflow=OverflowPolicy.DISCONNECT,
                ) as subscription:
                    handles = await subscription.subscribe_data_change(
                        nodes,
                        queuesize=self.queue_size,
                        sampling_interval=self.sampling_interval_ms,
                    )
                    failures = self._subscription_setup_failures(node_ids, handles)
                    integrity.active_nodes = set(node_ids) - set(failures)
                    for node_id in failures:
                        self._record_gap(
                            plc_id,
                            source="MONITORED_ITEM_SETUP",
                            reason="OPC UA server rejected the requested monitored item",
                            node_id=node_id,
                        )
                    realtime.last_subscription_error = (
                        f"{len(failures)} monitored item(s) were rejected by the OPC UA server"
                        if failures
                        else None
                    )
                    integrity.continuity_gap_open = False
                    last_subscription_id = getattr(subscription, "subscription_id", None)

                    while generation == realtime.generation:
                        event = await subscription.next_event(timeout=1.0)

                        current_subscription_id = getattr(
                            subscription, "subscription_id", None
                        )
                        if (
                            last_subscription_id is not None
                            and current_subscription_id is not None
                            and current_subscription_id != last_subscription_id
                        ):
                            self._record_gap(
                                plc_id,
                                source="SUBSCRIPTION_RECREATED",
                                reason=(
                                    "asyncua recreated the OPC UA subscription; complete "
                                    "notification continuity could not be proven"
                                ),
                            )
                            last_subscription_id = current_subscription_id

                        sequence = getattr(subscription, "last_sequence_number", None)
                        if sequence is not None:
                            try:
                                integrity.last_sequence_number = int(sequence)
                            except (TypeError, ValueError):
                                pass

                        if event is None:
                            if not bool(getattr(outer_client, "connected", False)):
                                self._invalidate_realtime(
                                    plc_id,
                                    reason="OPC UA subscription lost connection continuity",
                                )
                                break
                            continue

                        if isinstance(event, StatusChangeEvent):
                            status = event.notification.Status
                            if event.replayed:
                                integrity.replayed_events += 1
                            if status is not None and status.is_bad():
                                reconnecting = bool(
                                    getattr(outer_client, "auto_reconnect", False)
                                ) and (
                                    _is_graceful_shutdown_status(status)
                                    or str(
                                        getattr(outer_client, "connection_state", "")
                                    ).upper()
                                    in {"CONNECTING", "DISCONNECTED", "RECONNECTING"}
                                )
                                self._invalidate_realtime(
                                    plc_id,
                                    reason=f"subscription status {_status_name(status)}",
                                )
                                if not reconnecting:
                                    raise RuntimeError(
                                        f"subscription status changed to {_status_name(status)}"
                                    )
                                break
                            continue

                        if not isinstance(event, DataChangeEvent):
                            continue

                        node_id = _node_id_text(event.node.nodeid)
                        data_value = event.data.monitored_item.Value
                        status = getattr(data_value, "StatusCode", None)
                        if status_has_monitored_item_overflow(status):
                            self._record_gap(
                                plc_id,
                                source="SERVER_MONITORED_ITEM_OVERFLOW",
                                reason=(
                                    "OPC UA MonitoredItem Overflow InfoBit indicates the "
                                    "server queue purged detected changes"
                                ),
                                node_id=node_id,
                            )
                        if event.replayed:
                            integrity.replayed_events += 1

                        value = _runtime_value_from_datavalue(
                            node_id,
                            data_value,
                            stale_after_seconds=outer_client.stale_after_seconds,
                            replayed=event.replayed,
                        )
                        self._update_cache(realtime, value, source="SUBSCRIPTION")
                        self._append_subscription_event(plc_id, value)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                integrity.active_nodes = set()
                self._invalidate_realtime(plc_id, reason=str(exc))
                await asyncio.sleep(0.1)


@dataclass(frozen=True)
class ProductionHistoricalDiagnosis(LiveHistoricalDiagnosis):
    evidence_complete: bool = True
    evidence_gaps: tuple[LiveEvidenceGap, ...] = ()

    def render_text(self) -> str:
        base = super().render_text()
        state = "COMPLETE" if self.evidence_complete else "INCOMPLETE"
        lines = [base, "", f"Evidence integrity: {state}"]
        if self.evidence_gaps:
            lines.append(f"Evidence gaps in requested window: {len(self.evidence_gaps)}")
            lines.extend(f"- {gap.render_text()}" for gap in self.evidence_gaps[:10])
            if len(self.evidence_gaps) > 10:
                lines.append(
                    f"- ... {len(self.evidence_gaps) - 10} additional evidence gap(s) omitted"
                )
        return "\n".join(lines)


class EvidenceIntegrityTimelineStore(LiveTimelineStore):
    """Timeline that preserves late events and makes evidence gaps query-visible."""

    def __init__(self, *args: Any, max_evidence_gaps: int = 5000, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if max_evidence_gaps < 1:
            raise ValueError("max_evidence_gaps must be >= 1")
        self._evidence_gaps: deque[LiveEvidenceGap] = deque(maxlen=max_evidence_gaps)

    def record_gap(self, gap: LiveEvidenceGap) -> None:
        self._evidence_gaps.append(gap)
        self._trim_integrity(self._now())

    def evidence_gaps(self) -> tuple[LiveEvidenceGap, ...]:
        self._trim_integrity(self._now())
        return tuple(self._evidence_gaps)

    def _trim_integrity(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.retention_seconds)
        while self._evidence_gaps and self._evidence_gaps[0].timestamp < cutoff:
            self._evidence_gaps.popleft()

    @staticmethod
    def _sample_identity(sample: LiveHistoricalSample) -> tuple[object, ...]:
        return (
            sample.timestamp,
            sample.plc_id,
            sample.tag_id,
            sample.node_id,
            repr(sample.value),
            sample.definitive_current,
            sample.quality,
            sample.trust,
        )

    def append(self, sample: LiveHistoricalSample) -> None:
        self.append_many((sample,))

    def append_many(self, samples: Iterable[LiveHistoricalSample]) -> None:
        incoming = tuple(samples)
        if not incoming:
            self._trim_integrity(self._now())
            return

        max_samples = self._samples.maxlen or 20000
        max_transitions = self._transitions.maxlen or 10000
        now = self._now()
        cutoff = now - timedelta(seconds=self.retention_seconds)
        merged = [
            item
            for item in (*tuple(self._samples), *incoming)
            if item.timestamp >= cutoff
        ]
        merged.sort(key=lambda item: (item.timestamp, item.tag_id, item.node_id))

        deduped: list[LiveHistoricalSample] = []
        previous_identity: tuple[object, ...] | None = None
        for item in merged:
            identity = self._sample_identity(item)
            if identity == previous_identity:
                continue
            deduped.append(item)
            previous_identity = identity
        if len(deduped) > max_samples:
            deduped = deduped[-max_samples:]

        latest: dict[str, LiveHistoricalSample] = {}
        previous_by_tag: dict[str, LiveHistoricalSample] = {}
        transitions: list[LiveSignalTransition] = []
        for item in deduped:
            previous = previous_by_tag.get(item.tag_id)
            latest[item.tag_id] = item
            if previous is not None:
                gap_seconds = (item.timestamp - previous.timestamp).total_seconds()
                continuous = 0.0 < gap_seconds <= self.continuity_seconds
                if (
                    continuous
                    and previous.definitive_current
                    and item.definitive_current
                    and previous.value != item.value
                ):
                    transitions.append(
                        LiveSignalTransition(
                            timestamp=item.timestamp,
                            plc_id=item.plc_id,
                            tag_id=item.tag_id,
                            tag_name=item.tag_name,
                            node_id=item.node_id,
                            old_value=previous.value,
                            new_value=item.value,
                        )
                    )
            # Equal timestamps with conflicting values have no defensible temporal
            # ordering, so they intentionally do not produce a transition.
            if previous is None or item.timestamp >= previous.timestamp:
                previous_by_tag[item.tag_id] = item

        self._samples = deque(deduped, maxlen=max_samples)
        self._transitions = deque(transitions[-max_transitions:], maxlen=max_transitions)
        self._latest_by_tag = latest
        self._trim_integrity(now)

    def diagnose_recent_transition(self, *args: Any, **kwargs: Any) -> ProductionHistoricalDiagnosis:
        base = super().diagnose_recent_transition(*args, **kwargs)
        current = kwargs.get("now") or self._now()
        cutoff = current - timedelta(seconds=base.lookback_seconds)
        gaps = tuple(
            gap
            for gap in self._evidence_gaps
            if cutoff <= gap.timestamp <= current
        )
        limitations = list(base.limitations)
        if gaps:
            limitations.append(
                f"Evidence integrity is INCOMPLETE: {len(gaps)} detected realtime gap(s) overlap the requested historical window."
            )
            if base.transition is None:
                limitations.append(
                    "Because the requested window contains an evidence gap, absence of a captured transition is not proof that no transition occurred."
                )
        return ProductionHistoricalDiagnosis(
            target_output=base.target_output,
            transition=base.transition,
            preceding_changes=base.preceding_changes,
            lookback_seconds=base.lookback_seconds,
            limitations=tuple(limitations),
            evidence_complete=not gaps,
            evidence_gaps=gaps,
        )


class ProductionLiveHistoryCollector(LiveHistoryCollector):
    """History collector that binds realtime integrity gaps into the retained timeline."""

    def __init__(
        self,
        manager: Any,
        reconciliation: Any,
        *,
        retention_seconds: float = 300.0,
        poll_interval_seconds: float = 1.0,
        max_tags: int = 64,
        preferred_tag_ids: Iterable[str] = (),
        store: LiveTimelineStore | None = None,
    ) -> None:
        integrity_store = store or EvidenceIntegrityTimelineStore(
            retention_seconds=retention_seconds,
            continuity_seconds=max(2.0, poll_interval_seconds * 3.0),
        )
        super().__init__(
            manager,
            reconciliation,
            retention_seconds=retention_seconds,
            poll_interval_seconds=poll_interval_seconds,
            max_tags=max_tags,
            preferred_tag_ids=preferred_tag_ids,
            store=integrity_store,
        )

    async def start(self) -> None:
        if self.active or not self._mappings:
            return
        ensure = getattr(self.manager, "ensure_monitored_node_ids", None)
        if callable(ensure):
            await ensure(self.reconciliation.plc_id, tuple(self._node_to_mapping))
        else:
            monitor = getattr(self.manager, "monitor_node_ids", None)
            if callable(monitor):
                await monitor(self.reconciliation.plc_id, tuple(self._node_to_mapping))
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(), name="devagent-live-production-history"
        )

    def _drain_integrity_gaps(self) -> None:
        drain = getattr(self.manager, "drain_evidence_gaps", None)
        record = getattr(self.store, "record_gap", None)
        if not callable(drain) or not callable(record):
            return
        for gap in drain(self.reconciliation.plc_id, max_gaps=5000):
            record(gap)

    async def _collect_once(self) -> None:
        self._drain_integrity_gaps()
        await super()._collect_once()
        self._drain_integrity_gaps()


__all__ = [
    "EvidenceIntegrityTimelineStore",
    "LiveEvidenceGap",
    "LiveEvidenceIntegrityStatus",
    "ProductionHistoricalDiagnosis",
    "ProductionLiveHistoryCollector",
    "ProductionRealtimeMultiPlcConnectionManager",
    "status_has_monitored_item_overflow",
]
