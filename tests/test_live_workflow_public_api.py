from __future__ import annotations

import devagent.live as live


def test_commissioning_workflow_is_exported_from_public_live_api() -> None:
    for name in (
        "LiveCommissioningPlcResult",
        "LiveCommissioningPlcSpec",
        "LiveCommissioningState",
        "LiveCommissioningWorkflow",
        "LiveCommissioningWorkflowResult",
    ):
        assert name in live.__all__
        assert getattr(live, name) is not None


def test_public_workflow_api_remains_read_only() -> None:
    workflow_type = live.LiveCommissioningWorkflow
    for prohibited in (
        "write",
        "write_value",
        "set_value",
        "call_method",
        "force",
        "reset",
        "download",
        "change_mode",
    ):
        assert not hasattr(workflow_type, prohibited)
