from __future__ import annotations

from collections import Counter
import sys

from devagent.plc import production_review as _review
from devagent.plc.production_models import OptimizationCandidate, RequirementStatus, Severity
from devagent.plc.production_utils import stable_id
from devagent.plc.rockwell_state_machine_v11 import state_transitions

_ORIGINAL_OPTIMIZATION = _review.optimization_candidates
_INSTALLED = False


def _candidate_key(item: OptimizationCandidate) -> tuple[str, str, tuple[str, ...]]:
    return (item.category.casefold(), item.title.casefold(), tuple(item.evidence_ids))


def optimization_candidates(engineering, risks) -> list[OptimizationCandidate]:
    """Complete the bounded five-area PLC optimization-review contract.

    Optimization output is advisory only. Candidates are derived from deterministic
    findings already present in the review pipeline and never authorize PLC edits.
    Every proposed structural change requires engineer review and regression FAT.
    """
    result = list(_ORIGINAL_OPTIMIZATION(engineering, risks))
    seen = {_candidate_key(item) for item in result}

    def add(item: OptimizationCandidate) -> None:
        key = _candidate_key(item)
        if key not in seen:
            seen.add(key)
            result.append(item)

    # Preserve the established duplicate-logic candidate while also exposing the
    # commercial contract category explicitly as DUPLICATION.
    for item in list(result):
        if item.category == "DUPLICATE_LOGIC":
            add(
                OptimizationCandidate(
                    stable_id("OPT", "DUPLICATION", *item.evidence_ids),
                    "DUPLICATION",
                    item.title,
                    item.current_state,
                    item.proposed_change,
                    item.expected_benefit,
                    item.change_risk,
                    item.evidence_ids,
                )
            )

    for risk in risks:
        if risk.category == "MULTIPLE_WRITERS":
            add(
                OptimizationCandidate(
                    stable_id("OPT", "OWNERSHIP", risk.id),
                    "OWNERSHIP",
                    f"Clarify ownership: {risk.title}",
                    risk.summary,
                    (
                        "Define one owning routine/output arbitration point where functionally safe, or explicitly document intentional writer precedence. "
                        "Do not change writer ordering without engineer review and regression FAT."
                    ),
                    "Clearer output ownership, less scan-order ambiguity, and more reliable change-impact/FAT traceability.",
                    Severity.MEDIUM,
                    risk.evidence_ids,
                )
            )
        elif risk.category in {"CONTRADICTORY_LOGIC", "UNREACHABLE_LOGIC"}:
            add(
                OptimizationCandidate(
                    stable_id("OPT", "SIMPLIFICATION", risk.id),
                    "SIMPLIFICATION",
                    f"Simplification candidate: {risk.title}",
                    risk.summary,
                    (
                        "After the PLC engineer confirms the intended behavior, simplify or remove the impossible/inactive logic while preserving required behavior. "
                        "Regenerate the engineering review and rerun affected FAT cases after any edit."
                    ),
                    "Reduces dead or misleading logic, lowers maintenance burden, and shrinks the regression surface.",
                    Severity.HIGH if risk.category == "CONTRADICTORY_LOGIC" else Severity.MEDIUM,
                    risk.evidence_ids,
                )
            )
        elif risk.category == "SEQUENCING":
            add(
                OptimizationCandidate(
                    stable_id("OPT", "STRUCTURE", risk.id),
                    "STRUCTURAL_IMPROVEMENT",
                    f"Sequence structure candidate: {risk.title}",
                    risk.summary,
                    (
                        "Make transition arbitration, mutual-exclusion conditions, and state ownership explicit where functionally appropriate; otherwise document the intentional priority. "
                        "Treat this as an engineer-reviewed refactor, not an automatic code change."
                    ),
                    "Clearer state-machine intent, easier fault-path review, and more deterministic regression/FAT planning.",
                    Severity.MEDIUM,
                    risk.evidence_ids,
                )
            )

    return result


def _risk_count(result, category: str) -> int:
    return sum(1 for item in result.risks if item.category == category)


def render_four_core_contract_section(result) -> str:
    """Render an explicit customer-facing proof that all four core product areas ran."""
    project = result.engineering.project
    transitions = state_transitions(project)
    edge_counts = Counter(edge.kind for edge in result.engineering.graph.edges)
    optimization_counts = Counter(item.category for item in result.optimizations)
    requirement_conflicts = sum(
        1 for item in result.requirement_verification if item.status is RequirementStatus.CONFLICT
    )
    suspicious_categories = {
        "RETENTIVE_LOGIC",
        "SEMANTIC_COVERAGE",
        "SEQUENCING",
        "AI_REVIEW_CANDIDATE",
    }
    suspicious_count = sum(1 for item in result.risks if item.category in suspicious_categories)
    tests = result.engineering.fat_tests
    complete_setup = sum(1 for item in tests if item.setup_steps)
    complete_actions = sum(1 for item in tests if item.action_steps)
    complete_why = sum(1 for item in tests if item.why_required)
    complete_expected = sum(1 for item in tests if item.expected)

    lines = [
        "## DevAgent Four-Core PLC Review Contract",
        "",
        "> DevAgent analyzes and plans. It does not connect to or execute external PLC software. Optimization items are recommendations only; PLC engineers own edits and FAT execution.",
        "",
        "### 1. Engineering Analysis",
        "",
        f"- Machine logic inventory: **{len(project.tags)} tags, {len(project.programs)} programs, {len(project.routines)} routines, {len(project.rungs)} RLL rungs, {project.st_statement_total} ST statements, {len(project.aois)} AOIs**",
        f"- Modeled output-logic objects: **{len(project.output_logic)}**",
        f"- Cause/effect + dependency graph edges: **{len(result.engineering.graph.edges)}**",
        f"- Dependency edge kinds: **{', '.join(f'{name}={count}' for name, count in sorted(edge_counts.items())) or 'none'}**",
        f"- Discovered sequence/state transitions: **{len(transitions)}**",
        f"- Static semantic outcome: **{result.engineering.outcome.value}**",
        "",
        "### 2. Risks / Logic Problems",
        "",
        f"- Multiple-writer findings: **{_risk_count(result, 'MULTIPLE_WRITERS')}**",
        f"- Contradictory-logic findings: **{_risk_count(result, 'CONTRADICTORY_LOGIC')}**",
        f"- Unreachable-logic findings: **{_risk_count(result, 'UNREACHABLE_LOGIC')}**",
        f"- Requirement conflicts: **{requirement_conflicts}**",
        f"- Suspicious/needs-review behavior candidates: **{suspicious_count}**",
        f"- Total risk findings: **{len(result.risks)}**",
        "",
        "### 3. Optimization Recommendations",
        "",
        f"- Maintainability candidates: **{optimization_counts['MAINTAINABILITY']}**",
        f"- Simplification candidates: **{optimization_counts['SIMPLIFICATION']}**",
        f"- Duplication candidates: **{optimization_counts['DUPLICATION']}**",
        f"- Ownership candidates: **{optimization_counts['OWNERSHIP']}**",
        f"- Structural-improvement candidates: **{optimization_counts['STRUCTURAL_IMPROVEMENT']}**",
        f"- Total optimization candidates: **{len(result.optimizations)}**",
        "",
        "### 4. FAT Plan",
        "",
        f"- What must/recommended to be tested: **{len(tests)} engineer-executed FAT procedure(s)**",
        f"- Procedures with explicit reason/why: **{complete_why}/{len(tests)}**",
        f"- Procedures with exact evidence-linked setup/preconditions: **{complete_setup}/{len(tests)}**",
        f"- Procedures with test actions: **{complete_actions}/{len(tests)}**",
        f"- Procedures with expected behavior: **{complete_expected}/{len(tests)}**",
        "- Detailed setup, actions, watch tags, expected results, evidence to capture, and failure implications are listed in **Engineer FAT Procedures**.",
        "",
    ]
    return "\n".join(lines)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _review.optimization_candidates = optimization_candidates

    # If a caller imported a production module before installation, keep its
    # by-value function bindings aligned. Normal package import order installs
    # this module before production/production_v5 are loaded.
    for module_name in ("devagent.plc.production", "devagent.plc.production_v5"):
        module = sys.modules.get(module_name)
        if module is not None:
            setattr(module, "optimization_candidates", optimization_candidates)
    _INSTALLED = True


__all__ = [
    "install",
    "optimization_candidates",
    "render_four_core_contract_section",
]
