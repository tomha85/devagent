from __future__ import annotations

import asyncio
from types import SimpleNamespace

from devagent.live.assistant import LiveAssistantReply, LiveAssistantReplyKind
from devagent.live.recursive_assistant import RecursiveLiveCommissioningAssistant
from devagent.live.semantic_assistant import (
    SemanticLiveCommissioningAssistant,
    _historical_metadata_for_target,
)
from devagent.live.semantic_intent import (
    LiveSemanticIntent,
    LiveSemanticRoute,
    LiveSemanticTimeScope,
    resolve_semantic_intent,
)
from devagent.providers import ScriptedFakeProvider


class _BaseContext:
    vendor = "ROCKWELL"
    controller_name = "WarehouseCommissioningDemo"

    def __init__(self) -> None:
        self.tags = (
            SimpleNamespace(
                id="run",
                name="RunCmd",
                scoped_name="RunCmd",
                description="Conveyor run command",
                data_type="BOOL",
                scope="Controller",
            ),
            SimpleNamespace(
                id="drive-ready",
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
        return ("Ready",)


class _OverlappingTargetContext:
    vendor = "ROCKWELL"
    controller_name = "OverlapDemo"

    def __init__(self) -> None:
        self.ready = SimpleNamespace(
            id="ready",
            name="Ready",
            scoped_name="Ready",
            description="Modeled ready output",
            data_type="BOOL",
            scope="Controller",
        )
        self.drive_ready = SimpleNamespace(
            id="drive-ready",
            name="DriveReady",
            scoped_name="DriveReady",
            description="Drive ready feedback",
            data_type="BOOL",
            scope="Controller",
        )
        self.run_cmd = SimpleNamespace(
            id="run-cmd",
            name="RunCmd",
            scoped_name="RunCmd",
            description="Run command",
            data_type="BOOL",
            scope="Controller",
        )
        self.tags = (self.ready, self.drive_ready, self.run_cmd)

    def output_names(self) -> tuple[str, ...]:
        return ("Ready", "RunCmd")

    def unique_tag_for_reference(self, reference: str):
        matches = [tag for tag in self.tags if reference in {tag.name, tag.scoped_name}]
        return matches[0] if len(matches) == 1 else None

    def rules_for_output(self, reference: str):
        if reference in {"Ready", "RunCmd"}:
            return (SimpleNamespace(output_tag=reference),)
        return ()


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


def _route(
    intent: LiveSemanticIntent,
    *,
    target: str | None,
    time_scope: LiveSemanticTimeScope,
) -> LiveSemanticRoute:
    return LiveSemanticRoute(
        intent=intent,
        target=target,
        time_scope=time_scope,
        confidence=0.99,
        reason="test",
    )


def test_ambiguous_unqualified_program_tag_is_neither_exposed_nor_accepted() -> None:
    provider = ScriptedFakeProvider([_response("ROOT_CAUSE", target="Ready")])

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
    provider = ScriptedFakeProvider([_response("TAG_STATUS", target="RunCmd")])
    dispatched: list[tuple[str, str | None]] = []

    async def fake_dispatch(self, question: str, route: LiveSemanticRoute) -> LiveAssistantReply:
        dispatched.append((question, route.target))
        return LiveAssistantReply(
            question=question,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text="target-status",
            target_output=route.target,
        )

    async def forbidden_parent(self, question: str) -> LiveAssistantReply:
        raise AssertionError(f"generic parent dispatch must not run: {question}")

    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "_dispatch_current_target",
        fake_dispatch,
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

    original = "Is RunCmd false, and does the system have any faults?"
    reply = asyncio.run(assistant.answer(original))

    assert dispatched == [(original, "RunCmd")]
    assert reply.target_output == "RunCmd"


def test_current_overlapping_non_output_target_is_authoritative(monkeypatch) -> None:
    context = _OverlappingTargetContext()
    captured: list[str] = []

    async def fake_direct(self, question: str, tag) -> LiveAssistantReply:
        captured.append(tag.scoped_name)
        return LiveAssistantReply(
            question=question,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text="direct-signal",
            target_output=tag.scoped_name,
        )

    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "connected",
        property(lambda self: True),
    )
    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "_direct_signal_reply",
        fake_direct,
    )

    assistant = object.__new__(SemanticLiveCommissioningAssistant)
    assistant.loaded = SimpleNamespace(context=context)
    assistant.provider = ScriptedFakeProvider([])
    assistant._last_target = None
    assistant.reconciliation = object()

    route = _route(
        LiveSemanticIntent.TAG_STATUS,
        target="DriveReady",
        time_scope=LiveSemanticTimeScope.CURRENT,
    )
    reply = asyncio.run(assistant._dispatch_current_target("what is DriveReady?", route))

    assert captured == ["DriveReady"]
    assert reply.target_output == "DriveReady"


def test_historical_route_bypasses_current_system_health_preemption(monkeypatch) -> None:
    provider = ScriptedFakeProvider(
        [_response("HISTORICAL_ROOT_CAUSE", target="RunCmd", time_scope="HISTORICAL")]
    )
    dispatched: list[tuple[str, str | None]] = []

    async def fake_historical_dispatch(
        self,
        question: str,
        route: LiveSemanticRoute,
    ) -> LiveAssistantReply:
        dispatched.append((question, route.target))
        return LiveAssistantReply(
            question=question,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text="historical-result",
            target_output=route.target,
        )

    async def forbidden_parent(self, question: str) -> LiveAssistantReply:
        raise AssertionError(f"current-state parent dispatch must not run: {question}")

    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "_dispatch_historical_route",
        fake_historical_dispatch,
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

    original = "Why did RunCmd stop 30 seconds ago - does the system have any faults?"
    reply = asyncio.run(assistant.answer(original))

    assert dispatched == [(original, "RunCmd")]
    assert reply.target_output == "RunCmd"


def test_historical_follow_up_preserves_historical_scope(monkeypatch) -> None:
    provider = ScriptedFakeProvider(
        [_response("FOLLOW_UP", target=None, time_scope="HISTORICAL")]
    )
    dispatched: list[tuple[str, str | None, LiveSemanticTimeScope]] = []

    async def fake_historical_dispatch(
        self,
        question: str,
        route: LiveSemanticRoute,
    ) -> LiveAssistantReply:
        dispatched.append((question, route.target, route.time_scope))
        return LiveAssistantReply(
            question=question,
            kind=LiveAssistantReplyKind.DIAGNOSIS,
            text="historical-follow-up",
            target_output=route.target,
        )

    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "_dispatch_historical_route",
        fake_historical_dispatch,
    )

    assistant = object.__new__(SemanticLiveCommissioningAssistant)
    assistant.loaded = SimpleNamespace(context=_BaseContext())
    assistant.provider = provider
    assistant._last_target = "RunCmd"

    reply = asyncio.run(assistant.answer("why?"))

    assert dispatched == [("why?", "RunCmd", LiveSemanticTimeScope.HISTORICAL)]
    assert reply.target_output == "RunCmd"


def test_historical_metadata_is_bound_to_validated_target_clause() -> None:
    metadata = _historical_metadata_for_target(
        "Why did RunCmd start 45 seconds ago after DriveReady stopped?",
        "RunCmd",
    )

    assert metadata is not None
    assert metadata.direction == "START"
    assert metadata.target_age_seconds == 45.0
    assert metadata.lookback_seconds == 45.0


def test_implicit_historical_target_with_multiple_events_fails_closed() -> None:
    metadata = _historical_metadata_for_target(
        "it started 45 seconds ago after DriveReady stopped",
        "RunCmd",
    )

    assert metadata is None


def test_historical_overlapping_non_output_target_returns_exact_limitation(monkeypatch) -> None:
    context = _OverlappingTargetContext()

    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "connected",
        property(lambda self: True),
    )

    assistant = object.__new__(SemanticLiveCommissioningAssistant)
    assistant.loaded = SimpleNamespace(context=context)
    assistant.provider = ScriptedFakeProvider([])
    assistant._last_target = None
    assistant.reconciliation = object()

    route = _route(
        LiveSemanticIntent.HISTORICAL_ROOT_CAUSE,
        target="DriveReady",
        time_scope=LiveSemanticTimeScope.HISTORICAL,
    )
    reply = asyncio.run(
        assistant._dispatch_historical_route(
            "Why did DriveReady stop 20 seconds ago?",
            route,
        )
    )

    assert reply.kind is LiveAssistantReplyKind.LIMITATION
    assert reply.target_output == "DriveReady"
    assert "exact non-output signal DriveReady" in reply.text
    assert "substitute another output" in reply.text


def test_historical_output_dispatch_uses_exact_target_and_bound_metadata(monkeypatch) -> None:
    context = _OverlappingTargetContext()
    calls: list[dict[str, object]] = []

    class _Diagnosis:
        def render_text(self) -> str:
            return "historical-run-cmd"

    class _Store:
        def diagnose_recent_transition(self, context_arg, target, **kwargs):
            calls.append({"target": target, **kwargs})
            return _Diagnosis()

    monkeypatch.setattr(
        SemanticLiveCommissioningAssistant,
        "connected",
        property(lambda self: True),
    )
    monkeypatch.setattr(
        "devagent.live.semantic_assistant.required_tag_ids_for_recursive_output",
        lambda *args, **kwargs: (),
    )

    assistant = object.__new__(SemanticLiveCommissioningAssistant)
    assistant.loaded = SimpleNamespace(context=context)
    assistant.provider = ScriptedFakeProvider([])
    assistant._last_target = None
    assistant.reconciliation = object()
    assistant.trace_max_depth = 6
    assistant.trace_max_nodes = 64
    assistant.history_seconds = 300.0
    assistant.history_collector = SimpleNamespace(store=_Store())

    route = _route(
        LiveSemanticIntent.HISTORICAL_ROOT_CAUSE,
        target="RunCmd",
        time_scope=LiveSemanticTimeScope.HISTORICAL,
    )
    reply = asyncio.run(
        assistant._dispatch_historical_route(
            "Why did RunCmd start 45 seconds ago after DriveReady stopped?",
            route,
        )
    )

    assert reply.target_output == "RunCmd"
    assert len(calls) == 1
    assert calls[0]["target"] == "RunCmd"
    assert calls[0]["direction"] == "START"
    assert calls[0]["target_age_seconds"] == 45.0
