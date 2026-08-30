from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from devagent.providers import ModelProvider, ProviderError

from .engineering_context import LiveEngineeringContext


class LiveSemanticIntent(str, Enum):
    SYSTEM_HEALTH = "SYSTEM_HEALTH"
    SYSTEM_OVERVIEW = "SYSTEM_OVERVIEW"
    ROOT_CAUSE = "ROOT_CAUSE"
    TAG_STATUS = "TAG_STATUS"
    HISTORICAL_ROOT_CAUSE = "HISTORICAL_ROOT_CAUSE"
    FOLLOW_UP = "FOLLOW_UP"
    UNKNOWN = "UNKNOWN"


class LiveSemanticTimeScope(str, Enum):
    CURRENT = "CURRENT"
    HISTORICAL = "HISTORICAL"
    NONE = "NONE"


LIVE_SEMANTIC_INTENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "target", "time_scope", "confidence", "reason"],
    "properties": {
        "intent": {
            "type": "string",
            "enum": [item.value for item in LiveSemanticIntent],
        },
        "target": {
            "anyOf": [
                {"type": "string", "minLength": 1},
                {"type": "null"},
            ]
        },
        "time_scope": {
            "type": "string",
            "enum": [item.value for item in LiveSemanticTimeScope],
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reason": {"type": "string", "minLength": 1},
    },
}


@dataclass(frozen=True)
class LiveSemanticRoute:
    intent: LiveSemanticIntent
    target: str | None
    time_scope: LiveSemanticTimeScope
    confidence: float
    reason: str


_TARGET_REQUIRED = {
    LiveSemanticIntent.ROOT_CAUSE,
    LiveSemanticIntent.TAG_STATUS,
    LiveSemanticIntent.HISTORICAL_ROOT_CAUSE,
}


def _canonical_targets(context: LiveEngineeringContext) -> tuple[str, ...]:
    result: list[str] = []

    def add(value: str | None) -> None:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)

    for output in context.output_names():
        add(output)
    for tag in context.tags:
        add(tag.scoped_name)
        add(tag.name)
    return tuple(result)


def _engineering_target_hints(context: LiveEngineeringContext) -> list[dict[str, str | None]]:
    hints: list[dict[str, str | None]] = []
    seen: set[str] = set()
    for tag in context.tags:
        target = str(tag.scoped_name).strip()
        if not target or target in seen:
            continue
        seen.add(target)
        hints.append(
            {
                "target": target,
                "description": (
                    str(tag.description).strip()
                    if getattr(tag, "description", None)
                    else None
                ),
                "data_type": str(getattr(tag, "data_type", "") or "").strip() or None,
                "scope": str(getattr(tag, "scope", "") or "").strip() or None,
            }
        )
        if len(hints) >= 160:
            break
    return hints


def _canonical_target(
    context: LiveEngineeringContext,
    value: object,
) -> str | None:
    if value is None:
        return None
    requested = str(value).strip()
    if not requested:
        return None
    targets = _canonical_targets(context)
    exact = {item: item for item in targets}
    if requested in exact:
        return exact[requested]
    folded: dict[str, str] = {}
    ambiguous: set[str] = set()
    for item in targets:
        key = item.casefold()
        if key in folded and folded[key] != item:
            ambiguous.add(key)
        else:
            folded[key] = item
    key = requested.casefold()
    if key in ambiguous:
        return None
    return folded.get(key)


def _router_payload(
    question: str,
    context: LiveEngineeringContext,
    *,
    previous_target: str | None,
) -> dict[str, Any]:
    outputs = list(context.output_names()[:64])
    tags = list(_canonical_targets(context)[:160])
    return {
        "instruction": (
            "You are only the natural-language intent interpreter for a read-only industrial PLC commissioning assistant. "
            "Do not answer the engineering question and do not decide whether the machine is healthy. "
            "Translate the engineer's wording into exactly one supported intent and, when needed, one exact target from known_targets. "
            "Engineers may use slang, shorthand, typos, incomplete sentences, or languages other than English. Interpret meaning rather than matching fixed phrases. "
            "Use engineering_target_hints only to understand what a known tag/output represents; those hints are static engineering metadata, not runtime truth. "
            "SYSTEM_HEALTH means a whole-system current-health/fault/problem question such as whether things are good, normal, healthy, wrong, alarming, or need attention. "
            "SYSTEM_OVERVIEW means asking what the system is, what it does, or what is available. "
            "ROOT_CAUSE means asking why a known output/signal is in its current state or what blocks it. "
            "TAG_STATUS means asking for the current state/value of one known signal. "
            "HISTORICAL_ROOT_CAUSE means asking why a known signal changed or failed in the past. "
            "FOLLOW_UP means a contextual follow-up about last_target. "
            "UNKNOWN means the request cannot be mapped safely to these intents. "
            "Never invent a PLC tag, output, controller state, fault, cause, runtime value, or evidence. "
            "For target, return an exact string from known_targets or null. "
            "If the user names no target but clearly refers to last_target, use FOLLOW_UP. "
            "When uncertain, use UNKNOWN instead of guessing."
        ),
        "question": question,
        "last_target": previous_target,
        "engineering_context": {
            "vendor": str(getattr(context, "vendor", "") or "") or None,
            "controller_name": str(getattr(context, "controller_name", "") or "") or None,
        },
        "known_outputs": outputs,
        "known_targets": tags,
        "engineering_target_hints": _engineering_target_hints(context),
        "allowed_intents": [item.value for item in LiveSemanticIntent],
    }


def resolve_semantic_intent(
    question: str,
    context: LiveEngineeringContext,
    provider: ModelProvider | None,
    *,
    previous_target: str | None = None,
    minimum_confidence: float = 0.55,
) -> LiveSemanticRoute | None:
    """Interpret free-form engineer language without granting the model engineering authority.

    Returning ``None`` is a safe fail-closed signal: callers should use the existing
    deterministic resolver/fallback rather than guessing.
    """
    text = str(question or "").strip()
    if not text or provider is None:
        return None
    if not 0.0 <= minimum_confidence <= 1.0:
        raise ValueError("minimum_confidence must be between 0.0 and 1.0")

    try:
        response = provider.request(
            role="live_semantic_intent_router",
            payload=_router_payload(text, context, previous_target=previous_target),
            schema=LIVE_SEMANTIC_INTENT_SCHEMA,
        )
    except ProviderError:
        return None

    try:
        intent = LiveSemanticIntent(str(response["intent"]))
        time_scope = LiveSemanticTimeScope(str(response["time_scope"]))
        confidence = float(response["confidence"])
    except (KeyError, TypeError, ValueError):
        return None
    if confidence < minimum_confidence:
        return None

    target = _canonical_target(context, response.get("target"))
    if response.get("target") is not None and target is None:
        return None

    if intent is LiveSemanticIntent.FOLLOW_UP:
        if target is None:
            target = _canonical_target(context, previous_target)
        if target is None:
            return None
    elif intent in _TARGET_REQUIRED and target is None:
        return None
    elif intent in {
        LiveSemanticIntent.SYSTEM_HEALTH,
        LiveSemanticIntent.SYSTEM_OVERVIEW,
        LiveSemanticIntent.UNKNOWN,
    }:
        target = None

    if intent is LiveSemanticIntent.HISTORICAL_ROOT_CAUSE:
        if time_scope is not LiveSemanticTimeScope.HISTORICAL:
            return None
    elif time_scope is LiveSemanticTimeScope.HISTORICAL and intent is not LiveSemanticIntent.FOLLOW_UP:
        return None

    return LiveSemanticRoute(
        intent=intent,
        target=target,
        time_scope=time_scope,
        confidence=confidence,
        reason=str(response["reason"]).strip(),
    )


__all__ = [
    "LIVE_SEMANTIC_INTENT_SCHEMA",
    "LiveSemanticIntent",
    "LiveSemanticRoute",
    "LiveSemanticTimeScope",
    "resolve_semantic_intent",
]
