from __future__ import annotations

from collections import defaultdict
import re

from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.plc_dispatch import analyze_plc_project
from devagent.plc.production_models import (
    EngineeringFinding,
    EvidenceItem,
    ExecutionStatus,
    OptimizationCandidate,
    RequirementStatus,
    RequirementVerification,
    RiskFinding,
    Severity,
    StageRecord,
)
from devagent.plc.production_utils import explicit_bool, stable_id
from devagent.plc.schneider_control_expert_v1 import schneider_capability_profile

_INSTALLED = False


def _is_schneider(engineering_or_project) -> bool:
    project = getattr(engineering_or_project, "project", engineering_or_project)
    return str(project.metadata.vendor).casefold().startswith("schneider")


def _logic_paths_payload(logic) -> list[list[dict[str, object]]]:
    return [[{"tag": term.tag, "required": term.required} for term in path.terms] for path in logic.paths]


def _evidence_index(engineering) -> list[EvidenceItem]:
    project = engineering.project
    profile = schneider_capability_profile(project)
    capability_id = f"SCHNEIDER-CAPABILITY:{project.metadata.source_sha256}"
    result = [
        EvidenceItem(
            capability_id,
            "SCHNEIDER_CONTROL_EXPERT_CAPABILITY_PROFILE",
            f"Schneider Control Expert V1 support contract: {profile['static_contract']} for export {project.metadata.controller_name}.",
            project.metadata.source_path,
            project.metadata.source_sha256,
            profile,
        )
    ]
    for tag in project.tags:
        result.append(
            EvidenceItem(
                f"TAG:{tag.scope}:{tag.name}",
                "TAG",
                f"{tag.scope} Schneider variable {tag.name}: {tag.data_type}",
                payload={
                    "tag": tag.name,
                    "scope": tag.scope,
                    "data_type": tag.data_type,
                    "tag_type": tag.tag_type,
                    "description": tag.description,
                },
            )
        )
    for statement in project.logic_statements:
        result.append(
            EvidenceItem(
                statement.id,
                f"SCHNEIDER_{statement.language}_STATEMENT",
                f"{statement.source.locator}: {statement.text[:240]}",
                statement.source.locator,
                project.metadata.source_sha256,
                {
                    "reads": list(statement.reads),
                    "writes": list(statement.writes),
                    "calls": list(statement.calls),
                    "semantic_state": statement.semantic_state.value,
                },
            )
        )
    for logic in project.output_logic:
        result.append(
            EvidenceItem(
                logic.id,
                "SCHNEIDER_OUTPUT_LOGIC",
                f"{logic.source.locator}: {logic.output_tag} via {logic.instruction} ({len(logic.paths)} modeled Boolean path(s))",
                logic.source.locator,
                project.metadata.source_sha256,
                {
                    "output_tag": logic.output_tag,
                    "instruction": logic.instruction,
                    "language": logic.language,
                    "semantic_state": logic.semantic_state.value,
                    "paths": _logic_paths_payload(logic),
                },
            )
        )
    for check in engineering.static_checks:
        result.append(
            EvidenceItem(
                f"CHECK:{check.id}",
                "STATIC_CHECK",
                f"{check.id}: {check.status.value} — {check.summary}",
                payload={"status": check.status.value, "evidence": list(check.evidence)},
            )
        )
    return result


def _findings(engineering, valid_evidence_ids: set[str]) -> list[EngineeringFinding]:
    project = engineering.project
    profile = schneider_capability_profile(project)
    capability_id = f"SCHNEIDER-CAPABILITY:{project.metadata.source_sha256}"
    checks = tuple(
        f"CHECK:{item.id}"
        for item in engineering.static_checks
        if f"CHECK:{item.id}" in valid_evidence_ids
    )
    findings = [
        EngineeringFinding(
            "ENG-INVENTORY",
            "PROJECT_INVENTORY",
            "Schneider Control Expert engineering export inventory normalized",
            Severity.INFO,
            f"Normalized {len(project.tags)} variables, {len(project.data_types)} data types, {len(project.routines)} sections/routines, {len(project.tasks)} task(s), and {len(project.logic_statements)} logic object(s).",
            "Use the canonical IR and source-linked evidence as the downstream engineering-review input; withheld semantics remain outside proof.",
            checks[:3],
        ),
        EngineeringFinding(
            "ENG-SCHNEIDER-SUPPORT-CONTRACT",
            "SCHNEIDER_SUPPORT",
            "Schneider Control Expert V1 support contract",
            Severity.INFO if profile["static_contract"] == "COMPLETE" else Severity.HIGH,
            (
                "All discovered executable logic objects are inside the Schneider V1 bounded local theorem."
                if profile["static_contract"] == "COMPLETE"
                else "One or more Control Expert ST/LD/FBD/SFC/IL regions remain PARTIAL/OPAQUE/NOT_PROVEN under the V1 contract."
            ),
            "Use the capability profile and source-linked static checks to disposition every semantic gap; use engineer-executed FAT for runtime behavior.",
            (capability_id,) if capability_id in valid_evidence_ids else checks,
        ),
        EngineeringFinding(
            "ENG-CAUSE-EFFECT-GRAPH",
            "CAUSE_EFFECT",
            "Evidence-linked Schneider cause/effect graph built",
            Severity.INFO,
            f"Built {len(engineering.graph.edges)} dependency/call edge(s) from the Schneider canonical IR; deterministic DEPENDS_ON edges are emitted only for FULL semantics.",
            "Use the graph to trace source-linked writers/readers/requirements/FAT candidates; PARTIAL/OPAQUE regions are not complete cause/effect proof.",
            tuple(dict.fromkeys(edge.evidence_id for edge in engineering.graph.edges if edge.evidence_id in valid_evidence_ids))[:20],
        ),
    ]
    if engineering.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        findings.append(
            EngineeringFinding(
                "ENG-SEMANTIC-GAPS",
                "SEMANTIC_COVERAGE",
                "Not all Schneider PLC behavior is statically proven",
                Severity.HIGH,
                f"Schneider V1 has {profile['partial_statements']} PARTIAL and {profile['opaque_statements']} OPAQUE logic object(s).",
                "Resolve/export a more explicit source representation where possible and execute linked FAT procedures rather than promoting traceability to proof.",
                checks,
            )
        )
    return findings


def _writer_risks(engineering) -> list[RiskFinding]:
    writers: dict[str, dict[str, object]] = defaultdict(dict)
    labels: dict[str, str] = {}
    for statement in engineering.project.logic_statements:
        for ref in statement.writes:
            key = ref.casefold()
            labels.setdefault(key, ref)
            writers[key][statement.id] = statement
    result: list[RiskFinding] = []
    for key, sources in sorted(writers.items()):
        if len(sources) <= 1:
            continue
        label = labels[key]
        result.append(
            RiskFinding(
                stable_id("RISK", "SCHNEIDER_MULTI_WRITER", key),
                "MULTIPLE_WRITERS",
                f"Multiple Schneider source writers for {label}",
                Severity.MEDIUM,
                f"{label} is written by {len(sources)} distinct normalized Control Expert source statements/networks.",
                "Final value can depend on MAST/FAST/section execution order, branch conditions, later assignments, or stateful block behavior that Schneider V1 does not simulate.",
                "Review variable ownership and task/section execution order; consolidate or explicitly document intentional arbitration and rerun affected FAT procedures after changes.",
                tuple(sorted(sources)),
            )
        )
    return result


def _detect_risks(engineering, verifications, executions, engineering_findings):
    project = engineering.project
    risks: list[RiskFinding] = []
    if engineering.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        risks.append(
            RiskFinding(
                stable_id("RISK", "SCHNEIDER_SEMANTICS", project.metadata.source_sha256),
                "SEMANTIC_COVERAGE",
                "Schneider behavior contains PARTIAL/OPAQUE areas",
                Severity.HIGH,
                "The deterministic Schneider V1 analyzer cannot prove all exported Control Expert behavior.",
                "Dependencies and static test candidates are intentionally incomplete for control-flow, stateful calls, complex LD, FBD, SFC, IL, or other withheld semantics.",
                "Export a deeper supported source representation where possible and explicitly disposition every semantic gap before release acceptance.",
                tuple(f"CHECK:{item.id}" for item in engineering.static_checks),
            )
        )
    risks.extend(_writer_risks(engineering))
    for item in verifications:
        if item.status is RequirementStatus.CONFLICT:
            risks.append(RiskFinding(stable_id("RISK", "REQ_CONFLICT", item.requirement_id), "REQUIREMENT", f"Requirement {item.requirement_id} conflicts with modeled Schneider behavior/evidence", Severity.CRITICAL, item.summary, "Customer acceptance criteria may not be met.", "Resolve the implementation or requirement discrepancy and rerun linked tests.", item.evidence_ids))
        elif item.status in {RequirementStatus.NOT_MAPPED, RequirementStatus.AI_CANDIDATE}:
            risks.append(RiskFinding(stable_id("RISK", "REQ_GAP", item.requirement_id), "REQUIREMENT", f"Requirement {item.requirement_id} is not deterministically proven", Severity.HIGH if item.status is RequirementStatus.NOT_MAPPED else Severity.MEDIUM, item.summary, "The FAT package cannot prove this requirement was implemented and tested.", "Map the requirement to concrete Schneider source/evidence and execute an acceptance test.", item.evidence_ids, item.confidence, "AI_CANDIDATE" if item.ai_assisted else "DETERMINISTIC"))
        elif item.status is RequirementStatus.TRACEABLE_NOT_PROVEN:
            risks.append(RiskFinding(stable_id("RISK", "REQ_PARTIAL", item.requirement_id), "REQUIREMENT", f"Requirement {item.requirement_id} is traceable but not proven", Severity.MEDIUM, item.summary, "Traceability alone does not prove functional behavior.", "Refine the mapping or add deterministic/engineer-executed runtime evidence.", item.evidence_ids, item.confidence))
        elif item.status is RequirementStatus.ACTION_EFFECT_PROVEN:
            risks.append(RiskFinding(stable_id("RISK", "REQ_ACTION", item.requirement_id), "REQUIREMENT", f"Requirement {item.requirement_id} has local action proof but no final runtime proof", Severity.MEDIUM, item.summary, "Local source semantics do not prove complete task/process behavior.", "Execute the linked FAT case before treating final machine behavior as verified.", item.evidence_ids, item.confidence))
    for execution in executions:
        if execution.status is ExecutionStatus.FAIL:
            risks.append(RiskFinding(stable_id("RISK", "TEST_FAIL", execution.test_id), "TEST_FAILURE", f"FAT test {execution.test_id} failed", Severity.CRITICAL, execution.observed or "Execution evidence reported FAIL.", "Observed runtime behavior did not satisfy the generated expectation.", "Investigate the Schneider source logic or test assumption; do not release until dispositioned and retested.", (execution.test_id,)))
    for finding in engineering_findings:
        if finding.origin == "AI_CANDIDATE" and finding.severity in {Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL}:
            risks.append(RiskFinding(stable_id("RISK", "AI", finding.id), "AI_REVIEW_CANDIDATE", finding.title, finding.severity, finding.summary, "AI review identified a plausible engineering concern; this is not deterministic proof of a defect.", finding.recommendation, finding.evidence_ids, finding.confidence, "AI_CANDIDATE"))
    return risks


def _bool_truth(logic, assignment: dict[str, bool], expected: bool) -> str:
    possible = 0
    definite = 0
    folded = {key.casefold(): value for key, value in assignment.items()}
    for path in logic.paths:
        contradicted = False
        complete = True
        for term in path.terms:
            key = term.tag.casefold()
            if key not in folded:
                complete = False
                continue
            if folded[key] != term.required:
                contradicted = True
                break
        if contradicted:
            continue
        possible += 1
        if complete:
            definite += 1
    if expected:
        return "PROVEN" if definite else "CONFLICT" if possible == 0 else "UNKNOWN"
    return "PROVEN" if possible == 0 else "CONFLICT" if definite else "UNKNOWN"


def _verify_requirement(requirement, engineering, evidence, tests) -> RequirementVerification:
    from devagent.plc.production_verification import requirement_candidates

    matched_tags, evidence_ids = requirement_candidates(requirement, engineering, evidence)
    explicit_by_fold = {tag.casefold(): tag for tag in matched_tags}
    modeled = [logic for logic in engineering.project.output_logic if logic.semantic_state is PLCSemanticState.FULL]
    outputs: list[str] = []
    for logic in modeled:
        matched = explicit_by_fold.get(logic.output_tag.casefold())
        if matched and explicit_bool(requirement.text, matched) is not None and logic.output_tag.casefold() not in {item.casefold() for item in outputs}:
            outputs.append(logic.output_tag)
    if len(outputs) > 1:
        return RequirementVerification(requirement.id, RequirementStatus.TRACEABLE_NOT_PROVEN, "Requirement constrains multiple modeled Schneider outputs; compound assertion semantics are not statically proven in V1.", tuple(evidence_ids), tuple(matched_tags))
    if len(outputs) == 1:
        output = outputs[0]
        output_text = explicit_by_fold[output.casefold()]
        expected = explicit_bool(requirement.text, output_text)
        assert expected is not None
        assignment = {
            tag: value
            for tag in matched_tags
            if tag.casefold() != output.casefold()
            for value in [explicit_bool(requirement.text, tag)]
            if value is not None
        }
        matching = [logic for logic in modeled if logic.output_tag.casefold() == output.casefold()]
        logic_evidence = tuple(logic.id for logic in matching)
        combined = tuple(dict.fromkeys([*evidence_ids, *logic_evidence]))
        writers = {
            statement.id
            for statement in engineering.project.logic_statements
            if any(ref.casefold() == output.casefold() for ref in statement.writes)
        }
        if len(matching) != 1 or len(writers) != 1:
            return RequirementVerification(requirement.id, RequirementStatus.TRACEABLE_NOT_PROVEN, f"{output} does not have one unambiguous bounded Schneider writer theorem; final output state is withheld without deterministic task/section writer-order semantics.", tuple(dict.fromkeys([*combined, *writers])), tuple(matched_tags))
        if not assignment:
            return RequirementVerification(requirement.id, RequirementStatus.TRACEABLE_NOT_PROVEN, f"Requirement names {output} but does not provide enough explicit Boolean input conditions for proof.", combined, tuple(matched_tags))
        truth = _bool_truth(matching[0], assignment, expected)
        linked = tuple(
            test.id
            for test in tests
            if test.output_tag.casefold() == output.casefold()
            and all(test.preconditions.get(key) == value for key, value in assignment.items())
        )
        if truth == "PROVEN":
            return RequirementVerification(requirement.id, RequirementStatus.STATICALLY_VERIFIED, f"Specified Boolean conditions deterministically imply {output}={'TRUE' if expected else 'FALSE'} in the single-writer bounded Schneider Control Expert V1 theorem; runtime behavior still requires FAT when policy requires dynamic proof.", combined, tuple(matched_tags), linked)
        if truth == "CONFLICT":
            return RequirementVerification(requirement.id, RequirementStatus.CONFLICT, f"Specified Boolean conditions make required {output}={'TRUE' if expected else 'FALSE'} impossible in the bounded Schneider Boolean theorem.", combined, tuple(matched_tags))
        return RequirementVerification(requirement.id, RequirementStatus.TRACEABLE_NOT_PROVEN, f"Requirement maps to {output}, but supplied Boolean conditions under-specify one or more modeled Schneider dependencies.", combined, tuple(matched_tags))
    if matched_tags:
        return RequirementVerification(requirement.id, RequirementStatus.TRACEABLE_NOT_PROVEN, "Requirement references known Schneider variables but its complete behavior is outside the bounded V1 theorem.", tuple(evidence_ids), tuple(matched_tags))
    if evidence_ids:
        return RequirementVerification(requirement.id, RequirementStatus.TRACEABLE_NOT_PROVEN, "Requirement has lexical Schneider trace candidates, but no explicit PLC identifier mapping is proven.", tuple(evidence_ids), confidence=0.5)
    return RequirementVerification(requirement.id, RequirementStatus.NOT_MAPPED, "No deterministic Schneider PLC implementation mapping was found.")


def _optimization(previous, engineering, risks):
    result = list(previous(engineering, risks))
    fingerprints: dict[str, list] = defaultdict(list)
    for statement in engineering.project.logic_statements:
        if statement.language != "ST":
            continue
        normalized = re.sub(r"\s+", "", statement.text).casefold()
        if normalized:
            fingerprints[normalized].append(statement)
    for fingerprint, statements in fingerprints.items():
        if len(statements) < 2:
            continue
        result.append(
            OptimizationCandidate(
                stable_id("OPT-SCHNEIDER-DUP", fingerprint),
                "DUPLICATE_LOGIC",
                "Repeated identical Schneider ST source logic",
                f"The same normalized Control Expert ST source statement appears {len(statements)} times.",
                "Review whether a shared DFB/section/function pattern would reduce duplication where functionally safe; do not refactor safety logic solely for style.",
                "Potentially reduces maintenance drift and regression/FAT surface.",
                Severity.MEDIUM,
                tuple(item.id for item in statements),
            )
        )
    return result


def install() -> None:
    """Install Schneider Control Expert as an isolated vendor branch over the qualified production contract."""
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import production as _production
    from devagent.plc import production_evidence as _evidence
    from devagent.plc import production_review as _review
    from devagent.plc import production_verification as _verification

    previous_evidence = _evidence.evidence_index
    previous_findings = _evidence.deterministic_engineering_findings
    previous_risks = _review.detect_risks
    previous_optimization = _review.optimization_candidates
    previous_verify = _verification.verify_requirement
    previous_run = _production.run_production_verification

    def evidence_index(engineering):
        return _evidence_index(engineering) if _is_schneider(engineering) else previous_evidence(engineering)

    def deterministic_engineering_findings(engineering, valid_evidence_ids):
        return _findings(engineering, valid_evidence_ids) if _is_schneider(engineering) else previous_findings(engineering, valid_evidence_ids)

    def detect_risks(engineering, verifications, executions, engineering_findings):
        return _detect_risks(engineering, verifications, executions, engineering_findings) if _is_schneider(engineering) else previous_risks(engineering, verifications, executions, engineering_findings)

    def optimization_candidates(engineering, risks):
        return _optimization(previous_optimization, engineering, risks) if _is_schneider(engineering) else previous_optimization(engineering, risks)

    def verify_requirement(requirement, engineering, evidence, tests):
        return _verify_requirement(requirement, engineering, evidence, tests) if _is_schneider(engineering) else previous_verify(requirement, engineering, evidence, tests)

    _production.analyze_rockwell_l5x = analyze_plc_project

    def run_production_verification(*args, **kwargs):
        result = previous_run(*args, **kwargs)
        if _is_schneider(result.engineering):
            project = result.engineering.project
            result.stages[0] = StageRecord(
                1,
                result.stages[0].name,
                result.stages[0].status,
                f"Validated Schneider EcoStruxure Control Expert XML exchange export for {project.metadata.controller_name}; proprietary work/archive opening and simulator execution are outside DevAgent.",
                result.stages[0].evidence_ids,
            )
            result.stages[1] = StageRecord(
                2,
                result.stages[1].name,
                result.stages[1].status,
                f"Canonical IR: {len(project.tags)} variables, {len(project.routines)} sections/routines, {len(project.logic_statements)} ST/LD/FBD/SFC/IL logic object(s), {len(project.output_logic)} bounded Boolean output-logic object(s).",
                result.stages[1].evidence_ids,
            )
        return result

    _evidence.evidence_index = evidence_index
    _evidence.deterministic_engineering_findings = deterministic_engineering_findings
    _review.detect_risks = detect_risks
    _review.optimization_candidates = optimization_candidates
    _verification.verify_requirement = verify_requirement
    _production.evidence_index = evidence_index
    _production.deterministic_engineering_findings = deterministic_engineering_findings
    _production.detect_risks = detect_risks
    _production.optimization_candidates = optimization_candidates
    _production.verify_requirement = verify_requirement
    _production.run_production_verification = run_production_verification
    _INSTALLED = True


__all__ = ["install"]
