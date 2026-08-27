from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from devagent.plc import siemens_state_machine_v5 as _v5


_INSTALLED = False
_PREVIOUS_ANALYZER = _v5.analyze_siemens_tia_v5
_PREVIOUS_UPGRADE_STATEMENTS = _v5._upgrade_statements
_ORIGINAL_V1 = _v5._v1


def _strip_case_label_prefix(text: str) -> str:
    """Return the executable body when legacy SCL IR joins a CASE label to it.

    The V1 logical-statement builder intentionally buffers non-control lines,
    so a bounded CASE label can be represented as ``10: Delay(...);`` in one
    canonical statement. Reuse the V5 bounded label grammar rather than
    accepting an arbitrary colon prefix.
    """

    raw = str(text or "").lstrip()
    if ":" not in raw:
        return raw
    prefix, body = raw.split(":", 1)
    if _v5._LABEL.match(prefix.strip() + ":") is None:
        return raw
    return body.lstrip()


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
        executable = _strip_case_label_prefix(text)
        if _v5._CALL.match(executable):
            return None
        return self._matcher.match(text)


class _V1SequencingView:
    def __init__(self, module):
        self._module = module
        self._ASSIGNMENT = _AssignmentMatcher(module._ASSIGNMENT)

    def __getattr__(self, name):
        return getattr(self._module, name)


def _normalize_runtime_call_statements(project, machines) -> bool:
    """Attach runtime FB call identity without promoting its semantics.

    Runtime dependency identity has already been proven by V5 from the block
    declaration and CASE source. Use that identity directly when repairing the
    legacy statement IR so TON/TOF/TP/CTU/CTD calls retain provenance while
    remaining explicitly PARTIAL/runtime-dependent.
    """

    runtime_names = {
        dependency.split(":", 1)[0].casefold()
        for machine in machines
        for dependency in machine.runtime_dependencies
    }
    if not runtime_names:
        return False

    changed = False
    updated = []
    for statement in project.logic_statements:
        if statement.language != "SCL":
            updated.append(statement)
            continue

        executable = _strip_case_label_prefix(statement.text)
        match = _v5._CALL.match(executable)
        if match is None:
            updated.append(statement)
            continue
        instance = _ORIGINAL_V1._clean_name(match.group("name"))
        if instance.casefold() not in runtime_names:
            updated.append(statement)
            continue

        normalized = replace(
            statement,
            reads=_ORIGINAL_V1._extract_refs(executable),
            writes=(),
            calls=tuple(dict.fromkeys((*statement.calls, instance))),
            semantic_state=_v5.PLCSemanticState.PARTIAL,
        )
        updated.append(normalized)
        changed = changed or normalized != statement

    if changed:
        project.logic_statements = updated
        _v5._v4._refresh_counts(project)
    return changed


def _hardened_upgrade_statements(project, machines):
    """Run qualified V5 statement upgrades, then repair runtime-call provenance.

    This hook executes inside the V5 analyzer before dependency graph and static
    checks are generated. It avoids post-analysis mutation and keeps the V1-V4
    parsers unchanged.
    """

    _PREVIOUS_UPGRADE_STATEMENTS(project, machines)
    _normalize_runtime_call_statements(project, machines)


def _hardened_analyzer(path: Path):
    result = _PREVIOUS_ANALYZER(path)
    facts = getattr(result.project, "_siemens_v5_state_machine_facts", None)
    if facts is None:
        return result

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
        limitations=list(dict.fromkeys(limitations)),
    )


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import siemens_tia_v1 as _v1

    # Scope both corrections to the Siemens V5 extension only. V1-V4 and the
    # Rockwell/general software-engineering paths retain their exact behavior.
    _v5._v1 = _V1SequencingView(_ORIGINAL_V1)
    _v5._upgrade_statements = _hardened_upgrade_statements
    _v5.analyze_siemens_tia_v5 = _hardened_analyzer
    _v1.analyze_siemens_tia = _hardened_analyzer
    _dispatch.analyze_siemens_tia = _hardened_analyzer
    _INSTALLED = True


__all__ = ["install"]
