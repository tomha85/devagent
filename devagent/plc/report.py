from __future__ import annotations

from devagent.plc.models import PLCEngineeringResult


def render_fat_report(result: PLCEngineeringResult) -> str:
    project = result.project
    metadata = project.metadata
    graph = result.graph
    dependency_edges = [edge for edge in graph.edges if edge.kind == "DEPENDS_ON"]
    protected = sum(1 for routine in project.routines if routine.source_protected)
    routine_types = sorted({routine.routine_type for routine in project.routines})

    lines = [
        "DEVAGENT ROCKWELL PLC FAT REPORT",
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
        f"Routines: {len(project.routines)}",
        f"Routine types: {', '.join(routine_types) if routine_types else 'none'}",
        f"Protected/encoded routines: {protected}",
        f"Parsed RLL rungs: {len(project.rungs)}",
        f"Add-On Instructions: {len(project.aois)}",
        "",
        "CANONICAL PLC IR",
        "Canonical objects retain Rockwell controller/program/routine/rung provenance.",
        f"Deterministic instruction semantic coverage: {project.instruction_semantic_coverage:.1%} "
        f"({project.instruction_semantic_count}/{project.instruction_total})",
        "",
        "DEPENDENCY GRAPH",
        f"Total graph edges: {len(graph.edges)}",
        f"Tag DEPENDS_ON edges: {len(dependency_edges)}",
        "Dependency edges are evidence-linked to the source rung that established the relation.",
        "",
        "FAT TEST MODEL",
        f"Generated conservative static FAT candidates: {len(result.fat_tests)}",
        "Execution status: NOT_RUN",
    ]

    for test in result.fat_tests[:25]:
        conditions = ", ".join(
            f"{name}={'TRUE' if value else 'FALSE'}" for name, value in test.preconditions.items()
        )
        lines.extend(
            [
                "",
                f"{test.id} — {test.title}",
                f"Source: {test.source.locator}",
                f"Preconditions: {conditions or 'none'}",
                f"Expected: {test.expected}",
                f"Execution: {test.execution_status}",
            ]
        )
    if len(result.fat_tests) > 25:
        lines.extend(["", f"... {len(result.fat_tests) - 25} additional FAT candidates are available in fat_tests.json"])

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
            "STATICALLY_VERIFIED means the supported L5X structure, provenance, modeled RLL semantics, dependency edges, and test traceability passed deterministic checks.",
            "It does NOT mean machine behavior, safety performance, or FAT execution passed. Dynamic PASS/FAIL requires a simulator or approved controller execution backend.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"
