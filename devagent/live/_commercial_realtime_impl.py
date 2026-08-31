from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import groupby
from typing import Any, Iterable, Mapping

from .history import (
    LiveHistoricalDiagnosis,
    LiveHistoricalSample,
    LiveHistoryCollector,
    LiveSignalTransition,
    LiveTimelineStore,
)
from .manager import PlcReadResult, PlcSessionState
from .models import RuntimeValue
from .opcua_client import (
    _is_graceful_shutdown_status,
    _node_id_text,
    _runtime_value_from_datavalue,
    _status_name,
)
from .realtime_manager import RealtimeMultiPlcConnectionManager


_OPCUA_DATAVALUE_OVERFLOW_MASK = 0x00000480


@dataclass(frozen=True)
class LiveEvidenceGap:
    timestamp: datetime
    plc_id: str
    source: str
    reason: str
    node_id: str | None = None
    dropped_count: int = 1
    end_timestamp: datetime | None = None

    def overlaps(self, start: datetime, end: datetime) -> bool:
        effective_end = self.end_timestamp if self.end_timestamp is not None else end
        return self.timestamp <= end and effective_end >= start

    def key(self) -> tuple[object, ...]:
        return (self.plc_id, self.source, self.node_id, self.timestamp)

    def render_text(self) -> str:
        node = f" node={self.node_id}" if self.node_id else ""
        if self.end_timestamp is None:
            span = "open"
        elif self.end_timestamp == self.timestamp:
            span = "instant"
        else:
            span = f"end={self.end_timestamp.isoformat()}"
        return (
            f"{self.timestamp.isoformat()} source={self.source}{node} "
            f"count={self.dropped_count} span={span} reason={self.reason}"
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
    gap_metadata_overflows: int = 0
    continuous_monitoring_enabled: bool = True
    monitored_coverage_complete: bool = True


@dataclass
class _IntegrityState:
    closed_gaps: deque[LiveEvidenceGap]
    open_gaps: dict[tuple[object, ...], LiveEvidenceGap] = field(default_factory=dict)
    evidence_gap_count: int = 0
    server_overflow_events: int = 0
    local_buffer_drops: int = 0
    replayed_events: int = 0
    subscription_recreations: int = 0
    gap_metadata_overflows: int = 0
    desired_nodes: tuple[str, ...] = ()
    active_nodes: set[str] = field(default_factory=set)
    omitted_nodes: tuple[str, ...] = ()
    last_sequence_number: int | None = None
    last_gap_at: datetime | None = None
    last_gap_reason: str | None = None
    continuity_gap_started_at: datetime | None = None
    continuity_gap_reason: str | None = None
    capacity_gap_started_at: datetime | None = None
    monitoring_disabled_gap_started_at: datetime | None = None
    reconfiguration_gap_started_at: datetime | None = None
    setup_gap_starts: dict[str, datetime] = field(default_factory=dict)
    last_observed_at_by_node: dict[str, datetime] = field(default_factory=dict)
    monitoring_started_at: datetime | None = None
    last_subscription_event_at: datetime | None = None


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


def _value_timestamp(value: RuntimeValue) -> datetime:
    return value.source_timestamp or value.server_timestamp or value.received_at


def _closed_span(gaps: Iterable[LiveEvidenceGap], *, plc_id: str) -> LiveEvidenceGap:
    items = tuple(gaps)
    start = min(item.timestamp for item in items)
    ends = tuple(item.end_timestamp or item.timestamp for item in items)
    return LiveEvidenceGap(
        timestamp=start,
        end_timestamp=max(ends),
        plc_id=plc_id,
        source="EVIDENCE_GAP_METADATA_OVERFLOW",
        reason=(
            "Detailed evidence-gap metadata exceeded its bounded store; the coalesced "
            "closed interval remains conservatively incomplete"
        ),
        dropped_count=0,
    )


class ProductionRealtimeMultiPlcConnectionManager(RealtimeMultiPlcConnectionManager):
    """Commercial read-only realtime manager with defensible evidence integrity."""

    def __init__(
        self,
        *args: Any,
        iterator_queue_maxsize: int = 20000,
        evidence_gap_maxsize: int = 5000,
        **kwargs: Any,
    ) -> None:
        if iterator_queue_maxsize < 100:
            raise ValueError("iterator_queue_maxsize must be >= 100")
        if evidence_gap_maxsize < 16:
            raise ValueError("evidence_gap_maxsize must be >= 16")
        super().__init__(*args, **kwargs)
        self.iterator_queue_maxsize = int(iterator_queue_maxsize)
        self.evidence_gap_maxsize = int(evidence_gap_maxsize)
        self._integrity: dict[str, _IntegrityState] = {
            plc_id: _IntegrityState(deque(maxlen=self.evidence_gap_maxsize))
            for plc_id in self.plc_ids
        }

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.now(timezone.utc)

    def _integrity_state(self, plc_id: str) -> _IntegrityState:
        try:
            return self._integrity[plc_id]
        except KeyError as exc:
            raise KeyError(f"Unknown PLC id: {plc_id}") from exc

    def _append_closed_gap(self, state: _IntegrityState, gap: LiveEvidenceGap) -> None:
        maxlen = state.closed_gaps.maxlen
        if maxlen is not None and len(state.closed_gaps) >= maxlen:
            items = list(state.closed_gaps)
            if len(items) >= 2:
                compacted = _closed_span(items[:2], plc_id=gap.plc_id)
                items = [compacted, *items[2:]]
                state.closed_gaps = deque(items, maxlen=maxlen)
                state.gap_metadata_overflows += 1
            else:  # defensive; constructor guarantees a materially larger bound
                state.closed_gaps.clear()
                state.gap_metadata_overflows += 1
        state.closed_gaps.append(gap)

    def _record_gap(
        self,
        plc_id: str,
        *,
        source: str,
        reason: str,
        node_id: str | None = None,
        dropped_count: int = 1,
        timestamp: datetime | None = None,
        end_timestamp: datetime | None = None,
        open_interval: bool = False,
        count_toward_total: bool = True,
    ) -> LiveEvidenceGap:
        state = self._integrity_state(plc_id)
        stamp = timestamp or self._now_utc()
        if source == "SERVER_MONITORED_ITEM_OVERFLOW" and node_id is not None:
            retained_at = stamp
            previous = state.last_observed_at_by_node.get(node_id)
            if previous is not None and previous <= retained_at:
                stamp = previous
            elif state.monitoring_started_at is not None and state.monitoring_started_at <= retained_at:
                stamp = state.monitoring_started_at
            end_timestamp = retained_at
            open_interval = False
        if not open_interval and end_timestamp is None:
            end_timestamp = stamp
        gap = LiveEvidenceGap(
            timestamp=stamp,
            end_timestamp=end_timestamp,
            plc_id=plc_id,
            source=source,
            reason=str(reason),
            node_id=node_id,
            dropped_count=max(0, int(dropped_count)),
        )
        key = gap.key()
        if open_interval:
            state.open_gaps[key] = gap
        else:
            state.open_gaps.pop(key, None)
            self._append_closed_gap(state, gap)
        if count_toward_total:
            state.evidence_gap_count += max(1, gap.dropped_count)
        state.last_gap_at = end_timestamp or stamp
        state.last_gap_reason = gap.reason
        if count_toward_total and source == "SERVER_MONITORED_ITEM_OVERFLOW":
            state.server_overflow_events += max(1, gap.dropped_count)
        elif count_toward_total and source == "LOCAL_EVENT_BUFFER_OVERFLOW":
            state.local_buffer_drops += max(1, gap.dropped_count)
        elif count_toward_total and source == "SUBSCRIPTION_RECREATED":
            state.subscription_recreations += 1
        return gap

    def _open_continuity_gap(self, plc_id: str, reason: str) -> None:
        state = self._integrity_state(plc_id)
        if state.continuity_gap_started_at is not None:
            return
        start = self._now_utc()
        state.continuity_gap_started_at = start
        state.continuity_gap_reason = reason
        self._record_gap(
            plc_id,
            source="CONNECTION_CONTINUITY",
            reason=reason,
            timestamp=start,
            open_interval=True,
        )

    def _close_continuity_gap(self, plc_id: str) -> None:
        state = self._integrity_state(plc_id)
        start = state.continuity_gap_started_at
        if start is None:
            return
        reason = state.continuity_gap_reason or "OPC UA connection continuity gap"
        state.continuity_gap_started_at = None
        state.continuity_gap_reason = None
        self._record_gap(
            plc_id,
            source="CONNECTION_CONTINUITY",
            reason=reason,
            timestamp=start,
            end_timestamp=self._now_utc(),
            count_toward_total=False,
        )

    def _open_reconfiguration_gap(self, plc_id: str) -> None:
        state = self._integrity_state(plc_id)
        if state.reconfiguration_gap_started_at is not None:
            return
        start = self._now_utc()
        state.reconfiguration_gap_started_at = start
        self._record_gap(
            plc_id,
            source="MONITOR_SET_RECONFIGURATION",
            reason=(
                "the active OPC UA monitored set is being replaced; notification continuity "
                "during the reconfiguration interval cannot be proven"
            ),
            timestamp=start,
            open_interval=True,
        )

    def _close_reconfiguration_gap(self, plc_id: str) -> None:
        state = self._integrity_state(plc_id)
        start = state.reconfiguration_gap_started_at
        if start is None:
            return
        state.reconfiguration_gap_started_at = None
        self._record_gap(
            plc_id,
            source="MONITOR_SET_RECONFIGURATION",
            reason="the replacement monitored set became active",
            timestamp=start,
            end_timestamp=self._now_utc(),
            count_toward_total=False,
        )

    def _set_capacity_gap(self, plc_id: str, omitted: tuple[str, ...]) -> None:
        state = self._integrity_state(plc_id)
        if omitted and state.capacity_gap_started_at is None:
            start = self._now_utc()
            state.capacity_gap_started_at = start
            self._record_gap(
                plc_id,
                source="MONITOR_CAPACITY",
                reason=(
                    f"{len(omitted)} desired node(s) exceeded the configured continuous-monitor limit"
                ),
                timestamp=start,
                open_interval=True,
            )
        elif not omitted and state.capacity_gap_started_at is not None:
            start = state.capacity_gap_started_at
            state.capacity_gap_started_at = None
            self._record_gap(
                plc_id,
                source="MONITOR_CAPACITY",
                reason="continuous-monitor capacity coverage was restored",
                timestamp=start,
                end_timestamp=self._now_utc(),
                count_toward_total=False,
            )

    def _set_monitoring_disabled_gap(self, plc_id: str, has_desired_nodes: bool) -> None:
        state = self._integrity_state(plc_id)
        disabled = has_desired_nodes and not self.subscription_enabled
        if disabled and state.monitoring_disabled_gap_started_at is None:
            start = self._now_utc()
            state.monitoring_disabled_gap_started_at = start
            self._record_gap(
                plc_id,
                source="CONTINUOUS_MONITORING_DISABLED",
                reason=(
                    "continuous OPC UA monitoring is disabled; polling cannot prove all transient changes"
                ),
                timestamp=start,
                open_interval=True,
            )
        elif not disabled and state.monitoring_disabled_gap_started_at is not None:
            start = state.monitoring_disabled_gap_started_at
            state.monitoring_disabled_gap_started_at = None
            self._record_gap(
                plc_id,
                source="CONTINUOUS_MONITORING_DISABLED",
                reason="continuous monitoring coverage was restored",
                timestamp=start,
                end_timestamp=self._now_utc(),
                count_toward_total=False,
            )

    def _sync_setup_gaps(
        self,
        plc_id: str,
        *,
        desired_nodes: tuple[str, ...],
        failures: tuple[str, ...],
    ) -> None:
        state = self._integrity_state(plc_id)
        now = self._now_utc()
        failure_set = set(failures)
        desired_set = set(desired_nodes)
        for node_id in failures:
            if node_id in state.setup_gap_starts:
                continue
            state.setup_gap_starts[node_id] = now
            self._record_gap(
                plc_id,
                source="MONITORED_ITEM_SETUP",
                reason="OPC UA server rejected the requested monitored item",
                node_id=node_id,
                timestamp=now,
                open_interval=True,
            )
        resolved = [
            node_id
            for node_id in state.setup_gap_starts
            if node_id not in failure_set or node_id not in desired_set
        ]
        for node_id in resolved:
            start = state.setup_gap_starts.pop(node_id)
            self._record_gap(
                plc_id,
                source="MONITORED_ITEM_SETUP",
                reason="monitored-item coverage was restored or node left the desired set",
                node_id=node_id,
                timestamp=start,
                end_timestamp=now,
                count_toward_total=False,
            )

    def _invalidate_realtime(self, plc_id: str, *, reason: str | None = None) -> None:
        if (
            getattr(self, "_integrity", None) is not None
            and plc_id in self._integrity
            and reason
            and reason != "session disconnected"
        ):
            self._open_continuity_gap(plc_id, reason)
        super()._invalidate_realtime(plc_id, reason=reason)

    async def connect(self, plc_id: str):
        status = await super().connect(plc_id)
        if status.connected:
            self._close_continuity_gap(plc_id)
        return status

    def open_evidence_gaps(self, plc_id: str) -> tuple[LiveEvidenceGap, ...]:
        return tuple(self._integrity_state(plc_id).open_gaps.values())

    def integrity_status(self, plc_id: str) -> LiveEvidenceIntegrityStatus:
        state = self._integrity_state(plc_id)
        selected = set(state.desired_nodes[: self.max_monitored_nodes])
        if self.subscription_enabled:
            coverage_complete = not state.omitted_nodes and selected == state.active_nodes
        else:
            coverage_complete = not state.desired_nodes
        complete = state.evidence_gap_count == 0 and coverage_complete
        return LiveEvidenceIntegrityStatus(
            plc_id=plc_id,
            evidence_complete_since_session_start=complete,
            evidence_gap_count=state.evidence_gap_count,
            gap_backlog=len(state.closed_gaps) + len(state.open_gaps),
            server_overflow_events=state.server_overflow_events,
            local_buffer_drops=state.local_buffer_drops,
            replayed_events=state.replayed_events,
            subscription_recreations=state.subscription_recreations,
            desired_monitored_nodes=len(state.desired_nodes),
            active_monitored_nodes=len(state.active_nodes),
            omitted_monitored_nodes=len(state.omitted_nodes),
            last_sequence_number=state.last_sequence_number,
            last_gap_at=state.last_gap_at,
            last_gap_reason=state.last_gap_reason,
            gap_metadata_overflows=state.gap_metadata_overflows,
            continuous_monitoring_enabled=self.subscription_enabled,
            monitored_coverage_complete=coverage_complete,
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
        while state.closed_gaps and len(result) < max_gaps:
            result.append(state.closed_gaps.popleft())
        return tuple(result)

    def _append_subscription_event(self, plc_id: str, value: RuntimeValue) -> None:
        realtime = self._state(plc_id)
        maxlen = realtime.events.maxlen
        if maxlen is not None and len(realtime.events) >= maxlen:
            dropped = realtime.events[0]
            self._record_gap(
                plc_id,
                source="LOCAL_EVENT_BUFFER_OVERFLOW",
                reason=(
                    "DevAgent bounded realtime event buffer evicted the oldest event before history consumed it"
                ),
                node_id=dropped.node_id,
                timestamp=_value_timestamp(dropped),
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
        desired = requested if exact else tuple(
            dict.fromkeys((*integrity.desired_nodes, *requested))
        )
        selected = desired[: self.max_monitored_nodes]
        omitted = desired[self.max_monitored_nodes :]
        selected_set = set(selected)
        integrity.desired_nodes = desired
        integrity.omitted_nodes = omitted
        self._set_capacity_gap(plc_id, omitted)
        self._set_monitoring_disabled_gap(plc_id, bool(selected_set))
        self._sync_setup_gaps(
            plc_id,
            desired_nodes=selected,
            failures=tuple(
                node_id
                for node_id in integrity.setup_gap_starts
                if node_id in selected_set
            ),
        )

        async with realtime.lock:
            before = set(realtime.monitored_nodes)
            healthy_task = realtime.task is not None and not realtime.task.done()
            should_record_reconfiguration = (
                self.subscription_enabled
                and healthy_task
                and bool(before)
                and before != selected_set
            )
            realtime.monitored_nodes = selected_set
            if before == selected_set and (
                not self.subscription_enabled or healthy_task or not selected_set
            ):
                return
            realtime.generation += 1
            generation = realtime.generation
            old_task, realtime.task = realtime.task, None
            integrity.active_nodes.clear()

        if should_record_reconfiguration:
            self._open_reconfiguration_gap(plc_id)
        if old_task is not None:
            old_task.cancel()
            try:
                await old_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if not self.subscription_enabled or not selected_set:
            if not selected_set:
                self._close_reconfiguration_gap(plc_id)
            return
        async with realtime.lock:
            if generation != realtime.generation or not realtime.monitored_nodes:
                return
            realtime.task = asyncio.create_task(
                self._subscription_loop(plc_id, generation),
                name=f"devagent-live-commercial-realtime-{plc_id}",
            )

    async def monitor_node_ids(self, plc_id: str, node_ids: Iterable[str]) -> None:
        """Add monitored dependencies without removing history/mapping coverage."""
        await self._reconcile_monitored_node_ids(plc_id, node_ids, exact=False)

    async def ensure_monitored_node_ids(self, plc_id: str, node_ids: Iterable[str]) -> None:
        await self._reconcile_monitored_node_ids(plc_id, node_ids, exact=False)

    async def replace_monitored_node_ids(self, plc_id: str, node_ids: Iterable[str]) -> None:
        """Replace the desired set only for authoritative mapping refresh."""
        await self._reconcile_monitored_node_ids(plc_id, node_ids, exact=True)

    @staticmethod
    def _subscription_setup_failures(
        node_ids: tuple[str, ...],
        handles: object,
    ) -> tuple[str, ...]:
        if isinstance(handles, tuple):
            handles = list(handles)
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
        if len(handles) < len(node_ids):
            failed.extend(node_ids[len(handles) :])
        return tuple(dict.fromkeys(failed))

    async def _subscription_loop(self, plc_id: str, generation: int) -> None:
        realtime = self._state(plc_id)
        integrity = self._integrity_state(plc_id)
        while generation == realtime.generation:
            try:
                entry = self._entry(plc_id)
                outer_client = entry.client
                if outer_client is None:
                    self._invalidate_realtime(plc_id, reason="subscription has no OPC UA client")
                    await asyncio.sleep(0.1)
                    continue
                if not bool(getattr(outer_client, "connected", False)):
                    self._invalidate_realtime(plc_id, reason="OPC UA session is reconnecting")
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

                node_ids = tuple(
                    node_id
                    for node_id in integrity.desired_nodes[: self.max_monitored_nodes]
                    if node_id in realtime.monitored_nodes
                )
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
                    integrity.monitoring_started_at = self._now_utc()
                    self._sync_setup_gaps(
                        plc_id,
                        desired_nodes=node_ids,
                        failures=failures,
                    )
                    realtime.last_subscription_error = (
                        f"{len(failures)} monitored item(s) were rejected by the OPC UA server"
                        if failures
                        else None
                    )
                    self._close_continuity_gap(plc_id)
                    self._close_reconfiguration_gap(plc_id)
                    last_subscription_id = getattr(subscription, "subscription_id", None)

                    while generation == realtime.generation:
                        event = await subscription.next_event(timeout=1.0)
                        if event is None:
                            if not bool(getattr(outer_client, "connected", False)):
                                self._invalidate_realtime(
                                    plc_id,
                                    reason="OPC UA subscription lost connection continuity",
                                )
                                break
                            continue

                        now = self._now_utc()
                        current_subscription_id = getattr(subscription, "subscription_id", None)
                        if (
                            last_subscription_id is not None
                            and current_subscription_id is not None
                            and current_subscription_id != last_subscription_id
                        ):
                            start = integrity.last_subscription_event_at or now
                            self._record_gap(
                                plc_id,
                                source="SUBSCRIPTION_RECREATED",
                                reason=(
                                    "asyncua recreated the OPC UA subscription; complete notification "
                                    "continuity could not be proven"
                                ),
                                timestamp=min(start, now),
                                end_timestamp=now,
                            )
                            last_subscription_id = current_subscription_id

                        sequence = getattr(subscription, "last_sequence_number", None)
                        if sequence is not None:
                            try:
                                integrity.last_sequence_number = int(sequence)
                            except (TypeError, ValueError):
                                pass

                        if isinstance(event, StatusChangeEvent):
                            status = event.notification.Status
                            if bool(getattr(event, "replayed", False)):
                                integrity.replayed_events += 1
                            if status is not None and status.is_bad():
                                reconnecting = bool(
                                    getattr(outer_client, "auto_reconnect", False)
                                ) and (
                                    _is_graceful_shutdown_status(status)
                                    or str(getattr(outer_client, "connection_state", "")).upper()
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
                        replayed = bool(getattr(event, "replayed", False))
                        if replayed:
                            integrity.replayed_events += 1
                        value = _runtime_value_from_datavalue(
                            node_id,
                            data_value,
                            stale_after_seconds=outer_client.stale_after_seconds,
                            replayed=replayed,
                        )
                        stamp = _value_timestamp(value)
                        if status_has_monitored_item_overflow(
                            getattr(data_value, "StatusCode", None)
                        ):
                            self._record_gap(
                                plc_id,
                                source="SERVER_MONITORED_ITEM_OVERFLOW",
                                reason=(
                                    "OPC UA MonitoredItem Overflow InfoBit indicates the server queue "
                                    "purged detected changes"
                                ),
                                node_id=node_id,
                                timestamp=stamp,
                            )
                        previous = integrity.last_observed_at_by_node.get(node_id)
                        if previous is None or stamp > previous:
                            integrity.last_observed_at_by_node[node_id] = stamp
                        integrity.last_subscription_event_at = max(
                            stamp,
                            integrity.last_subscription_event_at or stamp,
                        )
                        self._update_cache(realtime, value, source="SUBSCRIPTION")
                        self._append_subscription_event(plc_id, value)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                integrity.active_nodes.clear()
                self._invalidate_realtime(plc_id, reason=str(exc))
                await asyncio.sleep(0.1)

    async def read_many(
        self,
        node_ids_by_plc: Mapping[str, Iterable[str]],
    ) -> dict[str, PlcReadResult]:
        unknown = [plc_id for plc_id in node_ids_by_plc if plc_id not in self.plc_ids]
        if unknown:
            raise KeyError(f"Unknown PLC id(s): {', '.join(unknown)}")
        normalized = {
            plc_id: tuple(
                dict.fromkeys(
                    str(node_id).strip()
                    for node_id in node_ids
                    if str(node_id).strip()
                )
            )
            for plc_id, node_ids in node_ids_by_plc.items()
        }
        await asyncio.gather(
            *(
                self.ensure_monitored_node_ids(plc_id, node_ids)
                for plc_id, node_ids in normalized.items()
            )
        )
        results = await asyncio.gather(
            *(
                self._batch_read_isolated(plc_id, node_ids)
                for plc_id, node_ids in normalized.items()
            )
        )
        for result in results:
            if result.error is not None:
                continue
            state = self._integrity_state(result.plc_id)
            for value in result.values:
                stamp = _value_timestamp(value)
                previous = state.last_observed_at_by_node.get(value.node_id)
                if previous is None or stamp > previous:
                    state.last_observed_at_by_node[value.node_id] = stamp
        return {result.plc_id: result for result in results}


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
    """Late-event-safe timeline with explicit evidence completeness."""

    def __init__(self, *args: Any, max_evidence_gaps: int = 5000, **kwargs: Any) -> None:
        if max_evidence_gaps < 512:
            raise ValueError("max_evidence_gaps must be >= 512")
        super().__init__(*args, **kwargs)
        self._evidence_gaps: deque[LiveEvidenceGap] = deque(maxlen=max_evidence_gaps)
        self._external_open_keys: set[tuple[object, ...]] = set()
        self._gap_metadata_overflows = 0

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

    @staticmethod
    def _sample_group_key(sample: LiveHistoricalSample) -> tuple[object, ...]:
        return (sample.timestamp, sample.plc_id, sample.tag_id, sample.node_id)

    def _compact_closed_gap_metadata(self, incoming: LiveEvidenceGap) -> None:
        maxlen = self._evidence_gaps.maxlen
        if maxlen is None or len(self._evidence_gaps) < maxlen:
            self._evidence_gaps.append(incoming)
            return
        items = list(self._evidence_gaps)
        closed_indices = [
            index for index, item in enumerate(items) if item.end_timestamp is not None
        ]
        if len(closed_indices) >= 2:
            first, second = closed_indices[:2]
            compacted = _closed_span((items[first], items[second]), plc_id=incoming.plc_id)
            rebuilt = [
                item for index, item in enumerate(items) if index not in {first, second}
            ]
            rebuilt.insert(first, compacted)
            self._evidence_gaps = deque(rebuilt, maxlen=maxlen)
            self._gap_metadata_overflows += 1
            self._evidence_gaps.append(incoming)
            return
        # Open intervals are authoritative and must never be evicted. The configured
        # production bound is intentionally far above the maximum simultaneously open
        # history scope; reaching this guard is itself an integrity limitation.
        if incoming.end_timestamp is None:
            raise RuntimeError("evidence-gap store exhausted by simultaneous open intervals")
        if closed_indices:
            index = closed_indices[0]
            items[index] = _closed_span((items[index], incoming), plc_id=incoming.plc_id)
            self._evidence_gaps = deque(items, maxlen=maxlen)
            self._gap_metadata_overflows += 1
            return
        raise RuntimeError("evidence-gap store has no safe slot for closed metadata")

    def record_gap(self, gap: LiveEvidenceGap) -> None:
        items = list(self._evidence_gaps)
        for index, existing in enumerate(items):
            if existing.key() == gap.key():
                if existing.end_timestamp is None and gap.end_timestamp is not None:
                    items[index] = gap
                    self._evidence_gaps = deque(items, maxlen=self._evidence_gaps.maxlen)
                self._trim_integrity(self._now())
                return
        self._compact_closed_gap_metadata(gap)
        self._trim_integrity(self._now())

    def sync_open_gaps(
        self,
        gaps: Iterable[LiveEvidenceGap],
        *,
        observed_at: datetime | None = None,
    ) -> None:
        now = observed_at or self._now()
        current = {gap.key(): gap for gap in gaps}
        for gap in current.values():
            self.record_gap(gap)
        ended = self._external_open_keys - set(current)
        if ended:
            items = list(self._evidence_gaps)
            for index, existing in enumerate(items):
                if existing.key() in ended and existing.end_timestamp is None:
                    items[index] = LiveEvidenceGap(
                        timestamp=existing.timestamp,
                        end_timestamp=now,
                        plc_id=existing.plc_id,
                        source=existing.source,
                        reason=existing.reason,
                        node_id=existing.node_id,
                        dropped_count=existing.dropped_count,
                    )
            self._evidence_gaps = deque(items, maxlen=self._evidence_gaps.maxlen)
        self._external_open_keys = set(current)
        self._trim_integrity(now)

    def evidence_gaps(self) -> tuple[LiveEvidenceGap, ...]:
        self._trim_integrity(self._now())
        return tuple(self._evidence_gaps)

    def _trim_integrity(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.retention_seconds)
        retained = [
            gap
            for gap in self._evidence_gaps
            if (gap.end_timestamp or now) >= cutoff
        ]
        self._evidence_gaps = deque(retained, maxlen=self._evidence_gaps.maxlen)

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
        merged.sort(
            key=lambda item: (
                item.timestamp,
                item.plc_id,
                item.tag_id,
                item.node_id,
                repr(item.value),
            )
        )

        normalized: list[LiveHistoricalSample] = []
        for _key, grouped in groupby(merged, key=self._sample_group_key):
            group = list(grouped)
            unique: dict[tuple[object, ...], LiveHistoricalSample] = {}
            for item in group:
                unique[self._sample_identity(item)] = item
            variants = tuple(unique.values())
            if len(variants) == 1:
                normalized.append(variants[0])
                continue
            first = variants[0]
            self.record_gap(
                LiveEvidenceGap(
                    timestamp=first.timestamp,
                    end_timestamp=first.timestamp,
                    plc_id=first.plc_id,
                    source="TIMESTAMP_CONFLICT",
                    reason=(
                        "conflicting values share the same source timestamp; temporal ordering is not defensible"
                    ),
                    node_id=first.node_id,
                )
            )
            normalized.append(
                LiveHistoricalSample(
                    timestamp=first.timestamp,
                    plc_id=first.plc_id,
                    tag_id=first.tag_id,
                    tag_name=first.tag_name,
                    node_id=first.node_id,
                    value=None,
                    definitive_current=False,
                    quality="UNCERTAIN",
                    trust="UNTRUSTED",
                )
            )

        if len(normalized) > max_samples:
            evicted = normalized[:-max_samples]
            retained = normalized[-max_samples:]
            self.record_gap(
                LiveEvidenceGap(
                    timestamp=evicted[0].timestamp,
                    end_timestamp=retained[0].timestamp,
                    plc_id=evicted[0].plc_id,
                    source="TIMELINE_SAMPLE_CAPACITY",
                    reason=(
                        "historical sample capacity evicted observations still inside the configured retention window"
                    ),
                    dropped_count=len(evicted),
                )
            )
            normalized = retained

        latest: dict[str, LiveHistoricalSample] = {}
        previous_by_tag: dict[str, LiveHistoricalSample] = {}
        transitions: list[LiveSignalTransition] = []
        for item in normalized:
            previous = previous_by_tag.get(item.tag_id)
            latest[item.tag_id] = item
            if previous is not None:
                gap_seconds = (item.timestamp - previous.timestamp).total_seconds()
                if (
                    0.0 < gap_seconds <= self.continuity_seconds
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
            previous_by_tag[item.tag_id] = item

        if len(transitions) > max_transitions:
            omitted_transitions = transitions[:-max_transitions]
            retained_transitions = transitions[-max_transitions:]
            self.record_gap(
                LiveEvidenceGap(
                    timestamp=omitted_transitions[0].timestamp,
                    end_timestamp=retained_transitions[0].timestamp,
                    plc_id=omitted_transitions[0].plc_id,
                    source="TIMELINE_TRANSITION_CAPACITY",
                    reason=(
                        "historical transition capacity omitted transitions reconstructed from retained samples"
                    ),
                    dropped_count=len(omitted_transitions),
                )
            )
            transitions = retained_transitions

        self._samples = deque(normalized, maxlen=max_samples)
        self._transitions = deque(transitions, maxlen=max_transitions)
        self._latest_by_tag = latest
        self._trim_integrity(now)

    def diagnose_recent_transition(self, *args: Any, **kwargs: Any) -> ProductionHistoricalDiagnosis:
        base = super().diagnose_recent_transition(*args, **kwargs)
        current = kwargs.get("now") or self._now()
        cutoff = current - timedelta(seconds=base.lookback_seconds)
        gaps = tuple(
            gap for gap in self._evidence_gaps if gap.overlaps(cutoff, current)
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
    """History collector that synchronizes manager integrity at poll and query time."""

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
            self._run(), name="devagent-live-commercial-history"
        )

    def sync_integrity_gaps(self) -> None:
        record = getattr(self.store, "record_gap", None)
        if not callable(record):
            return
        drain = getattr(self.manager, "drain_evidence_gaps", None)
        if callable(drain):
            for gap in drain(self.reconciliation.plc_id, max_gaps=5000):
                record(gap)
        sync_open = getattr(self.store, "sync_open_gaps", None)
        open_gaps = getattr(self.manager, "open_evidence_gaps", None)
        if callable(sync_open) and callable(open_gaps):
            sync_open(open_gaps(self.reconciliation.plc_id))

    async def _collect_once(self) -> None:
        self.sync_integrity_gaps()
        await super()._collect_once()
        self.sync_integrity_gaps()


__all__ = [
    "EvidenceIntegrityTimelineStore",
    "LiveEvidenceGap",
    "LiveEvidenceIntegrityStatus",
    "ProductionHistoricalDiagnosis",
    "ProductionLiveHistoryCollector",
    "ProductionRealtimeMultiPlcConnectionManager",
    "status_has_monitored_item_overflow",
]
