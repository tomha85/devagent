from __future__ import annotations

from types import SimpleNamespace

from devagent.live.semantic_assistant import _bridge_question
from devagent.live.semantic_intent import (
    LiveSemanticIntent,
    LiveSemanticRoute,
    LiveSemanticTimeScope,
    resolve_semantic_intent,
)
from devagent.providers import ScriptedFakeProvider


class _Context:
    def __init__(self) -> None:
        self.tags = (
            SimpleNamespace(name="RunCmd", scoped_name="RunCmd"),
            SimpleNamespace(name="DriveFault", scoped_name="DriveFault"),
            SimpleNamespace(name="DriveReady", scoped_name="DriveReady"),
            SimpleNamespace(name="MachineState", scoped_name="MachineState"),
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

    assert _bridge_question("is system good?", health) == "Does the system have any faults?"
    bridged = _bridge_question("why won't it go?", root)
    assert "why won't it go?" in bridged
    assert "Exact engineering target: RunCmd" in bridged


def test_provider_payload_contains_bounded_known_targets_not_runtime_facts() -> None:
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
    assert "runtime_values" not in payload
    assert "evidence" not in payload
