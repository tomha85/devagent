from __future__ import annotations

import time
from typing import Any

from .assistant import LiveAssistantReply, LiveAssistantReplyKind
from .diagnosis import LiveCommissioningDiagnosis, observations_from_reconciled
from .diagnosis_guard import diagnose_output
from .errors import LiveConfigurationError
from .qa import answer_commissioning_question
from .reconciled_evidence import build_reconciled_live_agent_evidence
from .recursive_diagnosis import (
    required_tag_ids_for_recursive_output,
    trace_recursive_diagnosis,
)
from .semantic_assistant import SemanticLiveCommissioningAssistant


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


class RealtimeSemanticLiveCommissioningAssistant(SemanticLiveCommissioningAssistant):
    """Semantic Live assistant with bounded final-state revalidation.

    Natural-language routing and AI explanation can take long enough for a running
    machine to change state. For modeled current-output diagnoses, this wrapper
    performs one final trusted evidence capture when answer preparation exceeded the
    configured latency threshold. If the deterministic diagnosis changed, the stale
    AI wording is discarded and a refreshed deterministic answer is returned.
    """

    def __init__(
        self,
        *args: Any,
        final_revalidation_after_seconds: float = 0.5,
        **kwargs: Any,
    ) -> None:
        if final_revalidation_after_seconds < 0:
            raise ValueError("final_revalidation_after_seconds must be >= 0")
        super().__init__(*args, **kwargs)
        self.final_revalidation_after_seconds = float(
            final_revalidation_after_seconds
        )

    async def _refresh_current_diagnosis(
        self,
        question: str,
        original: LiveCommissioningDiagnosis,
    ) -> LiveAssistantReply | None:
        if not self.connected or self.reconciliation is None:
            return None
        target = original.target_output
        if not target or not self.context.rules_for_output(target):
            return None

        required_tag_ids = required_tag_ids_for_recursive_output(
            self.context,
            target,
            max_depth=self.trace_max_depth,
            max_nodes=self.trace_max_nodes,
        )
        if not required_tag_ids:
            return None
        try:
            reconciled = await build_reconciled_live_agent_evidence(
                self.manager,
                self.reconciliation,
                required_tag_ids=required_tag_ids,
                require_all=False,
            )
        except LiveConfigurationError:
            return None

        observations = observations_from_reconciled(reconciled)
        refreshed = diagnose_output(self.context, target, observations)
        if _diagnosis_signature(refreshed) == _diagnosis_signature(original):
            return None

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
        text = (
            "DEVAGENT LIVE CURRENT STATE REFRESHED\n"
            "The PLC state changed while the answer was being prepared. "
            "The earlier wording was discarded and the result below was recomputed "
            "from fresh trusted OPC UA evidence.\n\n"
            + deterministic.render_text()
        )
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
        reply = await super().answer(question)
        diagnosis = reply.diagnosis
        if (
            reply.kind is not LiveAssistantReplyKind.DIAGNOSIS
            or diagnosis is None
            or not diagnosis.target_output
        ):
            return reply
        if (
            time.monotonic() - started
            < self.final_revalidation_after_seconds
        ):
            return reply
        refreshed = await self._refresh_current_diagnosis(question, diagnosis)
        return refreshed or reply


__all__ = [
    "RealtimeSemanticLiveCommissioningAssistant",
    "_diagnosis_signature",
]
