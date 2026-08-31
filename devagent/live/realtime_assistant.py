from __future__ import annotations

import time
from typing import Any

from .assistant import LiveAssistantReply, LiveAssistantReplyKind
from .diagnosis import (
    LiveCommissioningDiagnosis,
    LiveDiagnosisStatus,
    observations_from_reconciled,
)
from .diagnosis_guard import diagnose_output
from .errors import LiveError
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
    configured latency threshold. The displayed direct diagnosis and recursive trace
    are always rebuilt from that final evidence set so an unchanged immediate blocker
    cannot hide a changed upstream cause. If final truth cannot be established because
    the session/evidence path is unavailable, the original current-state claim is
    discarded and Live fails closed with an explicit evidence gap.
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
        reply = await super().answer(question)
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
]
