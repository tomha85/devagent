from __future__ import annotations

import asyncio
from types import SimpleNamespace

from devagent.live.assistant import LiveAssistantReply, LiveAssistantReplyKind
from devagent.live.diagnosis import (
    LiveCommissioningDiagnosis,
    LiveConditionEvaluation,
    LiveConditionState,
    LiveDiagnosisStatus,
)
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
