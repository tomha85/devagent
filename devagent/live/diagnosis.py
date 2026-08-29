from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

from .engineering_context import (
    LiveEngineeringContext,
    LiveEngineeringTag,
    LiveLogicRule,
    normalize_engineering_identifier,
)


class LiveConditionState(str, Enum):
    SATISFIED = "SATISFIED"
    BLOCKING = "BLOCKING"
    UNKNOWN = "UNKNOWN"


class LivePathState(str, Enum):
    SATISFIED = "SATISFIED"
    BLOCKED = "BLOCKED"
    INDETERMINATE = "INDETERMINATE"


class LiveDiagnosisStatus(str, Enum):
    BLOCKER_IDENTIFIED = "BLOCKER_IDENTIFIED"
    CONDITIONS_SATISFIED = "CONDITIONS_SATISFIED"
    LOGIC_CONFLICT = "LOGIC_CONFLICT"
    INDETERMINATE = "INDETERMINATE"
    NO_EVALUABLE_RULE = "NO_EVALUABLE_RULE"
    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    TARGET_AMBIGUOUS = "TARGET_AMBIGUOUS"


@dataclass(frozen=True)
class LiveObservedTag:
    tag_id: str
    tag_name: str
    node_id: str | None
    value: Any
    evidence_id: str | None
    definitive_current: bool
    mapping_status: str
    limitation: str | None = None


@dataclass(frozen=True)
class LiveConditionEvaluation:
    tag_reference: str
    tag_id: str | None
    tag_name: str | None
    required: bool
    observed_value: bool | None
    state: LiveConditionState
    evidence_id: str | None
    detail: str


@dataclass(frozen=True)
class LivePathEvaluation:
    index: int
    state: LivePathState
    conditions: tuple[LiveConditionEvaluation, ...]


@dataclass(frozen=True)
class LiveQuestionTarget:
    status: LiveDiagnosisStatus | None
    output_tag: str | None
    candidates: tuple[str, ...]
    detail: str


@dataclass(frozen=True)
class LiveCommissioningDiagnosis:
    target_output: str
    status: LiveDiagnosisStatus
    expected_output: bool | None
    observed_output: bool | None
    rule_ids: tuple[str, ...]
    source_locators: tuple[str, ...]
    paths: tuple[LivePathEvaluation, ...]
    blockers: tuple[LiveConditionEvaluation, ...]
    evidence_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    summary: str
    next_checks: tuple[str, ...]


_WORD_RE = re.compile(r"[A-Za-z]+|\d+")
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_STEMS = {
    "running": "run",
    "runs": "run",
    "ran": "run",
    "started": "start",
    "starting": "start",
    "starts": "start",
    "stopped": "stop",
    "stopping": "stop",
    "stops": "stop",
    "faulted": "fault",
    "faulting": "fault",
    "readying": "ready",
    "enabled": "enable",
    "enabling": "enable",
    "disabled": "disable",
    "disabling": "disable",
}
_GENERIC_QUESTION_WORDS = {
    "why",
    "is",
    "are",
    "the",
    "this",
    "that",
    "not",
    "what",
    "which",
    "how",
    "a",
    "an",
    "of",
    "to",
    "for",
    "please",
    "tell",
    "me",
}


def _tokens(value: str) -> tuple[str, ...]:
    expanded = _CAMEL_RE.sub(" ", str(value))
    words = [item.casefold() for item in _WORD_RE.findall(expanded.replace("_", " "))]
    return tuple(_STEMS.get(item, item) for item in words)


def _target_score(question: str, output_name: str) -> float:
    question_tokens = [
        token for token in _tokens(question)
        if token not in _GENERIC_QUESTION_WORDS
    ]
    output_tokens = list(_tokens(output_name))
    if not question_tokens or not output_tokens:
        return 0.0
    qset = set(question_tokens)
    oset = set(output_tokens)
    overlap = len(qset & oset)
    if overlap == 0:
        return 0.0
    return overlap / len(oset)


def resolve_question_target(
    context: LiveEngineeringContext,
    question: str,
) -> LiveQuestionTarget:
    text = str(question or "").strip()
    if not text:
        return LiveQuestionTarget(
            status=LiveDiagnosisStatus.TARGET_NOT_FOUND,
            output_tag=None,
            candidates=(),
            detail="Question is empty.",
        )

    normalized_question = normalize_engineering_identifier(text)
    direct: list[str] = []
    for output in context.output_names():
        normalized_output = normalize_engineering_identifier(output)
        if normalized_output and normalized_output in normalized_question:
            direct.append(output)
    if len(direct) == 1:
        return LiveQuestionTarget(None, direct[0], tuple(direct), "Unique output identity found in question.")
    if len(direct) > 1:
        longest = max(len(normalize_engineering_identifier(item)) for item in direct)
        narrowed = tuple(
            item for item in direct
            if len(normalize_engineering_identifier(item)) == longest
        )
        if len(narrowed) == 1:
            return LiveQuestionTarget(None, narrowed[0], narrowed, "Longest unique output identity found in question.")
        return LiveQuestionTarget(
            status=LiveDiagnosisStatus.TARGET_AMBIGUOUS,
            output_tag=None,
            candidates=narrowed,
            detail="Multiple output signals match the question.",
        )

    scored = sorted(
        (
            (_target_score(text, output), output)
            for output in context.output_names()
        ),
        key=lambda item: (-item[0], item[1].casefold()),
    )
    if not scored or scored[0][0] < 0.67:
        return LiveQuestionTarget(
            status=LiveDiagnosisStatus.TARGET_NOT_FOUND,
            output_tag=None,
            candidates=(),
            detail="No output signal could be identified confidently from the question.",
        )
    best = scored[0][0]
    candidates = tuple(output for score, output in scored if score == best and score >= 0.67)
    if len(candidates) != 1:
        return LiveQuestionTarget(
            status=LiveDiagnosisStatus.TARGET_AMBIGUOUS,
            output_tag=None,
            candidates=candidates,
            detail="Question matches multiple output signals with the same confidence.",
        )
    return LiveQuestionTarget(
        status=None,
        output_tag=candidates[0],
        candidates=candidates,
        detail="Unique output signal resolved from engineering names.",
    )


def _bool_value(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _observation_by_tag_id(
    observations: Iterable[LiveObservedTag],
) -> dict[str, LiveObservedTag]:
    return {item.tag_id: item for item in observations}


def _resolve_term_tag(
    context: LiveEngineeringContext,
    reference: str,
) -> tuple[LiveEngineeringTag | None, str | None]:
    matches = context.tags_for_reference(reference)
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, f"Engineering reference {reference!r} does not resolve to a canonical tag."
    return None, f"Engineering reference {reference!r} is ambiguous across {len(matches)} canonical tags."


def _evaluate_rule(
    context: LiveEngineeringContext,
    rule: LiveLogicRule,
    observations_by_id: Mapping[str, LiveObservedTag],
) -> tuple[tuple[LivePathEvaluation, ...], bool | None, list[str]]:
    limitations: list[str] = []
    if rule.semantic_state and rule.semantic_state.upper() != "FULL":
        limitations.append(
            f"Rule {rule.id} semantic_state={rule.semantic_state}; this rule is not complete enough for definitive blocker proof."
        )

    paths: list[LivePathEvaluation] = []
    for index, path in enumerate(rule.paths, start=1):
        conditions: list[LiveConditionEvaluation] = []
        for term in path.terms:
            tag, resolution_error = _resolve_term_tag(context, term.tag_reference)
            if tag is None:
                conditions.append(
                    LiveConditionEvaluation(
                        tag_reference=term.tag_reference,
                        tag_id=None,
                        tag_name=None,
                        required=term.required,
                        observed_value=None,
                        state=LiveConditionState.UNKNOWN,
                        evidence_id=None,
                        detail=resolution_error or "Engineering tag resolution failed.",
                    )
                )
                continue
            observed = observations_by_id.get(tag.id)
            if observed is None or not observed.definitive_current:
                detail = (
                    observed.limitation
                    if observed is not None and observed.limitation
                    else f"No definitive CURRENT runtime value is available for {tag.name}."
                )
                conditions.append(
                    LiveConditionEvaluation(
                        tag_reference=term.tag_reference,
                        tag_id=tag.id,
                        tag_name=tag.name,
                        required=term.required,
                        observed_value=None,
                        state=LiveConditionState.UNKNOWN,
                        evidence_id=observed.evidence_id if observed else None,
                        detail=detail,
                    )
                )
                continue
            boolean = _bool_value(observed.value)
            if boolean is None:
                conditions.append(
                    LiveConditionEvaluation(
                        tag_reference=term.tag_reference,
                        tag_id=tag.id,
                        tag_name=tag.name,
                        required=term.required,
                        observed_value=None,
                        state=LiveConditionState.UNKNOWN,
                        evidence_id=observed.evidence_id,
                        detail=f"Runtime value for {tag.name} is not Boolean and cannot be used to evaluate Boolean logic.",
                    )
                )
                continue
            state = (
                LiveConditionState.SATISFIED
                if boolean is term.required
                else LiveConditionState.BLOCKING
            )
            conditions.append(
                LiveConditionEvaluation(
                    tag_reference=term.tag_reference,
                    tag_id=tag.id,
                    tag_name=tag.name,
                    required=term.required,
                    observed_value=boolean,
                    state=state,
                    evidence_id=observed.evidence_id,
                    detail=(
                        f"{tag.name}={boolean} satisfies required={term.required}."
                        if state is LiveConditionState.SATISFIED
                        else f"{tag.name}={boolean} blocks required={term.required}."
                    ),
                )
            )

        if any(item.state is LiveConditionState.BLOCKING for item in conditions):
            path_state = LivePathState.BLOCKED
        elif conditions and all(item.state is LiveConditionState.SATISFIED for item in conditions):
            path_state = LivePathState.SATISFIED
        elif not conditions:
            path_state = LivePathState.INDETERMINATE
            limitations.append(f"Rule {rule.id} path {index} has no modeled Boolean terms.")
        else:
            path_state = LivePathState.INDETERMINATE
        paths.append(
            LivePathEvaluation(
                index=index,
                state=path_state,
                conditions=tuple(conditions),
            )
        )

    if not paths:
        limitations.append(f"Rule {rule.id} exposes no modeled Boolean paths.")
        expected = None
    elif any(path.state is LivePathState.SATISFIED for path in paths):
        expected = True
    elif all(path.state is LivePathState.BLOCKED for path in paths):
        expected = False
    else:
        expected = None
    return tuple(paths), expected, limitations


def _output_observation(
    context: LiveEngineeringContext,
    output_reference: str,
    observations_by_id: Mapping[str, LiveObservedTag],
) -> tuple[bool | None, str | None]:
    tag, error = _resolve_term_tag(context, output_reference)
    if tag is None:
        return None, error
    observed = observations_by_id.get(tag.id)
    if observed is None or not observed.definitive_current:
        return None, (
            observed.limitation
            if observed is not None and observed.limitation
            else f"No definitive CURRENT runtime value is available for output {tag.name}."
        )
    value = _bool_value(observed.value)
    if value is None:
        return None, f"Runtime output {tag.name} is not Boolean."
    return value, None


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in values if item))


def diagnose_output(
    context: LiveEngineeringContext,
    output_reference: str,
    observations: Iterable[LiveObservedTag],
) -> LiveCommissioningDiagnosis:
    rules = context.rules_for_output(output_reference)
    observations_by_id = _observation_by_tag_id(observations)

    if not rules:
        source_statements = tuple(
            statement
            for statement in context.statements
            if any(
                normalize_engineering_identifier(write)
                == normalize_engineering_identifier(output_reference)
                for write in statement.writes
            )
        )
        locators = _unique(
            statement.source_locator or statement.locator
            for statement in source_statements
        )
        limitations = list(context.limitations)
        if source_statements:
            limitations.append(
                "Source statements write this output, but no deterministically evaluable Boolean output rule is available."
            )
        else:
            limitations.append(
                "No canonical output rule or writing statement was found for this target."
            )
        return LiveCommissioningDiagnosis(
            target_output=output_reference,
            status=LiveDiagnosisStatus.NO_EVALUABLE_RULE,
            expected_output=None,
            observed_output=None,
            rule_ids=(),
            source_locators=locators,
            paths=(),
            blockers=(),
            evidence_ids=(),
            limitations=_unique(limitations),
            summary=(
                f"DevAgent Live cannot deterministically evaluate why {output_reference} is active or inactive from the available engineering model."
            ),
            next_checks=(
                "Inspect the listed PLC source location(s) and expose the relevant permissive/interlock tags through OPC UA.",
            ),
        )

    if len(rules) > 1:
        return LiveCommissioningDiagnosis(
            target_output=output_reference,
            status=LiveDiagnosisStatus.INDETERMINATE,
            expected_output=None,
            observed_output=_output_observation(context, output_reference, observations_by_id)[0],
            rule_ids=tuple(rule.id for rule in rules),
            source_locators=_unique(rule.source_locator for rule in rules),
            paths=(),
            blockers=(),
            evidence_ids=_unique(rule.evidence_id for rule in rules),
            limitations=_unique(
                (
                    *context.limitations,
                    f"{len(rules)} canonical output writers/rules target {output_reference}; V1 refuses to choose one writer as authoritative.",
                )
            ),
            summary=(
                f"{output_reference} has multiple modeled writers/rules, so a single blocking condition cannot be proven safely."
            ),
            next_checks=(
                "Review all listed writers and determine which write path is active for the current controller state.",
            ),
        )

    rule = rules[0]
    paths, expected, rule_limitations = _evaluate_rule(
        context,
        rule,
        observations_by_id,
    )
    observed_output, output_limitation = _output_observation(
        context,
        output_reference,
        observations_by_id,
    )

    blockers = tuple(
        condition
        for path in paths
        for condition in path.conditions
        if condition.state is LiveConditionState.BLOCKING
    )
    evidence_ids = _unique(
        (
            rule.evidence_id,
            *(
                condition.evidence_id or ""
                for path in paths
                for condition in path.conditions
            ),
        )
    )
    limitations = list(context.limitations)
    limitations.extend(rule_limitations)
    if output_limitation:
        limitations.append(output_limitation)

    if expected is False:
        if observed_output is True:
            status = LiveDiagnosisStatus.LOGIC_CONFLICT
            summary = (
                f"Modeled conditions for {output_reference} are blocked, but the CURRENT output value is TRUE. "
                "This may indicate another writer, retained/latch behavior, scan timing, or incomplete modeled semantics."
            )
            next_checks = (
                "Check for additional writers, latch/set-reset behavior, and the source locations controlling this output.",
            )
        else:
            status = LiveDiagnosisStatus.BLOCKER_IDENTIFIED
            blocker_names = ", ".join(
                condition.tag_name or condition.tag_reference
                for condition in blockers
            ) or "one or more modeled conditions"
            summary = (
                f"{output_reference} is blocked by the current modeled PLC logic. Blocking condition(s): {blocker_names}."
            )
            next_checks = tuple(
                f"Trace why {condition.tag_name or condition.tag_reference} is {condition.observed_value!r}; "
                f"the modeled logic requires {condition.required!r}."
                for condition in blockers
            ) or (
                "Inspect the modeled PLC permissive/interlock path.",
            )
    elif expected is True:
        if observed_output is False:
            status = LiveDiagnosisStatus.LOGIC_CONFLICT
            summary = (
                f"All modeled conditions for {output_reference} are currently satisfied, but the CURRENT output value is FALSE."
            )
            next_checks = (
                "Check for additional writers, unmodeled sequence/latch behavior, source protection, or runtime scan timing.",
            )
        else:
            status = LiveDiagnosisStatus.CONDITIONS_SATISFIED
            summary = (
                f"All modeled Boolean conditions for {output_reference} are currently satisfied."
                if observed_output is not None
                else f"All modeled Boolean conditions for {output_reference} are satisfied, but the output itself is not definitively observable."
            )
            next_checks = (
                "If the physical device is still not operating, check field-side drive/starter/actuator feedback and I/O diagnostics.",
            )
    else:
        status = LiveDiagnosisStatus.INDETERMINATE
        unknowns = [
            condition
            for path in paths
            for condition in path.conditions
            if condition.state is LiveConditionState.UNKNOWN
        ]
        summary = (
            f"DevAgent Live cannot determine the current logical result for {output_reference} because required evidence is incomplete."
        )
        next_checks = tuple(
            f"Expose or restore a trusted CURRENT value for {condition.tag_name or condition.tag_reference}."
            for condition in unknowns
        ) or (
            "Inspect missing runtime evidence for the modeled logic path.",
        )

    return LiveCommissioningDiagnosis(
        target_output=output_reference,
        status=status,
        expected_output=expected,
        observed_output=observed_output,
        rule_ids=(rule.id,),
        source_locators=_unique((rule.source_locator,)),
        paths=paths,
        blockers=blockers,
        evidence_ids=evidence_ids,
        limitations=_unique(limitations),
        summary=summary,
        next_checks=_unique(next_checks),
    )


def observations_from_reconciled(
    reconciled: Any,
) -> tuple[LiveObservedTag, ...]:
    reconciliation = reconciled.reconciliation
    live_pack = reconciled.live_pack
    current_by_node = {
        record.node_id: record
        for record in live_pack.current_records()
    }
    excluded_by_node = {
        record.node_id: record
        for record in live_pack.records
        if not record.definitive_current
    }

    observations: list[LiveObservedTag] = []
    for mapping in reconciliation.mappings:
        selected_node_id = mapping.selected_node_id
        current = current_by_node.get(selected_node_id) if selected_node_id else None
        excluded = excluded_by_node.get(selected_node_id) if selected_node_id else None
        if current is not None:
            observations.append(
                LiveObservedTag(
                    tag_id=mapping.tag_id,
                    tag_name=mapping.tag_name,
                    node_id=current.node_id,
                    value=current.value,
                    evidence_id=current.evidence_id,
                    definitive_current=True,
                    mapping_status=mapping.status.value,
                )
            )
            continue
        limitation = mapping.reason
        evidence_id = None
        value = None
        if excluded is not None:
            limitation = (
                f"Runtime value excluded: disposition={excluded.disposition.value}, "
                f"quality={excluded.quality}, trust={excluded.trust}."
            )
            evidence_id = excluded.evidence_id
        observations.append(
            LiveObservedTag(
                tag_id=mapping.tag_id,
                tag_name=mapping.tag_name,
                node_id=selected_node_id,
                value=value,
                evidence_id=evidence_id,
                definitive_current=False,
                mapping_status=mapping.status.value,
                limitation=limitation,
            )
        )
    return tuple(observations)


def required_tag_ids_for_output(
    context: LiveEngineeringContext,
    output_reference: str,
) -> tuple[str, ...]:
    ordered: list[str] = []

    def add_reference(reference: str) -> None:
        tag = context.unique_tag_for_reference(reference)
        if tag is not None and tag.id not in ordered:
            ordered.append(tag.id)

    add_reference(output_reference)
    for rule in context.rules_for_output(output_reference):
        for path in rule.paths:
            for term in path.terms:
                add_reference(term.tag_reference)
    return tuple(ordered)


__all__ = [
    "LiveCommissioningDiagnosis",
    "LiveConditionEvaluation",
    "LiveConditionState",
    "LiveDiagnosisStatus",
    "LiveObservedTag",
    "LivePathEvaluation",
    "LivePathState",
    "LiveQuestionTarget",
    "diagnose_output",
    "observations_from_reconciled",
    "required_tag_ids_for_output",
    "resolve_question_target",
]
