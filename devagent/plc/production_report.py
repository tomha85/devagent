from __future__ import annotations

from collections import Counter

from devagent.plc.production_models import ExecutionStatus, PLCProductionResult, RequirementStatus


def _pct(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "N/A"
    return f"{100.0 * numerator / denominator:.1f}%"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_None._", ""]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(value.replace("|", "\\|").replace("\n", " ") for value in row)
            + " |"
        )
    lines.append("")
    return lines


def render_production_report(result: PLCProductionResult) -> str:
    project = result.engineering.project
    readiness = result.readiness
    policy = result.release_policy or {}
    lines: list[str] = [
        "# DevAgent PLC Engineering Verification / FAT Report",
        "",
        "## Executive Summary",
        "",
        f"- Controller: `{project.metadata.controller_name}`",
        f"- Processor: `{project.metadata.processor_type or 'unknown'}`",
        f"- Source: `{project.metadata.source_path}`",
        f"- Source SHA-256: `{project.metadata.source_sha256}`",
        f"- Baseline SHA-256: `{result.baseline_sha256 or 'not supplied'}`",
        f"- Static semantic outcome: **{result.engineering.outcome.value}**",
        f"- Release policy: `{policy.get('policy_id', 'not evaluated')}`",
        f"- Release policy SHA-256: `{result.release_policy_sha256 or 'not evaluated'}`",
        f"- Trust store SHA-256: `{result.trust_store_sha256 or 'not supplied'}`",
        f"- Verified signatures: **{len(result.verified_signatures)}**",
        f"- Qualified execution backend: `{result.execution_backend_id or 'not supplied'}`",
        f"- Execution backend kind: `{result.execution_backend_kind or 'not supplied'}`",
        f"- Backend registry SHA-256: `{result.execution_backend_registry_sha256 or 'not supplied'}`",
        f"- Execution results SHA-256: `{result.execution_results_sha256 or 'not supplied'}`",
        f"- Verification context SHA-256: `{result.verification_context_sha256 or 'not evaluated'}`",
        f"- Release readiness: **{readiness.status.value if readiness else 'NOT_EVALUATED'}**",
        f"- Readiness score: **{readiness.score if readiness else 0}/100** (deterministic rubric; not a probability)",
        "",
        "> Release readiness is evidence- and policy-based. Static analysis does not equal machine behavior verification. Dynamic FAT PASS evidence is accepted only from a qualified backend and all configured trust/context gates must match this run.",
        "",
        "## 15-Stage Pipeline",
        "",
    ]
    lines += _table(
        ["Stage", "Status", "Summary"],
        [[f"{item.number}. {item.name}", item.status.value, item.summary] for item in result.stages],
    )

    lines += [
        "## Project / Logic Coverage",
        "",
        f"- Tags: {len(project.tags)}",
        f"- Programs: {len(project.programs)}",
        f"- Routines: {len(project.routines)}",
        f"- RLL rungs: {len(project.rungs)}",
        f"- ST statements: {project.st_statement_total}",
        f"- AOIs: {len(project.aois)}",
        f"- Instruction semantic coverage: {_pct(project.instruction_semantic_count, project.instruction_total)} ({project.instruction_semantic_count}/{project.instruction_total})",
        f"- Branch-path coverage: {_pct(project.branch_rung_semantic_count, project.branch_rung_total)} ({project.branch_rung_semantic_count}/{project.branch_rung_total})",
        f"- ST semantic coverage: {_pct(project.st_statement_semantic_count, project.st_statement_total)} ({project.st_statement_semantic_count}/{project.st_statement_total})",
        f"- AOI body coverage: {_pct(project.aoi_internal_modeled_count, project.aoi_internal_total)} ({project.aoi_internal_modeled_count}/{project.aoi_internal_total})",
        f"- AOI call binding: {_pct(project.aoi_call_bound_count, project.aoi_call_total)} ({project.aoi_call_bound_count}/{project.aoi_call_total})",
        f"- Dependency graph edges: {len(result.engineering.graph.edges)}",
        "",
    ]

    lines += ["## Production Release Policy", ""]
    if not policy:
        lines += ["_No release policy evaluated._", ""]
    else:
        limits = policy.get("max_deterministic_risks", {})
        lines += [
            f"- Policy ID: `{policy.get('policy_id')}`",
            f"- Policy SHA-256: `{result.release_policy_sha256}`",
            f"- Policy source: `{policy.get('source_path')}`",
            f"- Built-in policy: `{'yes' if policy.get('builtin') else 'no'}`",
            f"- Approved by: `{policy.get('approved_by')}`",
            f"- Require dynamic proof for: `{', '.join(policy.get('require_dynamic_for', [])) or 'none'}`",
            f"- Require regression baseline for: `{', '.join(policy.get('require_baseline_for', [])) or 'none'}`",
            f"- Allowed execution backend kinds: `{', '.join(policy.get('allowed_backend_kinds', []))}`",
            f"- Deterministic risk budgets: `CRITICAL={limits.get('CRITICAL')}, HIGH={limits.get('HIGH')}, MEDIUM={limits.get('MEDIUM')}`",
            f"- Require all generated tests PASS: `{'yes' if policy.get('require_all_generated_tests_pass') else 'no'}`",
            f"- Required signature purposes: `{', '.join(policy.get('require_signatures_for', [])) or 'none'}`",
            "",
        ]

    lines += ["## Cryptographic Trust", ""]
    if result.trust_store is None:
        lines += ["_No operator trust store supplied._", ""]
    else:
        lines += [
            f"- Trust store source: `{result.trust_store.get('source_path')}`",
            f"- Trust store SHA-256: `{result.trust_store_sha256}`",
            f"- Trust store approved by: `{result.trust_store.get('approved_by')}`",
            "",
        ]
    lines += _table(
        ["Purpose", "Algorithm", "Trusted key", "Artifact SHA-256"],
        [
            [
                str(item.get("purpose")),
                str(item.get("algorithm")),
                str(item.get("key_id")),
                str(item.get("artifact_sha256")),
            ]
            for item in result.verified_signatures
        ],
    )

    lines += ["## AI / Engineering Review", ""]
    lines += _table(
        ["ID", "Origin", "Severity", "Category", "Finding", "Evidence"],
        [
            [item.id, item.origin, item.severity.value, item.category, item.title, ", ".join(item.evidence_ids)]
            for item in result.engineering_findings
        ],
    )

    req_counts = Counter(item.status for item in result.requirement_verification)
    req_by_id = {item.id: item for item in result.requirements}
    lines += [
        "## Requirement Verification",
        "",
        f"- Requirements ingested: {len(result.requirements)}",
        f"- Dynamically verified: {req_counts[RequirementStatus.DYNAMICALLY_VERIFIED]}",
        f"- Statically verified: {req_counts[RequirementStatus.STATICALLY_VERIFIED]}",
        f"- Traceable, not proven: {req_counts[RequirementStatus.TRACEABLE_NOT_PROVEN]}",
        f"- AI candidates: {req_counts[RequirementStatus.AI_CANDIDATE]}",
        f"- Not mapped: {req_counts[RequirementStatus.NOT_MAPPED]}",
        f"- Conflicts: {req_counts[RequirementStatus.CONFLICT]}",
        "",
    ]
    lines += _table(
        ["Requirement", "Criticality", "Declared verification", "Status", "Matched tags", "Tests", "Evidence", "Summary"],
        [
            [
                item.requirement_id,
                req_by_id[item.requirement_id].criticality.value if item.requirement_id in req_by_id else "UNKNOWN",
                req_by_id[item.requirement_id].verification_mode.value if item.requirement_id in req_by_id else "UNKNOWN",
                item.status.value,
                ", ".join(item.matched_tags),
                ", ".join(item.linked_test_ids),
                ", ".join(item.evidence_ids),
                item.summary,
            ]
            for item in result.requirement_verification
        ],
    )

    lines += ["## Execution Backend Qualification", ""]
    if result.execution_backend_registry is None:
        lines += ["_No execution backend registry supplied._", ""]
    else:
        registry = result.execution_backend_registry
        lines += [
            f"- Registry source: `{registry.get('source_path')}`",
            f"- Registry SHA-256: `{result.execution_backend_registry_sha256}`",
            f"- Approved by: `{registry.get('approved_by')}`",
            f"- Approved at: `{registry.get('approved_at')}`",
            f"- Execution backend used: `{result.execution_backend_id or 'none'}`",
            f"- Execution backend kind: `{result.execution_backend_kind or 'none'}`",
            f"- Execution results SHA-256: `{result.execution_results_sha256 or 'not supplied'}`",
            "",
        ]
        lines += _table(
            ["Backend", "Kind", "Status", "Project scope", "Qualification evidence"],
            [
                [
                    str(item.get("id")),
                    str(item.get("kind")),
                    str(item.get("status")),
                    ", ".join(item.get("project_sha256", [])),
                    ", ".join(item.get("qualification_evidence", [])),
                ]
                for item in registry.get("backends", [])
            ],
        )

    execution_by_test = {item.test_id: item for item in result.executions}
    lines += ["## FAT Test Plan and Execution", ""]
    lines += _table(
        ["Test", "Scenario", "Output", "Preconditions", "Expected", "Execution"],
        [
            [
                test.id,
                test.scenario,
                test.output_tag,
                ", ".join(f"{key}={value}" for key, value in test.preconditions.items()),
                test.expected,
                execution_by_test[test.id].status.value if test.id in execution_by_test else ExecutionStatus.NOT_RUN.value,
            ]
            for test in result.engineering.fat_tests
        ],
    )

    lines += ["## Risk Detection", ""]
    lines += _table(
        ["Risk", "Severity", "Origin", "Category", "Finding", "Consequence", "Recommendation", "Evidence"],
        [
            [
                item.id,
                item.severity.value,
                item.origin,
                item.category,
                item.title,
                item.consequence,
                item.recommendation,
                ", ".join(item.evidence_ids),
            ]
            for item in result.risks
        ],
    )

    lines += ["## Optimization Review", ""]
    lines += _table(
        ["ID", "Category", "Current", "Proposed", "Benefit", "Change risk", "Evidence"],
        [
            [
                item.id,
                item.category,
                item.current_state,
                item.proposed_change,
                item.expected_benefit,
                item.change_risk.value,
                ", ".join(item.evidence_ids),
            ]
            for item in result.optimizations
        ],
    )

    lines += ["## Regression Analysis", ""]
    lines += _table(
        ["Change", "Type", "Subject", "Severity", "Requirements", "Tests", "Summary"],
        [
            [
                item.id,
                item.change_type,
                item.subject,
                item.severity.value,
                ", ".join(item.affected_requirement_ids),
                ", ".join(item.affected_test_ids),
                item.summary,
            ]
            for item in result.regression_changes
        ],
    )

    lines += ["## Recommendations", ""]
    lines += _table(
        ["ID", "Priority", "Title", "Action", "Rationale", "Evidence"],
        [
            [
                item.id,
                item.priority.value,
                item.title,
                item.action,
                item.rationale,
                ", ".join(item.evidence_ids),
            ]
            for item in result.recommendations
        ],
    )

    lines += ["## Release Readiness", ""]
    if readiness is not None:
        lines += [
            f"**Status: {readiness.status.value}**",
            "",
            f"**Score: {readiness.score}/100**",
            "",
            readiness.summary,
            "",
            "### Blockers",
            "",
        ]
        lines += [f"- {item}" for item in readiness.blockers] or ["- None"]
        lines += ["", "### Conditions", ""]
        lines += [f"- {item}" for item in readiness.conditions] or ["- None"]
        lines += ["", "### Readiness Metrics", ""]
        lines += _table(["Metric", "Value"], [[key, str(value)] for key, value in readiness.metrics.items()])
        lines += [
            f"- Human approval required: {'yes' if readiness.human_approval_required else 'no'}",
            f"- Human approval supplied: {'yes' if readiness.human_approval else 'no'}",
            "",
        ]

    if result.warnings:
        lines += ["## Run Warnings", ""]
        lines += [f"- {warning}" for warning in result.warnings]
        lines.append("")

    lines += [
        "## Verification Boundaries",
        "",
        "- DevAgent does not infer SIL/PL/safety certification from ordinary control logic.",
        "- Dynamic PASS evidence is accepted only when project hash, FAT plan hash, backend-registry hash, execution artifact hash, backend qualification, release-policy hash, and trust-store hash match the current verification context.",
        "- Requirement criticality and declared verification mode are separate inputs to the release policy; policy may require dynamic proof or a regression baseline even when static logic proof exists.",
        "- External release policies are accepted only after Ed25519 verification against the operator-supplied trust store. Other artifact signatures are enforced by configured policy purposes.",
        "- AI findings are evidence-constrained review candidates. They cannot by themselves promote a requirement, test, signature, or release gate to VERIFIED/PASS.",
        "- Human engineering approval remains a separate gate and is bound to the exact V5 verification context, including execution-results hash.",
        "- No PLC/controller write or download path is authorized by this report.",
        "",
        f"Evidence items assembled: {len(result.evidence)}",
        "",
    ]
    return "\n".join(lines)
