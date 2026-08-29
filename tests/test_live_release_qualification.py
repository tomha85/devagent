from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from devagent.live.qualification import (
    LIVE_RELEASE_QUALIFICATION_CASES,
    LiveQualificationCaseResult,
    LiveQualificationStatus,
    LiveReleaseQualificationReport,
    run_live_release_qualification,
    write_live_release_qualification_artifacts,
)


def test_catalog_has_stable_unique_case_ids_and_expected_runtime_split() -> None:
    ids = [case.case_id for case in LIVE_RELEASE_QUALIFICATION_CASES]
    assert ids == [f"LQ-{index:03d}" for index in range(1, 15)]
    assert len(ids) == len(set(ids)) == 14
    assert sum(not case.runtime_required for case in LIVE_RELEASE_QUALIFICATION_CASES) == 2
    assert sum(case.runtime_required for case in LIVE_RELEASE_QUALIFICATION_CASES) == 12


def test_missing_runtime_blocks_runtime_cases_without_hiding_deterministic_passes() -> None:
    report = asyncio.run(
        run_live_release_qualification(runtime_available=False, runtime_version=None)
    )
    assert report.status is LiveQualificationStatus.BLOCKED
    assert report.all_passed is False
    assert report.counts() == {"PASS": 2, "FAIL": 0, "BLOCKED": 12}
    assert [case.status for case in report.cases[:2]] == [
        LiveQualificationStatus.PASS,
        LiveQualificationStatus.PASS,
    ]
    assert all(
        case.status is LiveQualificationStatus.BLOCKED
        for case in report.cases[2:]
    )
    assert report.runtime_available is False
    assert report.runtime_version is None


def test_all_injected_cases_can_produce_release_pass() -> None:
    async def passing(_context):
        return "injected pass"

    overrides = {case.case_id: passing for case in LIVE_RELEASE_QUALIFICATION_CASES}
    report = asyncio.run(
        run_live_release_qualification(
            runtime_available=True,
            runtime_version="2.test",
            runner_overrides=overrides,
        )
    )
    assert report.status is LiveQualificationStatus.PASS
    assert report.all_passed is True
    assert report.counts() == {"PASS": 14, "FAIL": 0, "BLOCKED": 0}
    assert report.runtime_version == "2.test"


def test_failure_has_precedence_over_blocked() -> None:
    async def failing(_context):
        raise AssertionError("qualification failure")

    report = asyncio.run(
        run_live_release_qualification(
            runtime_available=False,
            runner_overrides={"LQ-001": failing},
        )
    )
    assert report.status is LiveQualificationStatus.FAIL
    assert report.counts() == {"PASS": 1, "FAIL": 1, "BLOCKED": 12}
    assert report.cases[0].status is LiveQualificationStatus.FAIL
    assert "qualification failure" in report.cases[0].detail


def test_runner_errors_are_secret_redacted() -> None:
    async def leaking(_context):
        raise RuntimeError("login failed: devagent-qualification-password")

    report = asyncio.run(
        run_live_release_qualification(
            runtime_available=False,
            runner_overrides={"LQ-001": leaking},
        )
    )
    detail = report.cases[0].detail
    assert "devagent-qualification-password" not in detail
    assert "<redacted>" in detail


def test_unknown_runner_override_is_rejected() -> None:
    async def passing(_context):
        return None

    with pytest.raises(ValueError, match="Unknown qualification case"):
        asyncio.run(
            run_live_release_qualification(
                runtime_available=False,
                runner_overrides={"LQ-999": passing},
            )
        )


def test_report_status_properties_for_manual_case_sets() -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    def make(status):
        return LiveQualificationCaseResult(
            case_id="X",
            title="X",
            status=status,
            detail="x",
            runtime_required=False,
            duration_seconds=0.0,
        )

    passed = LiveReleaseQualificationReport(now, now, True, "2", (make(LiveQualificationStatus.PASS),))
    blocked = LiveReleaseQualificationReport(now, now, False, None, (make(LiveQualificationStatus.BLOCKED),))
    failed = LiveReleaseQualificationReport(
        now,
        now,
        False,
        None,
        (make(LiveQualificationStatus.BLOCKED), make(LiveQualificationStatus.FAIL)),
    )
    assert passed.status is LiveQualificationStatus.PASS and passed.all_passed
    assert blocked.status is LiveQualificationStatus.BLOCKED and not blocked.all_passed
    assert failed.status is LiveQualificationStatus.FAIL and not failed.all_passed


def test_json_shape_is_explicitly_read_only() -> None:
    report = asyncio.run(
        run_live_release_qualification(runtime_available=False)
    )
    payload = report.as_dict()
    assert payload["schema"] == "devagent-live-release-qualification-v1"
    assert payload["mode"] == "READ_ONLY"
    assert payload["status"] == "BLOCKED"
    assert payload["runtime"]["asyncua_available"] is False
    assert len(payload["cases"]) == 14


def test_artifact_writer_hashes_report_and_refuses_overwrite(tmp_path: Path) -> None:
    report = asyncio.run(
        run_live_release_qualification(runtime_available=False)
    )
    destination = tmp_path / "qualification"
    written = write_live_release_qualification_artifacts(destination, report)
    assert written == destination.resolve()

    report_path = destination / "live_release_qualification.json"
    manifest_path = destination / "manifest.json"
    assert report_path.is_file()
    assert manifest_path.is_file()

    payload = json.loads(report_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "BLOCKED"
    assert payload["mode"] == "READ_ONLY"
    assert manifest["qualification_status"] == "BLOCKED"
    assert manifest["mode"] == "READ_ONLY"

    import hashlib

    digest = hashlib.sha256(report_path.read_bytes()).hexdigest()
    assert manifest["artifacts"]["live_release_qualification.json"]["sha256"] == digest
    assert manifest["artifacts"]["live_release_qualification.json"]["bytes"] == len(report_path.read_bytes())

    with pytest.raises(FileExistsError):
        write_live_release_qualification_artifacts(destination, report)


def test_qualification_api_has_no_control_operations() -> None:
    from devagent.live import qualification

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
        assert not hasattr(qualification, prohibited)
