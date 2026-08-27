from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.production_models import RequirementStatus
from devagent.plc.siemens_call_graph_v3 import siemens_capability_profile_v3


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _qualified(root: Path) -> dict[str, object]:
    source = _write(
        root / "qualified.scl",
        '''
FUNCTION_BLOCK "ChildFB"
VAR_INPUT
    Start : Bool;
END_VAR
VAR_OUTPUT
    Run : Bool;
END_VAR
BEGIN
    Run := Start;
END_FUNCTION_BLOCK

FUNCTION_BLOCK "ParentFB"
VAR_INPUT
    Start : Bool;
END_VAR
VAR_OUTPUT
    Run : Bool;
END_VAR
VAR_STAT
    Child : ChildFB;
END_VAR
BEGIN
    Child(Start := Start, Run => Run);
END_FUNCTION_BLOCK

DATA_BLOCK "ParentDB" "ParentFB"
BEGIN
END_DATA_BLOCK

ORGANIZATION_BLOCK "Main"
VAR_TEMP
    MainStart : Bool;
    MotorRun : Bool;
END_VAR
BEGIN
    ParentDB(Start := MainStart, Run => MotorRun);
END_ORGANIZATION_BLOCK
''',
    )
    requirement = _write(
        root / "requirements.md",
        "REQ-V3-QUAL: When MainStart=TRUE, MotorRun=TRUE.",
    )
    result = run_production_verification_v5(
        source,
        requirement_paths=[requirement],
    )
    project = result.engineering.project
    facts = project._siemens_v3_facts
    profile = siemens_capability_profile_v3(project)
    if result.engineering.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        raise RuntimeError(
            "qualified nested call closure should be STATICALLY_VERIFIED: "
            f"{result.engineering.outcome}"
        )
    if profile["execution_closure"] != "COMPLETE" or profile["calls_bound"] != 2:
        raise RuntimeError(f"nested call closure not complete: {profile}")
    if set(profile["reachable_blocks"]) != {"Main", "ParentFB", "ChildFB"}:
        raise RuntimeError(
            f"unexpected reachable closure: {profile['reachable_blocks']}"
        )
    if (
        result.requirement_verification[0].status
        is not RequirementStatus.STATICALLY_VERIFIED
    ):
        raise RuntimeError(
            "cross-block requirement was not proven: "
            f"{result.requirement_verification[0]}"
        )
    if not facts.projected_logic_ids:
        raise RuntimeError(
            "qualified call closure did not project any cross-block theorem"
        )
    return {
        "outcome": result.engineering.outcome.value,
        "calls": profile["calls"],
        "calls_bound": profile["calls_bound"],
        "reachable_blocks": profile["reachable_blocks"],
        "projected_call_theorems": profile["projected_call_theorems"],
        "requirement_status": result.requirement_verification[0].status.value,
    }


def _fail_closed(root: Path) -> dict[str, object]:
    source = _write(
        root / "fail_closed.scl",
        '''
FUNCTION "LogicFC"
VAR_INPUT
    Start : Bool;
    Guard : Bool;
END_VAR
VAR_OUTPUT
    Run : Bool;
END_VAR
BEGIN
    Run := Start AND Guard;
END_FUNCTION

ORGANIZATION_BLOCK "Main"
VAR_TEMP
    MainStart : Bool;
    MotorRun : Bool;
END_VAR
BEGIN
    LogicFC(Start := MainStart, Run => MotorRun);
END_ORGANIZATION_BLOCK
''',
    )
    result = run_production_verification_v5(source)
    facts = result.engineering.project._siemens_v3_facts
    if result.engineering.outcome is not PLCOutcome.PARTIALLY_VERIFIED:
        raise RuntimeError(
            "missing required call binding must remain PARTIALLY_VERIFIED"
        )
    if (
        len(facts.calls) != 1
        or facts.calls[0].semantic_state is not PLCSemanticState.PARTIAL
    ):
        raise RuntimeError(f"missing binding was not withheld: {facts.calls}")
    if not facts.calls[0].resolution.startswith("missing_required_binding:"):
        raise RuntimeError(
            f"unexpected fail-closed reason: {facts.calls[0].resolution}"
        )
    if not any(
        item.scenario == "SIEMENS_CALL_RUNTIME"
        for item in result.engineering.fat_tests
    ):
        raise RuntimeError(
            "fail-closed call did not generate engineer runtime FAT"
        )
    return {
        "outcome": result.engineering.outcome.value,
        "reason": facts.calls[0].resolution,
        "runtime_fat": sum(
            item.scenario == "SIEMENS_CALL_RUNTIME"
            for item in result.engineering.fat_tests
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Qualify DevAgent Siemens TIA call/interface execution closure V3"
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="devagent-siemens-v3-") as directory:
        root = Path(directory)
        report = {
            "schema": "devagent-siemens-production-qualification-v3",
            "contract": (
                "bounded OB/FB/FC call graph, FC/instance-DB/multi-instance target "
                "resolution, named interface binding, OB reachability, and cross-block "
                "Boolean theorem projection; no PLC execution"
            ),
            "qualified_nested_call_closure": _qualified(root),
            "missing_binding_fail_closed": _fail_closed(root),
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
