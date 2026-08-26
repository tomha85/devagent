from __future__ import annotations

from devagent.plc.analysis import _has_neutral_text_branch
from devagent.plc.models import PLCEngineeringResult


def render_fat_report(result: PLCEngineeringResult) -> str:
    project = result.project
    metadata = project.metadata
    graph = result.graph
    dependency_edges = [edge for edge in graph.edges if edge.kind == "DEPENDS_ON"]
    protected_routines = sum(1 for routine in project.routines if routine.source_protected)
    protected_aois = sum(1 for aoi in project.aois if aoi.source_protected)
    branched_rungs = sum(1 for rung in project.rungs if _has_neutral_text_branch(rung.text))
    routine_types = sorted({routine.routine_type for routine in project.routines})

    lines = [
        "DEVAGENT ROCKWELL PLC ENGINEERING VERIFICATION REPORT",
        "",
        "STATUS",
        result.outcome.value,
        "",
        "ARTIFACT",
        f"Vendor: {metadata.vendor}",
        f"Engineering tool: {metadata.engineering_tool}",
        f"Source: {metadata.source_path}",
        f"SHA-256: {metadata.source_sha256}",
        f"L5X schema revision: {metadata.schema_revision or 'UNKNOWN'}",
        f"Software revision: {metadata.software_revision or 'UNKNOWN'}",
        f"Target type: {metadata.target_type or 'UNKNOWN'}",
        "Full-project export: YES",
        "",
        "CONTROLLER",
        f"Name: {metadata.controller_name}",
        f"Processor: {metadata.processor_type or 'UNKNOWN'}",
        f"Revision: {metadata.major_revision or '?'}{'.' + metadata.minor_revision if metadata.minor_revision else ''}",
        "",
        "PROJECT INVENTORY",
        f"Data types: {len(project.data_types)}",
        f"Modules: {len(project.modules)}",
        f"Tasks: {len(project.tasks)}",
        f"Controller/program tags: {len(project.tags)}",
        f"Programs: {len(project.programs)}",
        f"Program routines: {len(project.routines)}",
        f"Routine types: {', '.join(routine_types) if routine_types else 'none'}",
        f"Protected/encoded routines: {protected_routines}",
        f"Parsed program RLL rungs: {len(project.rungs)}",
        f"Parsed program RLL instructions: {project.instruction_total}",
        f"Branched RLL rungs: {branched_rungs}",
        f"Structured Text semantic statements: {project.st_statement_total}",
        f"Add-On Instructions: {len(project.aois)}",
        f"AOI internal bodies normalized: {project.aoi_internal_modeled_count}/{project.aoi_internal_total}",
        f"AOI calls directionally bound: {project.aoi_call_bound_count}/{project.aoi_call_total}",
        f"Protected/encoded AOIs: {protected_aois}",
        "",
        "CANONICAL PLC IR — V2 COMPLETE LOGIC UNDERSTANDING",
        "Canonical objects retain controller/program/routine/rung-or-line provenance.",
        f"Directional RLL instruction semantic coverage: {project.instruction_semantic_coverage:.1%} "
        f"({project.instruction_semantic_count}/{project.instruction_total})",
        f"RLL branch-path semantic coverage: {project.branch_semantic_coverage:.1%} "
        f"({project.branch_rung_semantic_count}/{project.branch_rung_total})",
        f"Structured Text semantic coverage: {project.st_semantic_coverage:.1%} "
        f"({project.st_statement_semantic_count}/{project.st_statement_total})",
        f"Recognized-partial instruction names: {', '.join(project.partially_modeled_instruction_names) if project.partially_modeled_instruction_names else 'none'}",
        f"Unmodeled instruction names: {', '.join(project.unknown_instruction_names) if project.unknown_instruction_names else 'none'}",
        "",
        "DEPENDENCY GRAPH",
        f"Total graph edges: {len(graph.edges)}",
        f"Deterministic DEPENDS_ON edges: {len(dependency_edges)}",
        "READS/WRITES/CALLS remain source facts for normalized logic.",
        "DEPENDS_ON is emitted only from deterministic output-specific RLL paths, fully modeled ST statements, or proven AOI call translations; partial/opaque logic is withheld.",
        "",
        "FAT TEST MODEL",
        f"Generated deterministic static FAT candidates: {len(result.fat_tests)}",
        "Execution status: NOT_RUN",
    ]

    for test in result.fat_tests[:40]:
        conditions = ", ".join(
            f"{name}={'TRUE' if value else 'FALSE'}" for name, value in test.preconditions.items()
        )
        lines.extend(
            [
                "",
                f"{test.id} — {test.title}",
                f"Scenario: {test.scenario}",
                f"Source: {test.source.locator}",
                f"Preconditions: {conditions or 'none'}",
                f"Expected: {test.expected}",
                f"Execution: {test.execution_status}",
            ]
        )
    if len(result.fat_tests) > 40:
        lines.extend(["", f"... {len(result.fat_tests) - 40} additional FAT candidates are available in fat_tests.json"])

    lines.extend(["", "STATIC VERIFICATION"])
    for check in result.static_checks:
        lines.append(f"{check.status.value}  {check.id}: {check.summary}")
        for evidence in check.evidence:
            lines.append(f"  Evidence: {evidence}")

    lines.extend(["", "LIMITATIONS"])
    for limitation in result.limitations:
        lines.append(f"- {limitation}")

    lines.extend(
        [
            "",
            "VERIFICATION BOUNDARY",
            "STATICALLY_VERIFIED means the exported project structure, source provenance, and every discovered logic region that contributes to the status are covered by the supported deterministic V2 RLL/ST/AOI/branch semantic model, with derived dependencies and FAT candidates traceable to source.",
            "It does NOT mean real machine behavior, safety performance, timing, physical I/O, network/device behavior, or FAT execution passed. Protected, partial, opaque, or unresolved logic is never promoted to proven behavior. Dynamic PASS/FAIL requires an approved simulator or controller execution backend.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"
