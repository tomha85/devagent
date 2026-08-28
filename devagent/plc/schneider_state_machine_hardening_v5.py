from __future__ import annotations

from dataclasses import replace
import re

from devagent.plc.models import PLCSemanticState
from devagent.plc import schneider_state_machine_v5 as _v5


_INSTALLED = False
_PREVIOUS_NORMALIZE_STATE_VALUE = _v5._normalize_state_value
_PREVIOUS_EXCLUSIVE_PATHS = _v5._exclusive_paths
_PREVIOUS_RUNTIME_CALL = _v5._runtime_call
_PREVIOUS_PARSE_MACHINE = _v5._parse_machine
_PREVIOUS_CAPABILITY_V5 = _v5.schneider_capability_profile_v5

_DECIMAL = re.compile(r"^[+-]?\d+$")
_BASE = re.compile(r"^(?P<base>2|8|16)#(?P<digits>[0-9A-Fa-f_]+)$")
_RANGES = {
    "SINT": (-(2**7), 2**7 - 1),
    "USINT": (0, 2**8 - 1),
    "BYTE": (0, 2**8 - 1),
    "INT": (-(2**15), 2**15 - 1),
    "UINT": (0, 2**16 - 1),
    "WORD": (0, 2**16 - 1),
    "DINT": (-(2**31), 2**31 - 1),
    "UDINT": (0, 2**32 - 1),
    "DWORD": (0, 2**32 - 1),
    "LINT": (-(2**63), 2**63 - 1),
    "ULINT": (0, 2**64 - 1),
    "LWORD": (0, 2**64 - 1),
}


def _strict_state_value(value: str) -> str | None:
    """Canonicalize only bounded numeric CASE labels/targets.

    Named constants/enums and typed literals are intentionally withheld until
    Schneider V8 owns project-wide constant/type identity. Converting numeric
    base literals to decimal also prevents 16#0A and 10 from becoming distinct
    pseudo-states in the V5 graph.
    """

    raw = str(value or "").strip().replace(" ", "")
    if _DECIMAL.fullmatch(raw):
        try:
            return str(int(raw, 10))
        except ValueError:
            return None
    match = _BASE.fullmatch(raw)
    if match is None:
        return None
    base = int(match.group("base"))
    digits = match.group("digits").replace("_", "")
    if not digits:
        return None
    try:
        return str(int(digits, base))
    except ValueError:
        return None


def _nonempty_exclusive_paths(asts, index: int, *, is_else: bool):
    paths = _PREVIOUS_EXCLUSIVE_PATHS(asts, index, is_else=is_else)
    if not paths:
        raise _v5._Unsupported("transition_guard_never_true")
    return paths


def _runtime_call_without_output_binding(text: str, globals_by_name):
    if "=>" in text:
        raise _v5._Unsupported("runtime_call_output_binding_unsupported")
    return _PREVIOUS_RUNTIME_CALL(text, globals_by_name)


def _parse_machine_with_range_check(**kwargs):
    parsed = _PREVIOUS_PARSE_MACHINE(**kwargs)
    if parsed is None:
        return None
    cursor, machine = parsed
    bounds = _RANGES.get(machine.state_type)
    if bounds is None:
        return parsed
    low, high = bounds
    values = [*machine.states, *(item.target_state for item in machine.transitions)]
    invalid = []
    for value in values:
        try:
            numeric = int(value, 10)
        except ValueError:
            invalid.append(value)
            continue
        if numeric < low or numeric > high:
            invalid.append(value)
    if not invalid:
        return parsed

    transitions = tuple(
        replace(
            item,
            semantic_state=PLCSemanticState.PARTIAL,
            reason="state_value_out_of_range",
        )
        for item in machine.transitions
    )
    reasons = [
        item
        for item in machine.reason.split(",")
        if item and item != "bounded_case_state_machine"
    ]
    reasons.append("state_value_out_of_range")
    return cursor, replace(
        machine,
        transitions=transitions,
        semantic_state=PLCSemanticState.PARTIAL,
        reason=",".join(dict.fromkeys(reasons)),
    )


def _reconcile_writers_by_case_region(project, machines):
    """Treat state writes inside one CASE as sequence transitions, not competitors."""

    if not machines:
        return tuple(machines)

    counts: dict[str, int] = {}
    for machine in machines:
        key = machine.state_tag.casefold()
        counts[key] = counts.get(key, 0) + 1

    updated = []
    for machine in machines:
        key = machine.state_tag.casefold()
        conflicts = []
        for statement in project.logic_statements:
            if not any(write.casefold() == key for write in statement.writes):
                continue
            line = _v5._statement_line(statement)
            owner = (
                statement.source.routine
                or statement.routine
                or statement.owner_name
                or ""
            ).casefold()
            inside_machine = (
                owner == machine.section.casefold()
                and line is not None
                and machine.case_line <= line <= machine.end_line
            )
            if not inside_machine:
                conflicts.append(statement.id)

        if counts[key] > 1:
            conflicts.append(f"multiple_case_machines:{machine.state_tag}")
        if not conflicts:
            updated.append(replace(machine, writer_conflicts=()))
            continue

        reasons = [
            item
            for item in machine.reason.split(",")
            if item and item != "bounded_case_state_machine"
        ]
        reasons.append("competing_state_writer")
        updated.append(
            replace(
                machine,
                writer_conflicts=tuple(sorted(set(conflicts), key=str.casefold)),
                semantic_state=PLCSemanticState.PARTIAL,
                reason=",".join(dict.fromkeys(reasons)),
            )
        )
    return tuple(updated)


def _hardened_capability(project) -> dict[str, object]:
    profile = dict(_PREVIOUS_CAPABILITY_V5(project))
    if profile.get("schema") == "devagent-schneider-control-expert-capability-v5":
        profile["bounded_state_machine_semantics"] = (
            "single-level Control Expert ST CASE over one exported integer state tag "
            "with canonical in-range numeric state literals and direct or Boolean "
            "IF/ELSIF/ELSE guarded state writes"
        )
        profile["state_identity_boundary"] = (
            "named constants, enums, typed symbolic literals, and project-wide constant "
            "identity remain fail-closed until the Schneider V8 canonical symbol/type layer"
        )
    return profile


def _rewrite_v5_text(value: str) -> str:
    text = str(value)
    for old in (
        "Schneider Control Expert V1",
        "Schneider Control Expert V2",
        "Schneider Control Expert V3",
        "Schneider Control Expert V4",
        "Schneider V1",
        "Schneider V2",
        "Schneider V3",
        "Schneider V4",
    ):
        text = text.replace(old, "Schneider V5")
    text = text.replace("under the V1 contract", "under the V5 contract")
    text = text.replace("under the V2 contract", "under the V5 contract")
    text = text.replace("under the V3 contract", "under the V5 contract")
    text = text.replace("under the V4 contract", "under the V5 contract")
    return text


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import schneider_control_expert_v1 as _v1
    from devagent.plc import schneider_integration_v1 as _integration
    from devagent.plc import schneider_report_install_v1 as _report

    _v5._normalize_state_value = _strict_state_value
    _v5._exclusive_paths = _nonempty_exclusive_paths
    _v5._runtime_call = _runtime_call_without_output_binding
    _v5._parse_machine = _parse_machine_with_range_check
    _v5._reconcile_writers = _reconcile_writers_by_case_region
    _v5.schneider_capability_profile_v5 = _hardened_capability
    _v1.schneider_capability_profile = _hardened_capability
    _integration.schneider_capability_profile = _hardened_capability

    previous_evidence = _integration._evidence_index
    previous_findings = _integration._findings
    previous_risks = _integration._detect_risks
    previous_render = _report._render

    def evidence_index(engineering):
        items = list(previous_evidence(engineering))
        if _v5._facts(engineering.project) is None:
            return items
        return [
            replace(item, summary=_rewrite_v5_text(item.summary))
            if item.kind == "SCHNEIDER_CONTROL_EXPERT_CAPABILITY_PROFILE"
            else item
            for item in items
        ]

    def findings(engineering, valid_evidence_ids):
        items = list(previous_findings(engineering, valid_evidence_ids))
        if _v5._facts(engineering.project) is None:
            return items
        return [
            replace(
                item,
                title=_rewrite_v5_text(item.title),
                summary=_rewrite_v5_text(item.summary),
                recommendation=_rewrite_v5_text(item.recommendation),
            )
            for item in items
        ]

    def detect_risks(engineering, verifications, executions, engineering_findings):
        risks = list(previous_risks(engineering, verifications, executions, engineering_findings))
        facts = _v5._facts(engineering.project)
        if facts is None:
            return risks
        state_tags = {machine.state_tag.casefold() for machine in facts.machines}
        return [
            risk
            for risk in risks
            if not (
                risk.category == "MULTIPLE_WRITERS"
                and risk.title.casefold().startswith("multiple schneider source writers for ")
                and risk.title.casefold().removeprefix(
                    "multiple schneider source writers for "
                ) in state_tags
            )
        ]

    def render(project):
        text = previous_render(project)
        if _v5._facts(project) is None:
            return text
        old = (
            "- V5 statically models only a bounded source transition relation for one exported integer state tag "
            "with simple CASE labels/targets and direct or Boolean IF/ELSIF/ELSE guarded state writes.\n"
        )
        new = (
            "- V5 statically models only a bounded source transition relation for one exported integer state tag "
            "with canonical in-range numeric CASE labels/targets and direct or Boolean IF/ELSIF/ELSE guarded state writes.\n"
            "- Named constants, enums, typed symbolic state literals, out-of-range values, timer/counter output bindings, "
            "and impossible transition guards remain fail-closed; project-wide constant/type identity is deferred to Schneider V8.\n"
        )
        return text.replace(old, new)

    _integration._evidence_index = evidence_index
    _integration._findings = findings
    _integration._detect_risks = detect_risks
    _report._render = render
    _INSTALLED = True


__all__ = ["install"]
