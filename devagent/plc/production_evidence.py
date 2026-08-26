from __future__ import annotations

from devagent.plc.models import PLCOutcome
from devagent.plc.production_models import EngineeringFinding, EvidenceItem, Severity


def evidence_index(engineering) -> list[EvidenceItem]:
    project = engineering.project
    result: list[EvidenceItem] = []
    for tag in project.tags:
        evidence_id = f"TAG:{tag.scope}:{tag.name}"
        result.append(EvidenceItem(
            evidence_id,
            "TAG",
            f"{tag.scope} tag {tag.name}: {tag.data_type}",
            payload={"tag": tag.name, "scope": tag.scope, "data_type": tag.data_type},
        ))
    for rung in project.rungs:
        result.append(EvidenceItem(
            rung.id,
            "RUNG",
            f"{rung.source.locator}: {rung.text[:240]}",
            rung.source.locator,
            project.metadata.source_sha256,
            {"reads": list(rung.reads), "writes": list(rung.writes), "calls": list(rung.calls)},
        ))
    for statement in project.logic_statements:
        result.append(EvidenceItem(
            statement.id,
            f"{statement.language}_STATEMENT",
            f"{statement.source.locator}: {statement.text[:240]}",
            statement.source.locator,
            project.metadata.source_sha256,
            {"reads": list(statement.reads), "writes": list(statement.writes), "semantic_state": statement.semantic_state.value},
        ))
    for logic in project.output_logic:
        result.append(EvidenceItem(
            logic.id,
            "OUTPUT_LOGIC",
            f"{logic.source.locator}: {logic.output_tag} via {logic.instruction} ({len(logic.paths)} modeled path(s))",
            logic.source.locator,
            project.metadata.source_sha256,
            {"output_tag": logic.output_tag, "instruction": logic.instruction, "origin": logic.origin},
        ))
    for check in engineering.static_checks:
        evidence_id = f"CHECK:{check.id}"
        result.append(EvidenceItem(
            evidence_id,
            "STATIC_CHECK",
            f"{check.id}: {check.status.value} — {check.summary}",
            payload={"status": check.status.value, "evidence": list(check.evidence)},
        ))
    return result


def deterministic_engineering_findings(engineering, valid_evidence_ids: set[str]) -> list[EngineeringFinding]:
    project = engineering.project
    checks = tuple(
        f"CHECK:{item.id}"
        for item in engineering.static_checks
        if f"CHECK:{item.id}" in valid_evidence_ids
    )
    findings = [EngineeringFinding(
        "ENG-INVENTORY",
        "PROJECT_INVENTORY",
        "PLC project inventory normalized",
        Severity.INFO,
        f"Normalized {len(project.tags)} tags, {len(project.programs)} programs, {len(project.routines)} routines, {len(project.rungs)} RLL rungs, {project.st_statement_total} ST statements, and {len(project.aois)} AOIs.",
        "Use the canonical IR as the authoritative downstream engineering input.",
        checks[:3],
    )]
    if engineering.outcome is not PLCOutcome.STATICALLY_VERIFIED:
        findings.append(EngineeringFinding(
            "ENG-SEMANTIC-GAPS",
            "SEMANTIC_COVERAGE",
            "Not all PLC behavior is statically proven",
            Severity.HIGH,
            "One or more protected, unsupported, indirect, partial, or unresolved semantics remain fail-closed.",
            "Resolve or explicitly disposition every NOT_PROVEN/PARTIAL semantic before release readiness can pass.",
            checks,
        ))
    if project.branch_rung_total:
        findings.append(EngineeringFinding(
            "ENG-BRANCH-COVERAGE",
            "RLL_BRANCHING",
            "RLL branch-path coverage",
            Severity.INFO if project.branch_rung_semantic_count == project.branch_rung_total else Severity.MEDIUM,
            f"Modeled {project.branch_rung_semantic_count}/{project.branch_rung_total} branched rungs with deterministic output-specific paths.",
            "Review any unmodeled branch before relying on its dependency/FAT evidence.",
            checks,
        ))
    if project.aois:
        complete = (
            project.aoi_internal_modeled_count == project.aoi_internal_total
            and project.aoi_call_bound_count == project.aoi_call_total
        )
        findings.append(EngineeringFinding(
            "ENG-AOI-COVERAGE",
            "AOI",
            "Add-On Instruction coverage",
            Severity.INFO if complete else Severity.MEDIUM,
            f"Modeled {project.aoi_internal_modeled_count}/{project.aoi_internal_total} AOI bodies and directionally bound {project.aoi_call_bound_count}/{project.aoi_call_total} AOI calls.",
            "Keep unresolved/protected AOIs outside VERIFIED claims until their exported bodies/interfaces are deterministically bound.",
            checks,
        ))
    return findings
