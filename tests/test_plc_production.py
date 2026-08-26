from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from devagent.plc.cli import main as plc_main
from devagent.plc.production import (
    compute_requirements_sha256,
    compute_test_plan_sha256,
    run_production_verification,
)
from devagent.plc.production_models import (
    ReadinessStatus,
    RequirementStatus,
    RequirementVerificationMode,
)
from devagent.providers import ScriptedFakeProvider


PROJECT = """<?xml version="1.0" encoding="UTF-8"?>
<RSLogix5000Content SchemaRevision="1.0" SoftwareRevision="36.00" TargetName="Prod" TargetType="Controller">
  <Controller Use="Target" Name="Prod" ProcessorType="1756-L85E" MajorRev="36" MinorRev="11">
    <AddOnInstructionDefinitions />
    <Tags>
      <Tag Name="Start" TagType="Base" DataType="BOOL" />
      <Tag Name="Guard" TagType="Base" DataType="BOOL" />
      <Tag Name="Run" TagType="Base" DataType="BOOL" />
    </Tags>
    <Programs><Program Name="Main"><Routines>
      <Routine Name="Logic" Type="RLL"><RLLContent>
        <Rung Number="0"><Text><![CDATA[XIC(Start)XIC(Guard)OTE(Run);]]></Text></Rung>
      </RLLContent></Routine>
    </Routines></Program></Programs>
    <Tasks><Task Name="MainTask" Type="CONTINUOUS" /></Tasks>
  </Controller>
</RSLogix5000Content>
"""


def _write_project(tmp_path: Path, body: str = PROJECT, name: str = "Machine.L5X") -> Path:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def _write_requirement(tmp_path: Path, text: str, name: str = "requirements.md") -> Path:
    path = tmp_path / name
    path.write_text(text + "\n", encoding="utf-8")
    return path


def _write_registry(
    tmp_path: Path,
    project_sha256: str,
    *,
    backend_id: str = "qualified-test-backend",
    status: str = "QUALIFIED",
) -> Path:
    payload = {
        "schema": "devagent-plc-execution-backend-registry-v1",
        "approved_by": "Controls Platform Owner",
        "approved_at": "2026-08-26T12:00:00Z",
        "backends": [
            {
                "id": backend_id,
                "kind": "SIMULATOR",
                "status": status,
                "project_sha256": [project_sha256],
                "qualification_evidence": ["QUAL-PLC-SIM-001", "procedure://simulator-qualification"],
            }
        ],
    }
    path = tmp_path / "backend-registry.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def _write_execution(
    tmp_path: Path,
    result,
    registry: Path,
    *,
    project_sha: str | None = None,
    plan_sha: str | None = None,
    registry_sha: str | None = None,
    backend: str = "qualified-test-backend",
) -> Path:
    payload = {
        "schema": "devagent-plc-execution-results-v1",
        "project_sha256": project_sha or result.engineering.project.metadata.source_sha256,
        "test_plan_sha256": plan_sha or compute_test_plan_sha256(result.engineering.fat_tests),
        "backend_registry_sha256": registry_sha or hashlib.sha256(registry.read_bytes()).hexdigest(),
        "backend": backend,
        "run_id": "RUN-001",
        "results": [
            {
                "test_id": test.id,
                "status": "PASS",
                "observed": "Expected behavior observed",
                "timestamp": "2026-08-26T12:15:00Z",
                "evidence": [f"trace://{test.id}"],
            }
            for test in result.engineering.fat_tests
        ],
    }
    path = tmp_path / "execution.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_production_pipeline_requires_qualified_execution_then_matching_human_approval(tmp_path: Path) -> None:
    project = _write_project(tmp_path)
    requirements = _write_requirement(
        tmp_path,
        "REQ-1: When Start=TRUE and Guard=TRUE, Run shall be TRUE.",
    )

    static = run_production_verification(project, requirement_paths=[requirements])
    assert len(static.stages) == 15
    assert static.stages[4].name == "AI ENGINEERING REVIEW"
    assert static.stages[4].status.value == "SKIPPED"
    assert static.stages[8].name == "TEST EXECUTION"
    assert static.stages[8].status.value == "NOT_RUN"
    assert static.requirements[0].verification_mode is RequirementVerificationMode.DYNAMIC
    assert static.requirement_verification[0].status is RequirementStatus.STATICALLY_VERIFIED
    assert static.readiness is not None
    assert static.readiness.status is ReadinessStatus.NOT_READY
    assert static.readiness.metrics["requirements_release_gaps"] == 1
    assert static.readiness.score < 100

    registry = _write_registry(tmp_path, static.engineering.project.metadata.source_sha256)
    execution = _write_execution(tmp_path, static, registry)
    dynamic = run_production_verification(
        project,
        requirement_paths=[requirements],
        execution_results_path=execution,
        execution_backend_registry_path=registry,
    )
    assert dynamic.requirement_verification[0].status is RequirementStatus.DYNAMICALLY_VERIFIED
    assert dynamic.execution_backend_id == "qualified-test-backend"
    assert dynamic.execution_backend_registry_sha256 == hashlib.sha256(registry.read_bytes()).hexdigest()
    assert dynamic.readiness is not None
    assert dynamic.readiness.status is ReadinessStatus.READY_FOR_ENGINEERING_APPROVAL
    assert dynamic.verification_context_sha256

    approval = tmp_path / "approval.json"
    approval.write_text(
        json.dumps(
            {
                "project_sha256": dynamic.engineering.project.metadata.source_sha256,
                "test_plan_sha256": compute_test_plan_sha256(dynamic.engineering.fat_tests),
                "requirements_sha256": compute_requirements_sha256(dynamic.requirements),
                "backend_registry_sha256": dynamic.execution_backend_registry_sha256,
                "baseline_sha256": dynamic.baseline_sha256,
                "verification_context_sha256": dynamic.verification_context_sha256,
                "decision": "APPROVE",
                "approved_by": "Lead Controls Engineer",
                "approved_at": "2026-08-26T12:30:00Z",
            }
        ),
        encoding="utf-8",
    )
    approved = run_production_verification(
        project,
        requirement_paths=[requirements],
        execution_results_path=execution,
        execution_backend_registry_path=registry,
        approval_path=approval,
    )
    assert approved.readiness is not None
    assert approved.readiness.status is ReadinessStatus.APPROVED_FOR_RELEASE
    assert approved.readiness.human_approval is not None


def test_execution_evidence_rejects_stale_project_plan_and_registry_hashes(tmp_path: Path) -> None:
    project = _write_project(tmp_path)
    requirements = _write_requirement(tmp_path, "REQ-1: Start=TRUE and Guard=TRUE shall make Run=TRUE.")
    static = run_production_verification(project, requirement_paths=[requirements])
    registry = _write_registry(tmp_path, static.engineering.project.metadata.source_sha256)

    stale_project = _write_execution(tmp_path, static, registry, project_sha="0" * 64)
    with pytest.raises(ValueError, match="project_sha256 does not match"):
        run_production_verification(
            project,
            requirement_paths=[requirements],
            execution_results_path=stale_project,
            execution_backend_registry_path=registry,
        )

    stale_plan = _write_execution(tmp_path, static, registry, plan_sha="f" * 64)
    with pytest.raises(ValueError, match="test_plan_sha256 does not match"):
        run_production_verification(
            project,
            requirement_paths=[requirements],
            execution_results_path=stale_plan,
            execution_backend_registry_path=registry,
        )

    stale_registry = _write_execution(tmp_path, static, registry, registry_sha="a" * 64)
    with pytest.raises(ValueError, match="backend_registry_sha256 does not match"):
        run_production_verification(
            project,
            requirement_paths=[requirements],
            execution_results_path=stale_registry,
            execution_backend_registry_path=registry,
        )


def test_execution_requires_qualified_backend_registry(tmp_path: Path) -> None:
    project = _write_project(tmp_path)
    requirements = _write_requirement(tmp_path, "REQ-1: Start=TRUE and Guard=TRUE shall make Run=TRUE.")
    static = run_production_verification(project, requirement_paths=[requirements])
    qualified = _write_registry(tmp_path, static.engineering.project.metadata.source_sha256)
    execution = _write_execution(tmp_path, static, qualified)

    with pytest.raises(ValueError, match="requires --execution-backend-registry"):
        run_production_verification(
            project,
            requirement_paths=[requirements],
            execution_results_path=execution,
        )

    experimental = _write_registry(
        tmp_path,
        static.engineering.project.metadata.source_sha256,
        status="EXPERIMENTAL",
    )
    execution = _write_execution(tmp_path, static, experimental)
    with pytest.raises(ValueError, match="is not QUALIFIED"):
        run_production_verification(
            project,
            requirement_paths=[requirements],
            execution_results_path=execution,
            execution_backend_registry_path=experimental,
        )


def test_under_specified_true_requirement_is_traceable_not_false_conflict(tmp_path: Path) -> None:
    project = _write_project(tmp_path)
    requirements = _write_requirement(tmp_path, "REQ-1: When Start=TRUE, Run shall be TRUE.")
    result = run_production_verification(project, requirement_paths=[requirements])

    verification = result.requirement_verification[0]
    assert verification.status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert "under-specify" in verification.summary
    assert not any(risk.severity.value == "CRITICAL" for risk in result.risks)


def test_partial_assignment_can_prove_false_when_it_blocks_every_path(tmp_path: Path) -> None:
    project = _write_project(tmp_path)
    requirements = _write_requirement(tmp_path, "REQ-1: When Start=FALSE, Run shall be FALSE.")
    result = run_production_verification(project, requirement_paths=[requirements])

    verification = result.requirement_verification[0]
    assert verification.status is RequirementStatus.STATICALLY_VERIFIED


def test_multiple_writers_withhold_static_requirement_proof(tmp_path: Path) -> None:
    multi = PROJECT.replace(
        "</RLLContent>",
        '<Rung Number="1"><Text><![CDATA[XIC(Guard)OTE(Run);]]></Text></Rung></RLLContent>',
    )
    project = _write_project(tmp_path, multi)
    requirements = _write_requirement(tmp_path, "REQ-1: When Start=TRUE and Guard=TRUE, Run shall be TRUE.")
    result = run_production_verification(project, requirement_paths=[requirements])

    assert result.requirement_verification[0].status is RequirementStatus.TRACEABLE_NOT_PROVEN
    assert "writer logic" in result.requirement_verification[0].summary


def test_static_requirement_policy_can_accept_deterministic_proof(tmp_path: Path) -> None:
    project = _write_project(tmp_path)
    requirements = tmp_path / "requirements.json"
    requirements.write_text(
        json.dumps(
            {
                "requirements": [
                    {
                        "id": "REQ-STATIC",
                        "text": "When Start=TRUE and Guard=TRUE, Run shall be TRUE.",
                        "verification_mode": "STATIC",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    result = run_production_verification(project, requirement_paths=[requirements])

    assert result.requirements[0].verification_mode is RequirementVerificationMode.STATIC
    assert result.requirement_verification[0].status is RequirementStatus.STATICALLY_VERIFIED
    assert result.readiness is not None
    assert result.readiness.metrics["requirements_release_gaps"] == 0
    assert result.readiness.status is ReadinessStatus.NOT_READY


def test_requirement_conflict_creates_critical_risk_and_blocks_release(tmp_path: Path) -> None:
    project = _write_project(tmp_path)
    requirements = _write_requirement(
        tmp_path,
        "REQ-FAIL: When Start=TRUE and Guard=TRUE, Run shall be FALSE.",
    )
    result = run_production_verification(project, requirement_paths=[requirements])

    assert result.requirement_verification[0].status is RequirementStatus.CONFLICT
    assert any(
        risk.category == "REQUIREMENT" and risk.severity.value == "CRITICAL"
        for risk in result.risks
    )
    assert result.readiness is not None
    assert result.readiness.status is ReadinessStatus.BLOCKED


def test_ai_finding_with_unknown_evidence_is_discarded(tmp_path: Path) -> None:
    project = _write_project(tmp_path)
    provider = ScriptedFakeProvider(
        [
            {
                "_role": "plc_engineering_reviewer",
                "findings": [
                    {
                        "id": "FAKE",
                        "category": "SAFETY",
                        "title": "Unsupported claim",
                        "severity": "HIGH",
                        "summary": "Candidate cites evidence that does not exist.",
                        "recommendation": "Do not accept without evidence.",
                        "evidence_ids": ["DOES-NOT-EXIST"],
                        "confidence": 0.99,
                    }
                ],
            }
        ]
    )
    result = run_production_verification(
        project,
        provider=provider,
        ai_enabled=True,
        ai_provider_name="fake",
        ai_model_name="fake",
    )

    assert not any(item.id == "AI-FAKE" for item in result.engineering_findings)
    assert any("unknown evidence IDs" in warning for warning in result.warnings)


def test_semantic_regression_links_changed_output_to_impacted_fat_tests(tmp_path: Path) -> None:
    baseline_text = PROJECT.replace("XIC(Start)XIC(Guard)OTE(Run);", "XIC(Start)OTE(Run);")
    baseline = _write_project(tmp_path, baseline_text, "Baseline.L5X")
    current = _write_project(tmp_path, PROJECT, "Current.L5X")
    requirements = _write_requirement(tmp_path, "REQ-1: Start=TRUE and Guard=TRUE shall make Run=TRUE.")

    result = run_production_verification(
        current,
        requirement_paths=[requirements],
        baseline_path=baseline,
    )

    assert result.regression_changes
    changed = next(change for change in result.regression_changes if "Run" in change.affected_tags)
    assert changed.change_type == "LOGIC_CHANGED"
    assert changed.affected_test_ids
    assert result.baseline_sha256
    assert result.verification_context_sha256


def test_cli_emits_15_stages_and_hashes_every_persisted_artifact(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _write_project(tmp_path)
    requirements = _write_requirement(tmp_path, "REQ-1: Start=TRUE and Guard=TRUE shall make Run=TRUE.")
    output = tmp_path / "run"

    assert plc_main(
        [str(project), "--requirements", str(requirements), "--output-dir", str(output)]
    ) == 2
    stdout = capsys.readouterr().out
    assert "[ 1/15] PROJECT VALIDATION" in stdout
    assert "[ 5/15] AI ENGINEERING REVIEW      SKIPPED" in stdout
    assert "[15/15] RELEASE READINESS" in stdout
    assert "NOT_READY" in stdout

    expected = {
        "canonical_ir.json",
        "dependency_graph.json",
        "static_verification.json",
        "engineering_review.json",
        "requirements.json",
        "requirement_verification.json",
        "fat_tests.json",
        "execution_plan.json",
        "test_execution.json",
        "risks.json",
        "optimizations.json",
        "regression.json",
        "recommendations.json",
        "evidence_manifest.json",
        "release_readiness.json",
        "pipeline_stages.json",
        "fat_report.md",
        "run_manifest.json",
    }
    assert expected <= {path.name for path in output.iterdir()}

    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "devagent-plc-run-v4"
    assert manifest["verification_context_sha256"]
    for name, expected_sha in manifest["artifacts"].items():
        actual = hashlib.sha256((output / name).read_bytes()).hexdigest()
        assert actual == expected_sha


def test_execution_evidence_rejects_non_list_evidence_field(tmp_path: Path) -> None:
    project = _write_project(tmp_path)
    requirements = _write_requirement(tmp_path, "REQ-1: Start=TRUE and Guard=TRUE shall make Run=TRUE.")
    static = run_production_verification(project, requirement_paths=[requirements])
    registry = _write_registry(tmp_path, static.engineering.project.metadata.source_sha256)
    payload = {
        "schema": "devagent-plc-execution-results-v1",
        "project_sha256": static.engineering.project.metadata.source_sha256,
        "test_plan_sha256": compute_test_plan_sha256(static.engineering.fat_tests),
        "backend_registry_sha256": hashlib.sha256(registry.read_bytes()).hexdigest(),
        "backend": "qualified-test-backend",
        "run_id": "RUN-002",
        "results": [
            {
                "test_id": static.engineering.fat_tests[0].id,
                "status": "PASS",
                "evidence": "trace://must-not-be-a-string",
            }
        ],
    }
    execution = tmp_path / "invalid-execution.json"
    execution.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="requires an evidence list"):
        run_production_verification(
            project,
            requirement_paths=[requirements],
            execution_results_path=execution,
            execution_backend_registry_path=registry,
        )


def test_cli_rejects_execution_results_without_backend_registry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = _write_project(tmp_path)
    execution = tmp_path / "execution.json"
    execution.write_text("{}", encoding="utf-8")

    assert plc_main([str(project), "--execution-results", str(execution), "--no-write"]) == 1
    stderr = capsys.readouterr().err
    assert "requires --execution-backend-registry" in stderr
