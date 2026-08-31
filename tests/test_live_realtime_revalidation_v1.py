from __future__ import annotations

import asyncio
from types import SimpleNamespace

import devagent.live.realtime_assistant as realtime_assistant_module
from devagent.live.assistant import LiveAssistantReply, LiveAssistantReplyKind
from devagent.live.diagnosis import (
    LiveCommissioningDiagnosis,
    LiveConditionEvaluation,
    LiveConditionState,
    LiveDiagnosisStatus,
)
from devagent.live.manager import PlcSessionState
from devagent.live.realtime_assistant import (
    RealtimeSemanticLiveCommissioningAssistant,
    _diagnosis_signature,
)
from devagent.live.semantic_assistant import SemanticLiveCommissioningAssistant


def _diagnosis(*, observed_output: bool, blocker_value: bool) -> LiveCommissioningDiagnosis:
    blocker = LiveConditionEvaluation(
        tag_reference="DownstreamReady",
        tag_id="tag-downstream",
        tag_name="DownstreamReady",
        required=True,
        observed_value=blocker_value,
        state=(
            LiveConditionState.SATISFIED
            if blocker_value
            else LiveConditionState.BLOCKING
        ),
        evidence_id="LIVE:1",
        detail="test",
    )
    return LiveCommissioningDiagnosis(
        target_output="RunCmd",
        status=(
            LiveDiagnosisStatus.CONDITIONS_SATISFIED
            if observed_output
            else LiveDiagnosisStatus.BLOCKER_IDENTIFIED
        ),
        expected_output=True,
        observed_output=observed_output,
        rule_ids=("rule-1",),
        source_locators=("test:1",),
        paths=(),
        blockers=(() if blocker_value else (blocker,)),
        evidence_ids=("LIVE:1",),
        limitations=(),
        summary="test",
        next_checks=(),
    )


def test_diagnosis_signature_changes_when_current_truth_changes() -> None:
    old = _diagnosis(observed_output=False, blocker_value=False)
    new = _diagnosis(observed_output=True, blocker_value=True)
    assert _diagnosis_signature(old) != _diagnosis_signature(new)


def test_answer_replaces_stale_current_reply_after_threshold(monkeypatch) -> None:
    old = _diagnosis(observed_output=False, blocker_value=False)
    original = LiveAssistantReply(
        question="why is RunCmd false?",
        kind=LiveAssistantReplyKind.DIAGNOSIS,
        text="old ai wording",
        target_output="RunCmd",
        diagnosis=old,
    )
    refreshed = LiveAssistantReply(
        question=original.question,
        kind=LiveAssistantReplyKind.DIAGNOSIS,
        text="refreshed deterministic result",
        target_output="RunCmd",
        diagnosis=_diagnosis(observed_output=True, blocker_value=True),
    )

    async def fake_parent(self, question: str):
        return original

    async def fake_refresh(self, question: str, diagnosis):
        return refreshed

    monkeypatch.setattr(SemanticLiveCommissioningAssistant, "answer", fake_parent)
    monkeypatch.setattr(
        RealtimeSemanticLiveCommissioningAssistant,
        "_refresh_current_diagnosis",
        fake_refresh,
    )

    assistant = object.__new__(RealtimeSemanticLiveCommissioningAssistant)
    assistant.final_revalidation_after_seconds = 0.0
    reply = asyncio.run(assistant.answer(original.question))
    assert reply.text == "refreshed deterministic result"
    assert reply.diagnosis is refreshed.diagnosis


def test_slow_answer_fails_closed_when_final_revalidation_loses_session(
    monkeypatch,
) -> None:
    original = LiveAssistantReply(
        question="why is RunCmd false?",
        kind=LiveAssistantReplyKind.DIAGNOSIS,
        text="old current-state ai wording",
        target_output="RunCmd",
        diagnosis=_diagnosis(observed_output=False, blocker_value=False),
    )

    async def fake_parent(self, question: str):
        return original

    monkeypatch.setattr(SemanticLiveCommissioningAssistant, "answer", fake_parent)

    assistant = object.__new__(RealtimeSemanticLiveCommissioningAssistant)
    assistant.final_revalidation_after_seconds = 0.0
    assistant.reconciliation = object()
    assistant.connection = SimpleNamespace(plc_id="plc1")
    assistant.manager = SimpleNamespace(
        status=lambda plc_id: SimpleNamespace(
            connected=False,
            state=PlcSessionState.RECONNECTING,
        )
    )

    reply = asyncio.run(assistant.answer(original.question))

    assert reply.kind is LiveAssistantReplyKind.LIMITATION
    assert reply.diagnosis is not None
    assert reply.diagnosis.status is LiveDiagnosisStatus.INDETERMINATE
    assert reply.diagnosis.observed_output is None
    assert reply.diagnosis.evidence_ids == ()
    assert "CURRENT STATE NOT REVALIDATED" in reply.text
    assert "RECONNECTING" in reply.text
    assert "old current-state ai wording" not in reply.text


def test_revalidation_rebuilds_recursive_trace_when_direct_diagnosis_is_unchanged(
    monkeypatch,
) -> None:
    original = _diagnosis(observed_output=False, blocker_value=False)

    assistant = object.__new__(RealtimeSemanticLiveCommissioningAssistant)
    assistant.reconciliation = object()
    assistant.connection = SimpleNamespace(plc_id="plc1")
    assistant.manager = SimpleNamespace(
        status=lambda plc_id: SimpleNamespace(
            connected=True,
            state=PlcSessionState.CONNECTED,
        )
    )
    assistant.context = SimpleNamespace(
        rules_for_output=lambda target: (SimpleNamespace(id="rule-1"),)
    )
    assistant.trace_max_depth = 6
    assistant.trace_max_nodes = 64

    monkeypatch.setattr(
        realtime_assistant_module,
        "required_tag_ids_for_recursive_output",
        lambda *args, **kwargs: ("tag-run", "tag-downstream", "tag-sensor"),
    )

    async def fake_evidence(*args, **kwargs):
        return object()

    monkeypatch.setattr(
        realtime_assistant_module,
        "build_reconciled_live_agent_evidence",
        fake_evidence,
    )
    monkeypatch.setattr(
        realtime_assistant_module,
        "observations_from_reconciled",
        lambda value: (SimpleNamespace(tag_id="tag-sensor", value=True),),
    )
    monkeypatch.setattr(
        realtime_assistant_module,
        "diagnose_output",
        lambda *args, **kwargs: original,
    )
    monkeypatch.setattr(
        realtime_assistant_module,
        "answer_commissioning_question",
        lambda *args, **kwargs: SimpleNamespace(
            render_text=lambda: "fresh direct diagnosis"
        ),
    )
    monkeypatch.setattr(
        realtime_assistant_module,
        "trace_recursive_diagnosis",
        lambda *args, **kwargs: SimpleNamespace(
            roots=(SimpleNamespace(signal="SensorB"),),
            limitations=(),
            render_text=lambda: "FRESH RECURSIVE ROOT: SensorB",
        ),
    )

    reply = asyncio.run(
        assistant._refresh_current_diagnosis("why is RunCmd false?", original)
    )

    assert reply.kind is LiveAssistantReplyKind.DIAGNOSIS
    assert reply.diagnosis is original
    assert "CURRENT STATE REVALIDATED" in reply.text
    assert "FRESH RECURSIVE ROOT: SensorB" in reply.text
    assert "fresh direct diagnosis" in reply.text


def test_answer_keeps_fast_reply_without_extra_revalidation(monkeypatch) -> None:
    original = LiveAssistantReply(
        question="why is RunCmd false?",
        kind=LiveAssistantReplyKind.DIAGNOSIS,
        text="fast answer",
        target_output="RunCmd",
        diagnosis=_diagnosis(observed_output=False, blocker_value=False),
    )
    calls: list[str] = []

    async def fake_parent(self, question: str):
        return original

    async def forbidden_refresh(self, question: str, diagnosis):
        calls.append(question)
        return None

    monkeypatch.setattr(SemanticLiveCommissioningAssistant, "answer", fake_parent)
    monkeypatch.setattr(
        RealtimeSemanticLiveCommissioningAssistant,
        "_refresh_current_diagnosis",
        forbidden_refresh,
    )

    assistant = object.__new__(RealtimeSemanticLiveCommissioningAssistant)
    assistant.final_revalidation_after_seconds = 60.0
    reply = asyncio.run(assistant.answer(original.question))
    assert reply is original
    assert calls == []


def test_non_diagnosis_reply_is_never_revalidated(monkeypatch) -> None:
    original = LiveAssistantReply(
        question="what is this system?",
        kind=LiveAssistantReplyKind.SYSTEM_OVERVIEW,
        text="overview",
    )

    async def fake_parent(self, question: str):
        return original

    monkeypatch.setattr(SemanticLiveCommissioningAssistant, "answer", fake_parent)
    assistant = object.__new__(RealtimeSemanticLiveCommissioningAssistant)
    assistant.final_revalidation_after_seconds = 0.0
    reply = asyncio.run(assistant.answer(original.question))
    assert reply is original
