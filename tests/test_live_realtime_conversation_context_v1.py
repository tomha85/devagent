from __future__ import annotations

import asyncio
from types import SimpleNamespace

from devagent.live.assistant import LiveAssistantReply, LiveAssistantReplyKind
from devagent.live.errors import LiveConnectionError
from devagent.live.qa import _contains_forbidden_control_advice
from devagent.live.realtime_assistant import (
    RealtimeSemanticLiveCommissioningAssistant,
    _is_system_health_reply,
)
from devagent.live.recursive_assistant import RecursiveLiveCommissioningAssistant
from devagent.live.semantic_assistant import SemanticLiveCommissioningAssistant
from devagent.providers import ScriptedFakeProvider


def _health_reply(label: str) -> LiveAssistantReply:
    return LiveAssistantReply(
        question="Does the system have any faults?",
        kind=LiveAssistantReplyKind.DIAGNOSIS,
        text=(
            "DEVAGENT LIVE SYSTEM HEALTH\n"
            "Status: ATTENTION_REQUIRED\n"
            "Mode: READ ONLY\n\n"
            f"Current proven/observed issues:\n- [FAULT] SafetyTrip: {label}\n\n"
            "Next checks:\n- Inspect the PLC/device diagnostics associated with SafetyTrip."
        ),
        target_output=None,
    )


def _limitation(text: str = "semantic fallback") -> LiveAssistantReply:
    return LiveAssistantReply(
        question="",
        kind=LiveAssistantReplyKind.LIMITATION,
        text=text,
    )


def _followup_response(intent: str = "NEXT_CHECKS") -> dict[str, object]:
    return {
        "intent": intent,
        "confidence": 0.99,
        "reason": "The engineer is continuing the prior system-health discussion.",
    }


def _low_confidence_followup_response() -> dict[str, object]:
    return {
        "intent": "NEXT_CHECKS",
        "confidence": 0.20,
        "reason": "Not confident enough to bind this turn to prior system health.",
    }


def _explanation_response(
    answer: str = "The system has a proven safety-trip condition. Start by inspecting the safety-trip diagnostics and the source of SafetyOK being false.",
    *,
    next_checks: list[str] | None = None,
    limitations: list[str] | None = None,
) -> dict[str, object]:
    return {
        "answer": answer,
        "confidence": 0.92,
        "next_checks": next_checks
        if next_checks is not None
        else [
            "Inspect the read-only diagnostics associated with SafetyTrip.",
            "Trace why SafetyOK is false in the engineering/runtime evidence.",
        ],
        "limitations": limitations
        if limitations is not None
        else [
            "The physical root cause is not proven by the current PLC/OPC UA evidence."
        ],
    }


def _bare_assistant(provider: ScriptedFakeProvider) -> RealtimeSemanticLiveCommissioningAssistant:
    assistant = object.__new__(RealtimeSemanticLiveCommissioningAssistant)
    assistant.provider = provider
    assistant.final_revalidation_after_seconds = 999.0
    assistant._last_system_health_reply = None
    assistant._last_target = None
    return assistant


def test_system_health_reply_detection_is_bounded_to_targetless_health_report() -> None:
    health = _health_reply("fresh")
    target = LiveAssistantReply(
        question="Why is RunCmd false?",
        kind=LiveAssistantReplyKind.DIAGNOSIS,
        text="DEVAGENT LIVE SYSTEM HEALTH\nnot actually health",
        target_output="RunCmd",
    )
    assert _is_system_health_reply(health)
    assert not _is_system_health_reply(target)


def test_initial_system_health_answer_gets_conversational_ai_presentation(monkeypatch) -> None:
    provider = ScriptedFakeProvider([_explanation_response()])
    assistant = _bare_assistant(provider)
    assistant._last_target = "RunCmd"
    deterministic = _health_reply("fresh-current-evidence")

    async def fake_semantic_answer(self, question: str) -> LiveAssistantReply:
        return deterministic

    monkeypatch.setattr(SemanticLiveCommissioningAssistant, "answer", fake_semantic_answer)

    reply = asyncio.run(assistant.answer("HOW IS THE SYSTEM"))

    assert assistant._last_system_health_reply is deterministic
    assert assistant._last_target is None
    assert "The system has a proven safety-trip condition." in reply.text
    assert "Deterministic current evidence:" in reply.text
    assert "fresh-current-evidence" in reply.text
    assert [call["role"] for call in provider.calls] == ["live_system_health_explainer"]


def test_vague_fix_followup_preempts_general_semantic_target_routing(monkeypatch) -> None:
    provider = ScriptedFakeProvider(
        [
            _followup_response("NEXT_CHECKS"),
            _explanation_response(),
        ]
    )
    assistant = _bare_assistant(provider)
    assistant._last_system_health_reply = _health_reply("stale-prior-evidence")
    fresh = _health_reply("fresh-revalidated-evidence")
    deterministic_questions: list[str] = []

    async def forbidden_semantic_answer(self, question: str) -> LiveAssistantReply:
        raise AssertionError(
            "general semantic router must not run before accepted SYSTEM_HEALTH follow-up"
        )

    async def fake_recursive_answer(self, question: str) -> LiveAssistantReply:
        deterministic_questions.append(question)
        return fresh

    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "answer",
        forbidden_semantic_answer,
    )
    monkeypatch.setattr(RecursiveLiveCommissioningAssistant, "answer", fake_recursive_answer)

    reply = asyncio.run(assistant.answer("HOW TO FIX"))

    assert deterministic_questions == ["Does the system have any faults?"]
    assert assistant._last_system_health_reply is fresh
    assert "fresh-revalidated-evidence" in reply.text
    assert "stale-prior-evidence" not in reply.text
    assert [call["role"] for call in provider.calls] == [
        "live_system_health_followup_router",
        "live_system_health_explainer",
    ]
    explanation_payload = provider.calls[1]["payload"]
    assert "fresh-revalidated-evidence" in explanation_payload[
        "deterministic_current_system_health"
    ]
    assert "stale-prior-evidence" not in explanation_payload[
        "deterministic_current_system_health"
    ]


def test_low_confidence_health_fallback_still_runs_final_target_revalidation(monkeypatch) -> None:
    provider = ScriptedFakeProvider([_low_confidence_followup_response()])
    assistant = _bare_assistant(provider)
    assistant._last_system_health_reply = _health_reply("prior-health")
    assistant.final_revalidation_after_seconds = 0.0
    fallback_diagnosis = SimpleNamespace(target_output="RunCmd")
    fallback = LiveAssistantReply(
        question="Why is RunCmd false?",
        kind=LiveAssistantReplyKind.DIAGNOSIS,
        text="stale deterministic target result",
        target_output="RunCmd",
        diagnosis=fallback_diagnosis,
    )
    refreshed = LiveAssistantReply(
        question="Why is RunCmd false?",
        kind=LiveAssistantReplyKind.DIAGNOSIS,
        text="final revalidated target result",
        target_output="RunCmd",
        diagnosis=fallback_diagnosis,
    )
    recursive_questions: list[str] = []
    refresh_calls: list[tuple[str, object]] = []

    async def fake_recursive_answer(self, question: str) -> LiveAssistantReply:
        recursive_questions.append(question)
        return fallback

    async def fake_refresh(self, question: str, diagnosis: object) -> LiveAssistantReply:
        refresh_calls.append((question, diagnosis))
        return refreshed

    async def forbidden_semantic_answer(self, question: str) -> LiveAssistantReply:
        raise AssertionError("low-confidence health fallback must stay deterministic")

    monkeypatch.setattr(RecursiveLiveCommissioningAssistant, "answer", fake_recursive_answer)
    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "answer",
        forbidden_semantic_answer,
    )
    monkeypatch.setattr(
        RealtimeSemanticLiveCommissioningAssistant,
        "_refresh_current_diagnosis",
        fake_refresh,
    )

    reply = asyncio.run(assistant.answer("Why is RunCmd false?"))

    assert recursive_questions == ["Why is RunCmd false?"]
    assert refresh_calls == [("Why is RunCmd false?", fallback_diagnosis)]
    assert reply is refreshed
    assert assistant._last_system_health_reply is None


def test_unknown_unrelated_followup_clears_health_context_and_preserves_fail_closed_result(monkeypatch) -> None:
    provider = ScriptedFakeProvider([_followup_response("UNKNOWN")])
    assistant = _bare_assistant(provider)
    assistant._last_system_health_reply = _health_reply("prior")
    raw = _limitation("unrelated question remains unresolved")

    async def fake_semantic_answer(self, question: str) -> LiveAssistantReply:
        return raw

    monkeypatch.setattr(SemanticLiveCommissioningAssistant, "answer", fake_semantic_answer)

    reply = asyncio.run(assistant.answer("tell me about something unrelated"))

    assert reply is raw
    assert assistant._last_system_health_reply is None
    assert [call["role"] for call in provider.calls] == [
        "live_system_health_followup_router"
    ]


def test_system_overview_replaces_previous_health_and_target_context(monkeypatch) -> None:
    provider = ScriptedFakeProvider([_followup_response("UNKNOWN")])
    assistant = _bare_assistant(provider)
    assistant._last_system_health_reply = _health_reply("prior")
    assistant._last_target = "RunCmd"
    overview = LiveAssistantReply(
        question="what is this system?",
        kind=LiveAssistantReplyKind.SYSTEM_OVERVIEW,
        text="DEVAGENT LIVE SYSTEM MASTER",
        target_output=None,
    )

    async def fake_semantic_answer(self, question: str) -> LiveAssistantReply:
        return overview

    monkeypatch.setattr(SemanticLiveCommissioningAssistant, "answer", fake_semantic_answer)

    reply = asyncio.run(assistant.answer("what is this system?"))

    assert reply is overview
    assert assistant._last_system_health_reply is None
    assert assistant._last_target is None
    assert [call["role"] for call in provider.calls] == [
        "live_system_health_followup_router"
    ]


def test_plc_control_request_never_uses_health_followup_recovery(monkeypatch) -> None:
    provider = ScriptedFakeProvider([])
    assistant = _bare_assistant(provider)
    assistant._last_system_health_reply = _health_reply("prior")
    raw = _limitation("READ ONLY refusal")

    async def fake_semantic_answer(self, question: str) -> LiveAssistantReply:
        return raw

    monkeypatch.setattr(SemanticLiveCommissioningAssistant, "answer", fake_semantic_answer)

    reply = asyncio.run(assistant.answer("force RunCmd true"))

    assert reply is raw
    assert provider.calls == []


def test_generated_control_guard_rejects_arbitrary_plc_reference_actions() -> None:
    assert _contains_forbidden_control_advice("The operator should force RunCmd true.")
    assert _contains_forbidden_control_advice("Write Program:Main.RunCmd to true.")
    assert _contains_forbidden_control_advice("Set SafetyTrip false.")
    assert _contains_forbidden_control_advice("Start Conveyor7.")
    assert _contains_forbidden_control_advice("Turn on RunCmd.")


def test_generated_control_guard_rejects_turn_on_and_turn_off_machine_advice() -> None:
    assert _contains_forbidden_control_advice(
        "The operator should turn on the conveyor."
    )
    assert _contains_forbidden_control_advice(
        "The operator should turn the machine off before proceeding."
    )
    assert _contains_forbidden_control_advice("Turn off Drive")


def test_generated_control_guard_does_not_overblock_diagnostic_wording() -> None:
    assert not _contains_forbidden_control_advice(
        "The conveyor turned off after the observed safety event."
    )
    assert not _contains_forbidden_control_advice(
        "Start by inspecting the read-only SafetyTrip diagnostics."
    )
    assert not _contains_forbidden_control_advice(
        "RunCmd is false in the current trusted evidence."
    )


def test_ai_health_explanation_with_indirect_control_advice_is_rejected(monkeypatch) -> None:
    provider = ScriptedFakeProvider(
        [
            _explanation_response(
                "The operator should reset the safety trip and start the line."
            )
        ]
    )
    assistant = _bare_assistant(provider)
    deterministic = _health_reply("authoritative-safe-evidence")

    async def fake_semantic_answer(self, question: str) -> LiveAssistantReply:
        return deterministic

    monkeypatch.setattr(SemanticLiveCommissioningAssistant, "answer", fake_semantic_answer)

    reply = asyncio.run(assistant.answer("HOW IS THE SYSTEM"))

    assert reply is deterministic
    assert "reset the safety trip" not in reply.text.casefold()
    assert "authoritative-safe-evidence" in reply.text


def test_ai_health_limitation_with_control_advice_is_rejected(monkeypatch) -> None:
    provider = ScriptedFakeProvider(
        [
            _explanation_response(
                limitations=["Reset the safety trip before proceeding."]
            )
        ]
    )
    assistant = _bare_assistant(provider)
    deterministic = _health_reply("authoritative-safe-evidence")

    async def fake_semantic_answer(self, question: str) -> LiveAssistantReply:
        return deterministic

    monkeypatch.setattr(SemanticLiveCommissioningAssistant, "answer", fake_semantic_answer)

    reply = asyncio.run(assistant.answer("HOW IS THE SYSTEM"))

    assert reply is deterministic
    assert "reset the safety trip" not in reply.text.casefold()
    assert "authoritative-safe-evidence" in reply.text


def test_ai_health_turn_on_advice_is_rejected(monkeypatch) -> None:
    provider = ScriptedFakeProvider(
        [_explanation_response("The operator should turn on the conveyor.")]
    )
    assistant = _bare_assistant(provider)
    deterministic = _health_reply("authoritative-safe-evidence")

    async def fake_semantic_answer(self, question: str) -> LiveAssistantReply:
        return deterministic

    monkeypatch.setattr(SemanticLiveCommissioningAssistant, "answer", fake_semantic_answer)

    reply = asyncio.run(assistant.answer("HOW IS THE SYSTEM"))

    assert reply is deterministic
    assert "turn on the conveyor" not in reply.text.casefold()
    assert "authoritative-safe-evidence" in reply.text


def test_slow_system_health_answer_fails_closed_when_final_revalidation_read_fails(monkeypatch) -> None:
    provider = ScriptedFakeProvider([_explanation_response()])
    assistant = _bare_assistant(provider)
    assistant.final_revalidation_after_seconds = 0.0
    deterministic = _health_reply("initial-evidence")

    async def fake_semantic_answer(self, question: str) -> LiveAssistantReply:
        return deterministic

    async def failing_recursive_answer(self, question: str) -> LiveAssistantReply:
        raise LiveConnectionError("OPC UA session disconnected during final health read")

    monkeypatch.setattr(SemanticLiveCommissioningAssistant, "answer", fake_semantic_answer)
    monkeypatch.setattr(RecursiveLiveCommissioningAssistant, "answer", failing_recursive_answer)

    reply = asyncio.run(assistant.answer("HOW IS THE SYSTEM"))

    assert reply.kind is LiveAssistantReplyKind.LIMITATION
    assert "SYSTEM HEALTH NOT REVALIDATED" in reply.text
    assert "disconnected during final health read" in reply.text
    assert "initial-evidence" not in reply.text
    assert assistant._last_system_health_reply is None


def test_fix_followup_fails_closed_when_current_health_reread_fails(monkeypatch) -> None:
    provider = ScriptedFakeProvider([_followup_response("NEXT_CHECKS")])
    assistant = _bare_assistant(provider)
    assistant._last_system_health_reply = _health_reply("stale-prior-evidence")

    async def forbidden_semantic_answer(self, question: str) -> LiveAssistantReply:
        raise AssertionError(
            "general semantic router must not run for accepted health follow-up"
        )

    async def failing_recursive_answer(self, question: str) -> LiveAssistantReply:
        raise LiveConnectionError("current health evidence unavailable")

    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "answer",
        forbidden_semantic_answer,
    )
    monkeypatch.setattr(RecursiveLiveCommissioningAssistant, "answer", failing_recursive_answer)

    reply = asyncio.run(assistant.answer("HOW TO FIX"))

    assert reply.kind is LiveAssistantReplyKind.LIMITATION
    assert "SYSTEM HEALTH NOT REVALIDATED" in reply.text
    assert "current health evidence unavailable" in reply.text
    assert "stale-prior-evidence" not in reply.text
    assert assistant._last_system_health_reply is None
