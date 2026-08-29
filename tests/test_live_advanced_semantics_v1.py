from __future__ import annotations

from types import SimpleNamespace

from devagent.live.advanced_assistant import (
    LiveAdvancedDiagnosisStatus,
    diagnose_advanced_model,
    diagnose_numeric_comparison,
    resolve_advanced_target,
)
from devagent.live.advanced_semantics import LiveAdvancedKind, build_live_advanced_coverage
from devagent.live.diagnosis import LiveObservedTag
from devagent.live.engineering_context import LiveEngineeringContext, LiveEngineeringTag, LiveLogicStatement


def _tag(tag_id: str, name: str, dtype: str = "BOOL", description: str | None = None):
    return LiveEngineeringTag(
        id=tag_id,
        name=name,
        scope="Controller",
        data_type=dtype,
        description=description,
        external_access="Read Only",
        alias_for=None,
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


def _statement(text: str, *, statement_id: str = "S1", calls=(), reads=(), writes=(), owner_type="PROGRAM", owner_name="Main"):
    return LiveLogicStatement(
        id=statement_id,
        language="ST",
        owner_type=owner_type,
        owner_name=owner_name,
        routine="Main",
        locator="PLC1/Main/Line1",
        text=text,
        reads=tuple(reads),
        writes=tuple(writes),
        calls=tuple(calls),
        semantic_state="FULL",
        source_locator="PLC1/Main/Line1",
    )


def _project(*, rungs=(), logic_statements=(), aois=(), data_types=()):
    return SimpleNamespace(
        rungs=list(rungs),
        logic_statements=list(logic_statements),
        aois=list(aois),
        data_types=list(data_types),
    )


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


def test_numeric_assignment_comparison_is_extracted_and_evaluated():
    stmt = _statement(
        "Overspeed := Speed > SpeedLimit;",
        reads=("Speed", "SpeedLimit"),
        writes=("Overspeed",),
    )
    context = _context(
        tags=(
            _tag("T1", "Speed", "REAL"),
            _tag("T2", "SpeedLimit", "REAL"),
            _tag("T3", "Overspeed"),
        ),
        statements=(stmt,),
    )
    coverage = build_live_advanced_coverage(_project(logic_statements=(SimpleNamespace(**stmt.__dict__),)), context)

    assert len(coverage.numeric_comparisons) == 1
    item = coverage.numeric_comparisons[0]
    assert item.result_tag == "Overspeed"
    assert item.operator == ">"

    diagnosis = diagnose_numeric_comparison(
        item,
        {
            "speed": _obs("T1", "Speed", 125.0),
            "speedlimit": _obs("T2", "SpeedLimit", 100.0),
            "overspeed": _obs("T3", "Overspeed", True),
        },
    )
    assert diagnosis.status is LiveAdvancedDiagnosisStatus.CONDITION_TRUE
    assert "125.0" in diagnosis.summary


def test_numeric_comparison_rejects_untrusted_operand_and_fails_closed_on_result_mismatch():
    stmt = _statement("HighPressure := Pressure >= PressureLimit;")
    context = _context(
        tags=(
            _tag("P", "Pressure", "REAL"),
            _tag("L", "PressureLimit", "REAL"),
            _tag("H", "HighPressure"),
        ),
        statements=(stmt,),
    )
    coverage = build_live_advanced_coverage(_project(), context)
    item = coverage.numeric_comparisons[0]

    untrusted = diagnose_numeric_comparison(
        item,
        {
            "pressure": _obs("P", "Pressure", 90.0, current=False),
            "pressurelimit": _obs("L", "PressureLimit", 80.0),
        },
    )
    assert untrusted.status is LiveAdvancedDiagnosisStatus.INDETERMINATE

    mismatch = diagnose_numeric_comparison(
        item,
        {
            "pressure": _obs("P", "Pressure", 90.0),
            "pressurelimit": _obs("L", "PressureLimit", 80.0),
            "highpressure": _obs("H", "HighPressure", False),
        },
    )
    assert mismatch.status is LiveAdvancedDiagnosisStatus.INDETERMINATE
    assert any("not a proven atomic PLC scan" in value for value in mismatch.limitations)


def test_name_derived_handshake_is_inferred_and_waiting_response_is_not_overclaimed():
    context = _context(
        tags=(
            _tag("R", "CV204_StartReq"),
            _tag("A", "CV204_StartAck"),
            _tag("B", "CV204_StartBusy"),
            _tag("D", "CV204_StartDone"),
            _tag("F", "CV204_StartFault"),
        )
    )
    coverage = build_live_advanced_coverage(_project(), context)
    models = [item for item in coverage.models if item.kind is LiveAdvancedKind.HANDSHAKE]
    assert len(models) == 1
    model = models[0]
    assert model.semantic_state == "INFERRED"

    diagnosis = diagnose_advanced_model(
        context,
        coverage,
        model,
        {
            "cv204startreq": _obs("R", "CV204_StartReq", True),
            "cv204startack": _obs("A", "CV204_StartAck", False),
            "cv204startbusy": _obs("B", "CV204_StartBusy", False),
            "cv204startdone": _obs("D", "CV204_StartDone", False),
            "cv204startfault": _obs("F", "CV204_StartFault", False),
        },
    )
    assert diagnosis.status is LiveAdvancedDiagnosisStatus.WAITING_RESPONSE
    assert any("inferred" in item.casefold() for item in diagnosis.limitations)


def test_one_shot_latch_sequencer_motion_and_pid_are_detected_without_hidden_state_simulation():
    source = SimpleNamespace(locator="PLC1/Main/Rung 1")
    instructions = tuple(
        SimpleNamespace(name=name, arguments=args)
        for name, args in (
            ("ONS", ("PulseStore",)),
            ("OTL", ("AlarmLatched",)),
            ("SQO", ("SeqFile", "Mask", "Output", "SeqCtrl", "10", "0")),
            ("MAM", ("Axis1", "MoveCtrl")),
            ("PIDE", ("Loop1", "PV", "CV")),
        )
    )
    rung = SimpleNamespace(id="R1", text="", writes=(), instructions=instructions, source=source)
    context = _context(
        tags=tuple(
            _tag(str(i), name, "REAL" if name in {"PV", "CV"} else "BOOL")
            for i, name in enumerate(("PulseStore", "AlarmLatched", "SeqFile", "Mask", "Output", "SeqCtrl", "Axis1", "MoveCtrl", "Loop1", "PV", "CV"), start=1)
        )
    )
    coverage = build_live_advanced_coverage(_project(rungs=(rung,)), context)

    kinds = {item.kind for item in coverage.models}
    assert LiveAdvancedKind.ONE_SHOT in kinds
    assert LiveAdvancedKind.LATCH in kinds
    assert LiveAdvancedKind.SEQUENCER in kinds
    assert LiveAdvancedKind.MOTION in kinds
    assert LiveAdvancedKind.PID in kinds

    latch = next(item for item in coverage.models if item.kind is LiveAdvancedKind.LATCH)
    diagnosis = diagnose_advanced_model(context, coverage, latch, {})
    assert diagnosis.status is LiveAdvancedDiagnosisStatus.HISTORY_REQUIRED
    assert "last executed" in diagnosis.summary


def test_aoi_source_protected_definition_remains_partial_and_does_not_claim_internals():
    aoi = SimpleNamespace(
        id="AOI1",
        name="ConveyorAOI",
        parameters=(SimpleNamespace(name="Enable"), SimpleNamespace(name="Running")),
        source_protected=True,
        internal_body_modeled=False,
    )
    instruction = SimpleNamespace(name="ConveyorAOI", arguments=("CV204", "Enable", "Running"))
    rung = SimpleNamespace(
        id="R1",
        text="",
        writes=(),
        instructions=(instruction,),
        source=SimpleNamespace(locator="PLC1/Main/Rung 5"),
    )
    context = _context(tags=(_tag("E", "Enable"), _tag("R", "Running")))
    coverage = build_live_advanced_coverage(_project(rungs=(rung,), aois=(aoi,)), context)

    call = next(item for item in coverage.models if item.kind is LiveAdvancedKind.AOI_FB and not item.metadata.get("definition"))
    assert call.semantic_state == "PARTIAL"
    diagnosis = diagnose_advanced_model(context, coverage, call, {})
    assert diagnosis.status is LiveAdvancedDiagnosisStatus.INDETERMINATE
    assert any("not infer" in item.casefold() for item in diagnosis.limitations)


def test_fault_code_udt_and_array_context_are_detected_without_invented_meaning():
    dtype = SimpleNamespace(
        name="MotorUDT",
        members=(SimpleNamespace(name="Run"), SimpleNamespace(name="Fault")),
    )
    context = _context(
        tags=(
            _tag("F", "DriveFaultCode", "DINT", "Drive diagnostic code"),
            _tag("U", "Motor1", "MotorUDT"),
            _tag("A", "ZoneValues", "ARRAY[0..9] OF REAL"),
        )
    )
    coverage = build_live_advanced_coverage(_project(data_types=(dtype,)), context)
    kinds = {item.kind for item in coverage.models}
    assert LiveAdvancedKind.FAULT_CODE in kinds
    assert LiveAdvancedKind.UDT in kinds
    assert LiveAdvancedKind.ARRAY in kinds

    fault = next(item for item in coverage.models if item.kind is LiveAdvancedKind.FAULT_CODE)
    diagnosis = diagnose_advanced_model(
        context,
        coverage,
        fault,
        {"drivefaultcode": _obs("F", "DriveFaultCode", 42)},
    )
    assert diagnosis.status is LiveAdvancedDiagnosisStatus.OBSERVED
    assert "42" in diagnosis.summary
    assert any("does not invent" in item for item in diagnosis.limitations)


def test_question_resolver_prefers_numeric_result_identity():
    stmt = _statement("Overspeed := Speed > SpeedLimit;")
    context = _context(
        tags=(_tag("S", "Speed", "REAL"), _tag("L", "SpeedLimit", "REAL"), _tag("O", "Overspeed")),
        statements=(stmt,),
    )
    coverage = build_live_advanced_coverage(_project(), context)
    target = resolve_advanced_target(coverage, "Why is Overspeed active?", context=context)
    assert target.numeric is not None
    assert target.numeric.result_tag == "Overspeed"
