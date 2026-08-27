from __future__ import annotations

from devagent.plc import analysis as _analysis
from devagent.plc.models import PLCSemanticState
from devagent.plc.rockwell_general_actions import action_models

_PREVIOUS_COVERAGE_STATE = None
_INSTALLED = False


def _source_matches_rung(source, rung) -> bool:
    return (
        bool(source.program)
        and bool(source.routine)
        and source.rung is not None
        and source.program.casefold() == rung.program.casefold()
        and source.routine.casefold() == rung.routine.casefold()
        and str(source.rung) == str(rung.source.rung if rung.source.rung is not None else rung.number)
    )


def branch_coverage_profile(project) -> dict[str, object]:
    """Return bounded branch coverage across every installed Rockwell theorem.

    Earlier coverage counted only boolean-output branch theorems. Rockwell V11
    also proves bounded data/compute action paths through neutral-text branches.
    V16 reports the union of those independently deterministic surfaces while
    continuing to withhold any mixed branch whose complete grammar is not
    covered by a proof theorem.
    """

    branch_rungs = [rung for rung in project.rungs if _analysis._has_neutral_text_branch(rung.text)]
    branch_ids = {rung.id for rung in branch_rungs}

    boolean_ids: set[str] = set()
    for rung in branch_rungs:
        if any(
            logic.semantic_state is PLCSemanticState.FULL
            and not logic.origin.startswith("AOI_INTERNAL:")
            and _source_matches_rung(logic.source, rung)
            for logic in project.output_logic
        ):
            boolean_ids.add(rung.id)

    action_ids = {
        model.rung_id
        for model in action_models(project)
        if model.rung_id in branch_ids
    }
    modeled_ids = boolean_ids | action_ids
    withheld_ids = branch_ids - modeled_ids

    return {
        "schema": "devagent-rockwell-branch-coverage-v16",
        "branch_rungs": len(branch_ids),
        "boolean_branch_rungs": len(boolean_ids),
        "action_branch_rungs": len(action_ids),
        "modeled_branch_rungs": len(modeled_ids),
        "withheld_branch_rungs": len(withheld_ids),
        "modeled_rung_ids": tuple(sorted(modeled_ids)),
        "withheld_rung_ids": tuple(sorted(withheld_ids)),
    }


def _coverage_state(project):
    if _PREVIOUS_COVERAGE_STATE is None:  # pragma: no cover
        raise RuntimeError("Rockwell V16 branch coverage hardening was not installed")

    state = _PREVIOUS_COVERAGE_STATE(project)
    profile = branch_coverage_profile(project)

    # Preserve the established project fields for compatibility, but make their
    # meaning accurate: a branch is modeled when any bounded deterministic
    # theorem covers its complete grammar, not only the boolean-output theorem.
    project.branch_rung_total = int(profile["branch_rungs"])
    project.branch_rung_semantic_count = int(profile["modeled_branch_rungs"])
    state["unmodeled_branches"] = int(profile["withheld_branch_rungs"])
    return state


def install() -> None:
    global _INSTALLED, _PREVIOUS_COVERAGE_STATE
    if _INSTALLED:
        return
    _PREVIOUS_COVERAGE_STATE = _analysis._coverage_state
    _analysis._coverage_state = _coverage_state
    _INSTALLED = True


__all__ = ["branch_coverage_profile", "install"]
