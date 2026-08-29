from __future__ import annotations

from types import SimpleNamespace

from devagent.live.advanced_assistant import LiveAdvancedDiagnosisStatus, diagnose_advanced_model
from devagent.live.advanced_semantics import LiveAdvancedKind, build_live_advanced_coverage
from devagent.live.diagnosis import LiveObservedTag
from devagent.live.engineering_context import LiveEngineeringContext, LiveEngineeringTag, LiveLogicStatement


def _tag(tag_id: str, name: str, dtype: str = "BOOL"):
    return LiveEngineeringTag(tag_id, name, "Controller", dtype, None, "Read Only", None)


def _obs(tag_id: str, name: str, value, *, current: bool = True):
    return LiveObservedTag(
        tag_id=tag_id,
        tag_name=name,
        node_id=f"ns=2;s={name}",
        value=value,
        evidence_id=f"E:{tag_id}",
        definitive_current=current,
        mapping_status="MAPPED",
        limitation=None if current else "untrusted",
    )


def _context(*, tags=(), statements=()):
    return LiveEngineeringContext(
        vendor="TEST",
        engineering_tool="TEST",
        controller_name="PLC1",
        source_path="test",
        source_sha256="abc",
        full_project=True,
        tags=tuple(tags),
        rules=(),
        statements=tuple(statements),
        limitations=(),
    )


def _project(*, rungs=()):
    return SimpleNamespace(rungs=list(rungs), logic_statements=[], aois=[], data_types=[])


def test_compound_st_rhs_keeps_comparison_context_but_does_not_bind_result():
    statement = LiveLogicStatement(
        id="S1",
        language="ST",
        owner_type="PROGRAM",
        owner_name="Main",
        routine="Main",
        locator="PLC1/Main/Line1",
        text="HighPressure := Pressure > Limit OR Override;",
        reads=("Pressure", "Limit", "Override"),
        writes=("HighPressure",),
        calls=(),
        semantic_state="FULL",
        source_locator="PLC1/Main/Line1",
    )
    context = _context(
        tags=(
            _tag("P", "Pressure", "REAL"),
            _tag("L", "Limit", "REAL"),
            _tag("O", "Override"),
            _tag("H", "HighPressure"),
        ),
        statements=(statement,),
    )
    coverage = build_live_advanced_coverage(_project(), context)

    assert len(coverage.numeric_comparisons) == 1
    comparison = coverage.numeric_comparisons[0]
    assert comparison.result_tag is None
    assert comparison.origin == "STATEMENT_COMPARISON_CONTEXT"


def test_handshake_wait_requires_all_modeled_response_status_values_trusted():
    context = _context(
        tags=(
            _tag("R", "TransferReq"),
            _tag("A", "TransferAck"),
            _tag("D", "TransferDone"),
            _tag("F", "TransferFault"),
        )
    )
    coverage = build_live_advanced_coverage(_project(), context)
    model = next(item for item in coverage.models if item.kind is LiveAdvancedKind.HANDSHAKE)

    diagnosis = diagnose_advanced_model(
        context,
        coverage,
        model,
        {
            "transferreq": _obs("R", "TransferReq", True),
            "transferack": _obs("A", "TransferAck", False),
            # Done is unavailable; declaring WAITING_RESPONSE would be an overclaim.
            "transferfault": _obs("F", "TransferFault", False),
        },
    )
    assert diagnosis.status is LiveAdvancedDiagnosisStatus.INDETERMINATE
    assert "cannot prove" in diagnosis.summary


def test_motion_classifier_uses_exact_rockwell_mnemonics_and_namespaced_mc_calls():
    source = SimpleNamespace(locator="PLC1/Main/Rung 1")
    instructions = tuple(
        SimpleNamespace(name=name, arguments=("Axis1",))
        for name in ("MASD", "MC_MoveAbsolute", "MASTER_CONTROL", "MACHINE_JOG")
    )
    rung = SimpleNamespace(id="R1", text="", writes=(), instructions=instructions, source=source)
    context = _context(tags=(_tag("AX", "Axis1", "AXIS"),))
    coverage = build_live_advanced_coverage(_project(rungs=(rung,)), context)

    motion_names = {item.instruction for item in coverage.models if item.kind is LiveAdvancedKind.MOTION}
    assert "MASD" in motion_names
    assert "MC_MoveAbsolute" in motion_names
    assert "MASTER_CONTROL" not in motion_names
    assert "MACHINE_JOG" not in motion_names
