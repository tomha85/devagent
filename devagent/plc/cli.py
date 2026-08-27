from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from devagent.config import ProviderConfig, load_config, provider_defaults
from devagent.plc.models import plc_jsonable
from devagent.plc.production_models import (
    ReadinessStatus,
    StageRecord,
    capture_stage_progress,
)
from devagent.plc.production_report import render_production_report
from devagent.plc.production_v5 import run_production_verification_v5
from devagent.plc.production_verification import (
    compute_requirements_sha256,
    compute_test_plan_sha256,
)
from devagent.plc.rockwell_l5x import L5XError
from devagent.providers import ProviderError, create_provider

_STAGE_NAMES = (
    "PROJECT VALIDATION",
    "CANONICAL PLC IR",
    "LOGIC SEMANTICS",
    "DEPENDENCY GRAPH",
    "AI ENGINEERING REVIEW",
    "REQUIREMENT INGESTION",
    "REQUIREMENT VERIFICATION",
    "TEST GENERATION",
    "TEST EXECUTION",
    "RISK DETECTION",
    "OPTIMIZATION REVIEW",
    "REGRESSION ANALYSIS",
    "RECOMMENDATIONS",
    "EVIDENCE + FAT REPORT",
    "RELEASE READINESS",
)


class _PLCProgressStatus:
    """Render live PLC pipeline milestones while preserving final stage records."""

    def __init__(
        self,
        sink: Callable[[str], None] = print,
        *,
        verbose: bool = False,
    ) -> None:
        self.sink = sink
        self.verbose = verbose
        self._active_stage = 0
        self._completed: set[int] = set()

    def start(self) -> None:
        if self._active_stage == 0:
            self._show_stage(1)

    def _show_stage(self, number: int) -> None:
        if number < 1 or number > len(_STAGE_NAMES):
            return
        if number <= self._active_stage:
            return
        self._active_stage = number
        self.sink(f"[{number:2d}/15] {_STAGE_NAMES[number - 1]}")

    def __call__(self, stage: StageRecord) -> None:
        if stage.number not in self._completed:
            self._completed.add(stage.number)
            if stage.number > self._active_stage:
                self._show_stage(stage.number)
            if self.verbose:
                self.sink(f"      -> {stage.status.value}: {stage.summary}")
            if stage.number == self._active_stage:
                self._show_stage(stage.number + 1)
            return

        # Production V5 intentionally re-finalizes trust/execution/readiness
        # records after the V4 deterministic core. Keep concise output stable,
        # but make those refinements visible when the engineer asks for detail.
        if self.verbose:
            self.sink(
                f"      -> FINALIZED {stage.number:2d}/15 {stage.name}: "
                f"{stage.status.value}: {stage.summary}"
            )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devagent plc",
        description=(
            "Evidence-driven Rockwell Studio 5000 and Siemens TIA PLC engineering review, requirements verification, "
            "logic/risk analysis, regression impact analysis, and engineer-ready FAT planning. DevAgent does not connect "
            "to or execute external PLC software."
        ),
    )
    parser.add_argument(
        "project",
        type=Path,
        help="Rockwell full-project .L5X or Siemens TIA exported engineering file/directory",
    )
    parser.add_argument(
        "--requirements",
        action="append",
        default=[],
        type=Path,
        metavar="FILE",
        help="Requirement artifact (.txt/.md/.csv/.json/.docx; .pdf when pypdf is installed). Repeatable.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="Previous PLC project export for revision impact, regression risk, affected requirements, and FAT retest analysis",
    )
    parser.add_argument(
        "--execution-results",
        type=Path,
        help=(
            "Optional signed results imported after the PLC engineer manually executes FAT in an external test environment. "
            "DevAgent does not launch or control that environment."
        ),
    )
    parser.add_argument(
        "--execution-backend-registry",
        type=Path,
        help="Optional signed provenance/qualification registry required only when importing --execution-results.",
    )
    parser.add_argument(
        "--release-policy",
        type=Path,
        help="Optional signed engineering/release policy. External policies require trusted Ed25519 verification.",
    )
    parser.add_argument(
        "--trust-store",
        type=Path,
        help="Operator-supplied trusted signer store for imported policy/evidence/approval verification.",
    )
    parser.add_argument(
        "--approval",
        type=Path,
        help="Optional signed human engineering approval bound to the exact verification context",
    )
    parser.add_argument("--ai", action="store_true", help="Enable evidence-constrained AI engineering review and requirement trace candidates")
    parser.add_argument("--require-ai", action="store_true", help="Fail the run if requested AI review cannot complete")
    parser.add_argument("--provider", help="Override configured AI provider for this PLC run")
    parser.add_argument("--model", help="Override configured AI model for this PLC run")
    parser.add_argument("--base-url", help="Override OpenAI-compatible base URL for this PLC run")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show live PLC stage completion summaries and V5 finalization details",
    )
    parser.add_argument("--output-dir", type=Path, help="Write the complete FAT planning and engineering review package here")
    parser.add_argument("--no-write", action="store_true", help="Print the report without writing run artifacts")
    return parser


def _default_output_dir(project: Path) -> Path:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return project.expanduser().resolve(strict=False).parent / ".devagent" / "plc-runs" / run_id


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(plc_jsonable(value), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _persist_run(output_dir: Path, result, report: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=False)
    project_sha = result.engineering.project.metadata.source_sha256
    plan_sha = compute_test_plan_sha256(result.engineering.fat_tests)
    requirements_sha = compute_requirements_sha256(result.requirements)

    fat_plan = {
        "schema": "devagent-plc-fat-plan-v1",
        "project_sha256": project_sha,
        "test_plan_sha256": plan_sha,
        "requirements_sha256": requirements_sha,
        "baseline_sha256": result.baseline_sha256,
        "execution_owner": "PLC_ENGINEER",
        "devagent_connects_to_external_test_software": False,
        "tests": result.engineering.fat_tests,
    }
    # Preserve the established artifact name/schema for existing consumers while
    # making the ownership boundary explicit. This is a planning artifact only;
    # DevAgent does not launch, control, or write to external PLC test software.
    execution_plan = {
        "schema": "devagent-plc-execution-plan-v3",
        "project_sha256": project_sha,
        "test_plan_sha256": plan_sha,
        "requirements_sha256": requirements_sha,
        "backend_registry_sha256": result.execution_backend_registry_sha256,
        "execution_results_sha256": result.execution_results_sha256,
        "baseline_sha256": result.baseline_sha256,
        "release_policy_sha256": result.release_policy_sha256,
        "trust_store_sha256": result.trust_store_sha256,
        "verification_context_sha256": result.verification_context_sha256,
        "execution_owner": "PLC_ENGINEER",
        "devagent_connects_to_external_test_software": False,
        "tests": result.engineering.fat_tests,
    }

    files: dict[str, object | str] = {
        "canonical_ir.json": result.engineering.project,
        "dependency_graph.json": result.engineering.graph,
        "static_verification.json": {
            "outcome": result.engineering.outcome,
            "checks": result.engineering.static_checks,
            "limitations": result.engineering.limitations,
        },
        "engineering_review.json": result.engineering_findings,
        "agent_harness_trace.json": result.ai_harness_trace,
        "requirements.json": result.requirements,
        "requirement_verification.json": result.requirement_verification,
        "fat_tests.json": result.engineering.fat_tests,
        "fat_plan.json": fat_plan,
        "execution_plan.json": execution_plan,
        "test_execution.json": result.executions,
        "risks.json": result.risks,
        "optimizations.json": result.optimizations,
        "regression.json": result.regression_changes,
        "recommendations.json": result.recommendations,
        "evidence_manifest.json": {
            "project_sha256": project_sha,
            "backend_registry_sha256": result.execution_backend_registry_sha256,
            "execution_results_sha256": result.execution_results_sha256,
            "release_policy_sha256": result.release_policy_sha256,
            "trust_store_sha256": result.trust_store_sha256,
            "verification_context_sha256": result.verification_context_sha256,
            "verified_signatures": result.verified_signatures,
            "items": result.evidence,
            "warnings": result.warnings,
        },
        "release_readiness.json": result.readiness,
        "pipeline_stages.json": result.stages,
        "fat_report.md": report,
    }
    if result.execution_backend_registry is not None:
        files["execution_backend_registry_normalized.json"] = result.execution_backend_registry
    if result.release_policy is not None:
        files["release_policy_normalized.json"] = result.release_policy
    if result.trust_store is not None:
        files["trusted_signers_normalized.json"] = result.trust_store
    if result.verified_signatures:
        files["verified_signatures.json"] = result.verified_signatures

    written: list[Path] = []
    for name, value in files.items():
        path = output_dir / name
        if isinstance(value, str):
            path.write_text(value, encoding="utf-8")
        else:
            _write_json(path, value)
        written.append(path)

    manifest = {
        # V12 is additive at the artifact level. Keep the run-manifest schema
        # stable so existing automation does not break when FAT planning fields
        # are added.
        "schema": "devagent-plc-run-v4",
        "production_profile": "PLC_ENGINEERING_REVIEW_FAT_PLANNING",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project_source": result.engineering.project.metadata.source_path,
        "project_sha256": project_sha,
        "ai_provider": result.ai_provider,
        "ai_model": result.ai_model,
        "readiness": result.readiness.status.value if result.readiness else "NOT_EVALUATED",
        "test_plan_sha256": plan_sha,
        "requirements_sha256": requirements_sha,
        "backend_registry_sha256": result.execution_backend_registry_sha256,
        "execution_backend_id": result.execution_backend_id,
        "execution_backend_kind": result.execution_backend_kind,
        "execution_results_sha256": result.execution_results_sha256,
        "baseline_sha256": result.baseline_sha256,
        "release_policy_sha256": result.release_policy_sha256,
        "release_policy_id": (result.release_policy or {}).get("policy_id"),
        "trust_store_sha256": result.trust_store_sha256,
        "verified_signature_count": len(result.verified_signatures),
        "verification_context_sha256": result.verification_context_sha256,
        "execution_owner": "PLC_ENGINEER",
        "devagent_connects_to_external_test_software": False,
        "artifacts": {path.name: _sha256(path) for path in written},
    }
    _write_json(output_dir / "run_manifest.json", manifest)


def _provider_from_args(args) -> tuple[object | None, str | None, str | None]:
    if not args.ai:
        return None, None, None
    config = load_config()
    if args.provider:
        provider = args.provider.lower()
        default_model, default_env = provider_defaults(provider)
        config = ProviderConfig(
            provider=provider,
            model=args.model or default_model,
            base_url=args.base_url,
            api_key_env=default_env,
            timeout_seconds=config.timeout_seconds,
        )
    else:
        config = ProviderConfig(
            provider=config.provider,
            model=args.model or config.model,
            base_url=args.base_url or config.base_url,
            api_key_env=config.api_key_env,
            timeout_seconds=config.timeout_seconds,
        )
    return create_provider(config), config.provider, config.model


def _validate_execution_args(args) -> str | None:
    if args.execution_results is not None and args.execution_backend_registry is None:
        return "--execution-results requires --execution-backend-registry for imported-result provenance"
    if args.release_policy is not None and args.trust_store is None:
        return "--release-policy requires --trust-store because external policies must be signed"
    return None


def _print_run_header(args, provider_name: str | None, model_name: str | None) -> None:
    project = args.project.expanduser().resolve(strict=False)
    print("DevAgent PLC is working...")
    print(f"Project: {project}")
    print(f"Requirements: {len(args.requirements)} artifact(s)")
    print(f"Baseline: {'YES' if args.baseline is not None else 'NO'}")
    if args.ai:
        print(f"AI review: ENABLED ({provider_name or 'configured'}/{model_name or 'configured'})")
    else:
        print("AI review: DISABLED")
    print("")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.require_ai:
        args.ai = True
    argument_error = _validate_execution_args(args)
    if argument_error:
        print(f"DevAgent PLC failed: {argument_error}", file=sys.stderr)
        return 1

    try:
        provider, provider_name, model_name = _provider_from_args(args)
        _print_run_header(args, provider_name, model_name)
        progress = _PLCProgressStatus(verbose=args.verbose)
        progress.start()
        with capture_stage_progress(progress):
            result = run_production_verification_v5(
                args.project,
                requirement_paths=args.requirements,
                baseline_path=args.baseline,
                execution_results_path=args.execution_results,
                execution_backend_registry_path=args.execution_backend_registry,
                release_policy_path=args.release_policy,
                trust_store_path=args.trust_store,
                approval_path=args.approval,
                provider=provider,
                ai_enabled=args.ai,
                require_ai=args.require_ai,
                ai_provider_name=provider_name,
                ai_model_name=model_name,
            )

        report = render_production_report(result)
        print("")
        print("Final stage results:")
        for stage in result.stages:
            print(f"[{stage.number:2d}/15] {stage.name:<26} {stage.status.value}")
        print("")
        print(report, end="")
        if not args.no_write:
            output_dir = (
                args.output_dir.expanduser().resolve(strict=False)
                if args.output_dir
                else _default_output_dir(args.project)
            )
            _persist_run(output_dir, result, report)
            print(f"Artifacts: {output_dir}")
        if result.readiness and result.readiness.status in {
            ReadinessStatus.READY_FOR_ENGINEERING_APPROVAL,
            ReadinessStatus.APPROVED_FOR_RELEASE,
        }:
            return 0
        return 2
    except (L5XError, OSError, ValueError, ProviderError) as exc:
        print(f"DevAgent PLC failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
