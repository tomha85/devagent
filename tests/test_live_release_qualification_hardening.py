from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from devagent.live.qualification import (
    LiveQualificationCaseResult,
    LiveQualificationStatus,
    LiveReleaseQualificationReport,
    run_live_release_qualification,
    write_live_release_qualification_artifacts,
)


def test_unsupported_asyncua_major_blocks_runtime_cases() -> None:
    report = asyncio.run(
        run_live_release_qualification(runtime_available=True, runtime_version="3.0.0")
    )
    assert report.status is LiveQualificationStatus.BLOCKED
    assert report.counts() == {"PASS": 2, "FAIL": 0, "BLOCKED": 12}
    assert all("requires asyncua>=2.0,<3" in case.detail for case in report.cases[2:])


def test_report_json_redacts_qualification_secret_at_output_boundary() -> None:
    now = datetime.now(timezone.utc)
    report = LiveReleaseQualificationReport(
        now,
        now,
        True,
        "2.0.1",
        (
            LiveQualificationCaseResult(
                "X",
                "X",
                LiveQualificationStatus.FAIL,
                "leaked devagent-qualification-password",
                False,
                0.0,
            ),
        ),
    )
    rendered = json.dumps(report.as_dict())
    assert "devagent-qualification-password" not in rendered
    assert "<redacted>" in rendered


def test_artifact_writer_rolls_back_partial_directory_on_failure(tmp_path, monkeypatch) -> None:
    report = asyncio.run(run_live_release_qualification(runtime_available=False))
    destination = tmp_path / "partial"
    original = Path.write_text

    def fail_manifest(self, data, *args, **kwargs):
        if self.name == "manifest.json":
            raise OSError("simulated manifest failure")
        return original(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_manifest)
    with pytest.raises(OSError, match="simulated manifest failure"):
        write_live_release_qualification_artifacts(destination, report)
    assert not destination.exists()
