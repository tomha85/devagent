from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from typing import Any

from devagent.providers import ModelProvider, ProviderError

from .assistant import LiveAssistantReply, LiveAssistantReplyKind
from .control_guard import is_plc_control_request
from .diagnosis import LiveObservedTag, observations_from_reconciled
from .diagnosis_guard import diagnose_output
from .errors import LiveConfigurationError
from .history import requested_history_seconds
from .qa import answer_commissioning_question
from .reconciled_evidence import build_reconciled_live_agent_evidence
from .recursive_assistant import RecursiveLiveCommissioningAssistant
from .recursive_diagnosis import (
    required_tag_ids_for_recursive_output,
    trace_recursive_diagnosis,
)
from .semantic_intent import (
    LiveSemanticIntent,
    LiveSemanticRoute,
    LiveSemanticTimeScope,
    resolve_semantic_intent,
)


_TIME_TOKEN_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b",
    re.IGNORECASE,
)
_STOP_EVENT_RE = re.compile(
    r"\b(?:stop(?:ped)?|turn(?:ed)?\s+off|went\s+false|became\s+false|"
    r"dropped|de[- ]?energized)\b",
    re.IGNORECASE,
)
_START_EVENT_RE = re.compile(
    r"\b(?:start(?:ed)?|turn(?:ed)?\s+on|went\s+true|became\s+true|"
    r"energized|resumed|restart(?:ed)?)\b",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY_RE = re.compile(
    r"[,;]|\b(?:after|before|while|whereas|but|then)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _HistoricalRouteMetadata:
    lookback_seconds: float
    target_age_seconds: float | None
    direction: str | None


def _bounded_error_text(value: object, *, limit: int = 360) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _target_event_clause(original: str, target: str) -> str | None:
    """Return the bounded clause that explicitly contains the validated target."""
    text = str(original or "")
    target_text = str(target or "").strip()
    if not text or not target_text:
        return None

    match = re.search(re.escape(target_text), text, flags=re.IGNORECASE)
    if match is None:
        return None

    start = 0
    for boundary in _CLAUSE_BOUNDARY_RE.finditer(text, 0, match.start()):
        start = boundary.end()

    end = len(text)
    boundary = _CLAUSE_BOUNDARY_RE.search(text, match.end())
    if boundary is not None:
        end = boundary.start()

    clause = text[start:end].strip()
    return clause or None


def _historical_metadata_for_target(
    original: str,
    target: str,
) -> _HistoricalRouteMetadata | None:
    """Parse only safely attributable historical metadata for a validated target."""
    text = str(original or "").strip()
    clause = _target_event_clause(text, target)
    candidate = clause if clause is not None else text

    time_matches = tuple(_TIME_TOKEN_RE.finditer(candidate))
    stop_matches = tuple(_STOP_EVENT_RE.finditer(candidate))
    start_matches = tuple(_START_EVENT_RE.finditer(candidate))
    direction_count = len(stop_matches) + len(start_matches)

    if len(time_matches) > 1 or direction_count > 1:
        return None

    if clause is None:
        all_time_matches = tuple(_TIME_TOKEN_RE.finditer(text))
        all_direction_count = len(tuple(_STOP_EVENT_RE.finditer(text))) + len(
            tuple(_START_EVENT_RE.finditer(text))
        )
        if len(all_time_matches) > 1 or all_direction_count > 1:
            return None

    window = requested_history_seconds(candidate)
    return _HistoricalRouteMetadata(
        lookback_seconds=float(window),
        target_age_seconds=getattr(window, "age_seconds", None),
        direction=getattr(window, "direction", None),
    )


def _historical_bridge_question(original: str, target: str) -> str:
    """Create target-only historical wording for display/tests."""
    metadata = _historical_metadata_for_target(original, target)
    if metadata is None:
        return f"Why did {target} change earlier?"

    if metadata.direction == "STOP":
        base = f"Why did {target} stop"
    elif metadata.direction == "START":
        base = f"Why did {target} start"
    else:
        base = f"Why did {target} change"

    if metadata.target_age_seconds is not None:
        return f"{base} {metadata.target_age_seconds:g} seconds ago?"
    return f"{base} earlier?"


def _bridge_question(original: str, route: LiveSemanticRoute) -> str:
    """Create canonical wording without giving text resolution authority."""
    if route.intent is LiveSemanticIntent.SYSTEM_HEALTH:
        return "Does the system have any faults?"
    if route.intent is LiveSemanticIntent.SYSTEM_OVERVIEW:
        return "What is this system?"
    if route.time_scope is LiveSemanticTimeScope.HISTORICAL and route.target:
        return _historical_bridge_question(original, route.target)
    if route.intent is LiveSemanticIntent.TAG_STATUS and route.target:
        return f"What is the current value of {route.target}?"
    if route.target:
        return f"Why is {route.target} in its current state?"
    return original


def _restore_original_question(original: str, reply: LiveAssistantReply) -> LiveAssistantReply:
    return LiveAssistantReply(
        question=original,
        kind=reply.kind,
        text=reply.text,
        target_output=reply.target_output,
        diagnosis=reply.diagnosis,
        answer=reply.answer,
    )


@dataclass
class _DiagnosticProvider:
    """Provider proxy that preserves the bounded reason for fail-closed fallback."""

    inner: ModelProvider
    last_error: str | None = None

    def request(
        self,
        *,
        role: str,
        payload: dict[str, Any],
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return self.inner.request(role=role, payload=payload, schema=schema)
        except ProviderError as exc:
            self.last_error = _bounded_error_text(exc)
            raise


class SemanticLiveCommissioningAssistant(RecursiveLiveCommissioningAssistant):
    """Recursive Live assistant with provider-neutral natural-language intent routing."""

    _last_semantic_router_status: str = "NOT_RUN"

    def semantic_router_status_text(self) -> str:
        lines = [f"AI semantic router: {self._last_semantic_router_status}"]
        provider = self.provider
        if provider is None:
            lines.append("AI provider: DISABLED")
            return "\n".join(lines)
        config = getattr(provider, "config", None)
        if config is not None:
            provider_name = str(getattr(config, "provider", "") or "UNKNOWN")
            model = str(getattr(config, "model", "") or "UNKNOWN")
            key_env = str(getattr(config, "api_key_env", "") or "")
            lines.append(f"AI provider: {provider_name}")
            lines.append(f"AI model: {model}")
            if key_env:
                lines.append(f"AI key env: {key_env} ({'SET' if os.getenv(key_env) else 'MISSING'})")
        return "\n".join(lines)

    def _fallback_with_router_status(
        self,
        original: str,
        reply: LiveAssistantReply,
    ) -> LiveAssistantReply:
        diagnostic = self.semantic_router_status_text()
        return LiveAssistantReply(
            question=original,
            kind=reply.kind,
            text=(
                f"{reply.text}\n\n"
                "AI semantic routing did not produce an accepted route; deterministic fallback was used.\n"
                f"{diagnostic}"
            ),
            target_output=reply.target_output,
            diagnosis=reply.diagnosis,
            answer=reply.answer,
        )

    def _exact_target_limitation(
        self,
        original: str,
        target: str,
        detail: str,
    ) -> LiveAssistantReply:
        return LiveAssistantReply(
            question=original,
            kind=LiveAssistantReplyKind.LIMITATION,
            text=detail,
            target_output=target,
        )

    async def _dispatch_current_target(
        self,
        original: str,
        route: LiveSemanticRoute,
    ) -> LiveAssistantReply:
        """Diagnose the exact validated current target without text re-resolution."""
        assert route.target is not None
        if not self.connected or self.reconciliation is None:
            await self.start()
        assert self.reconciliation is not None

        tag = self.context.unique_tag_for_reference(route.target)
        if tag is None:
            return self._exact_target_limitation(
                original,
                route.target,
                "The validated semantic target no longer resolves to exactly one canonical engineering tag. DevAgent will not guess another signal.",
            )

        target = tag.scoped_name
        canonical_question = _bridge_question(original, route)

        if not self.context.rules_for_output(target):
            reply = await self._direct_signal_reply(canonical_question, tag)
            return _restore_original_question(original, reply)

        self._last_target = target
        required_tag_ids = required_tag_ids_for_recursive_output(
            self.context,
            target,
            max_depth=self.trace_max_depth,
            max_nodes=self.trace_max_nodes,
        )
        observations: tuple[LiveObservedTag, ...]
        if required_tag_ids:
            try:
                reconciled = await build_reconciled_live_agent_evidence(
                    self.manager,
                    self.reconciliation,
                    required_tag_ids=required_tag_ids,
                    require_all=False,
                )
                observations = observations_from_reconciled(reconciled)
            except LiveConfigurationError:
                observations = self._mapping_only_observations()
        else:
            observations = self._mapping_only_observations()

        diagnosis = diagnose_output(self.context, target, observations)
        answer = answer_commissioning_question(
            canonical_question,
            diagnosis,
            provider=self.provider,
        )
        recursive = trace_recursive_diagnosis(
            self.context,
            diagnosis,
            observations,
            max_depth=self.trace_max_depth,
            max_nodes=self.trace_max_nodes,
        )

        combined = answer.render_text()
        if diagnosis.source_locators:
            combined += "\n\nTarget PLC source:"
            combined += "".join(f"\n- {locator}" for locator in diagnosis.source_locators)
        if recursive.roots or recursive.limitations:
            combined += "\n\n" + recursive.render_text()
        if len(recursive.roots) == 1:
            immediate = recursive.roots[0].signal
            if self.context.rules_for_output(immediate):
                self._last_target = immediate

        return LiveAssistantReply(
            question=original,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text=combined,
            target_output=target,
            diagnosis=diagnosis,
            answer=None,
        )

    async def _dispatch_historical_route(
        self,
        original: str,
        route: LiveSemanticRoute,
    ) -> LiveAssistantReply:
        """Diagnose the exact validated historical output and bind metadata to it."""
        assert route.target is not None
        if not self.connected or self.reconciliation is None:
            await self.start()

        tag = self.context.unique_tag_for_reference(route.target)
        if tag is None:
            return self._exact_target_limitation(
                original,
                route.target,
                "The validated historical target no longer resolves to exactly one canonical engineering tag. DevAgent will not guess another signal.",
            )

        target = tag.scoped_name
        if not self.context.rules_for_output(target):
            return self._exact_target_limitation(
                original,
                target,
                (
                    f"Historical root-cause diagnosis for exact non-output signal {target} is not proven by the current bounded engine. "
                    "DevAgent will not silently substitute another output whose name overlaps this signal."
                ),
            )

        metadata = _historical_metadata_for_target(original, target)
        if metadata is None:
            return self._exact_target_limitation(
                original,
                target,
                (
                    f"Historical target {target} was validated, but the question contains multiple event/time markers that cannot be safely bound to that target. "
                    "Ask about one event and one time reference at a time; DevAgent will not guess which metadata belongs to the target."
                ),
            )

        self._last_target = target
        if self.history_collector is None:
            return self._exact_target_limitation(
                original,
                target,
                (
                    "Historical diagnosis is not available for this session. Start `devagent live assist` "
                    "with a positive history retention window and allow the session to observe the system before asking past-event questions."
                ),
            )

        dependency_ids = required_tag_ids_for_recursive_output(
            self.context,
            target,
            max_depth=self.trace_max_depth,
            max_nodes=self.trace_max_nodes,
        )
        window = min(metadata.lookback_seconds, self.history_seconds)
        diagnosis = self.history_collector.store.diagnose_recent_transition(
            self.context,
            target,
            dependency_tag_ids=dependency_ids,
            lookback_seconds=window,
            target_age_seconds=metadata.target_age_seconds,
            direction=metadata.direction,
        )
        return LiveAssistantReply(
            question=original,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text=diagnosis.render_text(),
            target_output=target,
        )

    async def answer(self, question: str) -> LiveAssistantReply:
        text = str(question or "").strip()

        if not text or is_plc_control_request(text) or self.provider is None:
            return await super().answer(question)

        diagnostic_provider = _DiagnosticProvider(self.provider)
        route = await asyncio.to_thread(
            resolve_semantic_intent,
            text,
            self.context,
            diagnostic_provider,
            previous_target=self._last_target,
        )
        if route is None:
            if diagnostic_provider.last_error:
                self._last_semantic_router_status = (
                    "PROVIDER_ERROR: " + diagnostic_provider.last_error
                )
            else:
                self._last_semantic_router_status = (
                    "REJECTED: provider output did not pass bounded intent/target validation"
                )
            fallback = await super().answer(question)
            return self._fallback_with_router_status(text, fallback)

        if route.intent is LiveSemanticIntent.UNKNOWN:
            self._last_semantic_router_status = (
                f"UNKNOWN confidence={route.confidence:.2f}: {_bounded_error_text(route.reason, limit=220)}"
            )
            fallback = await super().answer(question)
            return self._fallback_with_router_status(text, fallback)

        self._last_semantic_router_status = (
            f"ROUTED intent={route.intent.value} target={route.target or '-'} "
            f"confidence={route.confidence:.2f} scope={route.time_scope.value}"
        )

        if route.time_scope is LiveSemanticTimeScope.HISTORICAL and route.target:
            return await self._dispatch_historical_route(text, route)

        if route.target and route.intent in {
            LiveSemanticIntent.ROOT_CAUSE,
            LiveSemanticIntent.TAG_STATUS,
            LiveSemanticIntent.FOLLOW_UP,
        }:
            return await self._dispatch_current_target(text, route)

        bridged = _bridge_question(text, route)
        reply = await super().answer(bridged)
        return _restore_original_question(text, reply)


__all__ = [
    "SemanticLiveCommissioningAssistant",
    "_DiagnosticProvider",
    "_bridge_question",
    "_historical_bridge_question",
    "_historical_metadata_for_target",
]
