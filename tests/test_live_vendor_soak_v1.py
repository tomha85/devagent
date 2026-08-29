from __future__ import annotations

import asyncio
from types import SimpleNamespace

from devagent.live.runtime_environment import LiveOpcUaRuntimeStatus
from devagent.live.soak import LiveSoakStatus, run_live_soak
from devagent.live.vendor_qualification import (
    LiveVendorQualificationStatus,
    run_live_vendor_qualification,
)
from devagent.live.workflow import LiveCommissioningState


def _project(vendor: str):
    return SimpleNamespace(metadata=SimpleNamespace(vendor=vendor), tags=(SimpleNamespace(id="T1"),))


def _spec(vendor: str, plc_id: str):
    return SimpleNamespace(
        connection=SimpleNamespace(plc_id=plc_id, display_name=plc_id),
        engineering_project=_project(vendor),
    )


def _config():
    return SimpleNamespace(
        source_sha256="abc123",
        specs=(
            _spec("Rockwell Automation", "r1"),
            _spec("Siemens", "s1"),
            _spec("Schneider Electric", "c1"),
        ),
    )


def _result_item(state: LiveCommissioningState, *, current: int = 1):
    reconciliation = SimpleNamespace(
        accepted_mappings=lambda: (object(),),
        unresolved_mappings=lambda: (),
    )
    evidence = SimpleNamespace(
        live_pack=SimpleNamespace(
            definitive_current_evidence_ids=frozenset(f"e{index}" for index in range(current))
        )
    )
    return SimpleNamespace(
        state=state,
        reconciliation=reconciliation,
        evidence=evidence,
        error=None if state is LiveCommissioningState.COMPLETE else "connection failed",
    )


def _workflow_result(*, schneider_state: LiveCommissioningState = LiveCommissioningState.COMPLETE):
    return SimpleNamespace(
        plc_results={
            "r1": _result_item(LiveCommissioningState.COMPLETE),
            "s1": _result_item(LiveCommissioningState.COMPLETE),
            "c1": _result_item(schneider_state),
        }
    )


def test_vendor_qualification_blocks_without_supported_runtime(monkeypatch):
    import devagent.live.vendor_qualification as module

    called = False

    async def runner(_config):
        nonlocal called
        called = True
        raise AssertionError("workflow must not run when asyncua runtime is unsupported")

    monkeypatch.setattr(
        module,
        "detect_live_opcua_runtime",
        lambda: LiveOpcUaRuntimeStatus(False, None, False, "asyncua missing"),
    )

    report = asyncio.run(run_live_vendor_qualification(_config(), workflow_runner=runner))

    assert report.status is LiveVendorQualificationStatus.BLOCKED
    assert called is False
    assert all(item.status is LiveVendorQualificationStatus.BLOCKED for item in report.vendors)
    assert report.all_required_vendors_pass is False


def test_all_three_vendor_real_workflow_results_pass(monkeypatch):
    import devagent.live.vendor_qualification as module

    monkeypatch.setattr(
        module,
        "detect_live_opcua_runtime",
        lambda: LiveOpcUaRuntimeStatus(True, "2.0.1", True, "supported"),
    )

    async def runner(_config):
        return _workflow_result()

    report = asyncio.run(run_live_vendor_qualification(_config(), workflow_runner=runner))

    assert report.status is LiveVendorQualificationStatus.PASS
    assert report.all_required_vendors_pass is True
    assert [item.vendor for item in report.vendors] == ["ROCKWELL", "SIEMENS", "SCHNEIDER"]
    assert all(item.complete_plcs == 1 for item in report.vendors)
    assert all(item.definitive_current_evidence >= 1 for item in report.vendors)


def test_one_vendor_workflow_failure_fails_overall(monkeypatch):
    import devagent.live.vendor_qualification as module

    monkeypatch.setattr(
        module,
        "detect_live_opcua_runtime",
        lambda: LiveOpcUaRuntimeStatus(True, "2.0.1", True, "supported"),
    )

    async def runner(_config):
        return _workflow_result(schneider_state=LiveCommissioningState.CONNECT_FAILED)

    report = asyncio.run(run_live_vendor_qualification(_config(), workflow_runner=runner))

    assert report.status is LiveVendorQualificationStatus.FAIL
    assert report.vendors[2].status is LiveVendorQualificationStatus.FAIL
    assert report.all_required_vendors_pass is False


def test_missing_vendor_is_blocked_even_when_other_vendors_pass(monkeypatch):
    import devagent.live.vendor_qualification as module

    monkeypatch.setattr(
        module,
        "detect_live_opcua_runtime",
        lambda: LiveOpcUaRuntimeStatus(True, "2.0.1", True, "supported"),
    )
    config = SimpleNamespace(
        source_sha256="abc",
        specs=(_spec("Rockwell Automation", "r1"), _spec("Siemens", "s1")),
    )

    async def runner(_config):
        return SimpleNamespace(
            plc_results={
                "r1": _result_item(LiveCommissioningState.COMPLETE),
                "s1": _result_item(LiveCommissioningState.COMPLETE),
            }
        )

    report = asyncio.run(run_live_vendor_qualification(config, workflow_runner=runner))

    assert report.status is LiveVendorQualificationStatus.BLOCKED
    assert report.vendors[2].vendor == "SCHNEIDER"
    assert report.vendors[2].status is LiveVendorQualificationStatus.BLOCKED


def test_soak_blocks_before_connect_when_runtime_is_unsupported(monkeypatch):
    import devagent.live.soak as module

    monkeypatch.setattr(
        module,
        "detect_live_opcua_runtime",
        lambda: LiveOpcUaRuntimeStatus(False, None, False, "asyncua missing"),
    )

    report = asyncio.run(
        run_live_soak(
            _config(),
            duration_seconds=0.1,
            interval_seconds=0.01,
        )
    )

    assert report.status is LiveSoakStatus.BLOCKED
    assert report.setup_error == "DEPENDENCY:asyncua missing"
    assert report.actual_duration_seconds < 1
    assert all(item.status is LiveSoakStatus.BLOCKED for item in report.plcs)
    assert all(item.cycles == 0 for item in report.plcs)
