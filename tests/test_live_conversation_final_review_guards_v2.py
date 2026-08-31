from __future__ import annotations

import asyncio

from devagent.live.assistant import LiveAssistantReply, LiveAssistantReplyKind
from devagent.live.control_guard import is_plc_control_request
from devagent.live.qa import _contains_forbidden_control_advice
from devagent.live.realtime_assistant import RealtimeSemanticLiveCommissioningAssistant
from devagent.live.semantic_assistant import SemanticLiveCommissioningAssistant
from devagent.providers import ScriptedFakeProvider


def _health_reply() -> LiveAssistantReply:
    return LiveAssistantReply(
        question="Does the system have any faults?",
        kind=LiveAssistantReplyKind.DIAGNOSIS,
        text=(
            "DEVAGENT LIVE SYSTEM HEALTH\n"
            "Status: ATTENTION_REQUIRED\n"
            "Mode: READ ONLY\n\n"
            "Current proven/observed issues:\n"
            "- [FAULT] SafetyTrip: active"
        ),
        target_output=None,
    )


def _bare_assistant() -> RealtimeSemanticLiveCommissioningAssistant:
    assistant = object.__new__(RealtimeSemanticLiveCommissioningAssistant)
    assistant.provider = ScriptedFakeProvider([])
    assistant.final_revalidation_after_seconds = 999.0
    assistant._last_system_health_reply = None
    assistant._last_target = None
    return assistant


def test_generated_control_guard_rejects_delimited_canonical_references() -> None:
    assert _contains_forbidden_control_advice("Force `RunCmd` true.")
    assert _contains_forbidden_control_advice(
        'Write "Program:Main.RunCmd" to true.'
    )
    assert _contains_forbidden_control_advice("Set (SafetyTrip) false.")
    assert _contains_forbidden_control_advice("Turn on `Conveyor7`.")


def test_generated_control_guard_rejects_plc_transfer_with_intervening_object() -> None:
    assert _contains_forbidden_control_advice(
        "Download the project to the PLC."
    )
    assert _contains_forbidden_control_advice(
        "Upload the revised program into the controller."
    )


def test_generated_control_guard_allows_read_only_meta_wording() -> None:
    assert not _contains_forbidden_control_advice(
        "Write access is disabled in READ ONLY mode."
    )
    assert not _contains_forbidden_control_advice(
        "Reset commands are unavailable in READ ONLY mode."
    )
    assert not _contains_forbidden_control_advice(
        "Start by inspecting the read-only SafetyTrip diagnostics."
    )
    assert not _contains_forbidden_control_advice(
        "The conveyor turned off after the observed safety event."
    )


def test_control_request_guard_accepts_suffix_turn_forms_without_blocking_diagnostics() -> None:
    assert is_plc_control_request("turn the machine off")
    assert is_plc_control_request("Can you turn Conveyor7 on?")
    assert is_plc_control_request("please turn the motor off")
    assert not is_plc_control_request("Why did the motor turn off?")


def test_suffix_turn_control_request_clears_prior_health_and_target_context(monkeypatch) -> None:
    assistant = _bare_assistant()
    assistant._last_system_health_reply = _health_reply()
    assistant._last_target = "RunCmd"
    refusal = LiveAssistantReply(
        question="turn the machine off",
        kind=LiveAssistantReplyKind.LIMITATION,
        text="READ ONLY refusal",
        target_output=None,
    )

    async def fake_semantic_answer(self, question: str) -> LiveAssistantReply:
        return refusal

    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "answer",
        fake_semantic_answer,
    )

    reply = asyncio.run(assistant.answer("turn the machine off"))

    assert reply is refusal
    assert assistant._last_system_health_reply is None
    assert assistant._last_target is None
    assert assistant.provider.calls == []
