from __future__ import annotations

import asyncio
import time
from typing import Any

from devagent.providers import ProviderError

from .assistant import LiveAssistantReply, LiveAssistantReplyKind
from .control_guard import is_plc_control_request
from .diagnosis import (
    LiveCommissioningDiagnosis,
    LiveDiagnosisStatus,
    observations_from_reconciled,
)
from .diagnosis_guard import diagnose_output
from .errors import LiveError
from .qa import _contains_forbidden_control_advice, answer_commissioning_question
from .reconciled_evidence import build_reconciled_live_agent_evidence
from .recursive_assistant import RecursiveLiveCommissioningAssistant
from .recursive_diagnosis import (
    required_tag_ids_for_recursive_output,
    trace_recursive_diagnosis,
)
from .semantic_assistant import SemanticLiveCommissioningAssistant


_SYSTEM_HEALTH_HEADING = "DEVAGENT LIVE SYSTEM HEALTH"

_HEALTH_FOLLOWUP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["intent", "confidence", "reason"],
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["EXPLAIN", "NEXT_CHECKS", "STATUS", "UNKNOWN"],
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "reason": {"type": "string", "minLength": 1},
    },
}

_HEALTH_CONVERSATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "confidence", "next_checks", "limitations"],
    "properties": {
        "answer": {"type": "string", "minLength": 1},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "next_checks": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "minLength": 1},
        },
        "limitations": {
            "type": "array",
            "maxItems": 8,
            "items": {"type": "string", "minLength": 1},
        },
    },
}


def _diagnosis_signature(diagnosis: LiveCommissioningDiagnosis) -> tuple[Any, ...]:
    return (
        diagnosis.status.value,
        diagnosis.expected_output,
        diagnosis.observed_output,
        tuple(
            (
                item.tag_id,
                item.tag_reference,
                item.required,
                item.observed_value,
                item.state.value,
            )
            for item in diagnosis.blockers
        ),
    )


def _is_system_health_reply(reply: LiveAssistantReply) -> bool:
    return (
        reply.kind is LiveAssistantReplyKind.DIAGNOSIS
        and reply.target_output is None
        and reply.text.lstrip().startswith(_SYSTEM_HEALTH_HEADING)
    )


def _system_health_signature(reply: LiveAssistantReply) -> tuple[str, ...]:
    """Compare health truth while ignoring per-read evidence identifiers."""
    if not _is_system_health_reply(reply):
        return ()
    return tuple(
        line.strip()
        for line in reply.text.splitlines()
        if not line.strip().startswith("Evidence:")
    )


class RealtimeSemanticLiveCommissioningAssistant(SemanticLiveCommissioningAssistant):
    """Semantic Live assistant with bounded final-state revalidation.

    Natural-language routing and AI explanation can take long enough for a running
    machine to change state. For modeled current-output diagnoses, this wrapper
    performs one final trusted evidence capture when answer preparation exceeded the
    configured latency threshold. The displayed direct diagnosis and recursive trace
    are always rebuilt from that final evidence set so an unchanged immediate blocker
    cannot hide a changed upstream cause. If final truth cannot be established because
    the session/evidence path is unavailable, the original current-state claim is
    discarded and Live fails closed with an explicit evidence gap.

    The realtime layer also preserves one bounded whole-system conversational context.
    When a validated SYSTEM_HEALTH answer was just produced, a later free-form follow-up
    such as "how do I fix this?" may be interpreted as a question about that health
    result. Before answering, DevAgent re-runs the deterministic current health check;
    the LLM may explain only that freshly proven evidence. Whole-system AI explanations
    are final-revalidated as well: if health truth changes while the explanation is
    being prepared, the AI wording is discarded and fresh deterministic truth wins.
    No stale runtime truth is promoted into the next answer and the PLC control/write
    guard remains authoritative.
    """

    def __init__(
        self,
        *args: Any,
        final_revalidation_after_seconds: float = 0.25,
        **kwargs: Any,
    ) -> None:
        if final_revalidation_after_seconds < 0:
            raise ValueError("final_revalidation_after_seconds must be >= 0")
        super().__init__(*args, **kwargs)
        self.final_revalidation_after_seconds = float(
            final_revalidation_after_seconds
        )
        self._last_system_health_reply: LiveAssistantReply | None = None

    async def _resolve_system_health_followup(self, question: str) -> str | None:
        provider = self.provider
        if provider is None or self._last_system_health_reply is None:
            return None
        try:
            response = await asyncio.to_thread(
                provider.request,
                role="live_system_health_followup_router",
                payload={
                    "instruction": (
                        "The previous validated conversational context is a CURRENT whole-system health diagnosis. "
                        "Classify only whether the new engineer utterance is a follow-up to that system-health context. "
                        "EXPLAIN means asking what the prior health result means or why it matters. "
                        "NEXT_CHECKS means asking how to fix, what to do next, what to inspect, or how to investigate the proven issue. "
                        "STATUS means asking whether the system is still good/bad/healthy now. "
                        "UNKNOWN means the utterance is unrelated, requests a different target/topic, or cannot safely be tied to the prior system-health result. "
                        "Do not answer the engineering question. Do not invent PLC tags, causes, runtime values, or evidence. "
                        "A repair/fix request is only a request for safe diagnostic next checks; it does not authorize PLC writes, forces, resets, bypasses, downloads, mode changes, or machine control."
                    ),
                    "question": str(question or "").strip(),
                    "previous_context": "SYSTEM_HEALTH",
                },
                schema=_HEALTH_FOLLOWUP_SCHEMA,
            )
        except ProviderError:
            return None

        try:
            confidence = float(response["confidence"])
            intent = str(response["intent"])
        except (KeyError, TypeError, ValueError):
            return None
        if confidence < 0.55:
            return None
        if intent == "UNKNOWN":
            return "UNKNOWN"
        if intent not in {"EXPLAIN", "NEXT_CHECKS", "STATUS"}:
            return None
        return intent

    def _system_health_revalidation_gap_reply(
        self,
        question: str,
        detail: str,
    ) -> LiveAssistantReply:
        self._last_system_health_reply = None
        bounded = " ".join(str(detail or "unknown read-only evidence failure").split())
        return LiveAssistantReply(
            question=question,
            kind=LiveAssistantReplyKind.LIMITATION,
            text=(
                "DEVAGENT LIVE SYSTEM HEALTH NOT REVALIDATED\n"
                "Current whole-system health could not be revalidated from trusted CURRENT OPC UA evidence. "
                "DevAgent discarded earlier AI/current-state wording rather than presenting potentially stale machine truth.\n\n"
                f"Limitation: {bounded}\n"
                "Next check: verify the read-only OPC UA session and trusted current evidence, then ask again."
            ),
            target_output=None,
        )

    async def _finalize_system_health_evidence(
        self,
        question: str,
        deterministic: LiveAssistantReply,
        *,
        started: float | None,
    ) -> tuple[LiveAssistantReply, bool]:
        """Return final current health evidence and whether earlier AI wording must be discarded."""
        if (
            started is None
            or time.monotonic() - started < self.final_revalidation_after_seconds
        ):
            self._last_system_health_reply = deterministic
            return deterministic, False

        try:
            refreshed = await RecursiveLiveCommissioningAssistant.answer(
                self,
                "Does the system have any faults?",
            )
        except LiveError as exc:
            return self._system_health_revalidation_gap_reply(question, str(exc)), True

        if not _is_system_health_reply(refreshed):
            self._last_system_health_reply = None
            return (
                LiveAssistantReply(
                    question=question,
                    kind=LiveAssistantReplyKind.LIMITATION,
                    text=(
                        "DEVAGENT LIVE SYSTEM HEALTH NOT REVALIDATED\n"
                        "Current whole-system health could not be revalidated after AI answer preparation. "
                        "DevAgent discarded the earlier AI wording rather than presenting potentially stale machine truth.\n\n"
                        f"Final read-only result:\n{refreshed.text}"
                    ),
                    target_output=None,
                ),
                True,
            )

        changed = _system_health_signature(refreshed) != _system_health_signature(
            deterministic
        )
        self._last_system_health_reply = refreshed
        if changed:
            return (
                LiveAssistantReply(
                    question=question,
                    kind=LiveAssistantReplyKind.DIAGNOSIS,
                    text=(
                        "DEVAGENT LIVE SYSTEM HEALTH REFRESHED\n"
                        "The current PLC/system health changed while the AI explanation was being prepared. "
                        "The earlier AI wording was discarded; fresh deterministic OPC UA evidence is shown below.\n\n"
                        + refreshed.text
                    ),
                    target_output=None,
                ),
                True,
            )
        return refreshed, False

    async def _explain_system_health(
        self,
        question: str,
        deterministic: LiveAssistantReply,
        *,
        followup_intent: str | None = None,
        started: float | None = None,
    ) -> LiveAssistantReply:
        provider = self.provider
        if provider is None or not _is_system_health_reply(deterministic):
            return deterministic

        response: dict[str, Any] | None = None
        try:
            response = await asyncio.to_thread(
                provider.request,
                role="live_system_health_explainer",
                payload={
                    "instruction": (
                        "You are the conversational explanation layer for a READ-ONLY industrial commissioning assistant. "
                        "The deterministic CURRENT system-health report below is authoritative. Answer the engineer's wording naturally and concisely using only facts present in that report. "
                        "Do not invent PLC logic, tags, device faults, physical causes, safety claims, evidence, or repair success. "
                        "If the engineer asks how to fix the issue, explain safe diagnostic next checks grounded in the report; do not claim the physical root cause is known unless the report proves it. "
                        "Never instruct PLC writes, forcing, safety/interlock bypass, reset commands, downloads, controller mode changes, start/stop commands, or any other machine-control action. "
                        "Suggested next checks must be read-only observations, engineering-source inspection, or approved field/device diagnostics. "
                        "Do not weaken limitations stated in the deterministic report."
                    ),
                    "question": str(question or "").strip(),
                    "followup_intent": followup_intent,
                    "deterministic_current_system_health": deterministic.text,
                },
                schema=_HEALTH_CONVERSATION_SCHEMA,
            )
        except ProviderError:
            response = None

        authoritative, discard_ai = await self._finalize_system_health_evidence(
            question,
            deterministic,
            started=started,
        )
        if discard_ai or response is None:
            return authoritative

        answer = str(response.get("answer", "")).strip()
        next_checks = tuple(
            str(item).strip()
            for item in response.get("next_checks", ())
            if str(item).strip()
        )
        limitations = tuple(
            str(item).strip()
            for item in response.get("limitations", ())
            if str(item).strip()
        )
        if not answer:
            return authoritative
        if any(
            _contains_forbidden_control_advice(item)
            for item in (answer, *next_checks, *limitations)
        ):
            return authoritative

        lines = [answer]
        if next_checks:
            lines.extend(["", "Next checks:"])
            lines.extend(f"- {item}" for item in next_checks)
        if limitations:
            lines.extend(["", "Limitations:"])
            lines.extend(f"- {item}" for item in limitations)
        lines.extend(
            [
                "",
                "Deterministic current evidence:",
                authoritative.text,
            ]
        )
        return LiveAssistantReply(
            question=question,
            kind=authoritative.kind,
            text="\n".join(lines),
            target_output=authoritative.target_output,
            diagnosis=authoritative.diagnosis,
            answer=authoritative.answer,
        )

    def _revalidation_gap_reply(
        self,
        question: str,
        original: LiveCommissioningDiagnosis,
        detail: str,
    ) -> LiveAssistantReply:
        target = original.target_output
        summary = (
            f"Current state for {target} could not be revalidated before this answer was displayed. "
            "DevAgent discarded the earlier current-state wording rather than presenting potentially stale PLC truth."
        )
        limitation = f"Final current-state revalidation unavailable: {detail}"
        diagnosis = LiveCommissioningDiagnosis(
            target_output=target,
            status=LiveDiagnosisStatus.INDETERMINATE,
            expected_output=original.expected_output,
            observed_output=None,
            rule_ids=original.rule_ids,
            source_locators=original.source_locators,
            paths=(),
            blockers=(),
            evidence_ids=(),
            limitations=(limitation,),
            summary=summary,
            next_checks=(
                "Verify the read-only OPC UA session is CONNECTED with trusted CURRENT evidence, then ask the question again.",
            ),
        )
        text = (
            "DEVAGENT LIVE CURRENT STATE NOT REVALIDATED\n"
            f"{summary}\n\n"
            f"Diagnosis: {diagnosis.status.value}\n"
            f"Target: {target}\n"
            "Confidence: 0.35\n\n"
            "Next checks:\n"
            f"- {diagnosis.next_checks[0]}\n\n"
            "Limitations:\n"
            f"- {limitation}"
        )
        return LiveAssistantReply(
            question=question,
            kind=LiveAssistantReplyKind.LIMITATION,
            text=text,
            target_output=target,
            diagnosis=diagnosis,
            answer=None,
        )

    async def _refresh_current_diagnosis(
        self,
        question: str,
        original: LiveCommissioningDiagnosis,
    ) -> LiveAssistantReply:
        target = original.target_output
        if not target:
            return self._revalidation_gap_reply(
                question,
                original,
                "the previous diagnosis has no canonical target",
            )
        if self.reconciliation is None:
            return self._revalidation_gap_reply(
                question,
                original,
                "engineering-to-OPC-UA reconciliation is unavailable",
            )
        if not self.connected:
            status = self.manager.status(self.connection.plc_id)
            return self._revalidation_gap_reply(
                question,
                original,
                f"OPC UA session state is {status.state.value}",
            )
        if not self.context.rules_for_output(target):
            return self._revalidation_gap_reply(
                question,
                original,
                "the modeled output rule is no longer available in the loaded engineering context",
            )

        required_tag_ids = required_tag_ids_for_recursive_output(
            self.context,
            target,
            max_depth=self.trace_max_depth,
            max_nodes=self.trace_max_nodes,
        )
        if not required_tag_ids:
            return self._revalidation_gap_reply(
                question,
                original,
                "no bounded dependency evidence set could be produced for the target",
            )
        try:
            reconciled = await build_reconciled_live_agent_evidence(
                self.manager,
                self.reconciliation,
                required_tag_ids=required_tag_ids,
                require_all=False,
            )
        except LiveError as exc:
            return self._revalidation_gap_reply(
                question,
                original,
                str(exc),
            )

        observations = observations_from_reconciled(reconciled)
        refreshed = diagnose_output(self.context, target, observations)
        direct_changed = _diagnosis_signature(refreshed) != _diagnosis_signature(original)

        deterministic = answer_commissioning_question(
            question,
            refreshed,
            provider=None,
        )
        recursive = trace_recursive_diagnosis(
            self.context,
            refreshed,
            observations,
            max_depth=self.trace_max_depth,
            max_nodes=self.trace_max_nodes,
        )
        if direct_changed:
            heading = "DEVAGENT LIVE CURRENT STATE REFRESHED"
            detail = (
                "The PLC state changed while the answer was being prepared. "
                "The earlier wording was discarded and the result below was recomputed "
                "from fresh trusted OPC UA evidence."
            )
        else:
            heading = "DEVAGENT LIVE CURRENT STATE REVALIDATED"
            detail = (
                "The immediate diagnosis remained the same, but DevAgent rebuilt the "
                "entire deterministic and recursive root-cause trace from final trusted "
                "OPC UA evidence before displaying this answer."
            )
        text = f"{heading}\n{detail}\n\n" + deterministic.render_text()
        if refreshed.source_locators:
            text += "\n\nTarget PLC source:"
            text += "".join(f"\n- {locator}" for locator in refreshed.source_locators)
        if recursive.roots or recursive.limitations:
            text += "\n\n" + recursive.render_text()

        return LiveAssistantReply(
            question=question,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text=text,
            target_output=target,
            diagnosis=refreshed,
            answer=None,
        )

    async def answer(self, question: str) -> LiveAssistantReply:
        started = time.monotonic()
        text = str(question or "").strip()
        raw_reply: LiveAssistantReply | None = None

        # A validated whole-system health result is the newest conversational topic.
        # Resolve that bounded context before the general semantic router gets a
        # chance to select an unrelated canonical target from known_targets.
        if (
            getattr(self, "_last_system_health_reply", None) is not None
            and self.provider is not None
            and not is_plc_control_request(text)
        ):
            followup_intent = await self._resolve_system_health_followup(text)
            if followup_intent in {"EXPLAIN", "NEXT_CHECKS", "STATUS"}:
                try:
                    fresh_health = await RecursiveLiveCommissioningAssistant.answer(
                        self,
                        "Does the system have any faults?",
                    )
                except LiveError as exc:
                    return self._system_health_revalidation_gap_reply(
                        text,
                        str(exc),
                    )
                if _is_system_health_reply(fresh_health):
                    self._last_target = None
                    self._last_system_health_reply = fresh_health
                    return await self._explain_system_health(
                        text,
                        fresh_health,
                        followup_intent=followup_intent,
                        started=started,
                    )
                self._last_system_health_reply = None
                return fresh_health
            if followup_intent == "UNKNOWN":
                # A confidently unrelated/different topic ends the bounded health
                # conversation, then the normal semantic router may handle the new topic.
                self._last_system_health_reply = None
            elif followup_intent is None:
                # Provider failure, malformed output, or low confidence must not let a
                # second LLM route an ungrounded target ahead of the active health
                # context. Fail closed through the deterministic parent, but preserve
                # the normal final target revalidation before displaying that fallback.
                self._last_system_health_reply = None
                raw_reply = await RecursiveLiveCommissioningAssistant.answer(self, text)

        if raw_reply is None:
            raw_reply = await super().answer(question)

        if _is_system_health_reply(raw_reply):
            # Whole-system health is now the newest conversational topic. A
            # previous target must not remain eligible for FOLLOW_UP routing.
            self._last_target = None
            self._last_system_health_reply = raw_reply
            return await self._explain_system_health(
                question,
                raw_reply,
                started=started,
            )

        if raw_reply.kind is LiveAssistantReplyKind.SYSTEM_OVERVIEW:
            self._last_system_health_reply = None
            self._last_target = None
        elif (
            raw_reply.kind is LiveAssistantReplyKind.DIAGNOSIS
            and raw_reply.target_output is not None
        ):
            self._last_system_health_reply = None

        reply = raw_reply
        diagnosis = reply.diagnosis
        if (
            reply.kind is not LiveAssistantReplyKind.DIAGNOSIS
            or diagnosis is None
            or not diagnosis.target_output
        ):
            return reply
        if time.monotonic() - started < self.final_revalidation_after_seconds:
            return reply
        return await self._refresh_current_diagnosis(question, diagnosis)


__all__ = [
    "RealtimeSemanticLiveCommissioningAssistant",
    "_diagnosis_signature",
    "_is_system_health_reply",
    "_system_health_signature",
]
