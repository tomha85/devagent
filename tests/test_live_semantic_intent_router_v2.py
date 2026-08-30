from __future__ import annotations

import asyncio
from types import SimpleNamespace

from devagent.live.assistant import LiveAssistantReply, LiveAssistantReplyKind
from devagent.live.recursive_assistant import RecursiveLiveCommissioningAssistant
from devagent.live.semantic_assistant import SemanticLiveCommissioningAssistant, _bridge_question
from devagent.live.semantic_intent import (
    LiveSemanticIntent,
    LiveSemanticRoute,
    LiveSemanticTimeScope,
    resolve_semantic_intent,
)
from devagent.providers import ScriptedFakeProvider


class _Context:
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
                name="DriveFault",
                scoped_name="DriveFault",
                description="Drive fault active",
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
            SimpleNamespace(
                name="MachineState",
                scoped_name="MachineState",
                description="High-level machine operating state",
                data_type="STRING",
                scope="Controller",
            ),
        )

    def output_names(self) -> tuple[str, ...]:
        return ("RunCmd",)


def _route_response(
    intent: str,
    *,
    target=None,
    time_scope: str = "CURRENT",
    confidence: float = 0.98,
):
    return {
        "intent": intent,
        "target": target,
        "time_scope": time_scope,
        "confidence": confidence,
        "reason": "Semantic interpretation only; no PLC fact asserted.",
    }


def test_free_form_health_wording_is_decided_by_provider_not_phrase_table() -> None:
    questions = (
        "is system good?",
        "how are we looking?",
        "anything I should worry about?",
        "everything normal on this line?",
        "co van de gi khong?",
    )
    provider = ScriptedFakeProvider(
        [_route_response("SYSTEM_HEALTH") for _ in questions]
    )
    context = _Context()

    for question in questions:
        route = resolve_semantic_intent(question, context, provider)
        assert route is not None
        assert route.intent is LiveSemanticIntent.SYSTEM_HEALTH
        assert route.target is None

    assert len(provider.calls) == len(questions)
    assert all(call["role"] == "live_semantic_intent_router" for call in provider.calls)


def test_router_resolves_exact_known_engineering_target() -> None:
    provider = ScriptedFakeProvider(
        [_route_response("ROOT_CAUSE", target="RunCmd")]
    )

    route = resolve_semantic_intent(
        "the conveyor refuses to go, what is holding it up?",
        _Context(),
        provider,
    )

    assert route is not None
    assert route.intent is LiveSemanticIntent.ROOT_CAUSE
    assert route.target == "RunCmd"


def test_router_rejects_model_invented_target_and_fails_closed() -> None:
    provider = ScriptedFakeProvider(
        [_route_response("ROOT_CAUSE", target="InventedMotorReady123")]
    )

    route = resolve_semantic_intent(
        "why won't it start?",
        _Context(),
        provider,
    )

    assert route is None


def test_router_rejects_low_confidence_instead_of_guessing() -> None:
    provider = ScriptedFakeProvider(
        [_route_response("SYSTEM_HEALTH", confidence=0.40)]
    )

    route = resolve_semantic_intent(
        "things seem odd",
        _Context(),
        provider,
    )

    assert route is None


def test_follow_up_can_reuse_only_a_known_previous_target() -> None:
    provider = ScriptedFakeProvider(
        [_route_response("FOLLOW_UP", target=None)]
    )

    route = resolve_semantic_intent(
        "why?",
        _Context(),
        provider,
        previous_target="DriveFault",
    )

    assert route is not None
    assert route.intent is LiveSemanticIntent.FOLLOW_UP
    assert route.target == "DriveFault"


def test_follow_up_does_not_reuse_unknown_previous_target() -> None:
    provider = ScriptedFakeProvider(
        [_route_response("FOLLOW_UP", target=None)]
    )

    route = resolve_semantic_intent(
        "why?",
        _Context(),
        provider,
        previous_target="NotInEngineeringModel",
    )

    assert route is None


def test_historical_intent_requires_historical_time_scope() -> None:
    provider = ScriptedFakeProvider(
        [_route_response("HISTORICAL_ROOT_CAUSE", target="RunCmd", time_scope="CURRENT")]
    )

    route = resolve_semantic_intent(
        "why did the conveyor quit earlier?",
        _Context(),
        provider,
    )

    assert route is None


def test_semantic_bridge_only_supplies_intent_or_validated_target_to_deterministic_engine() -> None:
    health = LiveSemanticRoute(
        intent=LiveSemanticIntent.SYSTEM_HEALTH,
        target=None,
        time_scope=LiveSemanticTimeScope.CURRENT,
        confidence=0.99,
        reason="health",
    )
    root = LiveSemanticRoute(
        intent=LiveSemanticIntent.ROOT_CAUSE,
        target="RunCmd",
        time_scope=LiveSemanticTimeScope.CURRENT,
        confidence=0.99,
        reason="root cause",
    )
    historical = LiveSemanticRoute(
        intent=LiveSemanticIntent.HISTORICAL_ROOT_CAUSE,
        target="RunCmd",
        time_scope=LiveSemanticTimeScope.HISTORICAL,
        confidence=0.99,
        reason="history",
    )

    assert _bridge_question("is system good?", health) == "Does the system have any faults?"
    bridged = _bridge_question("why won't it go and does the system have any faults?", root)
    assert bridged == "Why is RunCmd in its current state?"
    assert "system have any faults" not in bridged

    historical_bridge = _bridge_question("it stopped about 90 seconds back, what caused that?", historical)
    assert historical_bridge == "Why did RunCmd stop 90 seconds ago?"


def test_provider_payload_contains_static_engineering_hints_but_not_runtime_facts() -> None:
    provider = ScriptedFakeProvider(
        [_route_response("TAG_STATUS", target="DriveFault")]
    )

    route = resolve_semantic_intent(
        "what's up with the drive fault bit?",
        _Context(),
        provider,
    )

    assert route is not None
    payload = provider.calls[0]["payload"]
    assert "RunCmd" in payload["known_outputs"]
    assert "DriveFault" in payload["known_targets"]
    assert payload["engineering_context"]["controller_name"] == "WarehouseCommissioningDemo"
    hints = {item["target"]: item for item in payload["engineering_target_hints"]}
    assert hints["DriveFault"]["description"] == "Drive fault active"
    assert "runtime_values" not in payload
    assert "evidence" not in payload


def test_semantic_assistant_routes_free_form_question_before_deterministic_parent(monkeypatch) -> None:
    provider = ScriptedFakeProvider([_route_response("SYSTEM_HEALTH")])
    parent_questions: list[str] = []

    async def fake_parent_answer(self, question: str) -> LiveAssistantReply:
        parent_questions.append(question)
        return LiveAssistantReply(
            question=question,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text="deterministic-result",
        )

    monkeypatch.setattr(RecursiveLiveCommissioningAssistant, "answer", fake_parent_answer)
    assistant = object.__new__(SemanticLiveCommissioningAssistant)
    assistant.loaded = SimpleNamespace(context=_Context())
    assistant.provider = provider
    assistant._last_target = None

    reply = asyncio.run(assistant.answer("is system good?"))

    assert len(provider.calls) == 1
    assert parent_questions == ["Does the system have any faults?"]
    assert reply.question == "is system good?"
    assert reply.text == "deterministic-result"


def test_control_request_never_reaches_semantic_provider(monkeypatch) -> None:
    provider = ScriptedFakeProvider([])
    parent_questions: list[str] = []

    async def fake_parent_answer(self, question: str) -> LiveAssistantReply:
        parent_questions.append(question)
        return LiveAssistantReply(
            question=question,
            kind=LiveAssistantReplyKind.LIMITATION,
            text="read-only refusal",
        )

    monkeypatch.setattr(RecursiveLiveCommissioningAssistant, "answer", fake_parent_answer)
    assistant = object.__new__(SemanticLiveCommissioningAssistant)
    assistant.loaded = SimpleNamespace(context=_Context())
    assistant.provider = provider
    assistant._last_target = None

    reply = asyncio.run(assistant.answer("force RunCmd true"))

    assert provider.calls == []
    assert parent_questions == ["force RunCmd true"]
    assert reply.text == "read-only refusal"
