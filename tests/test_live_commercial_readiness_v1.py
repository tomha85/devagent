from __future__ import annotations

import json
from pathlib import Path

from devagent.live.commercial_readiness import (
    LiveCommercialGateStatus,
    evaluate_live_commercial_readiness,
    write_live_commercial_readiness_artifacts,
)


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _runtime_payload() -> dict:
    return {
        "schema": "devagent-live-release-qualification-v1",
        "mode": "READ_ONLY",
        "status": "PASS",
        "counts": {"PASS": 14, "FAIL": 0, "BLOCKED": 0},
        "cases": [
            {"case_id": f"LQ-{index:03d}", "status": "PASS"}
            for index in range(1, 15)
        ],
    }


def _vendor_payload() -> dict:
    vendors = []
    for vendor, plc_id in (
        ("ROCKWELL", "rockwell-1"),
        ("SIEMENS", "siemens-1"),
        ("SCHNEIDER", "schneider-1"),
    ):
        vendors.append(
            {
                "vendor": vendor,
                "status": "PASS",
                "plc_ids": [plc_id],
                "complete_plcs": 1,
                "definitive_current_evidence": 3,
                "accepted_mappings": 3,
                "unresolved_mappings": 0,
            }
        )
    return {
        "schema": "devagent-live-vendor-qualification-v1",
        "mode": "READ_ONLY",
        "status": "PASS",
        "required_vendors": ["ROCKWELL", "SIEMENS", "SCHNEIDER"],
        "all_required_vendors_pass": True,
        "vendors": vendors,
    }


def _doctor_payload() -> dict:
    return {
        "schema": "devagent-live-doctor-v1",
        "mode": "READ_ONLY",
        "status": "PASS",
        "checks": [
            {"check_id": f"DR-{index:03d}", "status": "PASS"}
            for index in range(1, 9)
        ],
    }


def _soak_row(plc_id: str) -> dict:
    return {
        "plc_id": plc_id,
        "status": "PASS",
        "final_state": "CONNECTED",
        "cycles": 100,
        "total_values": 100,
        "current_values": 99,
        "noncurrent_values": 1,
        "read_error_cycles": 1,
        "max_consecutive_error_cycles": 1,
        "current_ratio": 0.99,
    }


def _soak_payload(*, duration_hours: float = 8.1, total_hours: float | None = None) -> dict:
    total = duration_hours if total_hours is None else total_hours
    return {
        "schema": "devagent-live-soak-v1",
        "mode": "READ_ONLY",
        "status": "PASS",
        "setup_error": None,
        "requested_duration_seconds": duration_hours * 3600.0,
        "actual_duration_seconds": total * 3600.0,
        "read_loop_duration_seconds": duration_hours * 3600.0,
        "plcs": [
            _soak_row("rockwell-1"),
            _soak_row("siemens-1"),
            _soak_row("schneider-1"),
        ],
    }


def _all_artifacts(tmp_path: Path, *, soak_hours: float = 8.1):
    return {
        "runtime_qualification_path": _write(tmp_path / "runtime.json", _runtime_payload()),
        "vendor_qualification_path": _write(tmp_path / "vendor.json", _vendor_payload()),
        "doctor_path": _write(tmp_path / "doctor.json", _doctor_payload()),
        "soak_path": _write(tmp_path / "soak.json", _soak_payload(duration_hours=soak_hours)),
    }


def test_missing_real_evidence_is_blocked_not_falsely_passed():
    report = evaluate_live_commercial_readiness()
    assert report.commercial_v1_ready is False
    assert report.status is LiveCommercialGateStatus.BLOCKED
    by_id = {gate.gate_id: gate for gate in report.gates}
    assert by_id["CV1-001"].status is LiveCommercialGateStatus.BLOCKED
    assert by_id["CV1-002"].status is LiveCommercialGateStatus.BLOCKED
    assert by_id["CV1-003"].status is LiveCommercialGateStatus.PASS
    assert by_id["CV1-004"].status is LiveCommercialGateStatus.PASS
    assert by_id["CV1-005"].status is LiveCommercialGateStatus.BLOCKED


def test_exact_five_gate_evidence_reaches_commercial_v1_ready(tmp_path: Path):
    report = evaluate_live_commercial_readiness(**_all_artifacts(tmp_path))
    assert report.status is LiveCommercialGateStatus.PASS
    assert report.commercial_v1_ready is True
    assert [gate.gate_id for gate in report.gates] == [
        "CV1-001", "CV1-002", "CV1-003", "CV1-004", "CV1-005"
    ]
    assert all(gate.status is LiveCommercialGateStatus.PASS for gate in report.gates)


def test_runtime_artifact_with_duplicate_case_ids_fails_closed(tmp_path: Path):
    artifacts = _all_artifacts(tmp_path)
    payload = _runtime_payload()
    payload["cases"][-1]["case_id"] = "LQ-001"
    _write(artifacts["runtime_qualification_path"], payload)
    report = evaluate_live_commercial_readiness(**artifacts)
    assert report.status is LiveCommercialGateStatus.FAIL
    assert report.gates[0].status is LiveCommercialGateStatus.FAIL


def test_vendor_artifact_requires_real_current_evidence_for_each_vendor(tmp_path: Path):
    artifacts = _all_artifacts(tmp_path)
    payload = _vendor_payload()
    payload["vendors"][1]["definitive_current_evidence"] = 0
    _write(artifacts["vendor_qualification_path"], payload)
    report = evaluate_live_commercial_readiness(**artifacts)
    assert report.status is LiveCommercialGateStatus.FAIL
    assert report.gates[1].status is LiveCommercialGateStatus.FAIL


def test_malformed_vendor_counter_fails_instead_of_raising(tmp_path: Path):
    artifacts = _all_artifacts(tmp_path)
    payload = _vendor_payload()
    payload["vendors"][0]["complete_plcs"] = "unknown"
    _write(artifacts["vendor_qualification_path"], payload)
    report = evaluate_live_commercial_readiness(**artifacts)
    assert report.status is LiveCommercialGateStatus.FAIL
    assert report.gates[1].status is LiveCommercialGateStatus.FAIL


def test_doctor_requires_exact_dr001_through_dr008(tmp_path: Path):
    artifacts = _all_artifacts(tmp_path)
    payload = _doctor_payload()
    payload["checks"][-1]["check_id"] = "DR-001"
    _write(artifacts["doctor_path"], payload)
    report = evaluate_live_commercial_readiness(**artifacts)
    assert report.status is LiveCommercialGateStatus.FAIL
    assert report.gates[4].status is LiveCommercialGateStatus.FAIL


def test_soak_shorter_than_eight_hours_fails_commercial_gate(tmp_path: Path):
    report = evaluate_live_commercial_readiness(
        **_all_artifacts(tmp_path, soak_hours=7.99),
        min_soak_hours=8.0,
    )
    assert report.commercial_v1_ready is False
    assert report.status is LiveCommercialGateStatus.FAIL
    assert report.gates[4].status is LiveCommercialGateStatus.FAIL


def test_setup_time_cannot_satisfy_eight_hour_soak_gate(tmp_path: Path):
    artifacts = _all_artifacts(tmp_path)
    payload = _soak_payload(duration_hours=7.5, total_hours=8.5)
    payload["requested_duration_seconds"] = 8.5 * 3600.0
    _write(artifacts["soak_path"], payload)
    report = evaluate_live_commercial_readiness(**artifacts, min_soak_hours=8.0)
    assert report.status is LiveCommercialGateStatus.FAIL
    assert report.gates[4].status is LiveCommercialGateStatus.FAIL


def test_soak_requires_each_plc_row_to_pass(tmp_path: Path):
    artifacts = _all_artifacts(tmp_path)
    payload = _soak_payload()
    payload["plcs"][2]["status"] = "FAIL"
    _write(artifacts["soak_path"], payload)
    report = evaluate_live_commercial_readiness(**artifacts)
    assert report.status is LiveCommercialGateStatus.FAIL
    assert report.gates[4].status is LiveCommercialGateStatus.FAIL


def test_commercial_artifact_hash_binds_report(tmp_path: Path):
    report = evaluate_live_commercial_readiness(**_all_artifacts(tmp_path))
    output = tmp_path / "commercial"
    written = write_live_commercial_readiness_artifacts(output, report)
    assert written == output.resolve()
    payload = json.loads((output / "live_commercial_readiness.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    assert payload["commercial_v1_ready"] is True
    assert manifest["commercial_v1_ready"] is True
    assert manifest["status"] == "PASS"
    assert len(manifest["artifacts"]["live_commercial_readiness.json"]["sha256"]) == 64


def test_commercial_artifact_refuses_overwrite(tmp_path: Path):
    report = evaluate_live_commercial_readiness(**_all_artifacts(tmp_path))
    output = tmp_path / "commercial"
    output.mkdir()
    try:
        write_live_commercial_readiness_artifacts(output, report)
    except FileExistsError:
        pass
    else:
        raise AssertionError("commercial readiness artifacts must not overwrite an existing directory")
