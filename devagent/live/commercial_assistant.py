from __future__ import annotations

from .realtime_assistant import RealtimeSemanticLiveCommissioningAssistant


class CommercialRealtimeSemanticLiveCommissioningAssistant(
    RealtimeSemanticLiveCommissioningAssistant
):
    """Production assistant glue for commercial realtime evidence semantics."""

    async def refresh_mapping(self):
        reconciliation = await super().refresh_mapping()
        replace = getattr(self.manager, "replace_monitored_node_ids", None)
        if callable(replace):
            await replace(
                self.connection.plc_id,
                (
                    mapping.selected_node_id
                    for mapping in reconciliation.accepted_mappings()
                    if mapping.selected_node_id is not None
                ),
            )
        return reconciliation

    async def _historical_reply(self, text: str):
        collector = self.history_collector
        if collector is not None:
            sync = getattr(collector, "sync_integrity_gaps", None)
            if callable(sync):
                sync()
        return await super()._historical_reply(text)


__all__ = ["CommercialRealtimeSemanticLiveCommissioningAssistant"]
