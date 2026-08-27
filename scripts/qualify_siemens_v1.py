from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from devagent.plc import analyze_plc_project, run_production_verification_v5
from devagent.plc.models import PLCOutcome
from devagent.plc.production_report import render_production_report
from devagent.plc.siemens_tia_v1 import SiemensInputError, siemens_capability_profile


def _write(path: Path, text: str) -> Path:
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def _bounded_bundle(root: Path) -> Path:
    bundle = root / "bounded-tia-export"
    bundle.mkdir()
    _write(
        bundle / "Main.scl",
        '''
ORGANIZATION_BLOCK "Main"
VAR_TEMP
    Start : Bool;
    Guard : Bool;
    Run : Bool;
END_VAR
BEGIN
    Run := Start AND Guard;
END_ORGANIZATION_BLOCK
''',
    )
    _write(
        bundle / "Tags.xml",
        '''
<Document>
  <SW.Tags.PlcTag ID="1"><AttributeList><Name>EStopHealthy</Name><DataTypeName>Bool</DataTypeName><LogicalAddress>%I0.1</LogicalAddress></AttributeList></SW.Tags.PlcTag>
</Document>
''',
    )
    return bundle


def _partial_bundle(root: Path) -> Path:
    bundle = root / "partial-tia-export"
    bundle.mkdir()
    _write(
        bundle / "Controlled.scl",
        '''
ORGANIZATION_BLOCK "Main"
VAR_TEMP
    Start : Bool;
    Guard : Bool;
    Run : Bool;
END_VAR
BEGIN
    IF Start THEN
        Run := Guard;
    END_IF;
END_ORGANIZATION_BLOCK
''',
    )
    return bundle


def _qualify_bounded(path: Path) -> dict[str, object]:
    result = run_production_verification_v5(path)
    project = result.engineering.project
    profile = siemens_capability_profile(project)
    if project.metadata.vendor != "Siemens":
        raise RuntimeError(f"Expected Siemens vendor, got {project.metadata.vendor}")
    if result.engineering.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        raise RuntimeError(f"Bounded Siemens source should be STATICALLY_VERIFIED, got {result.engineering.outcome.value}")
    if profile["static_contract"] != "COMPLETE":
        raise RuntimeError(f"Bounded Siemens support contract should be COMPLETE, got {profile['static_contract']}")
    if project.st_statement_total != 1 or project.st_statement_semantic_count != 1:
        raise RuntimeError(f"Expected one FULL SCL statement, got {project.st_statement_semantic_count}/{project.st_statement_total}")
    if len(project.output_logic) != 1 or project.output_logic[0].instruction != "ASSIGN_BOOL":
        raise RuntimeError("Bounded Siemens Boolean assignment theorem was not created exactly once")
    if len(result.engineering.fat_tests) < 2:
        raise RuntimeError(f"Expected positive + negative Siemens FAT procedures, got {len(result.engineering.fat_tests)}")
    if any(test.execution_status != "NOT_RUN" for test in result.engineering.fat_tests):
        raise RuntimeError("Static Siemens qualification must never mark FAT as executed")
    if result.readiness is None or result.readiness.status.value != "NOT_READY":
        raise RuntimeError("Project-only static Siemens qualification must remain NOT_READY without runtime/requirements evidence")
    if result.executions:
        raise RuntimeError("Static Siemens qualification unexpectedly imported runtime execution evidence")
    report = render_production_report(result)
    for marker in (
        "PROJECT_ONLY_ENGINEERING_REVIEW",
        "### Siemens TIA Export Inventory",
        "## Engineer FAT Procedures",
        "## Release Readiness",
    ):
        if marker not in report:
            raise RuntimeError(f"Siemens professional report missing marker: {marker}")
    return {
        "vendor": project.metadata.vendor,
        "source_sha256": project.metadata.source_sha256,
        "tags": len(project.tags),
        "routines": len(project.routines),
        "scl_statements": project.st_statement_total,
        "full_scl_statements": project.st_statement_semantic_count,
        "boolean_output_logic": len(project.output_logic),
        "dependency_edges": len(result.engineering.graph.edges),
        "fat_candidates": len(result.engineering.fat_tests),
        "static_outcome": result.engineering.outcome.value,
        "support_contract": profile["static_contract"],
        "readiness": result.readiness.status.value,
    }


def _qualify_partial(path: Path) -> dict[str, object]:
    result = run_production_verification_v5(path)
    profile = siemens_capability_profile(result.engineering.project)
    runtime = [item for item in result.engineering.fat_tests if item.scenario == "SCL_RUNTIME"]
    if result.engineering.outcome is not PLCOutcome.PARTIALLY_VERIFIED:
        raise RuntimeError("Control-flow Siemens source must remain PARTIALLY_VERIFIED in V1")
    if not runtime or any(item.execution_status != "NOT_RUN" for item in runtime):
        raise RuntimeError("Partial Siemens SCL must generate NOT_RUN engineer runtime FAT")
    if not any(risk.category == "SEMANTIC_COVERAGE" for risk in result.risks):
        raise RuntimeError("Partial Siemens SCL did not surface a semantic-coverage risk")
    return {
        "partial_statements": profile["partial_statements"],
        "runtime_fat_candidates": len(runtime),
        "static_outcome": result.engineering.outcome.value,
        "support_contract": profile["static_contract"],
    }


def _qualify_proprietary_rejection(root: Path) -> dict[str, object]:
    path = _write(root / "Machine.zap20", "not a parsed project")
    try:
        analyze_plc_project(path)
    except SiemensInputError as exc:
        message = str(exc)
        if "Openness" not in message or "GenerateSource" not in message:
            raise RuntimeError(f"Proprietary TIA rejection lacks export guidance: {message}") from exc
        return {"proprietary_archive_rejected": True, "reason": message}
    raise RuntimeError("Proprietary TIA .zap20 archive was incorrectly accepted")


def main() -> int:
    parser = argparse.ArgumentParser(description="Qualify DevAgent Siemens TIA engineering-export V1")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="devagent-siemens-v1-") as directory:
        root = Path(directory)
        report = {
            "schema": "devagent-siemens-production-qualification-v1",
            "input_contract": "TIA Portal Openness/XML or GenerateSource engineering export; no proprietary project execution",
            "bounded": _qualify_bounded(_bounded_bundle(root)),
            "partial_fail_closed": _qualify_partial(_partial_bundle(root)),
            "proprietary_project_contract": _qualify_proprietary_rejection(root),
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
