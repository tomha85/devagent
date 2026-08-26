from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from devagent.config import ProviderConfig, load_config, provider_defaults
from devagent.plc.models import plc_jsonable
from devagent.plc.production_v5 import run_production_verification_v5
from devagent.plc.production_models import ReadinessStatus
from devagent.plc.production_report import render_production_report
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="devagent plc",
        description="Evidence-driven PLC engineering review, requirements verification, qualified FAT execution, cryptographic trust, regression, and policy-based release readiness",
    )
    parser.add_argument("project", type=Path, help="Rockwell Studio 5000 full-project .L5X export")
    parser.add_argument(
        "--requirements",
        action="append",
        default=[],
        type=Path,
        metavar="FILE",
        help="Requirement artifact (.txt/.md/.csv/.json/.docx; .pdf when pypdf is installed). Repeatable.",
    )
    parser.add_argument("--baseline", type=Path, help="Previous full-project L5X for semantic regression analysis")
    parser.add_argument(
        "--execution-results",
        type=Path,
        help="JSON execution evidence from a simulator/HIL/controller backend",
    )
    parser.add_argument(
        "--execution-backend-registry",
        type=Path,
        help="Approved backend qualification registry. Required when --execution-results is supplied.",
    )
    parser.add_argument(
        "--release-policy",
        type=Path,
        help="Signed production release policy. External policies always require trusted Ed25519 verification.",
    )
    parser.add_argument(
        "--trust-store",
        type=Path,
        help="Operator-supplied trusted signer store for release policy/evidence/approval verification.",
    )
    parser.add_argument(
        "--approval",
        type=Path,
        help="JSON human engineering approval bound to the exact V5 verification context",
    )
    parser.add_argument("--ai", action="store_true", help="Enable evidence-constrained AI engineering review and requirement trace candidates")
    parser.add_argument("--require-ai", action="store_true", help="Fail the run if requested AI review cannot complete")
    parser.add_argument("--provider", help="Override configured AI provider for this PLC run")
    parser.add_argument("--model", help="Override configured AI model for this PLC run")
    parser.add_argument("--base-url", help="Override OpenAI-compatible base URL for this PLC run")
    parser.add_argument("--output-dir", type=Path, help="Write the complete evidence/FAT package here")
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
    files: dict[str, object | str] = {
        "canonical_ir.json": result.engineering.project,
        "dependency_graph.json": result.engineering.graph,
        "static_verification.json": {
            "outcome": result.engineering.outcome,
            "checks": result.engineering.static_checks,
            "limitations": result.engineering.limitations,
        },
        "engineering_review.json": result.engineering_findings,
        "requirements.json": result.requirements,
        "requirement_verification.json": result.requirement_verification,
        "fat_tests.json": result.engineering.fat_tests,
        "execution_plan.json": {
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
            "tests": result.engineering.fat_tests,
        },
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
    # Keep the v4 manifest schema identifier for backwards-compatible consumers;
    # V5 trust semantics are explicitly advertised by production_profile and the
    # added policy/trust/context fields below.
    manifest = {
        "schema": "devagent-plc-run-v4",
        "production_profile": "PLC_V5_TRUST",
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.require_ai:
        args.ai = True
    if args.execution_results is not None and args.execution_backend_registry is None:
        print(
            "DevAgent PLC failed: --execution-results requires --execution-backend-registry",
            file=sys.stderr,
        )
        return 1
    if args.release_policy is not None and args.trust_store is None:
        print(
            "DevAgent PLC failed: --release-policy requires --trust-store because external production policies must be signed",
            file=sys.stderr,
        )
        return 1
    try:
        provider, provider_name, model_name = _provider_from_args(args)
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
        print("DevAgent PLC is working...")
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
