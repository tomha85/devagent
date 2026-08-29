from __future__ import annotations

import inspect

from devagent.live.advanced_assistant import (
    LiveAdvancedDiagnosisStatus,
    diagnose_numeric_comparison,
)
from devagent.live.advanced_semantics import LiveNumericComparison, LiveNumericOperand
from devagent.live.diagnosis import LiveObservedTag
from devagent.live import recursive_assistant


def _obs(tag_id: str, name: str, value):
    return LiveObservedTag(
        tag_id=tag_id,
        tag_name=name,
        node_id=f"ns=2;s={name}",
        value=value,
        evidence_id=f"E:{tag_id}",
        definitive_current=True,
        mapping_status="MAPPED",
    )


def test_rll_comparator_truth_does_not_claim_output_conflict():
    comparison = LiveNumericComparison(
        id="RLL",
        result_tag="Overspeed",
        left=LiveNumericOperand(reference="Speed"),
        operator=">",
        right=LiveNumericOperand(reference="SpeedLimit"),
        source_locator="PLC1/Main/Rung 10",
        semantic_state="FULL",
        origin="RUNG_COMPARISON",
    )
    diagnosis = diagnose_numeric_comparison(
        comparison,
        {
            "speed": _obs("S", "Speed", 130.0),
            "speedlimit": _obs("L", "SpeedLimit", 120.0),
            "overspeed": _obs("O", "Overspeed", False),
        },
    )
    assert diagnosis.status is LiveAdvancedDiagnosisStatus.CONDITION_TRUE
    assert diagnosis.status is not LiveAdvancedDiagnosisStatus.LOGIC_CONFLICT
    assert any("one rung condition" in item for item in diagnosis.limitations)


def test_direct_assignment_result_mismatch_is_indeterminate_without_atomic_unique_writer_proof():
    comparison = LiveNumericComparison(
        id="ST",
        result_tag="Overspeed",
        left=LiveNumericOperand(reference="Speed"),
        operator=">",
        right=LiveNumericOperand(reference="SpeedLimit"),
        source_locator="PLC1/Main/Line 10",
        semantic_state="FULL",
        origin="STATEMENT_ASSIGNMENT",
    )
    diagnosis = diagnose_numeric_comparison(
        comparison,
        {
            "speed": _obs("S", "Speed", 130.0),
            "speedlimit": _obs("L", "SpeedLimit", 120.0),
            "overspeed": _obs("O", "Overspeed", False),
        },
    )
    assert diagnosis.status is LiveAdvancedDiagnosisStatus.INDETERMINATE
    assert any("not a proven atomic PLC scan" in item for item in diagnosis.limitations)


def test_recursive_assistant_passes_canonical_context_to_advanced_observation_map_and_resolver():
    source = inspect.getsource(recursive_assistant.RecursiveLiveCommissioningAssistant._advanced_reply)
    assert "resolve_advanced_target(self.advanced_coverage, text, context=self.context)" in source
    assert "advanced_observation_map(self.context, reconciled)" in source


def test_historical_advanced_question_routes_to_explicit_past_event_limitation():
    historical_source = inspect.getsource(recursive_assistant.RecursiveLiveCommissioningAssistant._historical_reply)
    advanced_source = inspect.getsource(recursive_assistant.RecursiveLiveCommissioningAssistant._advanced_reply)
    assert "resolve_advanced_target(self.advanced_coverage, text, context=self.context).found" in historical_source
    assert "is_historical_question(text)" in advanced_source
    assert "will not substitute present OPC UA state" in advanced_source
