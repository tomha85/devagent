from __future__ import annotations

from .assistant import LiveCommissioningAssistant
from .realtime_assistant import RealtimeSemanticLiveCommissioningAssistant


class CommercialRealtimeSemanticLiveCommissioningAssistant(
    RealtimeSemanticLiveCommissioningAssistant
):
    """Production assistant glue for commercial realtime evidence semantics."""

    @staticmethod
    def _accepted_node_ids(reconciliation):
        return tuple(
            mapping.selected_node_id
            for mapping in reconciliation.accepted_mappings()
            if mapping.selected_node_id is not None
        )

    async def _stop_history_before_reconfiguration(self) -> None:
        collector = self.history_collector
        if collector is None:
            return
        try:
            await collector.stop()
        finally:
            self.history_collector = None

    async def _await_authoritative_monitoring(self) -> bool:
        wait = getattr(self.manager, "wait_for_active_monitoring", None)
        if not callable(wait):
            return True
        return bool(await wait(self.connection.plc_id, timeout_seconds=5.0))

    async def start(self):
        # Reconnect startup must stop the old collector first. Otherwise that collector
        # can drain the just-closed continuity gap into a store that _start_history()
        # subsequently discards, allowing the replacement store to lose outage evidence.
        await self._stop_history_before_reconfiguration()

        # Build connection + reconciliation, arm the authoritative monitored set, wait
        # until the monitored items are active, and only then create the replacement
        # history collector. If readiness times out, the manager leaves an open evidence
        # interval so historical diagnosis remains fail-closed.
        status = await LiveCommissioningAssistant.start(self)
        assert self.reconciliation is not None
        replace = getattr(self.manager, "replace_monitored_node_ids", None)
        if callable(replace):
            await replace(
                self.connection.plc_id,
                self._accepted_node_ids(self.reconciliation),
            )
            await self._await_authoritative_monitoring()
        await self._start_history()
        self._sync_history_integrity()
        return status

    async def refresh_mapping(self):
        # Stop the old collector before exact replacement. Otherwise an in-flight poll
        # can add stale old-mapping dependencies after replace() and consume production
        # monitoring capacity even though the new collector no longer references them.
        await self._stop_history_before_reconfiguration()
        reconciliation = await LiveCommissioningAssistant.refresh_mapping(self)
        replace = getattr(self.manager, "replace_monitored_node_ids", None)
        if callable(replace):
            await replace(
                self.connection.plc_id,
                self._accepted_node_ids(reconciliation),
            )
            await self._await_authoritative_monitoring()
        await self._start_history()
        self._sync_history_integrity()
        return reconciliation

    def _sync_history_integrity(self) -> None:
        collector = self.history_collector
        if collector is None:
            return
        sync = getattr(collector, "sync_integrity_gaps", None)
        if callable(sync):
            sync()

    async def _historical_reply(self, text: str):
        # Deterministic answer() normally starts first, but preserve the same ordering
        # when this method is exercised directly: reconnect/startup before the barrier.
        if not self.connected or self.reconciliation is None:
            await self.start()
        self._sync_history_integrity()
        return await super()._historical_reply(text)

    async def _dispatch_historical_route(self, original, route):
        # Semantic historical dispatch previously synchronized the old collector before
        # its superclass noticed a disconnected session and called start(). Reconnect
        # first so the new collector/store receives the known continuity gap before the
        # first post-reconnect diagnosis can run.
        if not self.connected or self.reconciliation is None:
            await self.start()
        self._sync_history_integrity()
        return await super()._dispatch_historical_route(original, route)


__all__ = ["CommercialRealtimeSemanticLiveCommissioningAssistant"]
