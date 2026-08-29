from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .diagnosis import (
    LiveDiagnosisStatus,
    LiveObservedTag,
    diagnose_output,
    required_tag_ids_for_output,
)
from .engineering_context import LiveEngineeringContext, LiveEngineeringTag
from .tag_reconciliation import LiveTagReconciliation


DEFAULT_SYSTEM_HEALTH_MAX_TAGS = 128


class LiveSystemHealthStatus(str, Enum):
    ATTENTION_REQUIRED = "ATTENTION_REQUIRED"
    NO_CURRENT_PROVEN_FAULT = "NO_CURRENT_PROVEN_FAULT"
    INDETERMINATE = "INDETERMINATE"


class LiveSystemHealthFindingKind(str, Enum):
    FAULT = "FAULT"
    BLOCKER = "BLOCKER"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class LiveSystemHealthFinding:
    kind: LiveSystemHealthFindingKind
    target: str
    summary: str
    evidence_ids: tuple[str, ...] = ()
    source_locators: tuple[str, ...] = ()
    next_checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class LiveSystemHealthScope:
    tag_ids: tuple[str, ...]
    total_relevant_tags: int
    truncated: bool


@dataclass(frozen=True)
class LiveSystemHealthDiagnosis:
    status: LiveSystemHealthStatus
    findings: tuple[LiveSystemHealthFinding, ...]
    mapped_relevant_tags: int
    unresolved_relevant_tags: int
    trusted_current_tags: int
    inspected_relevant_tags: int
    total_relevant_tags: int
    clear_fault_signals: tuple[str, ...]
    inactive_outputs: tuple[str, ...]
    limitations: tuple[str, ...]

    def render_text(self) -> str:
        lines = [
            "DEVAGENT LIVE SYSTEM HEALTH",
            f"Status: {self.status.value}",
            "Mode: READ ONLY",
            (
                "Evidence boundary: "
                f"mapped={self.mapped_relevant_tags} "
                f"unresolved={self.unresolved_relevant_tags} "
                f"trusted_current={self.trusted_current_tags} "
                f"inspected={self.inspected_relevant_tags}/{self.total_relevant_tags} relevant engineering signals"
            ),
        ]

        active = [item for item in self.findings if item.kind is not LiveSystemHealthFindingKind.UNKNOWN]
        unknown = [item for item in self.findings if item.kind is LiveSystemHealthFindingKind.UNKNOWN]
        if active:
            lines.append("")
            lines.append("Current proven/observed issues:")
            for item in active:
                lines.append(f"- [{item.kind.value}] {item.target}: {item.summary}")
                for source in item.source_locators[:3]:
                    lines.append(f"  PLC source: {source}")
        else:
            lines.extend(["", "Current proven/observed issues: NONE"])

        if self.clear_fault_signals:
            lines.append("")
            lines.append("Explicit fault-like signals currently clear:")
            lines.extend(f"- {item}" for item in self.clear_fault_signals[:12])
            if len(self.clear_fault_signals) > 12:
                lines.append(f"- ... {len(self.clear_fault_signals) - 12} more")

        if self.inactive_outputs:
            lines.append("")
            lines.append("Modeled inactive outputs not classified as a fault:")
            lines.extend(f"- {item}" for item in self.inactive_outputs[:12])
            if len(self.inactive_outputs) > 12:
                lines.append(f"- ... {len(self.inactive_outputs) - 12} more")

        if unknown:
            lines.append("")
            lines.append("Unknown / incomplete evidence:")
            lines.extend(f"- {item.target}: {item.summary}" for item in unknown[:12])
            if len(unknown) > 12:
                lines.append(f"- ... {len(unknown) - 12} more")

        if self.status is LiveSystemHealthStatus.ATTENTION_REQUIRED:
            explicit_fault = any(item.kind is LiveSystemHealthFindingKind.FAULT for item in active)
            if explicit_fault:
                conclusion = "At least one current fault-like PLC signal or deterministic logic problem requires attention."
            else:
                conclusion = (
                    "No explicit PLC fault signal is proven active, but a current modeled blocker or logic conflict requires attention."
                )
        elif self.status is LiveSystemHealthStatus.NO_CURRENT_PROVEN_FAULT:
            conclusion = (
                "No current fault, operational blocker, or deterministic logic conflict was proven within the inspected engineering/live evidence."
            )
        else:
            conclusion = (
                "System-wide health cannot be concluded safely because relevant engineering/live evidence is incomplete or untrusted."
            )

        lines.extend(["", "Conclusion:", conclusion])
        lines.append(
            "This conclusion is bounded to imported PLC engineering logic plus trusted CURRENT OPC UA evidence; it does not prove the entire physical machine/process is healthy."
        )

        next_checks: list[str] = []
        for item in self.findings:
            for check in item.next_checks:
                if check and check not in next_checks:
                    next_checks.append(check)
        if next_checks:
            lines.append("")
            lines.append("Next checks:")
            lines.extend(f"- {item}" for item in next_checks[:12])

        if self.limitations:
            lines.append("")
            lines.append("Coverage limitations:")
            lines.extend(f"- {item}" for item in self.limitations[:12])
            if len(self.limitations) > 12:
                lines.append(f"- ... {len(self.limitations) - 12} more")
        return "\n".join(lines)


_SYSTEM_HEALTH_PHRASES = (
    "system health",
    "machine health",
    "is the system ok",
    "is the system okay",
    "is the machine ok",
    "is the machine okay",
    "does the system have any fault",
    "does the system have faults",
    "does the machine have any fault",
    "any faults in the system",
    "any errors in the system",
    "any alarms in the system",
    "any problems with the system",
    "anything wrong with the system",
    "what is wrong with the system",
    "what's wrong with the system",
    "what is wrong with this system",
    "what's wrong with this system",
    "system co loi",
    "he thong co loi",
    "may co loi",
    "hệ thống có lỗi",
    "máy có lỗi",
)


def is_system_health_question(question: str) -> bool:
    text = " ".join(str(question or "").casefold().strip().split())
    return bool(text and any(phrase in text for phrase in _SYSTEM_HEALTH_PHRASES))


_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_FAULT_WORDS = {"fault", "faulted", "alarm", "error", "trip", "tripped"}
_FAULT_CODE_WORDS = {"fault", "alarm", "error", "diagnostic", "diag"}
_FAULT_EXCLUSIONS = {
    "reset",
    "ack",
    "acknowledge",
    "acknowledged",
    "clear",
    "enable",
    "enabled",
    "mask",
    "masked",
    "inhibit",
    "inhibited",
    "bypass",
    "suppress",
    "suppressed",
}
_STATE_WORDS = {"state", "status"}
_STATE_FAULT_VALUES = {"fault", "faulted", "error", "alarm", "trip", "tripped", "failed", "failure"}
_STATE_BLOCKED_VALUES = {"blocked", "notready", "inhibited"}
_STATE_CLEAR_VALUES = {"ok", "normal", "clear", "ready", "running", "idle", "stopped", "healthy"}
_OPERATIONAL_BLOCKER_WORDS = {
    "ready",
    "fault",
    "faulted",
    "alarm",
    "error",
    "trip",
    "safe",
    "safety",
    "interlock",
    "permissive",
    "permit",
    "healthy",
    "health",
    "ok",
}


def _name_tokens(value: str) -> tuple[str, ...]:
    expanded = _CAMEL_RE.sub(" ", str(value)).replace("_", " ")
    return tuple(item.casefold() for item in _WORD_RE.findall(expanded))


def _fault_signal_role(tag: LiveEngineeringTag) -> str | None:
    tokens = _name_tokens(tag.name)
    if not tokens:
        return None
    if tokens[-1] == "code" and any(item in _FAULT_CODE_WORDS for item in tokens[:-1]):
        return "FAULT_CODE"
    if any(item in _FAULT_EXCLUSIONS for item in tokens):
        return None
    if tokens[-1] in _FAULT_WORDS:
        return "FAULT_BOOL"
    if tokens[-1] in _STATE_WORDS:
        return "STATE"
    return None


def _fault_value_state(role: str, value: object) -> str:
    if role == "FAULT_BOOL":
        if isinstance(value, bool):
            return "ACTIVE_FAULT" if value else "CLEAR"
        return "UNKNOWN"
    if role == "FAULT_CODE":
        if isinstance(value, bool):
            return "UNKNOWN"
        if isinstance(value, (int, float)):
            return "CLEAR" if value == 0 else "ACTIVE_FAULT"
        if isinstance(value, str):
            normalized = "".join(_name_tokens(value))
            if normalized in {"", "0", "none", "nofault", "noerror", "ok", "normal", "clear"}:
                return "CLEAR"
            return "ACTIVE_FAULT"
        return "UNKNOWN"
    if role == "STATE":
        if not isinstance(value, str):
            return "UNKNOWN"
        normalized = "".join(_name_tokens(value))
        if normalized in _STATE_FAULT_VALUES:
            return "ACTIVE_FAULT"
        if normalized in _STATE_BLOCKED_VALUES:
            return "ACTIVE_BLOCKER"
        if normalized in _STATE_CLEAR_VALUES:
            return "CLEAR"
        return "UNKNOWN"
    return "UNKNOWN"


def _is_operational_blocker(name: str | None) -> bool:
    return bool(name and set(_name_tokens(name)) & _OPERATIONAL_BLOCKER_WORDS)


def build_system_health_scope(
    context: LiveEngineeringContext,
    reconciliation: LiveTagReconciliation,
    *,
    max_tags: int = DEFAULT_SYSTEM_HEALTH_MAX_TAGS,
) -> LiveSystemHealthScope:
    if max_tags < 1:
        raise ValueError("max_tags must be >= 1")
    ordered: list[str] = []

    def add_tag(tag: LiveEngineeringTag | None) -> None:
        if tag is not None and tag.id not in ordered:
            ordered.append(tag.id)

    # Explicit fault/state signals are highest priority for a health question.
    for tag in context.tags:
        if _fault_signal_role(tag) is not None:
            add_tag(tag)

    # Then include every deterministic output and its modeled Boolean dependencies.
    for output in context.output_names():
        add_tag(context.unique_tag_for_reference(output))
        for tag_id in required_tag_ids_for_output(context, output):
            tag = context.tag_by_id().get(tag_id)
            add_tag(tag)

    # Only signals that exist in the reconciliation can yield current OPC UA evidence,
    # but retain unresolved relevant ids in the scope so coverage is reported honestly.
    known = reconciliation.mapping_by_tag_id()
    relevant = [tag_id for tag_id in ordered if tag_id in known]
    total = len(relevant)
    selected = tuple(relevant[:max_tags])
    return LiveSystemHealthScope(
        tag_ids=selected,
        total_relevant_tags=total,
        truncated=total > len(selected),
    )


def diagnose_system_health(
    context: LiveEngineeringContext,
    reconciliation: LiveTagReconciliation,
    observations: Iterable[LiveObservedTag],
    scope: LiveSystemHealthScope,
) -> LiveSystemHealthDiagnosis:
    observation_by_id = {item.tag_id: item for item in observations if item.tag_id in scope.tag_ids}
    mapping_by_id = reconciliation.mapping_by_tag_id()
    tag_by_id = context.tag_by_id()

    mapped = sum(
        1
        for tag_id in scope.tag_ids
        if tag_id in mapping_by_id and mapping_by_id[tag_id].accepted
    )
    unresolved = sum(
        1
        for tag_id in scope.tag_ids
        if tag_id in mapping_by_id and not mapping_by_id[tag_id].accepted
    )
    trusted = sum(1 for item in observation_by_id.values() if item.definitive_current)

    findings: list[LiveSystemHealthFinding] = []
    clear_fault_signals: list[str] = []
    inactive_outputs: list[str] = []
    limitations: list[str] = []

    if scope.truncated:
        limitations.append(
            f"System-health inspection was bounded to {len(scope.tag_ids)} of {scope.total_relevant_tags} relevant engineering signals."
        )

    for tag_id in scope.tag_ids:
        tag = tag_by_id.get(tag_id)
        if tag is None:
            continue
        role = _fault_signal_role(tag)
        if role is None:
            continue
        observed = observation_by_id.get(tag_id)
        if observed is None or not observed.definitive_current:
            findings.append(
                LiveSystemHealthFinding(
                    kind=LiveSystemHealthFindingKind.UNKNOWN,
                    target=tag.scoped_name,
                    summary=(
                        observed.limitation
                        if observed is not None and observed.limitation
                        else "No trusted CURRENT OPC UA value is available for this health-relevant signal."
                    ),
                    evidence_ids=((observed.evidence_id,) if observed and observed.evidence_id else ()),
                    next_checks=(f"Restore/reconcile a trusted CURRENT value for {tag.scoped_name}.",),
                )
            )
            continue

        state = _fault_value_state(role, observed.value)
        evidence = (observed.evidence_id,) if observed.evidence_id else ()
        if state == "ACTIVE_FAULT":
            findings.append(
                LiveSystemHealthFinding(
                    kind=LiveSystemHealthFindingKind.FAULT,
                    target=tag.scoped_name,
                    summary=f"Trusted CURRENT value {tag.name}={observed.value!r} indicates an active fault-like signal.",
                    evidence_ids=evidence,
                    next_checks=(f"Inspect the PLC/device diagnostics associated with {tag.scoped_name}.",),
                )
            )
        elif state == "ACTIVE_BLOCKER":
            findings.append(
                LiveSystemHealthFinding(
                    kind=LiveSystemHealthFindingKind.BLOCKER,
                    target=tag.scoped_name,
                    summary=f"Trusted CURRENT state {tag.name}={observed.value!r} indicates a blocked/not-ready state.",
                    evidence_ids=evidence,
                    next_checks=(f"Trace the modeled/upstream reason for {tag.scoped_name}={observed.value!r}.",),
                )
            )
        elif state == "CLEAR":
            clear_fault_signals.append(f"{tag.scoped_name} = {observed.value!r}")
        else:
            findings.append(
                LiveSystemHealthFinding(
                    kind=LiveSystemHealthFindingKind.UNKNOWN,
                    target=tag.scoped_name,
                    summary=f"Current value {observed.value!r} cannot be safely classified by System Health V1.",
                    evidence_ids=evidence,
                )
            )

    selected_set = set(scope.tag_ids)
    for output in context.output_names():
        required = set(required_tag_ids_for_output(context, output))
        if not required or not required.issubset(selected_set):
            if required:
                findings.append(
                    LiveSystemHealthFinding(
                        kind=LiveSystemHealthFindingKind.UNKNOWN,
                        target=output,
                        summary="Not all modeled dependencies were inside the bounded system-health evidence scope.",
                    )
                )
            continue

        diagnosis = diagnose_output(context, output, observation_by_id.values())
        if diagnosis.status is LiveDiagnosisStatus.LOGIC_CONFLICT:
            findings.append(
                LiveSystemHealthFinding(
                    kind=LiveSystemHealthFindingKind.CONFLICT,
                    target=output,
                    summary=diagnosis.summary,
                    evidence_ids=diagnosis.evidence_ids,
                    source_locators=diagnosis.source_locators,
                    next_checks=diagnosis.next_checks,
                )
            )
        elif diagnosis.status is LiveDiagnosisStatus.BLOCKER_IDENTIFIED:
            operational = tuple(
                item for item in diagnosis.blockers
                if _is_operational_blocker(item.tag_name or item.tag_reference)
            )
            if operational:
                names = ", ".join(item.tag_name or item.tag_reference for item in operational)
                findings.append(
                    LiveSystemHealthFinding(
                        kind=LiveSystemHealthFindingKind.BLOCKER,
                        target=output,
                        summary=f"Modeled output is currently blocked by operational permissive/interlock signal(s): {names}.",
                        evidence_ids=diagnosis.evidence_ids,
                        source_locators=diagnosis.source_locators,
                        next_checks=tuple(
                            f"Trace why {item.tag_name or item.tag_reference} is {item.observed_value!r}; modeled logic requires {item.required!r}."
                            for item in operational
                        ),
                    )
                )
            else:
                names = ", ".join(
                    item.tag_name or item.tag_reference for item in diagnosis.blockers
                ) or "modeled command/condition"
                inactive_outputs.append(
                    f"{output}: blocked by {names}; not classified as a system fault because no fault/readiness/safety/interlock signal is proven abnormal."
                )
        elif diagnosis.status is LiveDiagnosisStatus.INDETERMINATE:
            findings.append(
                LiveSystemHealthFinding(
                    kind=LiveSystemHealthFindingKind.UNKNOWN,
                    target=output,
                    summary=diagnosis.summary,
                    evidence_ids=diagnosis.evidence_ids,
                    source_locators=diagnosis.source_locators,
                    next_checks=diagnosis.next_checks,
                )
            )

    for tag_id in scope.tag_ids:
        mapping = mapping_by_id.get(tag_id)
        if mapping is not None and not mapping.accepted:
            limitations.append(
                f"Relevant engineering tag {mapping.tag_name} is unresolved: {mapping.status.value} - {mapping.reason}"
            )
        observed = observation_by_id.get(tag_id)
        if mapping is not None and mapping.accepted and (
            observed is None or not observed.definitive_current
        ):
            limitations.append(
                f"Relevant mapped tag {mapping.tag_name} lacks trusted CURRENT runtime evidence."
            )

    active_issue = any(
        item.kind in {
            LiveSystemHealthFindingKind.FAULT,
            LiveSystemHealthFindingKind.BLOCKER,
            LiveSystemHealthFindingKind.CONFLICT,
        }
        for item in findings
    )
    incomplete = (
        scope.total_relevant_tags == 0
        or scope.truncated
        or unresolved > 0
        or any(item.kind is LiveSystemHealthFindingKind.UNKNOWN for item in findings)
        or trusted < mapped
    )
    if active_issue:
        status = LiveSystemHealthStatus.ATTENTION_REQUIRED
    elif incomplete:
        status = LiveSystemHealthStatus.INDETERMINATE
    else:
        status = LiveSystemHealthStatus.NO_CURRENT_PROVEN_FAULT

    return LiveSystemHealthDiagnosis(
        status=status,
        findings=tuple(findings),
        mapped_relevant_tags=mapped,
        unresolved_relevant_tags=unresolved,
        trusted_current_tags=trusted,
        inspected_relevant_tags=len(scope.tag_ids),
        total_relevant_tags=scope.total_relevant_tags,
        clear_fault_signals=tuple(dict.fromkeys(clear_fault_signals)),
        inactive_outputs=tuple(dict.fromkeys(inactive_outputs)),
        limitations=tuple(dict.fromkeys(limitations)),
    )


__all__ = [
    "DEFAULT_SYSTEM_HEALTH_MAX_TAGS",
    "LiveSystemHealthDiagnosis",
    "LiveSystemHealthFinding",
    "LiveSystemHealthFindingKind",
    "LiveSystemHealthScope",
    "LiveSystemHealthStatus",
    "build_system_health_scope",
    "diagnose_system_health",
    "is_system_health_question",
]
