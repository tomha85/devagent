from __future__ import annotations

from devagent.live import (
    LiveDiagnosisStatus,
    LiveEngineeringContext,
    LiveEngineeringTag,
    LiveLogicPath,
    LiveLogicRule,
    LiveLogicTerm,
    LiveObservedTag,
    diagnose_output,
)


def _context(*, instruction="OTE", semantic_state="FULL", paths=None):
    tags = (
        LiveEngineeringTag("A", "A", "Controller", "BOOL", None, None, None),
        LiveEngineeringTag("B", "B", "Controller", "BOOL", None, None, None),
        LiveEngineeringTag("Y", "Y", "Controller", "BOOL", None, None, None),
    )
    if paths is None:
        paths = (LiveLogicPath((LiveLogicTerm("A", True),)),)
    rule = LiveLogicRule(
        id="R1",
        output_tag="Y",
        instruction=instruction,
        paths=paths,
        source_locator="PLC / Main / Rung 1",
        language="RLL",
        origin="RUNG",
        semantic_state=semantic_state,
        evidence_id="ENG-R1",
    )
    return LiveEngineeringContext(
        vendor="ROCKWELL",
        engineering_tool="Studio 5000",
        controller_name="PLC",
        source_path="project.L5X",
        source_sha256="a" * 64,
        full_project=True,
        tags=tags,
        rules=(rule,),
        statements=(),
        limitations=(),
    )


def _obs(tag_id, name, value):
    return LiveObservedTag(
        tag_id=tag_id,
        tag_name=name,
        node_id=f"ns=2;s={name}",
        value=value,
        evidence_id=f"E-{name}",
        definitive_current=True,
        mapping_status="AUTO_BOUND",
    )


def test_partial_semantic_rule_cannot_produce_definitive_blocker():
    diagnosis = diagnose_output(
        _context(semantic_state="PARTIAL"),
        "Y",
        (_obs("A", "A", False), _obs("Y", "Y", False)),
    )

    assert diagnosis.status is LiveDiagnosisStatus.INDETERMINATE
    assert diagnosis.expected_output is None
    assert diagnosis.blockers == ()
    assert any("not FULL" in item for item in diagnosis.limitations)


def test_stateful_latch_rule_cannot_be_treated_as_combinational_current_state():
    diagnosis = diagnose_output(
        _context(instruction="OTL"),
        "Y",
        (_obs("A", "A", False), _obs("Y", "Y", True)),
    )

    assert diagnosis.status is LiveDiagnosisStatus.INDETERMINATE
    assert diagnosis.expected_output is None
    assert diagnosis.blockers == ()
    assert any("stateful instruction" in item for item in diagnosis.limitations)


def test_false_condition_on_inactive_or_path_is_not_reported_as_active_blocker():
    context = _context(
        paths=(
            LiveLogicPath((LiveLogicTerm("A", True),)),
            LiveLogicPath((LiveLogicTerm("B", True),)),
        )
    )
    diagnosis = diagnose_output(
        context,
        "Y",
        (
            _obs("A", "A", True),
            _obs("B", "B", False),
            _obs("Y", "Y", True),
        ),
    )

    assert diagnosis.status is LiveDiagnosisStatus.CONDITIONS_SATISFIED
    assert diagnosis.expected_output is True
    assert diagnosis.blockers == ()
