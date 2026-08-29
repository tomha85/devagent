from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

from .advanced_semantics import (
    LiveAdvancedCoverage,
    LiveAdvancedKind,
    LiveAdvancedModel,
    LiveNumericComparison,
    LiveNumericOperand,
)
from .diagnosis import LiveObservedTag, observations_from_reconciled
from .engineering_context import LiveEngineeringContext, normalize_engineering_identifier
from .history import LiveTimelineStore
from .reconciled_evidence import ReconciledLiveAgentEvidence


class LiveAdvancedDiagnosisStatus(str, Enum):
    CONDITION_TRUE = "CONDITION_TRUE"
    CONDITION_FALSE = "CONDITION_FALSE"
    LOGIC_CONFLICT = "LOGIC_CONFLICT"
    OBSERVED = "OBSERVED"
    IDLE = "IDLE"
    IN_PROGRESS = "IN_PROGRESS"
    WAITING_RESPONSE = "WAITING_RESPONSE"
    ACTIVE_FAULT = "ACTIVE_FAULT"
    HISTORY_REQUIRED = "HISTORY_REQUIRED"
    INDETERMINATE = "INDETERMINATE"


@dataclass(frozen=True)
class LiveAdvancedTarget:
    numeric: LiveNumericComparison | None = None
    model: LiveAdvancedModel | None = None

    @property
    def found(self) -> bool:
        return self.numeric is not None or self.model is not None

    @property
    def name(self) -> str | None:
        if self.numeric is not None:
            return self.numeric.result_tag or self.numeric.left.reference or self.numeric.right.reference
        return self.model.name if self.model is not None else None


@dataclass(frozen=True)
class LiveAdvancedDiagnosis:
    kind: LiveAdvancedKind
    name: str
    status: LiveAdvancedDiagnosisStatus
    summary: str
    current_values: tuple[tuple[str, Any], ...]
    source_locators: tuple[str, ...]
    limitations: tuple[str, ...]
    next_checks: tuple[str, ...]

    def render_text(self) -> str:
        lines = [
            "Advanced commissioning diagnosis (read-only, evidence bounded):",
            f"- Kind: {self.kind.value}",
            f"- Target: {self.name}",
            f"- Status: {self.status.value}",
            f"- {self.summary}",
        ]
        if self.current_values:
            lines.append("- Trusted CURRENT values:")
            for name, value in self.current_values:
                lines.append(f"  - {name} = {value!r}")
        if self.source_locators:
            lines.append("- PLC source:")
            lines.extend(f"  - {item}" for item in self.source_locators)
        if self.next_checks:
            lines.append("- Next check(s):")
            lines.extend(f"  - {item}" for item in self.next_checks)
        if self.limitations:
            lines.append("- Limitations:")
            lines.extend(f"  - {item}" for item in self.limitations)
        return "\n".join(lines)


_KIND_TERMS: dict[LiveAdvancedKind, tuple[str, ...]] = {
    LiveAdvancedKind.NUMERIC_COMPARISON: ("threshold", "comparison", "greater", "less", "limit", "speed", "pressure", "temperature", "level"),
    LiveAdvancedKind.ONE_SHOT: ("one shot", "oneshot", "edge", "ons", "osr", "osf", "r trig", "f trig"),
    LiveAdvancedKind.LATCH: ("latch", "unlatch", "otl", "otu", "set reset", "set/reset"),
    LiveAdvancedKind.HANDSHAKE: ("handshake", "request", "ack", "acknowledge", "busy", "done"),
    LiveAdvancedKind.AOI_FB: ("aoi", "add on", "function block", "fb"),
    LiveAdvancedKind.FAULT_CODE: ("fault code", "error code", "alarm code", "diagnostic code"),
    LiveAdvancedKind.SEQUENCER: ("sequencer", "sequence", "sqo", "sqc"),
    LiveAdvancedKind.MOTION: ("motion", "axis", "move", "home", "servo"),
    LiveAdvancedKind.PID: ("pid", "loop", "setpoint", "process variable", "control variable"),
    LiveAdvancedKind.UDT: ("udt", "structure", "structured type"),
    LiveAdvancedKind.ARRAY: ("array", "index", "element"),
}


def _contains_identity(question: str, value: str | None) -> bool:
    target = normalize_engineering_identifier(value)
    return bool(target and target in normalize_engineering_identifier(question))


def _unique_model_matches(items: Iterable[LiveAdvancedModel]) -> list[LiveAdvancedModel]:
    result: list[LiveAdvancedModel] = []
    seen: set[str] = set()
    for item in items:
        if item.id in seen:
            continue
        seen.add(item.id)
        result.append(item)
    return result


def resolve_advanced_target(
    coverage: LiveAdvancedCoverage,
    question: str,
    *,
    context: LiveEngineeringContext | None = None,
) -> LiveAdvancedTarget:
    text = str(question or "").strip()
    if not text:
        return LiveAdvancedTarget()

    numeric_direct = [
        item
        for item in coverage.numeric_comparisons
        if item.result_tag and _contains_identity(text, item.result_tag)
    ]
    if len(numeric_direct) == 1:
        return LiveAdvancedTarget(numeric=numeric_direct[0])
    if len(numeric_direct) > 1:
        exact = [
            item
            for item in numeric_direct
            if normalize_engineering_identifier(item.result_tag) == normalize_engineering_identifier(text)
        ]
        if len(exact) == 1:
            return LiveAdvancedTarget(numeric=exact[0])
        return LiveAdvancedTarget()

    name_direct = _unique_model_matches(
        item for item in coverage.models if _contains_identity(text, item.name)
    )
    if len(name_direct) == 1:
        return LiveAdvancedTarget(model=name_direct[0])
    if len(name_direct) > 1:
        exact_name = [
            item
            for item in name_direct
            if normalize_engineering_identifier(item.name) == normalize_engineering_identifier(text)
        ]
        if len(exact_name) == 1:
            return LiveAdvancedTarget(model=exact_name[0])
        return LiveAdvancedTarget()

    if context is not None:
        reference_direct = _unique_model_matches(
            item
            for item in coverage.models
            if any(
                context.unique_tag_for_reference(reference) is not None
                and _contains_identity(text, reference)
                for reference in item.references
            )
        )
        if len(reference_direct) == 1:
            return LiveAdvancedTarget(model=reference_direct[0])
        if len(reference_direct) > 1:
            # A shared referenced tag (for example Enable) identifies the signal, not
            # which AOI/motion/handshake use the engineer intended.
            return LiveAdvancedTarget()

    lowered = text.casefold().replace("_", " ")
    requested_kinds = [
        kind
        for kind, terms in _KIND_TERMS.items()
        if any(term in lowered for term in terms)
    ]
    if len(requested_kinds) == 1:
        kind = requested_kinds[0]
        if kind is LiveAdvancedKind.NUMERIC_COMPARISON:
            if len(coverage.numeric_comparisons) == 1:
                return LiveAdvancedTarget(numeric=coverage.numeric_comparisons[0])
        else:
            matches = [item for item in coverage.models if item.kind is kind]
            if len(matches) == 1:
                return LiveAdvancedTarget(model=matches[0])
    return LiveAdvancedTarget()


def required_advanced_tag_ids(
    context: LiveEngineeringContext,
    target: LiveAdvancedTarget,
) -> tuple[str, ...]:
    refs: Iterable[str]
    if target.numeric is not None:
        refs = target.numeric.references
    elif target.model is not None:
        refs = target.model.references
    else:
        return ()
    result: list[str] = []
    for reference in refs:
        tag = context.unique_tag_for_reference(reference)
        if tag is not None and tag.id not in result:
            result.append(tag.id)
    return tuple(result)


def advanced_observation_map(
    context: LiveEngineeringContext,
    reconciled: ReconciledLiveAgentEvidence,
) -> dict[str, LiveObservedTag]:
    """Index observations by every exact canonical identity form."""
    result: dict[str, LiveObservedTag] = {}
    tags = context.tag_by_id()
    for item in observations_from_reconciled(reconciled):
        result[normalize_engineering_identifier(item.tag_name)] = item
        tag = tags.get(item.tag_id)
        if tag is None:
            continue
        for identity in tag.identity_forms():
            result[identity] = item
    return result


def _observed(
    observations: Mapping[str, LiveObservedTag],
    reference: str | None,
) -> LiveObservedTag | None:
    if not reference:
        return None
    return observations.get(normalize_engineering_identifier(reference))


def _numeric_value(
    operand: LiveNumericOperand,
    observations: Mapping[str, LiveObservedTag],
) -> tuple[float | int | None, str | None]:
    if operand.literal is not None:
        return operand.literal, None
    item = _observed(observations, operand.reference)
    if item is None:
        return None, f"No reconciled live observation for {operand.reference}."
    if not item.definitive_current:
        return None, item.limitation or f"{item.tag_name} is not trusted CURRENT evidence."
    if isinstance(item.value, bool) or not isinstance(item.value, (int, float)):
        return None, f"{item.tag_name} value {item.value!r} is not numeric."
    return item.value, None


def _compare(left: float | int, op: str, right: float | int) -> bool:
    if op == ">":
        return left > right
    if op == ">=":
        return left >= right
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    raise ValueError(f"unsupported comparison operator: {op}")


def diagnose_numeric_comparison(
    item: LiveNumericComparison,
    observations: Mapping[str, LiveObservedTag],
) -> LiveAdvancedDiagnosis:
    left, left_error = _numeric_value(item.left, observations)
    right, right_error = _numeric_value(item.right, observations)
    limitations = [error for error in (left_error, right_error) if error]
    current: list[tuple[str, Any]] = []
    for operand, value in ((item.left, left), (item.right, right)):
        if operand.reference and value is not None:
            current.append((operand.reference, value))
    result_observed = _observed(observations, item.result_tag)
    if result_observed is not None and result_observed.definitive_current:
        current.append((result_observed.tag_name, result_observed.value))

    name = item.result_tag or f"{item.left.display} {item.operator} {item.right.display}"
    if limitations or left is None or right is None:
        return LiveAdvancedDiagnosis(
            kind=LiveAdvancedKind.NUMERIC_COMPARISON,
            name=name,
            status=LiveAdvancedDiagnosisStatus.INDETERMINATE,
            summary=f"Cannot evaluate {item.left.display} {item.operator} {item.right.display} from trusted numeric live evidence.",
            current_values=tuple(current),
            source_locators=(item.source_locator,) if item.source_locator else (),
            limitations=tuple(limitations),
            next_checks=("Expose/reconcile the missing numeric operand through OPC UA.",),
        )

    expected = _compare(left, item.operator, right)
    direct_assignment = item.origin == "STATEMENT_ASSIGNMENT"
    if item.origin == "RUNG_COMPARISON":
        limitations.append(
            "This RLL comparator is one rung condition; its truth value alone does not prove a rung output because other contacts/branches may gate execution."
        )
    if item.origin == "STATEMENT_COMPARISON_CONTEXT":
        limitations.append(
            "This comparison is a sub-expression/context only; Live does not bind it to a result tag because the complete statement contains additional logic."
        )
    if direct_assignment and result_observed is not None and result_observed.definitive_current:
        if not isinstance(result_observed.value, bool):
            limitations.append(f"Result tag {result_observed.tag_name} is not Boolean; comparison/result consistency cannot be proven.")
        elif result_observed.value is not expected:
            return LiveAdvancedDiagnosis(
                kind=LiveAdvancedKind.NUMERIC_COMPARISON,
                name=name,
                status=LiveAdvancedDiagnosisStatus.INDETERMINATE,
                summary=(
                    f"Trusted operands currently evaluate {item.left.display} {item.operator} {item.right.display} as {expected}, "
                    f"while observed result {result_observed.tag_name}={result_observed.value}."
                ),
                current_values=tuple(current),
                source_locators=(item.source_locator,) if item.source_locator else (),
                limitations=(
                    "The values are not a proven atomic PLC scan snapshot and the result may have additional writers/scan-order effects; Live refuses to classify this mismatch as a logic conflict without stronger provenance.",
                ),
                next_checks=("Inspect result-tag writers, scan order, and source timestamps at the PLC source location.",),
            )

    return LiveAdvancedDiagnosis(
        kind=LiveAdvancedKind.NUMERIC_COMPARISON,
        name=name,
        status=(LiveAdvancedDiagnosisStatus.CONDITION_TRUE if expected else LiveAdvancedDiagnosisStatus.CONDITION_FALSE),
        summary=f"{item.left.display}={left!r} {item.operator} {item.right.display}={right!r} evaluates to {expected}.",
        current_values=tuple(current),
        source_locators=(item.source_locator,) if item.source_locator else (),
        limitations=tuple(limitations),
        next_checks=(),
    )


def _current_pairs(
    model: LiveAdvancedModel,
    observations: Mapping[str, LiveObservedTag],
) -> tuple[tuple[str, Any], ...]:
    result: list[tuple[str, Any]] = []
    for reference in model.references:
        item = _observed(observations, reference)
        if item is None or not item.definitive_current:
            continue
        if (item.tag_name, item.value) not in result:
            result.append((item.tag_name, item.value))
    return tuple(result)


def _last_transition_text(
    context: LiveEngineeringContext,
    model: LiveAdvancedModel,
    history: LiveTimelineStore | None,
) -> tuple[str, ...]:
    if history is None:
        return ()
    tag_ids = {
        tag.id
        for reference in model.references
        for tag in (context.unique_tag_for_reference(reference),)
        if tag is not None
    }
    candidates = [item for item in history.transitions() if item.tag_id in tag_ids]
    candidates.sort(key=lambda item: item.timestamp, reverse=True)
    return tuple(
        f"{item.tag_name} {item.old_value!r}->{item.new_value!r} at {item.timestamp.isoformat()}"
        for item in candidates[:6]
    )


def _diagnose_handshake(
    context: LiveEngineeringContext,
    model: LiveAdvancedModel,
    observations: Mapping[str, LiveObservedTag],
    history: LiveTimelineStore | None,
) -> LiveAdvancedDiagnosis:
    roles = dict(model.metadata.get("roles", {}))
    values: dict[str, bool] = {}
    missing_roles: list[str] = []
    missing_refs: list[str] = []
    for role, reference in roles.items():
        item = _observed(observations, reference)
        if item is None or not item.definitive_current or not isinstance(item.value, bool):
            missing_roles.append(role)
            missing_refs.append(reference)
            continue
        values[role] = item.value
    current = tuple((roles[role], values[role]) for role in roles if role in values)

    error_active = values.get("ERROR") is True or values.get("TIMEOUT") is True
    request = values.get("REQUEST")
    ack = values.get("ACK")
    busy = values.get("BUSY")
    done = values.get("DONE")
    positive_response = done is True or ack is True or busy is True
    response_roles = tuple(role for role in ("ACK", "BUSY", "DONE") if role in roles)
    status_roles = tuple(role for role in ("ERROR", "TIMEOUT") if role in roles)
    missing_for_negative_conclusion = tuple(
        role for role in (*response_roles, *status_roles) if role in missing_roles
    )

    if error_active:
        status = LiveAdvancedDiagnosisStatus.ACTIVE_FAULT
        summary = "Handshake exposes an active ERROR/TIMEOUT signal."
    elif request is True and done is True:
        status = LiveAdvancedDiagnosisStatus.OBSERVED
        summary = "Request and completion are both active; inspect reset/acknowledge semantics for this handshake."
    elif request is True and positive_response:
        status = LiveAdvancedDiagnosisStatus.IN_PROGRESS
        summary = "Request is active and a trusted response/busy state is observed."
    elif request is True and missing_for_negative_conclusion:
        status = LiveAdvancedDiagnosisStatus.INDETERMINATE
        summary = "Request is active, but Live cannot prove a waiting-response state because one or more modeled response/status signals are missing or untrusted."
    elif request is True:
        status = LiveAdvancedDiagnosisStatus.WAITING_RESPONSE
        summary = "Request is active and all modeled trusted response/status signals are inactive."
    elif request is False and positive_response:
        status = LiveAdvancedDiagnosisStatus.OBSERVED
        summary = "A trusted response state remains active while Request is false; this may be cleanup or a stuck handshake depending on PLC logic."
    elif request is False and missing_for_negative_conclusion:
        status = LiveAdvancedDiagnosisStatus.INDETERMINATE
        summary = "Request is inactive, but Live cannot prove the handshake is idle because one or more modeled response/status signals are missing or untrusted."
    elif request is False:
        status = LiveAdvancedDiagnosisStatus.IDLE
        summary = "Request and all modeled trusted response/status signals are inactive."
    else:
        status = LiveAdvancedDiagnosisStatus.INDETERMINATE
        summary = "Handshake Request is missing, untrusted, or non-Boolean."

    history_lines = _last_transition_text(context, model, history)
    limitations = [
        "Handshake grouping is inferred from canonical tag names; it is not promoted to PROVEN protocol semantics without explicit PLC logic.",
    ]
    if missing_refs:
        limitations.append("Missing/untrusted/non-Boolean handshake signals: " + ", ".join(missing_refs))
    if history_lines:
        limitations.append("Recent trusted transitions: " + " | ".join(history_lines))
    return LiveAdvancedDiagnosis(
        kind=model.kind,
        name=model.name,
        status=status,
        summary=summary,
        current_values=current,
        source_locators=model.source_locators,
        limitations=tuple(limitations),
        next_checks=("Trace the request/ack/busy/done writers in PLC logic if the observed handshake state is unexpected.",),
    )


def _diagnose_fault_code(
    coverage: LiveAdvancedCoverage,
    model: LiveAdvancedModel,
    observations: Mapping[str, LiveObservedTag],
) -> LiveAdvancedDiagnosis:
    item = _observed(observations, model.references[0] if model.references else model.name)
    if item is None or not item.definitive_current:
        return LiveAdvancedDiagnosis(
            kind=model.kind,
            name=model.name,
            status=LiveAdvancedDiagnosisStatus.INDETERMINATE,
            summary="Fault/error code is not available as trusted CURRENT evidence.",
            current_values=(),
            source_locators=model.source_locators,
            limitations=("Live does not invent a fault code when the OPC UA value is unavailable or untrusted.",),
            next_checks=("Expose/reconcile the fault-code tag through OPC UA.",),
        )

    related_sources: list[str] = []
    for comparison in coverage.numeric_comparisons:
        refs = {normalize_engineering_identifier(ref) for ref in comparison.references}
        if normalize_engineering_identifier(model.name) in refs and comparison.source_locator:
            related_sources.append(comparison.source_locator)
    description = model.metadata.get("description")
    limitations = [
        "Numeric code meaning is only reported when supported by imported PLC logic/description; DevAgent does not invent vendor fault dictionaries.",
    ]
    if description:
        limitations.append(f"Engineering description: {description}")
    if related_sources:
        limitations.append("PLC logic compares/references this code at: " + ", ".join(dict.fromkeys(related_sources)))
    return LiveAdvancedDiagnosis(
        kind=model.kind,
        name=model.name,
        status=LiveAdvancedDiagnosisStatus.OBSERVED,
        summary=f"Current fault/error code is {item.value!r}.",
        current_values=((item.tag_name, item.value),),
        source_locators=tuple(dict.fromkeys((*model.source_locators, *related_sources))),
        limitations=tuple(limitations),
        next_checks=("Inspect the PLC code branch associated with this code and the device/vendor diagnostic source if a textual meaning is not present in the engineering project.",),
    )


def diagnose_advanced_model(
    context: LiveEngineeringContext,
    coverage: LiveAdvancedCoverage,
    model: LiveAdvancedModel,
    observations: Mapping[str, LiveObservedTag],
    *,
    history: LiveTimelineStore | None = None,
) -> LiveAdvancedDiagnosis:
    if model.kind is LiveAdvancedKind.HANDSHAKE:
        return _diagnose_handshake(context, model, observations, history)
    if model.kind is LiveAdvancedKind.FAULT_CODE:
        return _diagnose_fault_code(coverage, model, observations)

    current = _current_pairs(model, observations)
    recent = _last_transition_text(context, model, history)
    limitations: list[str] = []
    next_checks: list[str] = []

    if model.kind is LiveAdvancedKind.ONE_SHOT:
        status = LiveAdvancedDiagnosisStatus.HISTORY_REQUIRED
        summary = f"{model.instruction} one-shot/edge instruction is present in the imported PLC logic."
        limitations.append("A one-shot pulse depends on edge memory and scan history; current tag values alone cannot prove the pulse occurred in a specific scan.")
        next_checks.append("Use the trusted timeline and source rung/block to confirm the triggering edge and storage state.")
    elif model.kind is LiveAdvancedKind.LATCH:
        status = LiveAdvancedDiagnosisStatus.HISTORY_REQUIRED
        summary = f"{model.instruction} latch/set-reset instruction is present; current state depends on the last executed set/reset path."
        limitations.append("Live refuses to treat a latched value as combinational logic. Scan order and execution history are required to prove why it is currently retained.")
        next_checks.append("Inspect both set and reset writers and the latest trusted target transition.")
    elif model.kind is LiveAdvancedKind.AOI_FB:
        full = str(model.semantic_state or "").upper() == "FULL"
        status = LiveAdvancedDiagnosisStatus.OBSERVED if full else LiveAdvancedDiagnosisStatus.INDETERMINATE
        summary = (
            f"{model.metadata.get('instance_kind', 'AOI/FB')} {model.instruction} context is available with modeled internals."
            if full
            else f"{model.metadata.get('instance_kind', 'AOI/FB')} {model.instruction} is present, but its internal semantics are partial/protected/unavailable."
        )
        if not full:
            limitations.append("Live will not infer internal AOI/FB decisions from call arguments when the canonical internal body is not FULL.")
        next_checks.append("Inspect the mapped input/output parameters and internal canonical statements when available.")
    elif model.kind is LiveAdvancedKind.SEQUENCER:
        status = LiveAdvancedDiagnosisStatus.OBSERVED
        summary = f"{model.instruction} sequencer context is identified; exposed control/data references are shown below."
        limitations.append("Live does not simulate sequence table indexing, prescan, control-word history, or hidden array contents that are not exposed through canonical/runtime evidence.")
        next_checks.append("Check current position/control members and the active step inputs in PLC logic.")
    elif model.kind is LiveAdvancedKind.MOTION:
        status = LiveAdvancedDiagnosisStatus.OBSERVED
        summary = f"Motion instruction/call {model.instruction} is identified with available live arguments/context."
        limitations.append("Axis command execution, coordinated motion, servo state, and physical position are not inferred unless the corresponding axis/status values are exposed and trusted.")
        next_checks.append("Inspect axis ready/servo/fault/home/position status tags and the instruction source.")
    elif model.kind is LiveAdvancedKind.PID:
        status = LiveAdvancedDiagnosisStatus.OBSERVED
        summary = f"PID/control-loop instruction {model.instruction} is identified with available live references."
        limitations.append("Live does not infer tuning quality, integral state, saturation, tracking, or loop stability from instruction presence alone.")
        next_checks.append("Inspect trusted PV/SP/CV, mode, limits and saturation/status members exposed by the controller.")
    elif model.kind is LiveAdvancedKind.UDT:
        status = LiveAdvancedDiagnosisStatus.OBSERVED
        summary = f"Structured type context is available for {model.name} ({model.metadata.get('data_type', 'unknown type')})."
        limitations.append("UDT structure context is not equivalent to live member evidence; individual members must be exposed/reconciled for diagnosis.")
    elif model.kind is LiveAdvancedKind.ARRAY:
        status = LiveAdvancedDiagnosisStatus.OBSERVED
        summary = f"Array context is available for {model.name} ({model.metadata.get('data_type', 'unknown type')})."
        limitations.append("Dynamic index semantics and individual elements are not inferred unless the indexed references are explicit in canonical logic/runtime evidence.")
    else:
        status = LiveAdvancedDiagnosisStatus.INDETERMINATE
        summary = f"Advanced model {model.kind.value} is present but has no dedicated deterministic diagnosis path."

    if recent:
        limitations.append("Recent trusted transitions: " + " | ".join(recent))
    if not current and model.references:
        limitations.append("No referenced signal is currently available as trusted CURRENT evidence.")
    return LiveAdvancedDiagnosis(
        kind=model.kind,
        name=model.name,
        status=status,
        summary=summary,
        current_values=current,
        source_locators=model.source_locators,
        limitations=tuple(limitations),
        next_checks=tuple(next_checks),
    )


def diagnose_advanced_target(
    context: LiveEngineeringContext,
    coverage: LiveAdvancedCoverage,
    target: LiveAdvancedTarget,
    observations: Mapping[str, LiveObservedTag],
    *,
    history: LiveTimelineStore | None = None,
) -> LiveAdvancedDiagnosis | None:
    if target.numeric is not None:
        return diagnose_numeric_comparison(target.numeric, observations)
    if target.model is not None:
        return diagnose_advanced_model(context, coverage, target.model, observations, history=history)
    return None


__all__ = [
    "LiveAdvancedDiagnosisStatus",
    "LiveAdvancedTarget",
    "LiveAdvancedDiagnosis",
    "resolve_advanced_target",
    "required_advanced_tag_ids",
    "advanced_observation_map",
    "diagnose_numeric_comparison",
    "diagnose_advanced_model",
    "diagnose_advanced_target",
]
