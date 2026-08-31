from __future__ import annotations

import asyncio

from devagent.live.assistant import LiveAssistantReply, LiveAssistantReplyKind
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


def _explanation_response(
    answer: str = "The system has a proven safety-trip condition. Start by inspecting the safety-trip diagnostics and the source of SafetyOK being false.",
) -> dict[str, object]:
    return {
        "answer": answer,
        "confidence": 0.92,
        "next_checks": [
            "Inspect the read-only diagnostics associated with SafetyTrip.",
            "Trace why SafetyOK is false in the engineering/runtime evidence.",
        ],
        "limitations": [
            "The physical root cause is not proven by the current PLC/OPC UA evidence."
        ],
    }


def _bare_assistant(provider: ScriptedFakeProvider) -> RealtimeSemanticLiveCommissioningAssistant:
    assistant = object.__new__(RealtimeSemanticLiveCommissioningAssistant)
    assistant.provider = provider
    assistant.final_revalidation_after_seconds = 999.0
    assistant._last_system_health_reply = None
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
    deterministic = _health_reply("fresh-current-evidence")

    async def fake_semantic_answer(self, question: str) -> LiveAssistantReply:
        return deterministic

    monkeypatch.setattr(SemanticLiveCommissioningAssistant, "answer", fake_semantic_answer)

    reply = asyncio.run(assistant.answer("HOW IS THE SYSTEM"))

    assert assistant._last_system_health_reply is deterministic
    assert "The system has a proven safety-trip condition." in reply.text
    assert "Deterministic current evidence:" in reply.text
    assert "fresh-current-evidence" in reply.text
    assert [call["role"] for call in provider.calls] == ["live_system_health_explainer"]


def test_vague_fix_followup_rechecks_current_health_before_ai_explains(monkeypatch) -> None:
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

    async def fake_semantic_answer(self, question: str) -> LiveAssistantReply:
        return _limitation(
            "AI semantic router: UNKNOWN confidence=0.99: vague follow-up"
        )

    async def fake_recursive_answer(self, question: str) -> LiveAssistantReply:
        deterministic_questions.append(question)
        return fresh

    monkeypatch.setattr(SemanticLiveCommissioningAssistant, "answer", fake_semantic_answer)
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


def test_unknown_unrelated_followup_preserves_fail_closed_semantic_result(monkeypatch) -> None:
    provider = ScriptedFakeProvider([_followup_response("UNKNOWN")])
    assistant = _bare_assistant(provider)
    assistant._last_system_health_reply = _health_reply("prior")
    raw = _limitation("unrelated question remains unresolved")

    async def fake_semantic_answer(self, question: str) -> LiveAssistantReply:
        return raw

    monkeypatch.setattr(SemanticLiveCommissioningAssistant, "answer", fake_semantic_answer)

    reply = asyncio.run(assistant.answer("tell me about something unrelated"))

    assert reply is raw
    assert [call["role"] for call in provider.calls] == [
        "live_system_health_followup_router"
    ]


def test_system_overview_replaces_previous_health_conversation_context(monkeypatch) -> None:
    provider = ScriptedFakeProvider([])
    assistant = _bare_assistant(provider)
    assistant._last_system_health_reply = _health_reply("prior")
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
    assert provider.calls == []


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


def test_ai_health_explanation_with_forbidden_control_advice_is_rejected(monkeypatch) -> None:
    provider = ScriptedFakeProvider(
        [
            _explanation_response(
                "Reset the safety trip and start the line."
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
    assert "Reset the safety trip" not in reply.text
    assert "authoritative-safe-evidence" in reply.text
