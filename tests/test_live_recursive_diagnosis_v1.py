from __future__ import annotations

from devagent.live.diagnosis import LiveObservedTag
from devagent.live.diagnosis_guard import diagnose_output
from devagent.live.engineering_context import (
    LiveEngineeringContext,
    LiveEngineeringTag,
    LiveLogicPath,
    LiveLogicRule,
    LiveLogicTerm,
)
from devagent.live.recursive_diagnosis import (
    LiveRootCauseStepStatus,
    required_tag_ids_for_recursive_output,
    trace_recursive_diagnosis,
)


def _tag(name: str) -> LiveEngineeringTag:
    return LiveEngineeringTag(
        id=f"tag-{name}",
        name=name,
        scope="Controller",
        data_type="BOOL",
        description=None,
        external_access=None,
        alias_for=None,
    )


def _rule(
    output: str,
    *paths: tuple[tuple[str, bool], ...],
    instruction: str = "OTE",
    semantic_state: str = "FULL",
) -> LiveLogicRule:
    return LiveLogicRule(
        id=f"rule-{output}",
        output_tag=output,
        instruction=instruction,
        paths=tuple(
            LiveLogicPath(
                tuple(LiveLogicTerm(tag_reference=name, required=required) for name, required in path)
            )
            for path in paths
        ),
        source_locator=f"PLC / Main / {output}",
        language="RLL",
        origin="RUNG",
        semantic_state=semantic_state,
        evidence_id=f"ENG-{output}",
    )


def _context(tags, rules) -> LiveEngineeringContext:
    return LiveEngineeringContext(
        vendor="ROCKWELL",
        engineering_tool="Studio 5000",
        controller_name="WarehousePLC",
        source_path="warehouse.L5X",
        source_sha256="a" * 64,
        full_project=True,
        tags=tuple(_tag(name) for name in tags),
        rules=tuple(rules),
        statements=(),
        limitations=(),
    )


def _obs(name: str, value: bool, *, current: bool = True) -> LiveObservedTag:
    return LiveObservedTag(
        tag_id=f"tag-{name}",
        tag_name=name,
        node_id=f"ns=2;s={name}",
        value=value if current else None,
        evidence_id=f"LIVE-{name}",
        definitive_current=current,
        mapping_status="AUTO_BOUND",
        limitation=None if current else f"{name} is not trusted CURRENT evidence.",
    )


def _warehouse_chain():
    tags = (
        "Conveyor7_Run",
        "AutoMode",
        "SafetyOK",
        "AutoEnable",
        "ManualMode",
        "HMI_Enable",
        "CommissioningAllowed",
    )
    rules = (
        _rule("Conveyor7_Run", (("AutoMode", True), ("SafetyOK", True))),
        _rule("AutoMode", (("AutoEnable", True), ("ManualMode", False))),
        _rule("AutoEnable", (("HMI_Enable", True), ("CommissioningAllowed", True))),
    )
    observations = (
        _obs("Conveyor7_Run", False),
        _obs("AutoMode", False),
        _obs("SafetyOK", True),
        _obs("AutoEnable", False),
        _obs("ManualMode", False),
        _obs("HMI_Enable", False),
        _obs("CommissioningAllowed", True),
    )
    return _context(tags, rules), observations


def test_recursive_trace_finds_deepest_trusted_logic_observation():
    context, observations = _warehouse_chain()
    direct = diagnose_output(context, "Conveyor7_Run", observations)

    recursive = trace_recursive_diagnosis(context, direct, observations)

    assert recursive.complete is True
    assert recursive.chains() == (
        ("Conveyor7_Run", "AutoMode", "AutoEnable", "HMI_Enable"),
    )
    root = recursive.roots[0]
    assert root.status is LiveRootCauseStepStatus.EXPANDED
    assert root.source_locator.endswith("AutoMode")
    assert root.children[0].status is LiveRootCauseStepStatus.EXPANDED
    leaf = root.children[0].children[0]
    assert leaf.signal == "HMI_Enable"
    assert leaf.observed_value is False
    assert leaf.status is LiveRootCauseStepStatus.ROOT_LOGIC_OBSERVATION
    assert "physical/process root cause" in recursive.render_text()


def test_recursive_required_tags_collect_bounded_dependency_closure():
    context, _observations = _warehouse_chain()

    required = required_tag_ids_for_recursive_output(context, "Conveyor7_Run")

    assert required == (
        "tag-Conveyor7_Run",
        "tag-AutoMode",
        "tag-SafetyOK",
        "tag-AutoEnable",
        "tag-ManualMode",
        "tag-HMI_Enable",
        "tag-CommissioningAllowed",
    )


def test_recursive_trace_can_explain_true_fault_signal_that_blocks_parent():
    context = _context(
        ("Conveyor7_Run", "DriveFault", "OverloadTrip", "VfdFault"),
        (
            _rule("Conveyor7_Run", (("DriveFault", False),)),
            _rule(
                "DriveFault",
                (("OverloadTrip", True),),
                (("VfdFault", True),),
            ),
        ),
    )
    observations = (
        _obs("Conveyor7_Run", False),
        _obs("DriveFault", True),
        _obs("OverloadTrip", True),
        _obs("VfdFault", False),
    )
    direct = diagnose_output(context, "Conveyor7_Run", observations)

    recursive = trace_recursive_diagnosis(context, direct, observations)

    assert recursive.chains() == (
        ("Conveyor7_Run", "DriveFault", "OverloadTrip"),
    )
    assert recursive.roots[0].observed_value is True
    assert recursive.roots[0].children[0].status is LiveRootCauseStepStatus.ROOT_LOGIC_OBSERVATION


def test_recursive_trace_detects_cycles_and_stops_fail_closed():
    context = _context(
        ("Y", "A", "B"),
        (
            _rule("Y", (("A", True),)),
            _rule("A", (("B", True),)),
            _rule("B", (("A", True),)),
        ),
    )
    observations = (_obs("Y", False), _obs("A", False), _obs("B", False))
    direct = diagnose_output(context, "Y", observations)

    recursive = trace_recursive_diagnosis(context, direct, observations)

    assert recursive.complete is False
    assert recursive.chains() == (("Y", "A", "B", "A"),)
    terminal = recursive.roots[0].children[0].children[0]
    assert terminal.status is LiveRootCauseStepStatus.CYCLE
    assert any("cycle" in item.casefold() for item in recursive.limitations)


def test_recursive_trace_refuses_stateful_upstream_rule():
    context = _context(
        ("Y", "LatchedPermit", "SetCondition"),
        (
            _rule("Y", (("LatchedPermit", True),)),
            _rule("LatchedPermit", (("SetCondition", True),), instruction="OTL"),
        ),
    )
    observations = (
        _obs("Y", False),
        _obs("LatchedPermit", False),
        _obs("SetCondition", False),
    )
    direct = diagnose_output(context, "Y", observations)

    recursive = trace_recursive_diagnosis(context, direct, observations)

    assert recursive.complete is False
    assert recursive.roots[0].signal == "LatchedPermit"
    assert recursive.roots[0].status is LiveRootCauseStepStatus.INDETERMINATE
    assert recursive.roots[0].children == ()
    assert any("stateful" in item.casefold() for item in recursive.limitations)


def test_recursive_trace_stops_when_upstream_current_evidence_is_untrusted():
    context, observations = _warehouse_chain()
    observations = tuple(
        _obs("AutoEnable", False, current=False) if item.tag_name == "AutoEnable" else item
        for item in observations
    )
    direct = diagnose_output(context, "Conveyor7_Run", observations)

    recursive = trace_recursive_diagnosis(context, direct, observations)

    assert recursive.complete is False
    auto = recursive.roots[0]
    assert auto.signal == "AutoMode"
    assert auto.status is LiveRootCauseStepStatus.INDETERMINATE
    assert any("cannot determine" in item.casefold() or "stopped" in item.casefold() for item in recursive.limitations)


def test_recursive_trace_honors_depth_limit():
    context, observations = _warehouse_chain()
    direct = diagnose_output(context, "Conveyor7_Run", observations)

    recursive = trace_recursive_diagnosis(
        context,
        direct,
        observations,
        max_depth=1,
        max_nodes=64,
    )

    assert recursive.complete is False
    assert recursive.roots[0].status is LiveRootCauseStepStatus.DEPTH_LIMIT
    assert any("depth limit" in item.casefold() for item in recursive.limitations)
