from __future__ import annotations

import asyncio
from typing import Iterable

# Backward-compatible production import surface. The evidence/timeline implementation
# remains in commercial_realtime; the production manager below adds the final
# fail-closed runtime barriers required by the default commercial CLI path.
from .commercial_realtime import (
    EvidenceIntegrityTimelineStore,
    LiveEvidenceGap,
    LiveEvidenceIntegrityStatus,
    ProductionHistoricalDiagnosis,
    ProductionLiveHistoryCollector,
    ProductionRealtimeMultiPlcConnectionManager as _CommercialRealtimeManager,
    _value_timestamp,
    status_has_monitored_item_overflow,
)
from .opcua_client import (
    _is_graceful_shutdown_status,
    _node_id_text,
    _runtime_value_from_datavalue,
    _status_name,
)


class ProductionRealtimeMultiPlcConnectionManager(_CommercialRealtimeManager):
    """Default commercial manager with final startup and idle-timeout integrity gates."""

    async def replace_monitored_node_ids(
        self,
        plc_id: str,
        node_ids: Iterable[str],
    ) -> None:
        requested = tuple(
            dict.fromkeys(str(item).strip() for item in node_ids if str(item).strip())
        )
        selected = set(requested[: self.max_monitored_nodes])
        integrity = self._integrity_state(plc_id)
        if (
            self.subscription_enabled
            and selected
            and selected != integrity.active_nodes
            and integrity.reconfiguration_gap_started_at is None
        ):
            # Initial startup previously had no active set, so the base exact-replace
            # path did not open a reconfiguration gap. Open one before scheduling the
            # subscription so the pre-monitoring interval can never be called COMPLETE.
            self._open_reconfiguration_gap(plc_id)
        await super().replace_monitored_node_ids(plc_id, requested)

    async def wait_for_active_monitoring(
        self,
        plc_id: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> bool:
        """Wait until the requested subscription attempt is established fail-closed.

        False means startup did not arm within the bound. The authoritative
        MONITOR_SET_RECONFIGURATION gap intentionally remains open in that case, so
        history may continue operating but cannot claim complete evidence.
        """
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        integrity = self._integrity_state(plc_id)
        selected = set(integrity.desired_nodes[: self.max_monitored_nodes])
        if not self.subscription_enabled or not selected:
            return True

        deadline = asyncio.get_running_loop().time() + float(timeout_seconds)
        while True:
            integrity = self._integrity_state(plc_id)
            if (
                integrity.monitoring_started_at is not None
                and integrity.reconfiguration_gap_started_at is None
            ):
                return True
            realtime = self._state(plc_id)
            if realtime.task is not None and realtime.task.done():
                try:
                    realtime.task.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.02)

    async def _subscription_loop(self, plc_id: str, generation: int) -> None:
        """Commercial subscription loop with idle TimeoutError treated as healthy idle."""
        realtime = self._state(plc_id)
        integrity = self._integrity_state(plc_id)
        while generation == realtime.generation:
            try:
                entry = self._entry(plc_id)
                outer_client = entry.client
                if outer_client is None:
                    self._invalidate_realtime(
                        plc_id,
                        reason="subscription has no OPC UA client",
                    )
                    await asyncio.sleep(0.1)
                    continue
                if not bool(getattr(outer_client, "connected", False)):
                    self._invalidate_realtime(
                        plc_id,
                        reason="OPC UA session is reconnecting",
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
                        try:
                            event = await subscription.next_event(timeout=1.0)
                        except asyncio.TimeoutError:
                            # asyncua uses TimeoutError for a quiet iterator on supported
                            # paths/versions. No notification is not evidence loss.
                            if not bool(getattr(outer_client, "connected", False)):
                                self._invalidate_realtime(
                                    plc_id,
                                    reason="OPC UA subscription lost connection continuity",
                                )
                                break
                            continue

                        if event is None:
                            if not bool(getattr(outer_client, "connected", False)):
                                self._invalidate_realtime(
                                    plc_id,
                                    reason="OPC UA subscription lost connection continuity",
                                )
                                break
                            continue

                        now = self._now_utc()
                        current_subscription_id = getattr(
                            subscription,
                            "subscription_id",
                            None,
                        )
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
                                    or str(
                                        getattr(
                                            outer_client,
                                            "connection_state",
                                            "",
                                        )
                                    ).upper()
                                    in {"CONNECTING", "DISCONNECTED", "RECONNECTING"}
                                )
                                self._invalidate_realtime(
                                    plc_id,
                                    reason=f"subscription status {_status_name(status)}",
                                )
                                if not reconnecting:
                                    raise RuntimeError(
                                        "subscription status changed to "
                                        f"{_status_name(status)}"
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


__all__ = [
    "EvidenceIntegrityTimelineStore",
    "LiveEvidenceGap",
    "LiveEvidenceIntegrityStatus",
    "ProductionHistoricalDiagnosis",
    "ProductionLiveHistoryCollector",
    "ProductionRealtimeMultiPlcConnectionManager",
    "status_has_monitored_item_overflow",
]
