from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from devagent.plc import run_production_verification_v5
from devagent.plc.models import PLCOutcome
from devagent.plc.production_report import render_production_report
from devagent.plc.siemens_scl_control_flow_v2 import siemens_capability_profile_v2


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _complete(root: Path) -> Path:
    return _write(
        root / "Complete.scl",
        '''
ORGANIZATION_BLOCK "Main"
VAR_TEMP
    Start : Bool;
    Guard : Bool;
    Run : Bool;
END_VAR
BEGIN
    IF Start AND Guard THEN
        Run := TRUE;
    ELSE
        Run := FALSE;
    END_IF;
END_ORGANIZATION_BLOCK
''',
    )


def _elsif(root: Path) -> Path:
    return _write(
        root / "Modes.scl",
        '''
ORGANIZATION_BLOCK "Main"
VAR_TEMP
    ModeA : Bool;
    ModeB : Bool;
    Run : Bool;
END_VAR
BEGIN
    IF ModeA THEN
        Run := TRUE;
    ELSIF ModeB THEN
        Run := TRUE;
    ELSE
        Run := FALSE;
    END_IF;
END_ORGANIZATION_BLOCK
''',
    )


def _missing_else(root: Path) -> Path:
    return _write(
        root / "MissingElse.scl",
        '''
ORGANIZATION_BLOCK "Main"
VAR_TEMP
    Start : Bool;
    Run : Bool;
END_VAR
BEGIN
    IF Start THEN
        Run := TRUE;
    END_IF;
END_ORGANIZATION_BLOCK
''',
    )


def _nested(root: Path) -> Path:
    return _write(
        root / "Nested.scl",
        '''
ORGANIZATION_BLOCK "Main"
VAR_TEMP
    Start : Bool;
    Guard : Bool;
    Run : Bool;
END_VAR
BEGIN
    IF Start THEN
        IF Guard THEN
            Run := TRUE;
        ELSE
            Run := FALSE;
        END_IF;
    ELSE
        Run := FALSE;
    END_IF;
END_ORGANIZATION_BLOCK
''',
    )


def _qualify_complete(path: Path) -> dict[str, object]:
    result = run_production_verification_v5(path)
    project = result.engineering.project
    profile = siemens_capability_profile_v2(project)
    if result.engineering.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        raise RuntimeError(f"Complete V2 IF/ELSE should be STATICALLY_VERIFIED, got {result.engineering.outcome.value}")
    if profile["schema"] != "devagent-siemens-tia-capability-v2":
        raise RuntimeError(f"Unexpected Siemens V2 capability schema: {profile['schema']}")
    if profile["static_contract"] != "COMPLETE" or profile["if_chain_models"] != 1:
        raise RuntimeError(f"Complete V2 control-flow contract not established: {profile}")
    if project.st_statement_semantic_count != project.st_statement_total:
        raise RuntimeError(
            f"Complete V2 chain should have full normalized SCL coverage, got {project.st_statement_semantic_count}/{project.st_statement_total}"
        )
    chain_logic = [item for item in project.output_logic if item.origin.startswith("SIEMENS_SCL_IF_CHAIN:")]
    if len(chain_logic) != 1 or chain_logic[0].output_tag != "Run":
        raise RuntimeError("Complete V2 IF/ELSE did not produce exactly one Run theorem object")
    if any(risk.category == "MULTIPLE_WRITERS" and "Run" in risk.title for risk in result.risks):
        raise RuntimeError("Mutually exclusive branch assignments were incorrectly treated as independent multiple writers")
    fat = [item for item in result.engineering.fat_tests if item.output_tag == "Run"]
    if len(fat) < 2 or any(item.execution_status != "NOT_RUN" for item in fat):
        raise RuntimeError("V2 static theorem must generate engineer FAT while preserving NOT_RUN execution status")
    report = render_production_report(result)
    if "### Siemens V2 Bounded Control-Flow Theorem" not in report:
        raise RuntimeError("Professional report is missing the Siemens V2 control-flow theorem section")
    if "does not execute PLCSIM, HIL, or a real PLC" not in report:
        raise RuntimeError("Siemens V2 report lost the external-execution trust boundary")
    return {
        "static_outcome": result.engineering.outcome.value,
        "support_contract": profile["static_contract"],
        "if_chain_models": profile["if_chain_models"],
        "if_chain_output_logic": profile["if_chain_output_logic"],
        "scl_statements": project.st_statement_total,
        "full_scl_statements": project.st_statement_semantic_count,
        "fat_candidates": len(fat),
        "runtime_results_imported": len(result.executions),
    }


def _qualify_elsif(path: Path) -> dict[str, object]:
    result = run_production_verification_v5(path)
    if result.engineering.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        raise RuntimeError("Bounded ELSIF chain should be statically modeled")
    logic = next(item for item in result.engineering.project.output_logic if item.output_tag == "Run")
    paths = [{term.tag: term.required for term in path.terms} for path in logic.paths]
    required = {"ModeA": False, "ModeB": True}
    if required not in paths or {"ModeB": True} in paths:
        raise RuntimeError(f"ELSIF exclusivity theorem is incorrect: {paths}")
    return {"true_paths": paths}


def _qualify_fail_closed(path: Path, label: str) -> dict[str, object]:
    result = run_production_verification_v5(path)
    if result.engineering.outcome is not PLCOutcome.PARTIALLY_VERIFIED:
        raise RuntimeError(f"{label} must remain PARTIALLY_VERIFIED")
    if any(item.origin.startswith("SIEMENS_SCL_IF_CHAIN:") for item in result.engineering.project.output_logic):
        raise RuntimeError(f"{label} incorrectly received a V2 IF-chain theorem object")
    runtime = [item for item in result.engineering.fat_tests if item.scenario == "SCL_RUNTIME"]
    if not runtime or any(item.execution_status != "NOT_RUN" for item in runtime):
        raise RuntimeError(f"{label} must generate NOT_RUN engineer runtime FAT")
    return {
        "static_outcome": result.engineering.outcome.value,
        "runtime_fat_candidates": len(runtime),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Qualify DevAgent Siemens TIA SCL control-flow V2")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="devagent-siemens-v2-") as directory:
        root = Path(directory)
        report = {
            "schema": "devagent-siemens-production-qualification-v2",
            "contract": "bounded complete single-level IF/ELSIF/ELSE Boolean assignment semantics; no PLC execution",
            "complete_if_else": _qualify_complete(_complete(root)),
            "elsif_exclusivity": _qualify_elsif(_elsif(root)),
            "missing_else_fail_closed": _qualify_fail_closed(_missing_else(root), "missing ELSE"),
            "nested_if_fail_closed": _qualify_fail_closed(_nested(root), "nested IF"),
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
