from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from .diagnosis import (
    LiveCommissioningDiagnosis,
    LiveConditionEvaluation,
    LiveConditionState,
    LiveDiagnosisStatus,
    LiveObservedTag,
    LivePathState,
)
from .diagnosis_guard import diagnose_output
from .engineering_context import (
    LiveEngineeringContext,
    LiveLogicRule,
    normalize_engineering_identifier,
)


DEFAULT_TRACE_MAX_DEPTH = 6
DEFAULT_TRACE_MAX_NODES = 64

_STATEFUL_INSTRUCTIONS = {
    "OTL",
    "OTU",
    "SET",
    "RESET",
    "LATCH",
    "UNLATCH",
    "SR",
    "RS",
}


class LiveRootCauseStepStatus(str, Enum):
    EXPANDED = "EXPANDED"
    ROOT_LOGIC_OBSERVATION = "ROOT_LOGIC_OBSERVATION"
    INDETERMINATE = "INDETERMINATE"
    CYCLE = "CYCLE"
    DEPTH_LIMIT = "DEPTH_LIMIT"
    NODE_LIMIT = "NODE_LIMIT"


@dataclass(frozen=True)
class LiveRootCauseStep:
    signal: str
    required_by_parent: bool | None
    observed_value: bool | None
    status: LiveRootCauseStepStatus
    depth: int
    source_locator: str | None
    rule_id: str | None
    evidence_id: str | None
    detail: str
    children: tuple["LiveRootCauseStep", ...] = ()

    @property
    def terminal(self) -> bool:
        return not self.children


@dataclass(frozen=True)
class LiveRecursiveDiagnosis:
    target_output: str
    direct_status: LiveDiagnosisStatus
    roots: tuple[LiveRootCauseStep, ...]
    evidence_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    max_depth: int
    max_nodes: int
    visited_nodes: int

    @property
    def complete(self) -> bool:
        blocked = {
            LiveRootCauseStepStatus.INDETERMINATE,
            LiveRootCauseStepStatus.CYCLE,
            LiveRootCauseStepStatus.DEPTH_LIMIT,
            LiveRootCauseStepStatus.NODE_LIMIT,
        }
        return bool(self.roots) and not any(
            step.status in blocked
            for root in self.roots
            for step in _walk(root)
        )

    def chains(self) -> tuple[tuple[str, ...], ...]:
        result: list[tuple[str, ...]] = []

        def visit(step: LiveRootCauseStep, prefix: tuple[str, ...]) -> None:
            current = (*prefix, step.signal)
            if not step.children:
                result.append((self.target_output, *current))
                return
            for child in step.children:
                visit(child, current)

        for root in self.roots:
            visit(root, ())
        return tuple(dict.fromkeys(result))

    def render_text(self) -> str:
        if not self.roots:
            return "Root-cause trace: no deeper deterministic PLC logic path was proven."
        lines = ["Root-cause trace (read-only, deterministic):"]
        for chain in self.chains():
            lines.append("- " + " -> ".join(chain))
        lines.append("")
        lines.append("Trace details:")
        for root in self.roots:
            _render_step(lines, root, indent=0)
        if self.limitations:
            lines.append("")
            lines.append("Root-cause trace limitations:")
            lines.extend(f"- {item}" for item in self.limitations)
        lines.append(
            "Root logic observations explain the modeled PLC causal path only; they do not by themselves prove the physical/process root cause."
        )
        return "\n".join(lines)


def _walk(step: LiveRootCauseStep):
    yield step
    for child in step.children:
        yield from _walk(child)


def _render_step(lines: list[str], step: LiveRootCauseStep, *, indent: int) -> None:
    pad = "  " * indent
    observed = "UNKNOWN" if step.observed_value is None else str(step.observed_value).upper()
    required = (
        "n/a"
        if step.required_by_parent is None
        else str(step.required_by_parent).upper()
    )
    lines.append(
        f"{pad}- {step.signal}: observed={observed}, required_by_parent={required}, status={step.status.value}"
    )
    if step.source_locator:
        lines.append(f"{pad}  source: {step.source_locator}")
    lines.append(f"{pad}  {step.detail}")
    for child in step.children:
        _render_step(lines, child, indent=indent + 1)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in values if item))


def _stateful_instruction(instruction: str) -> bool:
    normalized = str(instruction or "").strip().upper().replace("-", "_")
    tokens = {token for token in normalized.replace("/", "_").split("_") if token}
    return normalized in _STATEFUL_INSTRUCTIONS or bool(tokens & _STATEFUL_INSTRUCTIONS)


def _safe_recursive_rule(
    context: LiveEngineeringContext,
    signal: str,
) -> tuple[LiveLogicRule | None, str | None]:
    rules = context.rules_for_output(signal)
    if not rules:
        return None, None
    if len(rules) != 1:
        return None, f"{len(rules)} canonical writers/rules target {signal}; recursive tracing refuses to choose one writer."
    rule = rules[0]
    if str(rule.semantic_state or "").strip().upper() != "FULL":
        return None, f"Rule {rule.id} for {signal} is not FULL semantic coverage."
    if _stateful_instruction(rule.instruction):
        return None, f"Rule {rule.id} for {signal} uses stateful instruction {rule.instruction}; current state depends on history."
    if not rule.paths or not any(path.terms for path in rule.paths):
        return None, f"Rule {rule.id} for {signal} has no modeled Boolean terms."
    return rule, None


def _observation_map(observations: Iterable[LiveObservedTag]) -> dict[str, LiveObservedTag]:
    return {item.tag_id: item for item in observations}


def _resolve_observation(
    context: LiveEngineeringContext,
    observations_by_id: dict[str, LiveObservedTag],
    signal: str,
) -> tuple[LiveObservedTag | None, str | None]:
    matches = context.tags_for_reference(signal)
    if len(matches) != 1:
        if not matches:
            return None, f"Engineering signal {signal!r} does not resolve to one canonical tag."
        return None, f"Engineering signal {signal!r} is ambiguous across {len(matches)} canonical tags."
    observed = observations_by_id.get(matches[0].id)
    if observed is None:
        return None, f"No runtime observation exists for {matches[0].name}."
    if not observed.definitive_current:
        return observed, observed.limitation or f"{matches[0].name} is not trusted CURRENT evidence."
    if not isinstance(observed.value, bool):
        return observed, f"{matches[0].name} is not Boolean and cannot be recursively evaluated."
    return observed, None


def _dedupe_conditions(
    conditions: Iterable[LiveConditionEvaluation],
) -> tuple[LiveConditionEvaluation, ...]:
    seen: set[tuple[str, bool]] = set()
    result: list[LiveConditionEvaluation] = []
    for condition in conditions:
        key = (
            normalize_engineering_identifier(condition.tag_name or condition.tag_reference),
            condition.required,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(condition)
    return tuple(result)


def _causal_conditions(
    diagnosis: LiveCommissioningDiagnosis,
    observed_value: bool,
) -> tuple[LiveConditionEvaluation, ...]:
    if diagnosis.expected_output is not observed_value:
        return ()
    if observed_value is False:
        return _dedupe_conditions(diagnosis.blockers)
    return _dedupe_conditions(
        condition
        for path in diagnosis.paths
        if path.state is LivePathState.SATISFIED
        for condition in path.conditions
        if condition.state is LiveConditionState.SATISFIED
    )


def required_tag_ids_for_recursive_output(
    context: LiveEngineeringContext,
    output_reference: str,
    *,
    max_depth: int = DEFAULT_TRACE_MAX_DEPTH,
    max_nodes: int = DEFAULT_TRACE_MAX_NODES,
) -> tuple[str, ...]:
    if max_depth < 0:
        raise ValueError("max_depth must be >= 0")
    if max_nodes < 1:
        raise ValueError("max_nodes must be >= 1")

    ordered: list[str] = []
    seen_signals: set[str] = set()
    queue: list[tuple[str, int]] = [(output_reference, 0)]

    while queue and len(seen_signals) < max_nodes:
        signal, depth = queue.pop(0)
        normalized = normalize_engineering_identifier(signal)
        if not normalized or normalized in seen_signals:
            continue
        seen_signals.add(normalized)

        tag = context.unique_tag_for_reference(signal)
        if tag is not None and tag.id not in ordered:
            ordered.append(tag.id)
        if depth >= max_depth:
            continue

        rule, _limitation = _safe_recursive_rule(context, signal)
        if rule is None:
            continue
        for path in rule.paths:
            for term in path.terms:
                term_tag = context.unique_tag_for_reference(term.tag_reference)
                if term_tag is not None and term_tag.id not in ordered:
                    ordered.append(term_tag.id)
                queue.append((term.tag_reference, depth + 1))

    return tuple(ordered[:max_nodes])


def trace_recursive_diagnosis(
    context: LiveEngineeringContext,
    direct: LiveCommissioningDiagnosis,
    observations: Iterable[LiveObservedTag],
    *,
    max_depth: int = DEFAULT_TRACE_MAX_DEPTH,
    max_nodes: int = DEFAULT_TRACE_MAX_NODES,
) -> LiveRecursiveDiagnosis:
    if max_depth < 0:
        raise ValueError("max_depth must be >= 0")
    if max_nodes < 1:
        raise ValueError("max_nodes must be >= 1")

    observed_items = tuple(observations)
    observations_by_id = _observation_map(observed_items)
    limitations: list[str] = []
    evidence_ids: list[str] = list(direct.evidence_ids)
    budget = {"visited": 0}

    if direct.status is LiveDiagnosisStatus.BLOCKER_IDENTIFIED:
        seeds = _dedupe_conditions(direct.blockers)
    elif direct.status is LiveDiagnosisStatus.CONDITIONS_SATISFIED:
        seeds = _dedupe_conditions(
            condition
            for path in direct.paths
            if path.state is LivePathState.SATISFIED
            for condition in path.conditions
            if condition.state is LiveConditionState.SATISFIED
        )
    else:
        limitations.append(
            f"Direct diagnosis status={direct.status.value}; recursive cause expansion requires a deterministic blocked or satisfied current logic result."
        )
        seeds = ()

    def trace_condition(
        condition: LiveConditionEvaluation,
        *,
        depth: int,
        stack: tuple[str, ...],
    ) -> LiveRootCauseStep:
        signal = condition.tag_name or condition.tag_reference
        normalized = normalize_engineering_identifier(signal)
        budget["visited"] += 1
        if budget["visited"] > max_nodes:
            detail = f"Recursive trace node limit {max_nodes} reached before expanding {signal}."
            limitations.append(detail)
            return LiveRootCauseStep(
                signal=signal,
                required_by_parent=condition.required,
                observed_value=condition.observed_value,
                status=LiveRootCauseStepStatus.NODE_LIMIT,
                depth=depth,
                source_locator=None,
                rule_id=None,
                evidence_id=condition.evidence_id,
                detail=detail,
            )
        if normalized in stack:
            detail = f"Canonical logic cycle detected while tracing {signal}; recursion stopped safely."
            limitations.append(detail)
            return LiveRootCauseStep(
                signal=signal,
                required_by_parent=condition.required,
                observed_value=condition.observed_value,
                status=LiveRootCauseStepStatus.CYCLE,
                depth=depth,
                source_locator=None,
                rule_id=None,
                evidence_id=condition.evidence_id,
                detail=detail,
            )

        observed, observation_error = _resolve_observation(
            context,
            observations_by_id,
            signal,
        )
        if condition.evidence_id:
            evidence_ids.append(condition.evidence_id)
        if observed is not None and observed.evidence_id:
            evidence_ids.append(observed.evidence_id)
        observed_bool = (
            observed.value
            if observed is not None and observed.definitive_current and isinstance(observed.value, bool)
            else condition.observed_value
        )
        if observation_error:
            limitations.append(observation_error)
            return LiveRootCauseStep(
                signal=signal,
                required_by_parent=condition.required,
                observed_value=observed_bool,
                status=LiveRootCauseStepStatus.INDETERMINATE,
                depth=depth,
                source_locator=None,
                rule_id=None,
                evidence_id=observed.evidence_id if observed is not None else condition.evidence_id,
                detail=observation_error,
            )
        assert observed is not None
        assert isinstance(observed.value, bool)

        rule, rule_limitation = _safe_recursive_rule(context, signal)
        if rule is None:
            if rule_limitation:
                limitations.append(rule_limitation)
                return LiveRootCauseStep(
                    signal=signal,
                    required_by_parent=condition.required,
                    observed_value=observed.value,
                    status=LiveRootCauseStepStatus.INDETERMINATE,
                    depth=depth,
                    source_locator=None,
                    rule_id=None,
                    evidence_id=observed.evidence_id,
                    detail=rule_limitation,
                )
            return LiveRootCauseStep(
                signal=signal,
                required_by_parent=condition.required,
                observed_value=observed.value,
                status=LiveRootCauseStepStatus.ROOT_LOGIC_OBSERVATION,
                depth=depth,
                source_locator=None,
                rule_id=None,
                evidence_id=observed.evidence_id,
                detail=(
                    f"{signal}={observed.value} is trusted CURRENT evidence and no canonical derived Boolean rule writes this signal; "
                    "this is the deepest proven logic observation in the imported PLC model."
                ),
            )
        evidence_ids.append(rule.evidence_id)

        if depth >= max_depth:
            detail = f"Recursive trace depth limit {max_depth} reached at {signal}."
            limitations.append(detail)
            return LiveRootCauseStep(
                signal=signal,
                required_by_parent=condition.required,
                observed_value=observed.value,
                status=LiveRootCauseStepStatus.DEPTH_LIMIT,
                depth=depth,
                source_locator=rule.source_locator or None,
                rule_id=rule.id,
                evidence_id=observed.evidence_id,
                detail=detail,
            )

        diagnosis = diagnose_output(context, signal, observed_items)
        if diagnosis.status in {
            LiveDiagnosisStatus.LOGIC_CONFLICT,
            LiveDiagnosisStatus.INDETERMINATE,
            LiveDiagnosisStatus.NO_EVALUABLE_RULE,
        }:
            detail = (
                f"Recursive diagnosis for {signal} stopped with {diagnosis.status.value}: {diagnosis.summary}"
            )
            limitations.append(detail)
            evidence_ids.extend(diagnosis.evidence_ids)
            return LiveRootCauseStep(
                signal=signal,
                required_by_parent=condition.required,
                observed_value=observed.value,
                status=LiveRootCauseStepStatus.INDETERMINATE,
                depth=depth,
                source_locator=rule.source_locator or None,
                rule_id=rule.id,
                evidence_id=observed.evidence_id,
                detail=detail,
            )

        causal = _causal_conditions(diagnosis, observed.value)
        if not causal:
            detail = (
                f"No bounded causal child conditions were proven for {signal}={observed.value}; recursive expansion stopped."
            )
            limitations.append(detail)
            return LiveRootCauseStep(
                signal=signal,
                required_by_parent=condition.required,
                observed_value=observed.value,
                status=LiveRootCauseStepStatus.INDETERMINATE,
                depth=depth,
                source_locator=rule.source_locator or None,
                rule_id=rule.id,
                evidence_id=observed.evidence_id,
                detail=detail,
            )

        children = tuple(
            trace_condition(
                child,
                depth=depth + 1,
                stack=(*stack, normalized),
            )
            for child in causal
        )
        return LiveRootCauseStep(
            signal=signal,
            required_by_parent=condition.required,
            observed_value=observed.value,
            status=LiveRootCauseStepStatus.EXPANDED,
            depth=depth,
            source_locator=rule.source_locator or None,
            rule_id=rule.id,
            evidence_id=observed.evidence_id,
            detail=(
                f"Trusted CURRENT {signal}={observed.value} is consistent with canonical FULL stateless rule {rule.id}; tracing its causal condition(s)."
            ),
            children=children,
        )

    roots = tuple(
        trace_condition(seed, depth=1, stack=(normalize_engineering_identifier(direct.target_output),))
        for seed in seeds
    )
    return LiveRecursiveDiagnosis(
        target_output=direct.target_output,
        direct_status=direct.status,
        roots=roots,
        evidence_ids=_unique(evidence_ids),
        limitations=_unique(limitations),
        max_depth=max_depth,
        max_nodes=max_nodes,
        visited_nodes=min(budget["visited"], max_nodes),
    )


__all__ = [
    "DEFAULT_TRACE_MAX_DEPTH",
    "DEFAULT_TRACE_MAX_NODES",
    "LiveRecursiveDiagnosis",
    "LiveRootCauseStep",
    "LiveRootCauseStepStatus",
    "required_tag_ids_for_recursive_output",
    "trace_recursive_diagnosis",
]
