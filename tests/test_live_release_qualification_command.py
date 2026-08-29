from __future__ import annotations

import json

import devagent.live as live
from devagent.live import cli, qualification


def test_qualify_list_is_offline_and_lists_stable_matrix(monkeypatch, capsys) -> None:
    async def forbidden_run(*args, **kwargs):
        raise AssertionError("qualification execution must not run for --list")

    monkeypatch.setattr(qualification, "run_live_release_qualification", forbidden_run)
    assert cli.main(["qualify", "--list"]) == 0
    output = capsys.readouterr().out
    assert "DEVAGENT LIVE RELEASE QUALIFICATION" in output
    assert "Mode: READ ONLY" in output
    assert "LQ-001 [DETERMINISTIC] Read-only public surface" in output
    assert "LQ-014 [RUNTIME] Runtime browse surface remains read-only" in output
    assert "Overall:" not in output


def test_qualify_missing_runtime_returns_two_and_reports_blocked(monkeypatch, capsys) -> None:
    monkeypatch.setattr(qualification, "_runtime_info", lambda: (False, None))
    assert cli.main(["qualify"]) == 2
    output = capsys.readouterr().out
    assert "asyncua: UNAVAILABLE" in output
    assert "[PASS] LQ-001" in output
    assert "[PASS] LQ-002" in output
    assert "[BLOCKED] LQ-003" in output
    assert "Overall: BLOCKED (PASS=2 FAIL=0 BLOCKED=12)" in output


def test_qualify_blocked_run_writes_evidence_artifacts(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(qualification, "_runtime_info", lambda: (False, None))
    output_dir = tmp_path / "release-qualification"
    assert cli.main(["qualify", "--output-dir", str(output_dir)]) == 2
    capsys.readouterr()

    payload = json.loads((output_dir / "live_release_qualification.json").read_text(encoding="utf-8"))
    manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
    assert payload["status"] == "BLOCKED"
    assert payload["counts"] == {"BLOCKED": 12, "FAIL": 0, "PASS": 2}
    assert payload["mode"] == "READ_ONLY"
    assert manifest["qualification_status"] == "BLOCKED"


def test_public_live_api_exports_release_qualification_surface() -> None:
    expected = (
        "LIVE_RELEASE_QUALIFICATION_CASES",
        "LiveQualificationCase",
        "LiveQualificationCaseResult",
        "LiveQualificationStatus",
        "LiveReleaseQualificationReport",
        "run_live_release_qualification",
        "write_live_release_qualification_artifacts",
    )
    for name in expected:
        assert name in live.__all__
        assert getattr(live, name) is not None

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
        assert not hasattr(live.LiveReleaseQualificationReport, prohibited)
