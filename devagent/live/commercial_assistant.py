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

    async def start(self):
        # Build connection + reconciliation first, arm the authoritative full monitored
        # set second, and only then start history. This prevents startup from creating
        # an artificial subset->full-set reconfiguration gap before the session is ready.
        status = await LiveCommissioningAssistant.start(self)
        assert self.reconciliation is not None
        replace = getattr(self.manager, "replace_monitored_node_ids", None)
        if callable(replace):
            await replace(
                self.connection.plc_id,
                self._accepted_node_ids(self.reconciliation),
            )
        await self._start_history()
        return status

    async def refresh_mapping(self):
        # Bypass Recursive.refresh_mapping because it restarts history before the exact
        # monitored-set replacement. Commercial refresh must establish the authoritative
        # new set first, then bind the history collector to it.
        reconciliation = await LiveCommissioningAssistant.refresh_mapping(self)
        replace = getattr(self.manager, "replace_monitored_node_ids", None)
        if callable(replace):
            await replace(
                self.connection.plc_id,
                self._accepted_node_ids(reconciliation),
            )
        await self._start_history()
        return reconciliation

    def _sync_history_integrity(self) -> None:
        collector = self.history_collector
        if collector is None:
            return
        sync = getattr(collector, "sync_integrity_gaps", None)
        if callable(sync):
            sync()

    async def _historical_reply(self, text: str):
        self._sync_history_integrity()
        return await super()._historical_reply(text)

    async def _dispatch_historical_route(self, original, route):
        # AI-routed historical questions bypass _historical_reply in the semantic layer,
        # so they require the same query-time integrity barrier explicitly.
        self._sync_history_integrity()
        return await super()._dispatch_historical_route(original, route)


__all__ = ["CommercialRealtimeSemanticLiveCommissioningAssistant"]
