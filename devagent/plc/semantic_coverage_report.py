from __future__ import annotations

from devagent.plc import production_report as _report
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
    boundaries = manifest["project_boundaries"]
    warnings = boundaries["warnings"]
    st = languages["structured_text"]
    rll = languages["rll"]
    aoi = languages["aoi"]

    lines = [
        "## Semantic Coverage / Proof Boundary",
        "",
        "> This section separates deterministic behavior proof from structural parsing. Structural read/write/call recognition is not a behavioral PASS.",
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


def render_production_report(result) -> str:
    base = _ORIGINAL_RENDER(result)
    section = render_semantic_coverage_section(result.engineering.project)
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


__all__ = ["install", "render_production_report", "render_semantic_coverage_section"]
