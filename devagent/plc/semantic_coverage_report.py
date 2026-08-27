from __future__ import annotations

from devagent.plc import production_report as _report
from devagent.plc.four_contract_v13 import render_four_core_contract_section
from devagent.plc.semantic_coverage import build_semantic_coverage_manifest

_ORIGINAL_RENDER = _report.render_production_report
_INSTALLED = False


def _pct(value) -> str:
    return "N/A" if value is None else f"{value:.1f}%"


def render_semantic_coverage_section(project) -> str:
    manifest = build_semantic_coverage_manifest(project)
    inventory = manifest["inventory"]
    summary = manifest["instruction_summary"]
    languages = manifest["language_summary"]
    action = manifest["action_semantics"]
    stateful = manifest["stateful_runtime_semantics"]
    motion = manifest.get("motion_runtime_semantics", {})
    state_machine = manifest.get("state_machine_semantics", {})
    boundaries = manifest["project_boundaries"]
    warnings = boundaries["warnings"]
    st = languages["structured_text"]
    rll = languages["rll"]
    aoi = languages["aoi"]

    lines = [
        "## Semantic Coverage / Proof Boundary",
        "",
        "> This section separates deterministic behavior proof from structural parsing and FAT-required behavior. Structural recognition is not a behavioral PASS.",
        "",
        "### Project Inventory",
        "",
        f"- Tags: **{inventory['tags']}**",
        f"- Data types: **{inventory['data_types']}**",
        f"- Modules: **{inventory['modules']}**",
        f"- Tasks: **{inventory['tasks']}**",
        f"- Scheduled program entries: **{inventory['scheduled_program_entries']}**",
        f"- Programs: **{inventory['programs']}**",
        f"- Routines: **{inventory['routines']}**",
        f"- Program RLL rungs: **{inventory['program_rll_rungs']}**",
        f"- Structured Text statements: **{inventory['structured_text_statements']}**",
        f"- AOIs: **{inventory['aois']}**",
        f"- Analysis warnings: **{len(warnings)}**",
        "",
        "### Coverage",
        "",
        f"- Program RLL deterministic instruction coverage: **{_pct(summary['deterministic_pct'])}** ({summary['deterministic_occurrences']}/{summary['total_occurrences']})",
        f"- Program RLL structural-or-better instruction coverage: **{_pct(summary['structural_or_better_pct'])}**",
        f"- Program RLL structural-only instruction occurrences: **{summary['structural_only_occurrences']}**",
        f"- Program RLL partial instruction occurrences: **{summary['partial_occurrences']}**",
        f"- Program RLL unmodeled instruction occurrences: **{summary['unmodeled_occurrences']}** ({_pct(summary['unmodeled_pct'])})",
        f"- Deterministic Boolean RLL rungs: **{rll['deterministic_boolean_rungs']}**/{rll['program_rungs']}",
        f"- Bounded typed-compare RLL rungs: **{rll['bounded_compare_rungs']}**",
        f"- Bounded data/compute action RLL rungs: **{rll['bounded_action_rungs']}**",
        f"- Bounded action occurrences: **{action['modeled_actions']}** across **{action['modeled_rungs']}** rung(s)",
        f"- Stateful timer/counter runtime models: **{stateful['modeled_occurrences']}**",
        f"- Stateful runtime evidence required: **{'yes' if stateful['requires_qualified_runtime_evidence'] else 'no'}** (engineer-executed FAT; DevAgent does not execute external software)",
        f"- Motion runtime contracts: **{motion.get('modeled_occurrences', 0)}**",
        f"- Motion runtime evidence required: **{'yes' if motion.get('requires_qualified_runtime_evidence') else 'no'}** (engineer-executed FAT; DevAgent does not execute external software)",
        f"- Discovered state transitions: **{state_machine.get('transition_count', 0)}** across **{state_machine.get('state_tag_count', 0)}** state tag(s)",
        f"- State-machine runtime evidence required: **{'yes' if state_machine.get('runtime_evidence_required') else 'no'}** (engineer-executed FAT; DevAgent does not execute external software)",
        f"- ST statements discovered: **{st['statements']}**",
        f"- Reachable FULL ST dataflow: **{st['reachable_full_dataflow_statements']}**/{st['statements']} ({_pct(st['reachable_full_dataflow_pct'])})",
        f"- ST partial/unreachable: **{st['partial_or_unreachable_statements']}**",
        f"- ST opaque: **{st['opaque_statements']}**",
        f"- AOI internal bodies modeled: **{aoi['internal_bodies_modeled']}**/{aoi['internal_bodies_total']}",
        f"- AOI internal RLL statements FULL: **{aoi['internal_rll_full_statements']}**/{aoi['internal_rll_statements']}",
        f"- AOI internal ST statements FULL: **{aoi['internal_st_full_statements']}**/{aoi['internal_st_statements']}",
        f"- AOI calls bound: **{aoi['calls_bound']}**/{aoi['calls_total']}",
        "",
        "### Program RLL Instruction Coverage Breakdown",
        "",
        "| Instruction | Occurrences | Coverage levels |",
        "| --- | ---: | --- |",
    ]
    for item in manifest["instructions"]:
        levels = ", ".join(f"{name}={count}" for name, count in item["levels"].items())
        lines.append(f"| {item['instruction']} | {item['occurrences']} | {levels} |")

    lines += [
        "",
        "### Explicit Semantic Boundaries",
        "",
        "- Partially modeled instructions: `"
        + (", ".join(boundaries["partially_modeled_instruction_names"]) or "none")
        + "`",
        "- Unmodeled instructions: `"
        + (", ".join(boundaries["unmodeled_instruction_names"]) or "none")
        + "`",
        "- Unsupported routine types: `"
        + (", ".join(f"{name}={count}" for name, count in boundaries["unsupported_routine_types"].items()) or "none")
        + "`",
        f"- Protected routines: **{boundaries['protected_routines']}**",
        f"- Analysis warnings: **{len(warnings)}** (full warning text remains in canonical/evidence artifacts and `devagent plc inspect --json`)",
        "",
        str(manifest["trust_note"]),
        "",
    ]
    return "\n".join(lines)


def render_fat_procedure_section(result) -> str:
    lines = [
        "## Engineer FAT Procedures",
        "",
        "> DevAgent generates these procedures from PLC evidence and requirements. DevAgent does not connect to, write to, or execute an external simulator/HIL/PLC. The PLC engineer performs the procedure and records the result.",
        "",
        f"Recommended FAT procedures: **{len(result.engineering.fat_tests)}**",
        "",
    ]
    if not result.engineering.fat_tests:
        lines += ["_No bounded FAT procedure could be generated for this project._", ""]
        return "\n".join(lines)

    for test in result.engineering.fat_tests:
        lines += [
            f"### {test.id} — {test.title}",
            "",
            f"- Scenario: `{test.scenario}`",
            f"- Source: `{test.source.locator}`",
            f"- Output / primary watch target: `{test.output_tag}`",
            f"- Purpose: {test.purpose or test.title}",
            f"- Why this test is required: {test.why_required or 'Confirm the evidence-linked behavior before commissioning.'}",
            f"- Recommended environment: {test.recommended_environment}",
            f"- DevAgent execution status: **{test.execution_status}**",
            "",
            "**Setup / Preconditions**",
            "",
        ]
        lines += [f"{index}. {step}" for index, step in enumerate(test.setup_steps, start=1)] or ["1. Review the evidence-linked source and establish the required initial state."]
        lines += ["", "**Test Actions**", ""]
        lines += [f"{index}. {step}" for index, step in enumerate(test.action_steps, start=1)] or ["1. Execute the condition manually in the engineer-selected test environment."]
        lines += [
            "",
            "**Watch / Record**",
            "",
            "- " + (", ".join(f"`{tag}`" for tag in test.watch_tags) or f"`{test.output_tag}`"),
            "",
            "**Expected Result**",
            "",
            test.expected,
            "",
            "**Evidence to Capture**",
            "",
        ]
        lines += [f"- {item}" for item in test.evidence_required] or ["- Engineer-recorded observed result and relevant tag trace/screenshot."]
        lines += [
            "",
            "**Failure Implication**",
            "",
            test.failure_implication or "The implementation may not match the intended behavior and should be reviewed before commissioning.",
            "",
        ]
        if test.limitations:
            lines += ["**Analysis Boundaries**", ""]
            lines += [f"- {item}" for item in test.limitations]
            lines.append("")
    return "\n".join(lines)


def render_production_report(result) -> str:
    base = _ORIGINAL_RENDER(result)
    contract = render_four_core_contract_section(result)
    semantic = render_semantic_coverage_section(result.engineering.project)
    fat = render_fat_procedure_section(result)
    section = contract + "\n" + semantic + "\n" + fat
    marker = "## Production Release Policy"
    if marker in base:
        return base.replace(marker, section + "\n" + marker, 1)
    return base + "\n\n" + section


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _report.render_production_report = render_production_report
    _INSTALLED = True


__all__ = [
    "install",
    "render_fat_procedure_section",
    "render_production_report",
    "render_semantic_coverage_section",
]
