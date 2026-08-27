from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from devagent.plc import siemens_state_machine_v5 as _v5


_INSTALLED = False
_PREVIOUS_ANALYZER = _v5.analyze_siemens_tia_v5
_ORIGINAL_V1 = _v5._v1


class _AssignmentMatcher:
    """V5-only view of the legacy assignment matcher.

    Siemens named-argument FB calls contain ``:=`` inside parentheses. The
    legacy assignment regex is intentionally broad for source normalization and
    can therefore match a line such as ``Delay(IN := TRUE, PT := T#1s);``.
    V5 sequencing must classify that line as a call before generic assignment
    handling so timer/counter runtime dependencies are not silently lost.
    """

    def __init__(self, matcher):
        self._matcher = matcher

    def match(self, text: str):
        if _v5._CALL.match(text):
            return None
        return self._matcher.match(text)


class _V1SequencingView:
    def __init__(self, module):
        self._module = module
        self._ASSIGNMENT = _AssignmentMatcher(module._ASSIGNMENT)

    def __getattr__(self, name):
        return getattr(self._module, name)


def _line_number(statement) -> int | None:
    raw = statement.source.line
    if raw is None:
        return None
    try:
        return int(str(raw))
    except ValueError:
        return None


def _normalize_runtime_call_statements(project, facts) -> bool:
    """Attach source-call identity without promoting runtime FB semantics."""

    changed = False
    updated = []
    for statement in project.logic_statements:
        line = _line_number(statement)
        block = (statement.source.program or statement.owner_name or "").casefold()
        machine = next(
            (
                item
                for item in facts.machines
                if item.block.casefold() == block
                and line is not None
                and item.case_line < line < item.end_line
                and item.runtime_dependencies
            ),
            None,
        )
        if machine is None or statement.language != "SCL":
            updated.append(statement)
            continue

        match = _v5._CALL.match(statement.text)
        if match is None:
            updated.append(statement)
            continue
        instance = _ORIGINAL_V1._clean_name(match.group("name"))
        runtime_names = {
            dependency.split(":", 1)[0].casefold()
            for dependency in machine.runtime_dependencies
        }
        if instance.casefold() not in runtime_names:
            updated.append(statement)
            continue

        updated.append(
            replace(
                statement,
                calls=tuple(dict.fromkeys((*statement.calls, instance))),
                semantic_state=_v5.PLCSemanticState.PARTIAL,
            )
        )
        changed = True

    if changed:
        project.logic_statements = updated
        _v5._v4._refresh_counts(project)
    return changed


def _rebuild_graph(project):
    graph = _v5.build_dependency_graph(project)
    v3facts = _v5._v3._facts(project)
    if v3facts is not None:
        graph = _v5._v3._augment_graph(graph, v3facts)
    return graph


def _refresh_base_checks(result, graph):
    fresh = {
        item.id: item
        for item in _ORIGINAL_V1._siemens_checks(
            result.project,
            graph,
            result.fat_tests,
        )
    }
    checks = []
    seen = set()
    for item in result.static_checks:
        current = fresh.get(item.id, item)
        checks.append(current)
        seen.add(current.id)
    for item in fresh.values():
        if item.id not in seen:
            checks.append(item)
    return checks


def _hardened_analyzer(path: Path):
    result = _PREVIOUS_ANALYZER(path)
    facts = getattr(result.project, "_siemens_v5_state_machine_facts", None)
    if facts is None:
        return result

    normalized_calls = _normalize_runtime_call_statements(result.project, facts)
    graph = _rebuild_graph(result.project) if normalized_calls else result.graph
    checks = _refresh_base_checks(result, graph) if normalized_calls else result.static_checks

    runtime_dependent = any(
        machine.runtime_dependencies
        for machine in facts.machines
    )
    profile = _v5.siemens_capability_profile_v5(result.project)
    state_complete = profile["state_machine_contract"] in {"COMPLETE", "NONE"}
    if profile["static_contract"] == "NO_EXECUTABLE_LOGIC":
        outcome = _v5.PLCOutcome.BLOCKED
    elif (
        profile["static_contract"] == "COMPLETE"
        and state_complete
        and not runtime_dependent
    ):
        outcome = _v5.PLCOutcome.STATICALLY_VERIFIED
    else:
        outcome = _v5.PLCOutcome.PARTIALLY_VERIFIED

    limitations = list(result.limitations)
    if runtime_dependent:
        limitations.append(
            "At least one bounded Siemens V5 state transition depends on TON/TOF/TP/CTU/CTD runtime state. The transition relation is traceable, but whole-project static closure is withheld until engineer runtime evidence verifies timing/counting evolution."
        )

    return replace(
        result,
        outcome=outcome,
        graph=graph,
        static_checks=checks,
        limitations=list(dict.fromkeys(limitations)),
    )


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import siemens_tia_v1 as _v1

    # Scope the call-before-assignment precedence correction to V5 only. The
    # qualified V1-V4 source parser and Rockwell paths keep their exact behavior.
    _v5._v1 = _V1SequencingView(_ORIGINAL_V1)
    _v5.analyze_siemens_tia_v5 = _hardened_analyzer
    _v1.analyze_siemens_tia = _hardened_analyzer
    _dispatch.analyze_siemens_tia = _hardened_analyzer
    _INSTALLED = True


__all__ = ["install"]
