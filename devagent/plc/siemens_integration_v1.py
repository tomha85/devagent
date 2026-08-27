from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
import re

from devagent.plc.models import PLCOutcome, PLCSemanticState
from devagent.plc.plc_dispatch import analyze_plc_project
from devagent.plc.production_models import (
    EngineeringFinding,
    ExecutionStatus,
    OptimizationCandidate,
    RequirementStatus,
    RequirementVerification,
    RiskFinding,
    Severity,
    StageRecord,
)
from devagent.plc.production_utils import explicit_bool, stable_id
from devagent.plc.siemens_tia_v1 import siemens_capability_profile

_INSTALLED = False


def _is_siemens(engineering_or_project) -> bool:
    project = getattr(engineering_or_project, "project", engineering_or_project)
    return str(project.metadata.vendor).casefold() == "siemens"


def _logic_paths_payload(logic) -> list[list[dict[str, object]]]:
    return [[{"tag": term.tag, "required": term.required} for term in path.terms] for path in logic.paths]


def _siemens_evidence_index(engineering):
    from devagent.plc.production_models import EvidenceItem

    project = engineering.project
    profile = siemens_capability_profile(project)
    capability_id = f"SIEMENS-CAPABILITY:{project.metadata.source_sha256}"
    result = [
        EvidenceItem(
            capability_id,
            "SIEMENS_CAPABILITY_PROFILE",
            f"Siemens TIA V1 support contract: {profile['static_contract']} for export bundle {project.metadata.controller_name}.",
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
                f"{tag.scope} Siemens tag/symbol {tag.name}: {tag.data_type}",
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
                f"SIEMENS_{statement.language}_STATEMENT",
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
                "SIEMENS_OUTPUT_LOGIC",
                f"{logic.source.locator}: {logic.output_tag} via {logic.instruction} ({len(logic.paths)} modeled Boolean path(s))",
                logic.source.locator,
                project.metadata.source_sha256,
                {
                    "output_tag": logic.output_tag,
                    "instruction": logic.instruction,
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


def _siemens_findings(engineering, valid_evidence_ids: set[str]) -> list[EngineeringFinding]:
    project = engineering.project
    profile = siemens_capability_profile(project)
    capability_id = f"SIEMENS-CAPABILITY:{project.metadata.source_sha256}"
    checks = tuple(
        f"CHECK:{item.id}"
        for item in engineering.static_checks
        if f"CHECK:{item.id}" in valid_evidence_ids
    )
    findings = [
        EngineeringFinding(
            "ENG-INVENTORY",
            "PROJECT_INVENTORY",
            "Siemens engineering export inventory normalized",
            Severity.INFO,
            f"Normalized {len(project.tags)} tags/symbols, {len(project.programs)} blocks, {len(project.routines)} routines, {project.st_statement_total} SCL statements, and {len(project.tasks)} organization-block execution entries.",
            "Use the canonical IR and source-linked evidence as the downstream engineering-review input; withheld XML/control semantics remain outside proof.",
            checks[:3],
        ),
        EngineeringFinding(
            "ENG-SIEMENS-SUPPORT-CONTRACT",
            "SIEMENS_SUPPORT",
            "Siemens TIA engineering-export support contract",
            Severity.INFO if profile["static_contract"] == "COMPLETE" else Severity.HIGH,
            (
                "All discovered executable statements are within the Siemens V1 bounded static theorem."
                if profile["static_contract"] == "COMPLETE"
                else "One or more Siemens SCL/XML/protected engineering regions remain PARTIAL/OPAQUE/NOT_PROVEN under the V1 contract."
            ),
            "Use the capability profile and source-linked static checks to disposition every partial/protected/opaque region; export generated SCL when available and execute the generated FAT procedures for runtime evidence.",
            (capability_id,) if capability_id in valid_evidence_ids else checks,
        ),
        EngineeringFinding(
            "ENG-CAUSE-EFFECT-GRAPH",
            "CAUSE_EFFECT",
            "Evidence-linked Siemens cause/effect graph built",
            Severity.INFO,
            f"Built {len(engineering.graph.edges)} dependency/call edge(s) from the Siemens canonical IR; deterministic DEPENDS_ON edges are emitted only for FULL statements/output logic.",
            "Use the graph to trace source-linked writers/readers/requirements/FAT candidates. PARTIAL/OPAQUE regions must not be treated as complete cause/effect proof.",
            tuple(dict.fromkeys(edge.evidence_id for edge in engineering.graph.edges if edge.evidence_id in valid_evidence_ids))[:20],
        ),
    ]
    if engineering.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        findings.append(
            EngineeringFinding(
                "ENG-SEMANTIC-GAPS",
                "SEMANTIC_COVERAGE",
                "Not all Siemens PLC behavior is statically proven",
                Severity.HIGH,
                f"Siemens V1 has {profile['partial_statements']} PARTIAL and {profile['opaque_statements']} OPAQUE logic object(s); protected blocks={profile['protected_blocks']}.",
                "Export/decompose the unsupported region when possible, review the exact source boundary, and execute linked FAT procedures rather than promoting traceability to proof.",
                checks,
            )
        )
    findings.append(
        EngineeringFinding(
            "ENG-SIEMENS-SCL-COVERAGE",
            "SCL_COVERAGE",
            "Siemens SCL deterministic coverage",
            Severity.INFO if project.st_statement_total == project.st_statement_semantic_count else Severity.MEDIUM,
            f"Modeled {project.st_statement_semantic_count}/{project.st_statement_total} SCL statement(s) with bounded deterministic local semantics and discovered {len(project.output_logic)} Boolean assignment theorem object(s).",
            "Keep IF/CASE/loop/call or structured-network regions outside VERIFIED claims until an explicit theorem or engineer-executed FAT evidence covers them.",
            checks,
        )
    )
    return findings


def _writer_identity(project, statement, ref: str) -> tuple[str, str]:
    program_scope = f"program:{statement.source.program or statement.owner_name}".casefold()
    ref_folded = ref.casefold()
    for tag in project.tags:
        if tag.scope.casefold() == program_scope and tag.name.casefold() == ref_folded:
            return program_scope, ref_folded
    for tag in project.tags:
        if tag.scope.casefold() == "controller" and tag.name.casefold() == ref_folded:
            return "controller", ref_folded
    return "symbol", ref_folded


def _siemens_multiple_writer_risks(engineering) -> list[RiskFinding]:
    writers: dict[tuple[str, str], dict[str, object]] = defaultdict(dict)
    labels: dict[tuple[str, str], str] = {}
    for statement in engineering.project.logic_statements:
        for ref in statement.writes:
            identity = _writer_identity(engineering.project, statement, ref)
            labels.setdefault(identity, ref)
            writers[identity][statement.id] = statement
    result = []
    for identity, sources in sorted(writers.items(), key=lambda item: item[0]):
        if len(sources) <= 1:
            continue
        label = labels.get(identity, identity[1])
        result.append(
            RiskFinding(
                stable_id("RISK", "SIEMENS_MULTI_WRITER", identity[0], identity[1]),
                "MULTIPLE_WRITERS",
                f"Multiple Siemens source writers for {label}",
                Severity.MEDIUM,
                f"{label} is written by {len(sources)} distinct normalized Siemens source statements after block/symbol-scope resolution.",
                "Final value can depend on OB/block call order, branch conditions, later assignments, or runtime scheduling that Siemens V1 does not simulate.",
                "Review symbol ownership and block/OB execution order; consolidate or explicitly document intentional arbitration and rerun affected FAT procedures after changes.",
                tuple(sorted(sources)),
            )
        )
    return result


def _siemens_detect_risks(engineering, verifications, executions, engineering_findings):
    project = engineering.project
    risks: list[RiskFinding] = []
    if engineering.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        risks.append(
            RiskFinding(
                stable_id("RISK", "SIEMENS_SEMANTICS", project.metadata.source_sha256),
                "SEMANTIC_COVERAGE",
                "Siemens behavior contains PARTIAL/OPAQUE areas",
                Severity.HIGH,
                "The deterministic Siemens V1 analyzer cannot prove all exported behavior.",
                "Dependencies and generated static tests are intentionally incomplete for protected, control-flow, call, LAD/FBD/GRAPH/STL, or other withheld semantics.",
                "Export a deeper supported source representation where possible and explicitly disposition every semantic gap before release acceptance.",
                tuple(f"CHECK:{item.id}" for item in engineering.static_checks),
            )
        )
    risks.extend(_siemens_multiple_writer_risks(engineering))

    protected = [item for item in project.routines if item.source_protected]
    if protected:
        risks.append(
            RiskFinding(
                stable_id("RISK", "SIEMENS_PROTECTED", project.metadata.source_sha256),
                "PROTECTED_LOGIC",
                "Protected Siemens block implementation is not available for proof",
                Severity.HIGH,
                f"{len(protected)} protected/interface-only Siemens block(s) are visible without a complete implementation body.",
                "Required behavior can exist behind the protected interface but cannot be independently reviewed or proven from this export.",
                "Follow the customer's approved protection/unlock/export process or retain the block as NOT_PROVEN and cover its acceptance behavior with engineer-executed FAT evidence.",
                tuple(item.id for item in protected),
            )
        )

    for item in verifications:
        if item.status is RequirementStatus.CONFLICT:
            risks.append(RiskFinding(stable_id("RISK", "REQ_CONFLICT", item.requirement_id), "REQUIREMENT", f"Requirement {item.requirement_id} conflicts with modeled behavior/evidence", Severity.CRITICAL, item.summary, "Customer acceptance criteria may not be met.", "Resolve the implementation or requirement discrepancy and rerun linked tests.", item.evidence_ids))
        elif item.status in {RequirementStatus.NOT_MAPPED, RequirementStatus.AI_CANDIDATE}:
            risks.append(RiskFinding(stable_id("RISK", "REQ_GAP", item.requirement_id), "REQUIREMENT", f"Requirement {item.requirement_id} is not deterministically proven", Severity.HIGH if item.status is RequirementStatus.NOT_MAPPED else Severity.MEDIUM, item.summary, "The FAT package cannot prove this requirement was implemented and tested.", "Map the requirement to concrete Siemens source/evidence and execute an acceptance test.", item.evidence_ids, item.confidence, "AI_CANDIDATE" if item.ai_assisted else "DETERMINISTIC"))
        elif item.status is RequirementStatus.TRACEABLE_NOT_PROVEN:
            risks.append(RiskFinding(stable_id("RISK", "REQ_PARTIAL", item.requirement_id), "REQUIREMENT", f"Requirement {item.requirement_id} is traceable but not proven", Severity.MEDIUM, item.summary, "Traceability alone does not prove functional behavior.", "Refine the mapping or add deterministic/engineer-executed runtime evidence.", item.evidence_ids, item.confidence))
        elif item.status is RequirementStatus.ACTION_EFFECT_PROVEN:
            risks.append(RiskFinding(stable_id("RISK", "REQ_ACTION", item.requirement_id), "REQUIREMENT", f"Requirement {item.requirement_id} has local action proof but no final runtime proof", Severity.MEDIUM, item.summary, "Local source semantics do not prove complete task/process behavior.", "Execute the linked FAT case before treating final machine behavior as verified.", item.evidence_ids, item.confidence))

    for execution in executions:
        if execution.status is ExecutionStatus.FAIL:
            risks.append(RiskFinding(stable_id("RISK", "TEST_FAIL", execution.test_id), "TEST_FAILURE", f"FAT test {execution.test_id} failed", Severity.CRITICAL, execution.observed or "Execution evidence reported FAIL.", "Observed runtime behavior did not satisfy the generated expectation.", "Investigate the Siemens source logic or test assumption; do not release until dispositioned and retested.", (execution.test_id,)))
    for finding in engineering_findings:
        if finding.origin == "AI_CANDIDATE" and finding.severity in {Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL}:
            risks.append(RiskFinding(stable_id("RISK", "AI", finding.id), "AI_REVIEW_CANDIDATE", finding.title, finding.severity, finding.summary, "AI review identified a plausible engineering concern; this is not deterministic proof of a defect.", finding.recommendation, finding.evidence_ids, finding.confidence, "AI_CANDIDATE"))
    return risks


def _siemens_bool_truth(logic, assignment: dict[str, bool], expected: bool) -> str:
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


def _siemens_verify_requirement(requirement, engineering, evidence, tests) -> RequirementVerification:
    from devagent.plc.production_verification import requirement_candidates

    matched_tags, evidence_ids = requirement_candidates(requirement, engineering, evidence)
    explicit_by_fold = {tag.casefold(): tag for tag in matched_tags}
    modeled = [
        logic
        for logic in engineering.project.output_logic
        if logic.semantic_state is PLCSemanticState.FULL and logic.instruction == "ASSIGN_BOOL"
    ]
    outputs = []
    for logic in modeled:
        matched = explicit_by_fold.get(logic.output_tag.casefold())
        if matched and explicit_bool(requirement.text, matched) is not None and logic.output_tag.casefold() not in {item.casefold() for item in outputs}:
            outputs.append(logic.output_tag)
    if len(outputs) > 1:
        return RequirementVerification(requirement.id, RequirementStatus.TRACEABLE_NOT_PROVEN, "Requirement constrains multiple modeled Siemens outputs; compound assertion semantics are not statically proven in V1.", tuple(evidence_ids), tuple(matched_tags))
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
        if len(matching) != 1:
            return RequirementVerification(requirement.id, RequirementStatus.TRACEABLE_NOT_PROVEN, f"{output} has {len(matching)} modeled Siemens Boolean writer theorem object(s); final output state is withheld without deterministic writer-order semantics.", combined, tuple(matched_tags))
        writer_statements = {
            statement.id
            for statement in engineering.project.logic_statements
            if any(ref.casefold() == output.casefold() for ref in statement.writes)
        }
        if len(writer_statements) != 1:
            return RequirementVerification(requirement.id, RequirementStatus.TRACEABLE_NOT_PROVEN, f"{output} has {len(writer_statements)} normalized Siemens writer statement(s); static requirement proof is withheld without block/task writer-order semantics.", tuple(dict.fromkeys([*combined, *writer_statements])), tuple(matched_tags))
        if not assignment:
            return RequirementVerification(requirement.id, RequirementStatus.TRACEABLE_NOT_PROVEN, f"Requirement names {output} but does not provide enough explicit Boolean input conditions for proof.", combined, tuple(matched_tags))
        truth = _siemens_bool_truth(matching[0], assignment, expected)
        linked = tuple(
            test.id
            for test in tests
            if test.output_tag.casefold() == output.casefold()
            and all(test.preconditions.get(key) == value for key, value in assignment.items())
        )
        if truth == "PROVEN":
            return RequirementVerification(requirement.id, RequirementStatus.STATICALLY_VERIFIED, f"Specified Boolean conditions deterministically imply {output}={'TRUE' if expected else 'FALSE'} in the single-writer bounded Siemens SCL assignment theorem; runtime behavior still requires FAT when policy requires dynamic proof.", combined, tuple(matched_tags), linked)
        if truth == "CONFLICT":
            return RequirementVerification(requirement.id, RequirementStatus.CONFLICT, f"Specified Boolean conditions make required {output}={'TRUE' if expected else 'FALSE'} impossible in the bounded Siemens SCL assignment theorem.", combined, tuple(matched_tags))
        return RequirementVerification(requirement.id, RequirementStatus.TRACEABLE_NOT_PROVEN, f"Requirement maps to {output}, but supplied Boolean conditions under-specify one or more modeled SCL dependencies.", combined, tuple(matched_tags))
    if matched_tags:
        return RequirementVerification(requirement.id, RequirementStatus.TRACEABLE_NOT_PROVEN, "Requirement references known Siemens tags/symbols but its complete behavior is outside the bounded V1 theorem.", tuple(evidence_ids), tuple(matched_tags))
    if evidence_ids:
        return RequirementVerification(requirement.id, RequirementStatus.TRACEABLE_NOT_PROVEN, "Requirement has lexical Siemens trace candidates, but no explicit PLC identifier mapping is proven.", tuple(evidence_ids), confidence=0.5)
    return RequirementVerification(requirement.id, RequirementStatus.NOT_MAPPED, "No deterministic Siemens PLC implementation mapping was found.")


def _siemens_semantic_section(project) -> str:
    profile = siemens_capability_profile(project)
    total = int(profile["scl_statements"])
    full = int(profile["full_statements"])
    pct = "N/A" if total <= 0 else f"{100.0 * full / total:.1f}%"
    opaque_languages = sorted({item.language for item in project.logic_statements if item.semantic_state is PLCSemanticState.OPAQUE})
    lines = [
        "## Semantic Coverage / Proof Boundary",
        "",
        "> Siemens V1 separates source/interface traceability from bounded deterministic SCL behavior proof. TIA XML presence is not a behavioral PASS.",
        "",
        "### Siemens TIA Export Inventory",
        "",
        f"- Tags / symbols: **{len(project.tags)}**",
        f"- Data types: **{len(project.data_types)}**",
        f"- Organization-block execution entries: **{len(project.tasks)}**",
        f"- Blocks / programs: **{len(project.programs)}**",
        f"- Routines: **{len(project.routines)}**",
        f"- SCL statements discovered: **{project.st_statement_total}**",
        f"- Bounded FULL SCL statements: **{project.st_statement_semantic_count}/{project.st_statement_total} ({pct})**",
        f"- Boolean assignment theorem objects: **{len(project.output_logic)}**",
        f"- PARTIAL logic objects: **{profile['partial_statements']}**",
        f"- OPAQUE logic objects: **{profile['opaque_statements']}**",
        f"- Protected/interface-only blocks: **{profile['protected_blocks']}**",
        f"- Dependency graph proof uses only FULL statement/output semantics; PARTIAL/OPAQUE regions remain withheld.",
        "",
        "### Explicit Siemens V1 Boundaries",
        "",
        "- Top-level SCL Boolean assignments (`:=` with AND/OR/NOT) may receive deterministic local path proof and source-linked FAT procedures.",
        "- Simple top-level direct source/literal assignments may contribute bounded local dataflow, but do not by themselves prove final machine behavior.",
        "- SCL inside IF/ELSIF/CASE/FOR/WHILE/REPEAT, block-call semantics, and complex expressions remain PARTIAL until a dedicated theorem models their execution semantics.",
        "- TIA XML LAD/FBD/GRAPH/STL networks are imported structurally in V1 and remain OPAQUE to behavioral proof; generated SCL source should be exported when available for deeper review.",
        "- Protected/interface-only block implementations remain NOT_PROVEN.",
        "- DevAgent does not open proprietary `.ap*` / `.zap*` projects and does not execute PLCSIM, HIL, or a real PLC.",
        f"- OPAQUE languages/surfaces: `{', '.join(opaque_languages) or 'none'}`",
        "",
        "### Trust Boundary",
        "",
        "Static proof is limited to the exact exported source bytes and bounded semantics above. FAT procedures remain engineer-executed and NOT_RUN until authenticated evidence is imported.",
        "",
    ]
    return "\n".join(lines)


def _siemens_optimization(existing, engineering, risks):
    result = list(existing(engineering, risks))
    fingerprints: dict[str, list] = defaultdict(list)
    for statement in engineering.project.logic_statements:
        normalized = re.sub(r"\s+", "", statement.text).casefold()
        if normalized:
            fingerprints[normalized].append(statement)
    seen = {(item.category, item.title.casefold(), tuple(item.evidence_ids)) for item in result}
    for fingerprint, statements in fingerprints.items():
        if len(statements) < 2:
            continue
        evidence = tuple(item.id for item in statements)
        for category in ("DUPLICATE_LOGIC", "DUPLICATION"):
            candidate = OptimizationCandidate(
                stable_id("OPT-SIEMENS-DUP", category, fingerprint),
                category,
                "Repeated identical Siemens source logic",
                f"The same normalized Siemens source statement appears {len(statements)} times.",
                "Review whether a shared FC/FB/pattern would reduce duplication where functionally safe; do not refactor safety logic solely for style.",
                "Potentially reduces maintenance drift and regression/FAT surface.",
                Severity.MEDIUM,
                evidence,
            )
            key = (candidate.category, candidate.title.casefold(), tuple(candidate.evidence_ids))
            if key not in seen:
                seen.add(key)
                result.append(candidate)
    return result


def install() -> None:
    """Install Siemens as a vendor branch without changing qualified Rockwell paths."""
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import production_evidence as _evidence
    from devagent.plc import production_regression as _regression
    from devagent.plc import production_review as _review
    from devagent.plc import production_verification as _verification
    from devagent.plc import semantic_coverage_report as _semantic_report
    from devagent.plc import production as _production

    previous_evidence = _evidence.evidence_index
    previous_findings = _evidence.deterministic_engineering_findings
    previous_risks = _review.detect_risks
    previous_optimization = _review.optimization_candidates
    previous_verify = _verification.verify_requirement
    previous_regression = _regression.analyze_regression
    previous_run = _production.run_production_verification
    previous_semantic = _semantic_report.render_semantic_coverage_section

    def evidence_index(engineering):
        return _siemens_evidence_index(engineering) if _is_siemens(engineering) else previous_evidence(engineering)

    def deterministic_engineering_findings(engineering, valid_evidence_ids):
        return _siemens_findings(engineering, valid_evidence_ids) if _is_siemens(engineering) else previous_findings(engineering, valid_evidence_ids)

    def detect_risks(engineering, verifications, executions, engineering_findings):
        return _siemens_detect_risks(engineering, verifications, executions, engineering_findings) if _is_siemens(engineering) else previous_risks(engineering, verifications, executions, engineering_findings)

    def optimization_candidates(engineering, risks):
        return _siemens_optimization(previous_optimization, engineering, risks) if _is_siemens(engineering) else previous_optimization(engineering, risks)

    def verify_requirement(requirement, engineering, evidence, tests):
        return _siemens_verify_requirement(requirement, engineering, evidence, tests) if _is_siemens(engineering) else previous_verify(requirement, engineering, evidence, tests)

    # production_regression resolves this module global at runtime; replacing the
    # analyzer keeps the mature tag/output fingerprint logic while allowing a
    # same-vendor Siemens baseline. The V12 source-surface wrapper then also sees
    # Siemens logic_statement deltas.
    _regression.analyze_rockwell_l5x = analyze_plc_project

    def analyze_regression(baseline_path, engineering, verifications):
        changes, baseline = previous_regression(baseline_path, engineering, verifications)
        if baseline is not None and baseline.project.metadata.vendor.casefold() != engineering.project.metadata.vendor.casefold():
            raise ValueError(
                f"Regression baseline vendor {baseline.project.metadata.vendor} does not match current project vendor {engineering.project.metadata.vendor}"
            )
        return changes, baseline

    def render_semantic_coverage_section(project):
        return _siemens_semantic_section(project) if _is_siemens(project) else previous_semantic(project)

    # The V4 production function imported the old Rockwell analyzer by value.
    # Redirect only that module-global slot; qualified Rockwell inputs still
    # dispatch to the exact guarded Rockwell analyzer.
    _production.analyze_rockwell_l5x = analyze_plc_project

    def run_production_verification(*args, **kwargs):
        result = previous_run(*args, **kwargs)
        if _is_siemens(result.engineering):
            project = result.engineering.project
            result.stages[0] = StageRecord(
                1,
                result.stages[0].name,
                result.stages[0].status,
                f"Validated Siemens TIA Portal engineering export bundle for {project.metadata.controller_name}; proprietary TIA project execution/opening is outside DevAgent.",
                result.stages[0].evidence_ids,
            )
            result.stages[1] = StageRecord(
                2,
                result.stages[1].name,
                result.stages[1].status,
                f"Canonical IR: {len(project.tags)} tags/symbols, {len(project.routines)} routines/blocks, {project.st_statement_total} SCL statements, {len(project.output_logic)} bounded Boolean output-logic object(s).",
                result.stages[1].evidence_ids,
            )
        return result

    _evidence.evidence_index = evidence_index
    _evidence.deterministic_engineering_findings = deterministic_engineering_findings
    _review.detect_risks = detect_risks
    _review.optimization_candidates = optimization_candidates
    _verification.verify_requirement = verify_requirement
    _regression.analyze_regression = analyze_regression
    _semantic_report.render_semantic_coverage_section = render_semantic_coverage_section
    _production.evidence_index = evidence_index
    _production.deterministic_engineering_findings = deterministic_engineering_findings
    _production.detect_risks = detect_risks
    _production.optimization_candidates = optimization_candidates
    _production.verify_requirement = verify_requirement
    _production.analyze_regression = analyze_regression
    _production.run_production_verification = run_production_verification
    _INSTALLED = True


__all__ = ["install"]
