from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from devagent.plc import analyze_plc_project, run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.siemens_state_machine_v5 import siemens_capability_profile_v5


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _source(body: str, declarations: str = "") -> str:
    return f"""
ORGANIZATION_BLOCK Main
VAR
    State : Int;
    Start : Bool;
    Done : Bool;
    Reset : Bool;
{declarations}
END_VAR
BEGIN
{body}
END_ORGANIZATION_BLOCK
"""


def _complete(root: Path) -> dict[str, object]:
    source = _write(
        root / "complete.scl",
        _source(
            """
CASE State OF
    0:
        IF Start THEN
            State := 10;
        END_IF;
    10:
        IF Done THEN
            State := 20;
        ELSIF Reset THEN
            State := 0;
        END_IF;
    20:
        IF Reset THEN
            State := 0;
        END_IF;
END_CASE;
"""
        ),
    )
    result = analyze_plc_project(source)
    profile = siemens_capability_profile_v5(result.project)
    facts = result.project._siemens_v5_state_machine_facts
    if result.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        raise RuntimeError(f"bounded state machine did not close statically: {result.outcome}")
    if profile["state_machine_contract"] != "COMPLETE":
        raise RuntimeError(f"state-machine contract incomplete: {profile}")
    if profile["state_machine_transitions"] != 4:
        raise RuntimeError(f"unexpected transition count: {profile}")
    if any(
        item.semantic_state is not PLCSemanticState.FULL
        for item in result.project.logic_statements
        if item.language == "SCL"
    ):
        raise RuntimeError("complete bounded CASE left SCL statements partially modeled")
    return {
        "outcome": result.outcome.value,
        "states": list(facts.machines[0].states),
        "transitions": profile["state_machine_transitions"],
        "contract": profile["state_machine_contract"],
    }


def _overlap(root: Path) -> dict[str, object]:
    source = _write(
        root / "overlap.scl",
        _source(
            """
CASE State OF
    0:
        IF Start THEN
            State := 10;
        END_IF;
        IF Start THEN
            State := 20;
        END_IF;
    10:
        IF Reset THEN
            State := 0;
        END_IF;
    20:
        IF Reset THEN
            State := 0;
        END_IF;
END_CASE;
"""
        ),
    )
    result = run_production_verification_v5(source)
    profile = siemens_capability_profile_v5(result.engineering.project)
    if result.engineering.outcome is not PLCOutcome.PARTIALLY_VERIFIED:
        raise RuntimeError("overlapping transitions did not fail closed")
    if profile["state_machine_overlap_conflicts"] != 1:
        raise RuntimeError(f"overlap conflict was not detected: {profile}")
    if not any(risk.category == "SEQUENCE_AMBIGUITY" for risk in result.risks):
        raise RuntimeError("overlap conflict lost sequence risk")
    return {
        "outcome": result.engineering.outcome.value,
        "overlap_conflicts": profile["state_machine_overlap_conflicts"],
        "risk": "SEQUENCE_AMBIGUITY",
    }


def _dangling(root: Path) -> dict[str, object]:
    source = _write(
        root / "dangling.scl",
        _source(
            """
CASE State OF
    0:
        IF Start THEN
            State := 10;
        END_IF;
END_CASE;
"""
        ),
    )
    result = run_production_verification_v5(source)
    profile = siemens_capability_profile_v5(result.engineering.project)
    if result.engineering.outcome is not PLCOutcome.PARTIALLY_VERIFIED:
        raise RuntimeError("dangling transition did not fail closed")
    if profile["state_machine_dangling_targets"] != 1:
        raise RuntimeError(f"dangling target was not detected: {profile}")
    if not any(risk.category == "SEQUENCE_GAP" for risk in result.risks):
        raise RuntimeError("dangling transition lost sequence-gap risk")
    return {
        "outcome": result.engineering.outcome.value,
        "dangling_targets": profile["state_machine_dangling_targets"],
        "risk": "SEQUENCE_GAP",
    }


def _timer(root: Path) -> dict[str, object]:
    source = _write(
        root / "timer.scl",
        _source(
            """
CASE State OF
    0:
        IF Start THEN
            State := 10;
        END_IF;
    10:
        Delay(IN := TRUE, PT := T#1s);
        IF Delay.Q THEN
            State := 20;
        END_IF;
    20:
        IF Reset THEN
            State := 0;
        END_IF;
END_CASE;
""",
            declarations="    Delay : TON;",
        ),
    )
    result = run_production_verification_v5(source)
    profile = siemens_capability_profile_v5(result.engineering.project)
    machine = result.engineering.project._siemens_v5_state_machine_facts.machines[0]
    if result.engineering.outcome is not PLCOutcome.PARTIALLY_VERIFIED:
        raise RuntimeError("timer-dependent sequence incorrectly received whole-project static closure")
    if machine.runtime_dependencies != ("Delay:TON",):
        raise RuntimeError(f"timer dependency was not bound: {machine.runtime_dependencies}")
    if not any(
        test.scenario == "SIEMENS_STATE_TRANSITION"
        and "Runtime dependency: Delay:TON" in test.expected
        and test.execution_status == "NOT_RUN"
        for test in result.engineering.fat_tests
    ):
        raise RuntimeError("timer-dependent transition lost NOT_RUN engineer FAT")
    return {
        "outcome": result.engineering.outcome.value,
        "runtime_dependencies": list(machine.runtime_dependencies),
        "fat_status": "NOT_RUN",
        "external_execution": False,
        "profile_runtime_dependencies": profile["state_machine_runtime_dependencies"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Qualify DevAgent Siemens TIA V5 sequencing/state-machine contract"
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="devagent-siemens-v5-") as directory:
        root = Path(directory)
        report = {
            "schema": "devagent-siemens-production-qualification-v5",
            "contract": (
                "Siemens-only bounded SCL CASE state machines with exact scalar state identity, "
                "simple state labels/targets, Boolean IF/ELSIF/ELSE transition guards, deterministic "
                "overlap/dangling checks, and runtime-only timer/counter sequencing evidence"
            ),
            "bounded_complete_machine": _complete(root),
            "overlap_fail_closed": _overlap(root),
            "dangling_fail_closed": _dangling(root),
            "timer_runtime_boundary": _timer(root),
            "result": "PASS",
        }

    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    print(encoded, end="")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
