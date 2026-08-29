from __future__ import annotations

from types import SimpleNamespace

from devagent.live.advanced_assistant import LiveAdvancedDiagnosisStatus, diagnose_numeric_comparison
from devagent.live.advanced_semantics import build_live_advanced_coverage
from devagent.live.diagnosis import LiveObservedTag
from devagent.live.engineering_context import LiveEngineeringContext, LiveEngineeringTag


def _tag(tag_id: str, name: str, dtype: str):
    return LiveEngineeringTag(tag_id, name, "Controller", dtype, None, "Read Only", None)


def _obs(tag_id: str, name: str, value):
    return LiveObservedTag(tag_id, name, None, value, f"E:{tag_id}", True, "MAPPED")


def test_rll_grt_threshold_with_single_output_writer_is_evaluated():
    context = LiveEngineeringContext(
        vendor="ROCKWELL",
        engineering_tool="Studio 5000",
        controller_name="PLC1",
        source_path="test.L5X",
        source_sha256="abc",
        full_project=True,
        tags=(
            _tag("S", "LineSpeed", "REAL"),
            _tag("L", "MaxSpeed", "REAL"),
            _tag("O", "Overspeed", "BOOL"),
        ),
        rules=(),
        statements=(),
        limitations=(),
    )
    rung = SimpleNamespace(
        id="R1",
        text="GRT(LineSpeed,MaxSpeed)OTE(Overspeed);",
        writes=("Overspeed",),
        instructions=(),
        source=SimpleNamespace(locator="PLC1/Main/Rung 10"),
    )
    project = SimpleNamespace(rungs=[rung], logic_statements=[], aois=[], data_types=[])
    coverage = build_live_advanced_coverage(project, context)

    assert len(coverage.numeric_comparisons) == 1
    item = coverage.numeric_comparisons[0]
    assert item.result_tag == "Overspeed"
    diagnosis = diagnose_numeric_comparison(
        item,
        {
            "linespeed": _obs("S", "LineSpeed", 130.0),
            "maxspeed": _obs("L", "MaxSpeed", 120.0),
            "overspeed": _obs("O", "Overspeed", True),
        },
    )
    assert diagnosis.status is LiveAdvancedDiagnosisStatus.CONDITION_TRUE


def test_numeric_mode_comparison_is_supported_without_treating_mode_as_boolean():
    from devagent.live.advanced_semantics import LiveNumericComparison, LiveNumericOperand

    comparison = LiveNumericComparison(
        id="MODE",
        result_tag="AutoModeActive",
        left=LiveNumericOperand(reference="Mode"),
        operator="==",
        right=LiveNumericOperand(literal=2),
        source_locator="PLC1/Main/Line 4",
        semantic_state="FULL",
        origin="STATEMENT_ASSIGNMENT",
    )
    diagnosis = diagnose_numeric_comparison(
        comparison,
        {
            "mode": _obs("M", "Mode", 2),
            "automodeactive": _obs("A", "AutoModeActive", True),
        },
    )
    assert diagnosis.status is LiveAdvancedDiagnosisStatus.CONDITION_TRUE
