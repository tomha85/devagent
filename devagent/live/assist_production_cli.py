from __future__ import annotations

from typing import Sequence

from . import assist_cli as base_assist_cli
from . import recursive_assistant as recursive_assistant_module
from .production_realtime import (
    ProductionLiveHistoryCollector,
    ProductionRealtimeMultiPlcConnectionManager,
)


_ORIGINAL_PRINT_STATUS = base_assist_cli._print_status


def _production_print_status(assistant) -> None:
    _ORIGINAL_PRINT_STATUS(assistant)
    integrity_status = getattr(assistant.manager, "integrity_status", None)
    if not callable(integrity_status):
        return
    integrity = integrity_status(assistant.connection.plc_id)
    print(
        "Evidence integrity: "
        + (
            "COMPLETE"
            if integrity.evidence_complete_since_session_start
            else "INCOMPLETE"
        )
    )
    print(f"Evidence gaps detected: {integrity.evidence_gap_count}")
    print(f"Server monitored-item overflows: {integrity.server_overflow_events}")
    print(f"Local event-buffer drops: {integrity.local_buffer_drops}")
    print(f"Replay/Republish events observed: {integrity.replayed_events}")
    print(f"Subscription recreations: {integrity.subscription_recreations}")
    print(
        "Realtime monitored-set coverage: "
        f"desired={integrity.desired_monitored_nodes} "
        f"active={integrity.active_monitored_nodes} "
        f"omitted={integrity.omitted_monitored_nodes}"
    )
    if integrity.last_sequence_number is not None:
        print(f"Last OPC UA notification sequence: {integrity.last_sequence_number}")
    if integrity.last_gap_at is not None:
        print(f"Last evidence gap: {integrity.last_gap_at.isoformat()}")
    if integrity.last_gap_reason:
        print(f"Last evidence-gap reason: {integrity.last_gap_reason}")


def _install_production_runtime() -> None:
    # assist_cli imported these symbols directly; replace only its runtime globals.
    # The generic V1 classes remain available for tests/library callers.
    base_assist_cli.RealtimeMultiPlcConnectionManager = (
        ProductionRealtimeMultiPlcConnectionManager
    )
    base_assist_cli._print_status = _production_print_status

    # RecursiveLiveCommissioningAssistant resolves LiveHistoryCollector from its
    # defining module at runtime. Replace that factory only for the production CLI.
    recursive_assistant_module.LiveHistoryCollector = ProductionLiveHistoryCollector


def main(argv: Sequence[str] | None = None) -> int:
    _install_production_runtime()
    return base_assist_cli.main(argv)


__all__ = ["main"]
