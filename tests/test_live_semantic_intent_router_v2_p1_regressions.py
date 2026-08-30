from __future__ import annotations

import asyncio
from types import SimpleNamespace

from devagent.live.assistant import LiveAssistantReply, LiveAssistantReplyKind
from devagent.live.recursive_assistant import RecursiveLiveCommissioningAssistant
from devagent.live.semantic_assistant import SemanticLiveCommissioningAssistant
from devagent.live.semantic_intent import resolve_semantic_intent
from devagent.providers import ScriptedFakeProvider


class _BaseContext:
    vendor = "ROCKWELL"
    controller_name = "WarehouseCommissioningDemo"

    def __init__(self) -> None:
        self.tags = (
            SimpleNamespace(
                name="RunCmd",
                scoped_name="RunCmd",
                description="Conveyor run command",
                data_type="BOOL",
                scope="Controller",
            ),
            SimpleNamespace(
                name="DriveReady",
                scoped_name="DriveReady",
                description="Drive ready feedback",
                data_type="BOOL",
                scope="Controller",
            ),
        )

    def output_names(self) -> tuple[str, ...]:
        return ("RunCmd", "DriveReady")


class _AmbiguousScopedContext:
    vendor = "ROCKWELL"
    controller_name = "ScopedTagDemo"

    def __init__(self) -> None:
        self.tags = (
            SimpleNamespace(
                name="Ready",
                scoped_name="ProgramA.Ready",
                description="Program A ready",
                data_type="BOOL",
                scope="ProgramA",
            ),
            SimpleNamespace(
                name="Ready",
                scoped_name="ProgramB.Ready",
                description="Program B ready",
                data_type="BOOL",
                scope="ProgramB",
            ),
        )

    def output_names(self) -> tuple[str, ...]:
        # Simulate a downstream/project view that also exposes the ambiguous short
        # form as an output. The semantic boundary must still withhold it.
        return ("Ready",)


def _response(
    intent: str,
    *,
    target=None,
    time_scope: str = "CURRENT",
    confidence: float = 0.99,
):
    return {
        "intent": intent,
        "target": target,
        "time_scope": time_scope,
        "confidence": confidence,
        "reason": "Language interpretation only.",
    }


def test_ambiguous_unqualified_program_tag_is_neither_exposed_nor_accepted() -> None:
    provider = ScriptedFakeProvider(
        [_response("ROOT_CAUSE", target="Ready")]
    )

    route = resolve_semantic_intent(
        "why is Ready false?",
        _AmbiguousScopedContext(),
        provider,
    )

    assert route is None
    payload = provider.calls[0]["payload"]
    assert "Ready" not in payload["known_outputs"]
    assert "Ready" not in payload["known_targets"]
    assert "ProgramA.Ready" in payload["known_targets"]
    assert "ProgramB.Ready" in payload["known_targets"]


def test_current_validated_target_cannot_be_preempted_by_health_phrase(monkeypatch) -> None:
    provider = ScriptedFakeProvider(
        [_response("TAG_STATUS", target="RunCmd")]
    )
    parent_questions: list[str] = []

    async def fake_parent(self, question: str) -> LiveAssistantReply:
        parent_questions.append(question)
        return LiveAssistantReply(
            question=question,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text="target-status",
            target_output="RunCmd",
        )

    monkeypatch.setattr(RecursiveLiveCommissioningAssistant, "answer", fake_parent)

    assistant = object.__new__(SemanticLiveCommissioningAssistant)
    assistant.loaded = SimpleNamespace(context=_BaseContext())
    assistant.provider = provider
    assistant._last_target = None

    original = "Is RunCmd false, and does the system have any faults?"
    reply = asyncio.run(assistant.answer(original))

    assert parent_questions == ["What is the current value of RunCmd?"]
    assert "system have any faults" not in parent_questions[0]
    assert reply.question == original
    assert reply.target_output == "RunCmd"


def test_historical_route_bypasses_current_system_health_preemption(monkeypatch) -> None:
    provider = ScriptedFakeProvider(
        [
            _response(
                "HISTORICAL_ROOT_CAUSE",
                target="RunCmd",
                time_scope="HISTORICAL",
            )
        ]
    )
    historical_questions: list[str] = []

    async def fake_historical(self, question: str) -> LiveAssistantReply:
        historical_questions.append(question)
        return LiveAssistantReply(
            question=question,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text="historical-result",
            target_output="RunCmd",
        )

    async def forbidden_parent(self, question: str) -> LiveAssistantReply:
        raise AssertionError(f"current-state parent dispatch must not run: {question}")

    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "connected",
        property(lambda self: True),
    )
    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "_historical_reply",
        fake_historical,
    )
    monkeypatch.setattr(
        RecursiveLiveCommissioningAssistant,
        "answer",
        forbidden_parent,
    )

    assistant = object.__new__(SemanticLiveCommissioningAssistant)
    assistant.loaded = SimpleNamespace(context=_BaseContext())
    assistant.provider = provider
    assistant._last_target = None
    assistant.reconciliation = object()

    original = "Why did RunCmd stop 30 seconds ago - does the system have any faults?"
    reply = asyncio.run(assistant.answer(original))

    assert historical_questions == ["Why did RunCmd stop 30 seconds ago?"]
    assert "system have any faults" not in historical_questions[0]
    assert reply.question == original
    assert reply.text == "historical-result"


def test_historical_follow_up_preserves_historical_scope(monkeypatch) -> None:
    provider = ScriptedFakeProvider(
        [_response("FOLLOW_UP", target=None, time_scope="HISTORICAL")]
    )
    historical_questions: list[str] = []

    async def fake_historical(self, question: str) -> LiveAssistantReply:
        historical_questions.append(question)
        return LiveAssistantReply(
            question=question,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text="historical-follow-up",
            target_output="RunCmd",
        )

    async def forbidden_parent(self, question: str) -> LiveAssistantReply:
        raise AssertionError(f"current-state parent dispatch must not run: {question}")

    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "connected",
        property(lambda self: True),
    )
    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "_historical_reply",
        fake_historical,
    )
    monkeypatch.setattr(
        RecursiveLiveCommissioningAssistant,
        "answer",
        forbidden_parent,
    )

    assistant = object.__new__(SemanticLiveCommissioningAssistant)
    assistant.loaded = SimpleNamespace(context=_BaseContext())
    assistant.provider = provider
    assistant._last_target = "RunCmd"
    assistant.reconciliation = object()

    reply = asyncio.run(assistant.answer("why?"))

    assert historical_questions == ["Why did RunCmd change earlier?"]
    assert reply.question == "why?"
    assert reply.text == "historical-follow-up"


def test_historical_validated_target_cannot_be_overridden_by_other_signal_name(monkeypatch) -> None:
    provider = ScriptedFakeProvider(
        [
            _response(
                "HISTORICAL_ROOT_CAUSE",
                target="RunCmd",
                time_scope="HISTORICAL",
            )
        ]
    )
    historical_questions: list[str] = []

    async def fake_historical(self, question: str) -> LiveAssistantReply:
        historical_questions.append(question)
        return LiveAssistantReply(
            question=question,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text="historical-run-cmd",
            target_output="RunCmd",
        )

    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "connected",
        property(lambda self: True),
    )
    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "_historical_reply",
        fake_historical,
    )

    assistant = object.__new__(SemanticLiveCommissioningAssistant)
    assistant.loaded = SimpleNamespace(context=_BaseContext())
    assistant.provider = provider
    assistant._last_target = None
    assistant.reconciliation = object()

    original = "Why did RunCmd stop 45 seconds ago after DriveReady changed?"
    reply = asyncio.run(assistant.answer(original))

    assert historical_questions == ["Why did RunCmd stop 45 seconds ago?"]
    assert "DriveReady" not in historical_questions[0]
    assert reply.target_output == "RunCmd"
