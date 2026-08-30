from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any

from devagent.providers import ModelProvider, ProviderError

from .assistant import LiveAssistantReply, LiveAssistantReplyKind
from .control_guard import is_plc_control_request
from .history import requested_history_seconds
from .recursive_assistant import RecursiveLiveCommissioningAssistant
from .semantic_intent import (
    LiveSemanticIntent,
    LiveSemanticRoute,
    LiveSemanticTimeScope,
    resolve_semantic_intent,
)


def _historical_bridge_question(original: str, target: str) -> str:
    """Create target-only historical wording while preserving bounded time intent.

    The original engineer sentence is never forwarded to deterministic target
    resolution after the semantic router has validated a target. Only the parsed
    historical age and STOP/START direction are retained, preventing another signal
    name or a current-health phrase in the original wording from overriding the
    validated route.
    """
    window = requested_history_seconds(original)
    direction = getattr(window, "direction", None)
    age_seconds = getattr(window, "age_seconds", None)

    if direction == "STOP":
        base = f"Why did {target} stop"
    elif direction == "START":
        base = f"Why did {target} start"
    else:
        base = f"Why did {target} change"

    if age_seconds is not None:
        return f"{base} {age_seconds:g} seconds ago?"
    return f"{base} earlier?"


def _bridge_question(original: str, route: LiveSemanticRoute) -> str:
    """Bridge a validated semantic route into the deterministic Live engine.

    Once an exact target has been validated, current and historical dispatch use
    target-only canonical wording. The original sentence is intentionally not fed
    back into generic deterministic intent classifiers because it may contain other
    valid signal names or health phrases that conflict with the validated route.
    """
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


def _bounded_error_text(value: object, *, limit: int = 360) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


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
    """Recursive Live assistant with provider-neutral natural-language intent routing.

    Safety/control detection remains deterministic and executes before the language
    model. When AI is disabled, unavailable, low-confidence, or invalid, behavior
    falls back to the existing deterministic question resolver. AI fallback is never
    silent: the session records and renders a bounded reason so engineers can
    distinguish deterministic fallback from successful LLM interpretation.
    """

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

    async def _dispatch_historical_route(
        self,
        original: str,
        route: LiveSemanticRoute,
    ) -> LiveAssistantReply:
        """Dispatch validated historical scope without current-state reclassification."""
        if not self.connected or self.reconciliation is None:
            await self.start()
        bridged = _bridge_question(original, route)
        reply = await self._historical_reply(bridged)
        if reply is None:
            return LiveAssistantReply(
                question=original,
                kind=LiveAssistantReplyKind.LIMITATION,
                text=(
                    "A historical semantic intent was accepted, but the deterministic historical engine could not safely dispatch the request. "
                    "DevAgent will not substitute current OPC UA state for the requested past event."
                ),
                target_output=route.target,
            )
        return _restore_original_question(original, reply)

    async def answer(self, question: str) -> LiveAssistantReply:
        text = str(question or "").strip()

        # Keep safety-critical control/write interpretation outside the LLM.
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

        bridged = _bridge_question(text, route)
        reply = await super().answer(bridged)
        return _restore_original_question(text, reply)


__all__ = [
    "SemanticLiveCommissioningAssistant",
    "_DiagnosticProvider",
    "_bridge_question",
    "_historical_bridge_question",
]
