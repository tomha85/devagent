from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .manager import (
    MultiPlcConnectionManager,
    PlcConnectionSpec,
    PlcReadResult,
    PlcSessionState,
)
from .models import Quality, RuntimeValue
from .opcua_client import (
    _is_graceful_shutdown_status,
    _node_id_text,
    _require_asyncua,
    _runtime_value_from_datavalue,
    _status_name,
)


@dataclass(frozen=True)
class RealtimeSnapshotStatus:
    plc_id: str
    connection_epoch: int
    source: str
    requested_nodes: int
    cached_nodes: int
    monitored_nodes: int
    event_backlog: int
    max_timestamp_skew_seconds: float | None
    captured_at: datetime
    last_subscription_error: str | None


@dataclass
class _RealtimeState:
    epoch: int = 0
    monitored_nodes: set[str] = field(default_factory=set)
    cache: dict[str, RuntimeValue] = field(default_factory=dict)
    events: deque[RuntimeValue] = field(default_factory=lambda: deque(maxlen=20000))
    task: asyncio.Task[None] | None = None
    generation: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_subscription_error: str | None = None
    last_snapshot: RealtimeSnapshotStatus | None = None


class RealtimeMultiPlcConnectionManager(MultiPlcConnectionManager):
    """Drop-in read-only manager with persistent OPC UA monitoring and coherent snapshots.

    The deterministic Live diagnosis stack still consumes the normal ``read_many``
    contract. This manager improves that evidence path without granting any write or
    control capability:

    * accepted nodes can be monitored continuously through OPC UA subscriptions;
    * very fresh, coherent subscription values can satisfy a question immediately;
    * otherwise all requested nodes are refreshed in one OPC UA Read service call;
    * cache/event state is invalidated whenever the subscription loses continuity;
    * subscription events are retained in a bounded queue for the historical layer.
    """

    def __init__(
        self,
        specs: Iterable[PlcConnectionSpec],
        *,
        client_factory: Any = None,
        subscription_enabled: bool = True,
        publishing_interval_ms: float = 250.0,
        sampling_interval_ms: float = 100.0,
        queue_size: int = 100,
        cache_fresh_seconds: float = 0.25,
        max_snapshot_skew_seconds: float = 0.25,
        max_monitored_nodes: int = 256,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if client_factory is not None:
            kwargs["client_factory"] = client_factory
        super().__init__(specs, **kwargs)
        if publishing_interval_ms <= 0 or sampling_interval_ms <= 0:
            raise ValueError("realtime subscription intervals must be > 0")
        if queue_size < 1:
            raise ValueError("realtime queue_size must be >= 1")
        if cache_fresh_seconds <= 0:
            raise ValueError("cache_fresh_seconds must be > 0")
        if max_snapshot_skew_seconds < 0:
            raise ValueError("max_snapshot_skew_seconds must be >= 0")
        if max_monitored_nodes < 1 or max_monitored_nodes > 4096:
            raise ValueError("max_monitored_nodes must be between 1 and 4096")
        self.subscription_enabled = bool(subscription_enabled)
        self.publishing_interval_ms = float(publishing_interval_ms)
        self.sampling_interval_ms = float(sampling_interval_ms)
        self.queue_size = int(queue_size)
        self.cache_fresh_seconds = float(cache_fresh_seconds)
        self.max_snapshot_skew_seconds = float(max_snapshot_skew_seconds)
        self.max_monitored_nodes = int(max_monitored_nodes)
        self._realtime: dict[str, _RealtimeState] = {
            plc_id: _RealtimeState() for plc_id in self.plc_ids
        }

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _freshness_timestamp(value: RuntimeValue) -> datetime:
        return value.source_timestamp or value.server_timestamp or value.received_at

    def _state(self, plc_id: str) -> _RealtimeState:
        try:
            return self._realtime[plc_id]
        except KeyError as exc:
            raise KeyError(f"Unknown PLC id: {plc_id}") from exc

    def realtime_status(self, plc_id: str) -> RealtimeSnapshotStatus:
        state = self._state(plc_id)
        if state.last_snapshot is not None:
            return state.last_snapshot
        return RealtimeSnapshotStatus(
            plc_id=plc_id,
            connection_epoch=state.epoch,
            source="NOT_CAPTURED",
            requested_nodes=0,
            cached_nodes=len(state.cache),
            monitored_nodes=len(state.monitored_nodes),
            event_backlog=len(state.events),
            max_timestamp_skew_seconds=None,
            captured_at=self._now(),
            last_subscription_error=state.last_subscription_error,
        )

    def _invalidate_realtime(self, plc_id: str, *, reason: str | None = None) -> None:
        state = self._state(plc_id)
        state.epoch += 1
        state.cache.clear()
        state.events.clear()
        state.last_snapshot = None
        state.last_subscription_error = reason

    async def connect(self, plc_id: str):
        before = self.status(plc_id).successful_connections
        status = await super().connect(plc_id)
        if status.successful_connections != before:
            self._invalidate_realtime(plc_id)
        return status

    async def disconnect(self, plc_id: str):
        await self._stop_subscription(plc_id)
        self._invalidate_realtime(plc_id, reason="session disconnected")
        return await super().disconnect(plc_id)

    async def _stop_subscription(self, plc_id: str) -> None:
        state = self._state(plc_id)
        async with state.lock:
            state.generation += 1
            task, state.task = state.task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def monitor_node_ids(self, plc_id: str, node_ids: Iterable[str]) -> None:
        """Ensure a bounded union of read-only nodes is continuously monitored."""
        if not self.subscription_enabled:
            return
        requested = [str(item).strip() for item in node_ids if str(item).strip()]
        if not requested:
            return
        state = self._state(plc_id)
        async with state.lock:
            before = set(state.monitored_nodes)
            for node_id in requested:
                if len(state.monitored_nodes) >= self.max_monitored_nodes:
                    break
                state.monitored_nodes.add(node_id)
            if state.monitored_nodes == before and state.task is not None and not state.task.done():
                return
            state.generation += 1
            generation = state.generation
            old_task = state.task
            state.task = None
        if old_task is not None:
            old_task.cancel()
            try:
                await old_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        async with state.lock:
            if generation != state.generation or not state.monitored_nodes:
                return
            state.task = asyncio.create_task(
                self._subscription_loop(plc_id, generation),
                name=f"devagent-live-realtime-{plc_id}",
            )

    async def _subscription_loop(self, plc_id: str, generation: int) -> None:
        state = self._state(plc_id)
        while generation == state.generation:
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
                from asyncua.common.subscription import DataChangeEvent, StatusChangeEvent

                node_ids = tuple(state.monitored_nodes)
                nodes = [client.get_node(node_id) for node_id in node_ids]
                async with await client.create_subscription(self.publishing_interval_ms) as subscription:
                    await subscription.subscribe_data_change(
                        nodes,
                        queuesize=self.queue_size,
                        sampling_interval=self.sampling_interval_ms,
                    )
                    state.last_subscription_error = None
                    while generation == state.generation:
                        try:
                            event = await subscription.next_event(timeout=1.0)
                        except asyncio.TimeoutError:
                            if not bool(getattr(outer_client, "connected", False)):
                                self._invalidate_realtime(
                                    plc_id,
                                    reason="OPC UA subscription lost connection continuity",
                                )
                                break
                            continue
                        if isinstance(event, StatusChangeEvent):
                            status = event.notification.Status
                            if status is not None and status.is_bad():
                                reconnecting = bool(getattr(outer_client, "auto_reconnect", False)) and (
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
                        value = _runtime_value_from_datavalue(
                            _node_id_text(event.node.nodeid),
                            event.data.monitored_item.Value,
                            stale_after_seconds=outer_client.stale_after_seconds,
                            replayed=event.replayed,
                        )
                        state.cache[value.node_id] = value
                        state.events.append(value)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._invalidate_realtime(plc_id, reason=str(exc))
                await asyncio.sleep(0.1)

    def drain_realtime_events(
        self,
        plc_id: str,
        *,
        node_ids: Iterable[str] | None = None,
        max_events: int = 5000,
    ) -> tuple[RuntimeValue, ...]:
        """Drain subscription events in arrival order for the historical timeline."""
        if max_events < 1:
            raise ValueError("max_events must be >= 1")
        state = self._state(plc_id)
        allowed = None if node_ids is None else {str(item) for item in node_ids}
        kept: deque[RuntimeValue] = deque(maxlen=state.events.maxlen)
        result: list[RuntimeValue] = []
        while state.events:
            value = state.events.popleft()
            if allowed is None or value.node_id in allowed:
                if len(result) < max_events:
                    result.append(value)
                else:
                    kept.append(value)
            else:
                kept.append(value)
        state.events.extend(kept)
        return tuple(result)

    def _cached_snapshot(
        self,
        plc_id: str,
        node_ids: tuple[str, ...],
    ) -> tuple[RuntimeValue, ...] | None:
        status = self.status(plc_id)
        if not status.connected or status.state is not PlcSessionState.CONNECTED:
            return None
        state = self._state(plc_id)
        now = self._now()
        values: list[RuntimeValue] = []
        timestamps: list[datetime] = []
        for node_id in node_ids:
            value = state.cache.get(node_id)
            if (
                value is None
                or value.quality is not Quality.GOOD
                or value.stale
                or value.replayed
            ):
                return None
            stamp = self._freshness_timestamp(value)
            age = max(0.0, (now - stamp).total_seconds())
            if age > self.cache_fresh_seconds:
                return None
            values.append(value)
            timestamps.append(stamp)
        skew = self._timestamp_skew(timestamps)
        if skew is not None and skew > self.max_snapshot_skew_seconds:
            return None
        state.last_snapshot = RealtimeSnapshotStatus(
            plc_id=plc_id,
            connection_epoch=state.epoch,
            source="SUBSCRIPTION_CACHE",
            requested_nodes=len(node_ids),
            cached_nodes=len(state.cache),
            monitored_nodes=len(state.monitored_nodes),
            event_backlog=len(state.events),
            max_timestamp_skew_seconds=skew,
            captured_at=now,
            last_subscription_error=state.last_subscription_error,
        )
        return tuple(values)

    @staticmethod
    def _timestamp_skew(timestamps: Iterable[datetime]) -> float | None:
        values = tuple(timestamps)
        if len(values) < 2:
            return 0.0 if values else None
        return max(0.0, (max(values) - min(values)).total_seconds())

    async def _batch_read_isolated(
        self,
        plc_id: str,
        node_ids: tuple[str, ...],
    ) -> PlcReadResult:
        if not node_ids:
            return PlcReadResult(
                plc_id=plc_id,
                values=(),
                state=self.status(plc_id).state,
            )
        cached = self._cached_snapshot(plc_id, node_ids)
        if cached is not None:
            return PlcReadResult(
                plc_id=plc_id,
                values=cached,
                state=PlcSessionState.CONNECTED,
            )

        entry = self._entry(plc_id)
        async with entry.lock:
            outer_client = entry.client
            if outer_client is None or not bool(getattr(outer_client, "connected", False)):
                observed = self.status(plc_id)
                return PlcReadResult(
                    plc_id=plc_id,
                    values=(),
                    state=observed.state,
                    error=f"PLC {plc_id} session is not connected; state={observed.state.value}",
                )
            try:
                client = outer_client._require_connected()
                _Client, ua = _require_asyncua()
                nodes = [client.get_node(node_id) for node_id in node_ids]
                data_values = await client.uaclient.read_attributes(
                    [node.nodeid for node in nodes],
                    ua.AttributeIds.Value,
                )
                if len(data_values) != len(node_ids):
                    raise RuntimeError(
                        f"OPC UA batch read returned {len(data_values)} values for {len(node_ids)} requested nodes"
                    )
                values = tuple(
                    _runtime_value_from_datavalue(
                        node_id,
                        data_value,
                        stale_after_seconds=outer_client.stale_after_seconds,
                    )
                    for node_id, data_value in zip(node_ids, data_values, strict=True)
                )
                state = self._state(plc_id)
                for value in values:
                    state.cache[value.node_id] = value
                timestamps = [self._freshness_timestamp(value) for value in values]
                skew = self._timestamp_skew(timestamps)
                state.last_snapshot = RealtimeSnapshotStatus(
                    plc_id=plc_id,
                    connection_epoch=state.epoch,
                    source="OPCUA_BATCH_READ",
                    requested_nodes=len(node_ids),
                    cached_nodes=len(state.cache),
                    monitored_nodes=len(state.monitored_nodes),
                    event_backlog=len(state.events),
                    max_timestamp_skew_seconds=skew,
                    captured_at=self._now(),
                    last_subscription_error=state.last_subscription_error,
                )
                self._set_state(entry, PlcSessionState.CONNECTED)
                return PlcReadResult(
                    plc_id=plc_id,
                    values=values,
                    state=PlcSessionState.CONNECTED,
                )
            except Exception as exc:
                safe = self._safe_error(entry, exc)
                raw_state = str(getattr(outer_client, "connection_state", "UNKNOWN")).strip().upper()
                next_state = (
                    PlcSessionState.RECONNECTING
                    if raw_state in {"CONNECTING", "RECONNECTING"}
                    else PlcSessionState.DEGRADED
                )
                self._set_state(entry, next_state, error=safe)
                self._invalidate_realtime(plc_id, reason=safe)
                return PlcReadResult(
                    plc_id=plc_id,
                    values=(),
                    state=next_state,
                    error=f"PLC {plc_id} coherent batch read failed: {safe}",
                )

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
                self.monitor_node_ids(plc_id, node_ids)
                for plc_id, node_ids in normalized.items()
            )
        )
        results = await asyncio.gather(
            *(
                self._batch_read_isolated(plc_id, node_ids)
                for plc_id, node_ids in normalized.items()
            )
        )
        return {result.plc_id: result for result in results}


__all__ = [
    "RealtimeMultiPlcConnectionManager",
    "RealtimeSnapshotStatus",
]
