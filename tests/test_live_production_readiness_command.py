from __future__ import annotations

import json

from devagent.live import cli
from devagent.live.readiness import (
    LiveProductionControlResult,
    LiveProductionControlStatus,
    LiveProductionReadinessRating,
    LiveProductionReadinessReport,
)
from datetime import datetime, timezone


def _report(*, runtime_status=LiveProductionControlStatus.BLOCKED):
    controls = [
        LiveProductionControlResult(
            control_id=f"PR-{index:03d}",
            title=f"Control {index}",
            status=LiveProductionControlStatus.PASS,
            detail="pass",
        )
        for index in range(1, 10)
    ]
    controls.append(
        LiveProductionControlResult(
            control_id="PR-010",
            title="Runtime qualification",
            status=runtime_status,
            detail="runtime",
        )
    )
    return LiveProductionReadinessReport(
        generated_at=datetime.now(timezone.utc),
        controls=tuple(controls),
    )


def test_readiness_command_candidate_returns_zero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "devagent.live.readiness.evaluate_live_production_readiness",
        lambda qualification=None: _report(),
    )

    rc = cli.main(["readiness"])
    output = capsys.readouterr().out

    assert rc == 0
    assert "Score: 9/10" in output
    assert "Rating: PRODUCTION_CANDIDATE" in output
    assert "Production qualified: NO" in output
    assert "reserved" in output.lower()


def test_readiness_command_runtime_failure_returns_two(monkeypatch) -> None:
    monkeypatch.setattr(
        "devagent.live.readiness.evaluate_live_production_readiness",
        lambda qualification=None: _report(runtime_status=LiveProductionControlStatus.FAIL),
    )

    assert cli.main(["readiness"]) == 2


def test_readiness_command_forwards_qualification_path(monkeypatch, tmp_path) -> None:
    qualification = tmp_path / "qualification.json"
    qualification.write_text("{}", encoding="utf-8")
    observed = {}

    def evaluate(value=None):
        observed["value"] = value
        return _report()

    monkeypatch.setattr("devagent.live.readiness.evaluate_live_production_readiness", evaluate)

    assert cli.main(["readiness", "--qualification-report", str(qualification)]) == 0
    assert observed["value"] == qualification


def test_readiness_command_writes_requested_artifacts(monkeypatch, tmp_path) -> None:
    report = _report()
    observed = {}

    monkeypatch.setattr(
        "devagent.live.readiness.evaluate_live_production_readiness",
        lambda qualification=None: report,
    )

    def write(output_dir, value):
        observed["output"] = output_dir
        observed["report"] = value
        return output_dir

    monkeypatch.setattr(
        "devagent.live.readiness.write_live_production_readiness_artifacts",
        write,
    )
    output = tmp_path / "readiness"

    assert cli.main(["readiness", "--output-dir", str(output)]) == 0
    assert observed == {"output": output, "report": report}


def test_readiness_subcommand_is_public_and_has_no_literal_password_option() -> None:
    parser = cli._build_parser()
    subparsers = next(
        action for action in parser._actions
        if action.__class__.__name__ == "_SubParsersAction"
    )

    assert "readiness" in subparsers.choices
    readiness = subparsers.choices["readiness"]
    options = {
        option
        for action in readiness._actions
        for option in action.option_strings
    }
    assert "--qualification-report" in options
    assert "--output-dir" in options
    assert "--password" not in options


def test_readiness_json_artifact_stays_distinct_from_runtime_qualification(tmp_path) -> None:
    from devagent.live.readiness import write_live_production_readiness_artifacts

    output = tmp_path / "readiness"
    write_live_production_readiness_artifacts(output, _report())
    payload = json.loads((output / "live_production_readiness.json").read_text(encoding="utf-8"))

    assert payload["schema"] == "devagent-live-production-readiness-v1"
    assert payload["score"] == 9
    assert payload["production_qualified"] is False
    assert payload["rating"] == LiveProductionReadinessRating.PRODUCTION_CANDIDATE.value
