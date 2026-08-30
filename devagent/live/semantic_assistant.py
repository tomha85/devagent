from __future__ import annotations

from .assistant import LiveAssistantReply
from .control_guard import is_plc_control_request
from .recursive_assistant import RecursiveLiveCommissioningAssistant
from .semantic_intent import LiveSemanticIntent, LiveSemanticRoute, resolve_semantic_intent


def _bridge_question(original: str, route: LiveSemanticRoute) -> str:
    """Bridge a validated semantic route into the existing deterministic Live engine.

    The bridge is intentionally narrow: the model never supplies PLC facts. It only
    selects an intent and an exact engineering target already validated against the
    loaded project. The existing deterministic assistant still resolves logic and
    reads trusted OPC UA evidence.
    """
    if route.intent is LiveSemanticIntent.SYSTEM_HEALTH:
        return "Does the system have any faults?"
    if route.intent is LiveSemanticIntent.SYSTEM_OVERVIEW:
        return "What is this system?"
    if route.intent is LiveSemanticIntent.HISTORICAL_ROOT_CAUSE and route.target:
        # Preserve the original wording so existing bounded time-window and
        # transition-direction parsing can still use any explicit engineer detail,
        # while guaranteeing that the deterministic historical path is selected.
        return f"Why did {route.target} change?\nOriginal engineer question: {original}"
    if route.target:
        return f"{original}\nExact engineering target: {route.target}"
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


class SemanticLiveCommissioningAssistant(RecursiveLiveCommissioningAssistant):
    """Recursive Live assistant with provider-neutral natural-language intent routing.

    Safety/control detection remains deterministic and executes before the language
    model. When AI is disabled, unavailable, low-confidence, or invalid, behavior
    falls back to the existing deterministic question resolver.
    """

    async def answer(self, question: str) -> LiveAssistantReply:
        text = str(question or "").strip()

        # Keep safety-critical control/write interpretation outside the LLM.
        if not text or is_plc_control_request(text) or self.provider is None:
            return await super().answer(question)

        route = resolve_semantic_intent(
            text,
            self.context,
            self.provider,
            previous_target=self._last_target,
        )
        if route is None or route.intent is LiveSemanticIntent.UNKNOWN:
            return await super().answer(question)

        bridged = _bridge_question(text, route)
        reply = await super().answer(bridged)
        return _restore_original_question(text, reply)


__all__ = [
    "SemanticLiveCommissioningAssistant",
    "_bridge_question",
]
