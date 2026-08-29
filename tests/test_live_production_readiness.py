from __future__ import annotations

import hashlib
import json

import pytest

from devagent.live.readiness import (
    LiveProductionControlStatus,
    LiveProductionReadinessRating,
    evaluate_live_production_readiness,
    write_live_production_readiness_artifacts,
)


def _passing_controls():
    return {
        f"PR-{index:03d}": (lambda index=index: f"control {index} pass")
        for index in range(1, 10)
    }


def _qualification(status: str, passed: int, failed: int, blocked: int):
    return {
        "schema": "devagent-live-release-qualification-v1",
        "mode": "READ_ONLY",
        "status": status,
        "counts": {"PASS": passed, "FAIL": failed, "BLOCKED": blocked},
    }


def test_missing_runtime_evidence_is_exactly_nine_of_ten_candidate() -> None:
    report = evaluate_live_production_readiness(_control_checks=_passing_controls())

    assert report.score == 9
    assert report.max_score == 10
    assert report.meets_nine_of_ten is True
    assert report.production_qualified is False
    assert report.rating is LiveProductionReadinessRating.PRODUCTION_CANDIDATE
    assert report.controls[-1].status is LiveProductionControlStatus.BLOCKED


def test_blocked_runtime_evidence_is_nine_of_ten_without_false_runtime_pass() -> None:
    report = evaluate_live_production_readiness(
        _qualification("BLOCKED", 2, 0, 12),
        _control_checks=_passing_controls(),
    )

    assert report.score == 9
    assert report.rating is LiveProductionReadinessRating.PRODUCTION_CANDIDATE
    assert report.production_qualified is False
    assert "PASS=2" in report.controls[-1].detail
    assert "BLOCKED=12" in report.controls[-1].detail


def test_real_fourteen_of_fourteen_upgrades_to_ten_of_ten() -> None:
    report = evaluate_live_production_readiness(
        _qualification("PASS", 14, 0, 0),
        _control_checks=_passing_controls(),
    )

    assert report.score == 10
    assert report.production_qualified is True
    assert report.rating is LiveProductionReadinessRating.PRODUCTION_QUALIFIED
    assert report.controls[-1].status is LiveProductionControlStatus.PASS


def test_runtime_failure_is_not_ready_even_with_nine_other_passes() -> None:
    report = evaluate_live_production_readiness(
        _qualification("FAIL", 13, 1, 0),
        _control_checks=_passing_controls(),
    )

    assert report.score == 9
    assert report.has_failures is True
    assert report.meets_nine_of_ten is False
    assert report.rating is LiveProductionReadinessRating.NOT_READY
    assert report.controls[-1].status is LiveProductionControlStatus.FAIL


@pytest.mark.parametrize(
    "payload",
    [
        _qualification("PASS", 13, 0, 1),
        _qualification("BLOCKED", 2, 1, 11),
        {"schema": "wrong", "mode": "READ_ONLY", "status": "PASS", "counts": {"PASS": 14, "FAIL": 0, "BLOCKED": 0}},
        {"schema": "devagent-live-release-qualification-v1", "mode": "WRITE", "status": "PASS", "counts": {"PASS": 14, "FAIL": 0, "BLOCKED": 0}},
        {"schema": "devagent-live-release-qualification-v1", "mode": "READ_ONLY", "status": "PASS", "counts": {"PASS": 13, "FAIL": 0, "BLOCKED": 0}},
    ],
)
def test_invalid_or_inconsistent_runtime_evidence_fails_closed(payload) -> None:
    report = evaluate_live_production_readiness(payload, _control_checks=_passing_controls())

    assert report.controls[-1].status is LiveProductionControlStatus.FAIL
    assert report.rating is LiveProductionReadinessRating.NOT_READY


def test_deterministic_control_failure_prevents_nine_of_ten() -> None:
    checks = _passing_controls()

    def fail_control():
        raise AssertionError("broken deterministic control")

    checks["PR-004"] = fail_control
    report = evaluate_live_production_readiness(_control_checks=checks)

    assert report.score == 8
    assert report.meets_nine_of_ten is False
    assert report.rating is LiveProductionReadinessRating.NOT_READY
    assert report.controls[3].status is LiveProductionControlStatus.FAIL


def test_control_error_redacts_internal_test_secret() -> None:
    checks = _passing_controls()

    def fail_with_secret():
        raise ValueError("devagent-readiness-secret")

    checks["PR-002"] = fail_with_secret
    report = evaluate_live_production_readiness(_control_checks=checks)

    assert "devagent-readiness-secret" not in report.controls[1].detail
    assert "<redacted>" in report.controls[1].detail


def test_control_ids_are_stable_and_complete() -> None:
    report = evaluate_live_production_readiness(_control_checks=_passing_controls())

    assert [item.control_id for item in report.controls] == [
        f"PR-{index:03d}" for index in range(1, 11)
    ]


def test_json_path_qualification_is_supported(tmp_path) -> None:
    qualification_path = tmp_path / "qualification.json"
    qualification_path.write_text(
        json.dumps(_qualification("PASS", 14, 0, 0)),
        encoding="utf-8",
    )

    report = evaluate_live_production_readiness(
        qualification_path,
        _control_checks=_passing_controls(),
    )

    assert report.production_qualified is True


def test_artifacts_bind_score_and_report_hash(tmp_path) -> None:
    report = evaluate_live_production_readiness(_control_checks=_passing_controls())
    output = tmp_path / "readiness"

    written = write_live_production_readiness_artifacts(output, report)
    payload = (written / "live_production_readiness.json").read_bytes()
    manifest = json.loads((written / "manifest.json").read_text(encoding="utf-8"))
    report_json = json.loads(payload)

    assert report_json["score"] == 9
    assert report_json["rating"] == "PRODUCTION_CANDIDATE"
    assert report_json["production_qualified"] is False
    assert manifest["score"] == 9
    assert manifest["mode"] == "READ_ONLY"
    assert manifest["production_qualified"] is False
    assert manifest["artifacts"]["live_production_readiness.json"]["sha256"] == hashlib.sha256(payload).hexdigest()


def test_artifacts_refuse_overwrite(tmp_path) -> None:
    report = evaluate_live_production_readiness(_control_checks=_passing_controls())
    output = tmp_path / "readiness"
    write_live_production_readiness_artifacts(output, report)

    with pytest.raises(FileExistsError):
        write_live_production_readiness_artifacts(output, report)
