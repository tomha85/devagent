from __future__ import annotations

from devagent.plc.models import PLCOutcome
from devagent.plc.production_models import EngineeringFinding, EvidenceItem, Severity
from devagent.plc.rockwell_closeout import rockwell_capability_profile


def _logic_paths_payload(logic) -> list[list[dict[str, object]]]:
    """Serialize deterministic Boolean path terms into auditable evidence."""

    return [
        [
            {"tag": term.tag, "required": term.required}
            for term in path.terms
        ]
        for path in logic.paths
    ]


def evidence_index(engineering) -> list[EvidenceItem]:
    project = engineering.project
    capability = rockwell_capability_profile(project)
    capability_id = f"ROCKWELL-CAPABILITY:{project.metadata.source_sha256}"
    result: list[EvidenceItem] = [
        EvidenceItem(
            capability_id,
            "ROCKWELL_CAPABILITY_PROFILE",
            f"Rockwell V9 support contract: {capability['static_contract']} for {project.metadata.controller_name}.",
            project.metadata.source_path,
            project.metadata.source_sha256,
            capability,
        )
    ]
    for tag in project.tags:
        evidence_id = f"TAG:{tag.scope}:{tag.name}"
        result.append(EvidenceItem(
            evidence_id,
            "TAG",
            f"{tag.scope} tag {tag.name}: {tag.data_type}",
            payload={
                "tag": tag.name,
                "scope": tag.scope,
                "data_type": tag.data_type,
                "tag_type": tag.tag_type,
                "alias_for": tag.alias_for,
                "external_access": tag.external_access,
                "constant": tag.constant,
            },
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
            {
                "output_tag": logic.output_tag,
                "instruction": logic.instruction,
                "origin": logic.origin,
                "semantic_state": logic.semantic_state.value,
                "paths": _logic_paths_payload(logic),
            },
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
    capability = rockwell_capability_profile(project)
    capability_id = f"ROCKWELL-CAPABILITY:{project.metadata.source_sha256}"
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
    findings.append(EngineeringFinding(
        "ENG-ROCKWELL-SUPPORT-CONTRACT",
        "ROCKWELL_SUPPORT",
        "Rockwell production support contract",
        Severity.INFO if capability["static_contract"] == "COMPLETE" else Severity.HIGH,
        (
            "All discovered exported semantics are within the V9 static support contract."
            if capability["static_contract"] == "COMPLETE"
            else "One or more exported Rockwell features remain PARTIAL/NOT_PROVEN under the V9 support contract."
        ),
        "Use the capability profile and static checks to disposition every unsupported/protected/partial feature; use a qualified execution backend for runtime PASS evidence.",
        (capability_id,) if capability_id in valid_evidence_ids else checks,
    ))
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
