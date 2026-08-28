from __future__ import annotations

from devagent.plc.report_contract_v1 import build_report_contract


_INSTALLED = False


def _score(value: object) -> str:
    return "N/A" if value is None else f"{float(value):.1f}%"


def _decision_block(result) -> str:
    contract = build_report_contract(result)
    semantic = contract["semantic"]
    requirements = contract["requirements"]
    execution = contract["execution"]
    release = contract["release"]

    if requirements["scope"] == "NOT_PROVIDED":
        requirement_line = "NOT PROVIDED — customer requirement compliance was not evaluated."
    else:
        requirement_line = (
            f"{requirements['proven']} proven / {requirements['unresolved']} unresolved of {requirements['total']}."
        )

    if semantic["normalized_logic_objects"]:
        semantic_line = (
            f"{semantic['full']}/{semantic['normalized_logic_objects']} FULL ({_score(semantic['full_pct'])}); "
            f"PARTIAL {semantic['partial']}; OPAQUE {semantic['opaque']}."
        )
    else:
        semantic_line = "N/A — no normalized logic objects."

    release_line = release["status"]
    if release["score"] is not None:
        release_line += f" — {release['score']}/100"

    lines = [
        "DECISION SCORECARD",
        f"Engineering review coverage:       {_score(contract['engineering_analysis_score'])}",
        f"Normalized semantic FULL coverage: {semantic_line}",
        f"Requirement verification:          {requirement_line}",
        f"FAT plan completeness:             {_score(contract['fat_plan_completeness_score'])}",
        f"Runtime FAT execution:             {execution['executed']}/{execution['total']} executed ({_score(execution['executed_pct'])})",
        f"Evidence package:                  {contract['evidence_items']} item(s), {contract['verified_signatures']} verified signature(s)",
        f"Release readiness:                 {release_line}",
        f"Report consistency contract:       {contract['contract_status']}",
        "NOTE: Release-readiness score is a policy/evidence gate, not a score of engineering-analysis quality.",
    ]
    return "\n".join(lines)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import report_levels

    previous = report_levels.render_console_summary

    def render_console_summary(result, *, output_dir=None, scenario_limit=20):
        base = previous(result, output_dir=output_dir, scenario_limit=scenario_limit)
        marker = "SUMMARY\n"
        block = _decision_block(result)
        if marker in base and "DECISION SCORECARD\n" not in base:
            return base.replace(marker, block + "\n\n" + marker, 1)
        return block + "\n\n" + base

    report_levels.render_console_summary = render_console_summary
    _INSTALLED = True


__all__ = ["install"]
