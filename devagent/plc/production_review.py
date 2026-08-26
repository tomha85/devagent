from __future__ import annotations

import re
from collections import defaultdict

from devagent.plc.models import PLCOutcome
from devagent.plc.production_models import (
    EngineeringFinding,
    ExecutionStatus,
    OptimizationCandidate,
    Recommendation,
    RequirementStatus,
    RequirementVerification,
    RiskFinding,
    Severity,
    TestExecutionEvidence,
)
from devagent.plc.production_utils import stable_id


def detect_risks(
    engineering,
    verifications: list[RequirementVerification],
    executions: list[TestExecutionEvidence],
    engineering_findings: list[EngineeringFinding],
) -> list[RiskFinding]:
    project = engineering.project
    risks: list[RiskFinding] = []
    if engineering.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        risks.append(RiskFinding(
            stable_id("RISK", "SEMANTICS", project.metadata.source_sha256),
            "SEMANTIC_COVERAGE",
            "PLC behavior contains NOT_PROVEN/PARTIAL areas",
            Severity.HIGH,
            "The deterministic analyzer cannot prove all exported behavior.",
            "Generated dependencies/tests can be incomplete for unsupported or protected logic.",
            "Resolve, expose, or explicitly disposition every semantic gap before release.",
            tuple(f"CHECK:{item.id}" for item in engineering.static_checks),
        ))

    writers: dict[str, list] = defaultdict(list)
    for rung in project.rungs:
        for tag in rung.writes:
            writers[tag].append(rung)
    for tag, rungs in sorted(writers.items()):
        unique = {rung.id: rung for rung in rungs}
        if len(unique) > 1:
            risks.append(RiskFinding(
                stable_id("RISK", "MULTI_WRITER", tag),
                "MULTIPLE_WRITERS",
                f"Multiple static writers for {tag}",
                Severity.MEDIUM,
                f"{tag} is written by {len(unique)} distinct rungs.",
                "Final value can depend on scan/task ordering or latch semantics that a simple dependency graph does not simulate.",
                "Review writer precedence and consolidate or document intentional multi-writer behavior.",
                tuple(unique),
            ))

    latched: dict[str, list[str]] = defaultdict(list)
    unlatched: set[str] = set()
    for rung in project.rungs:
        for instruction in rung.instructions:
            if not instruction.arguments:
                continue
            operand = instruction.arguments[0].strip()
            if instruction.name.upper() == "OTL":
                latched[operand].append(rung.id)
            elif instruction.name.upper() == "OTU":
                unlatched.add(operand)
    for tag, evidence_ids in sorted(latched.items()):
        if tag and tag not in unlatched:
            risks.append(RiskFinding(
                stable_id("RISK", "LATCH", tag),
                "RETENTIVE_LOGIC",
                f"Latched output {tag} has no modeled OTU writer",
                Severity.MEDIUM,
                "An OTL writer was found without a corresponding modeled OTU in normalized RLL.",
                "A retained command/state may remain set longer than intended if reset logic is external, protected, or absent.",
                "Confirm the reset/unlatch path and document any external or AOI-based reset evidence.",
                tuple(evidence_ids),
            ))

    for item in verifications:
        if item.status is RequirementStatus.CONFLICT:
            risks.append(RiskFinding(
                stable_id("RISK", "REQ_CONFLICT", item.requirement_id),
                "REQUIREMENT",
                f"Requirement {item.requirement_id} conflicts with modeled behavior/evidence",
                Severity.CRITICAL,
                item.summary,
                "Customer acceptance criteria may not be met.",
                "Resolve the implementation or requirement discrepancy and rerun linked tests.",
                item.evidence_ids,
            ))
        elif item.status in {RequirementStatus.NOT_MAPPED, RequirementStatus.AI_CANDIDATE}:
            risks.append(RiskFinding(
                stable_id("RISK", "REQ_GAP", item.requirement_id),
                "REQUIREMENT",
                f"Requirement {item.requirement_id} is not deterministically proven",
                Severity.HIGH if item.status is RequirementStatus.NOT_MAPPED else Severity.MEDIUM,
                item.summary,
                "The FAT package cannot prove this requirement was implemented and tested.",
                "Map the requirement to concrete logic/evidence and create an executable acceptance test.",
                item.evidence_ids,
                item.confidence,
                "AI_CANDIDATE" if item.ai_assisted else "DETERMINISTIC",
            ))
        elif item.status is RequirementStatus.ACTION_EFFECT_PROVEN:
            risks.append(RiskFinding(
                stable_id("RISK", "REQ_ACTION", item.requirement_id),
                "REQUIREMENT",
                f"Requirement {item.requirement_id} has a proven local action effect but no final-state proof",
                Severity.MEDIUM,
                item.summary,
                "A local OTL/OTU write is proven, but retained/final scan state can still depend on later or overlapping writers and runtime ordering.",
                "Execute the linked FAT case or establish deterministic writer ordering before treating the requirement as fully verified.",
                item.evidence_ids,
                item.confidence,
            ))
        elif item.status is RequirementStatus.TRACEABLE_NOT_PROVEN:
            risks.append(RiskFinding(
                stable_id("RISK", "REQ_PARTIAL", item.requirement_id),
                "REQUIREMENT",
                f"Requirement {item.requirement_id} is traceable but not proven",
                Severity.MEDIUM,
                item.summary,
                "Traceability alone does not prove functional behavior.",
                "Refine the requirement mapping or add deterministic/dynamic evidence.",
                item.evidence_ids,
                item.confidence,
            ))

    for execution in executions:
        if execution.status is ExecutionStatus.FAIL:
            risks.append(RiskFinding(
                stable_id("RISK", "TEST_FAIL", execution.test_id),
                "TEST_FAILURE",
                f"FAT test {execution.test_id} failed",
                Severity.CRITICAL,
                execution.observed or "Execution backend reported FAIL.",
                "Observed controller/simulator behavior did not satisfy the generated expectation.",
                "Investigate the logic or test assumption; do not release until the failure is dispositioned and retested.",
                (execution.test_id,),
            ))

    for finding in engineering_findings:
        if finding.origin == "AI_CANDIDATE" and finding.severity in {Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL}:
            risks.append(RiskFinding(
                stable_id("RISK", "AI", finding.id),
                "AI_REVIEW_CANDIDATE",
                finding.title,
                finding.severity,
                finding.summary,
                "AI review identified a plausible engineering concern; this is not deterministic proof of a defect.",
                finding.recommendation,
                finding.evidence_ids,
                finding.confidence,
                "AI_CANDIDATE",
            ))
    return risks


def optimization_candidates(engineering, risks: list[RiskFinding]) -> list[OptimizationCandidate]:
    result: list[OptimizationCandidate] = []
    for risk in risks:
        if risk.category == "MULTIPLE_WRITERS":
            result.append(OptimizationCandidate(
                f"OPT-{risk.id.split('-', 1)[-1]}",
                "MAINTAINABILITY",
                risk.title,
                risk.summary,
                "Consolidate output ownership or centralize write arbitration when functionally safe.",
                "Clearer scan-order behavior, easier regression analysis, and simpler FAT traceability.",
                Severity.MEDIUM,
                risk.evidence_ids,
            ))
    fingerprints: dict[str, list] = defaultdict(list)
    for rung in engineering.project.rungs:
        normalized = re.sub(r"\s+", "", rung.text).casefold()
        if normalized:
            fingerprints[normalized].append(rung)
    for fingerprint, rungs in fingerprints.items():
        if len(rungs) < 2:
            continue
        result.append(OptimizationCandidate(
            stable_id("OPT-DUP", fingerprint),
            "DUPLICATE_LOGIC",
            "Repeated identical RLL logic",
            f"The same normalized rung text appears {len(rungs)} times.",
            "Review whether a reusable routine/AOI or generated pattern would reduce duplication; do not refactor safety logic solely for style.",
            "Potentially reduces maintenance drift and regression surface.",
            Severity.MEDIUM,
            tuple(rung.id for rung in rungs),
        ))
    return result


def recommendations(
    risks: list[RiskFinding],
    optimizations: list[OptimizationCandidate],
    executions: list[TestExecutionEvidence],
    regression_changes,
) -> list[Recommendation]:
    result: list[Recommendation] = []
    seen: set[str] = set()

    def add(priority: Severity, title: str, action: str, rationale: str, evidence_ids: tuple[str, ...], source_ids: tuple[str, ...]) -> None:
        key = f"{title}|{action}".casefold()
        if key in seen:
            return
        seen.add(key)
        result.append(Recommendation(
            stable_id("REC", key),
            priority,
            title,
            action,
            rationale,
            evidence_ids,
            source_ids,
        ))

    for risk in risks:
        if risk.severity in {Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM}:
            add(risk.severity, risk.title, risk.recommendation, risk.consequence, risk.evidence_ids, (risk.id,))
    if not executions:
        add(
            Severity.HIGH,
            "Execute generated FAT cases",
            "Run the generated tests using an approved simulator/HIL/controller test backend and import traceable execution evidence bound to this project and test-plan SHA-256.",
            "Static analysis cannot prove scan-time behavior, I/O, timing, task ordering, or device interactions.",
            (),
            (),
        )
    for change in regression_changes:
        if change.affected_test_ids:
            add(
                Severity.MEDIUM,
                f"Regression-test {change.subject}",
                f"Rerun impacted tests: {', '.join(change.affected_test_ids[:12])}",
                "Semantic/tag changes can invalidate previously accepted behavior.",
                change.evidence_ids,
                (change.id,),
            )
    for optimization in optimizations[:20]:
        add(
            Severity.LOW,
            optimization.title,
            optimization.proposed_change,
            optimization.expected_benefit,
            optimization.evidence_ids,
            (optimization.id,),
        )
    return result
