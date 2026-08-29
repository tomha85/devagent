from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from devagent.live.engineering_context import LiveEngineeringContext, LiveEngineeringTag
from devagent.live.history import (
    LiveHistoricalSample,
    LiveHistoryCollector,
    LiveTimelineStore,
    is_historical_question,
    requested_history_seconds,
)
from devagent.live.stateful_assistant import resolve_stateful_model
from devagent.live.stateful_context import (
    LiveStatefulDiagnosisStatus,
    LiveStatefulKind,
    LiveStatefulModel,
    LiveStatefulTransition,
    build_live_stateful_coverage,
    diagnose_live_stateful_model,
)


def _tag(tag_id: str, name: str) -> LiveEngineeringTag:
    return LiveEngineeringTag(
        id=tag_id,
        name=name,
        scope="Controller",
        data_type="BOOL",
        description=None,
        external_access="Read Only",
        alias_for=None,
    )


def _context() -> LiveEngineeringContext:
    return LiveEngineeringContext(
        vendor="TEST",
        engineering_tool="TEST",
        controller_name="TEST",
        source_path="test",
        source_sha256="test",
        full_project=True,
        tags=(_tag("OUT", "ConveyorRun"), _tag("PE", "JamPhotoeye")),
        rules=(),
        statements=(),
        limitations=(),
    )


def test_timer_enable_blocked_without_inferring_done_state():
    model = LiveStatefulModel(
        id="timer-1",
        vendor="ROCKWELL",
        kind=LiveStatefulKind.TIMER,
        name="T1",
        instruction="TON",
        semantic_state="RUNTIME_REQUIRED",
        source_locator="Program:Main/Rung:10",
        guard_paths=((('StartCmd', True), ('SafetyOK', True)),),
    )

    result = diagnose_live_stateful_model(
        model,
        {"StartCmd": True, "SafetyOK": False},
    )

    assert result.status is LiveStatefulDiagnosisStatus.TRANSITION_BLOCKED
    assert any("SafetyOK" in item for item in result.blocking_conditions)
    assert ".DN" not in result.detail


def test_timer_enabled_stays_observation_not_completion_proof():
    model = LiveStatefulModel(
        id="timer-1",
        vendor="ROCKWELL",
        kind=LiveStatefulKind.TIMER,
        name="T1",
        instruction="TON",
        semantic_state="RUNTIME_REQUIRED",
        source_locator="Program:Main/Rung:10",
        guard_paths=((('StartCmd', True),),),
    )

    result = diagnose_live_stateful_model(model, {"StartCmd": True})

    assert result.status is LiveStatefulDiagnosisStatus.STATE_OBSERVED
    assert "requires live state pins" in result.detail
    assert "does not infer" in result.detail


def test_state_machine_transition_ready_and_blocked():
    transition = LiveStatefulTransition(
        source_state="IDLE",
        target_state="RUN",
        guard_paths=((('StartCmd', True), ('SafetyOK', True)),),
        runtime_dependencies=(),
        source_locator="FB_Line:42",
    )
    model = LiveStatefulModel(
        id="sm-1",
        vendor="SIEMENS",
        kind=LiveStatefulKind.STATE_MACHINE,
        name="SequenceState",
        instruction="CASE_STATE_MACHINE",
        semantic_state="FULL",
        source_locator="FB_Line:30-80",
        states=("IDLE", "RUN"),
        transitions=(transition,),
    )

    ready = diagnose_live_stateful_model(
        model,
        {"SequenceState": "IDLE", "StartCmd": True, "SafetyOK": True},
    )
    blocked = diagnose_live_stateful_model(
        model,
        {"SequenceState": "IDLE", "StartCmd": True, "SafetyOK": False},
    )

    assert ready.status is LiveStatefulDiagnosisStatus.TRANSITION_READY
    assert ready.candidate_targets == ("RUN",)
    assert blocked.status is LiveStatefulDiagnosisStatus.TRANSITION_BLOCKED
    assert any("SafetyOK" in item for item in blocked.blocking_conditions)


def test_state_machine_missing_guard_is_indeterminate():
    transition = LiveStatefulTransition(
        source_state="1",
        target_state="2",
        guard_paths=((('Ready', True),),),
        runtime_dependencies=(),
        source_locator="source:2",
    )
    model = LiveStatefulModel(
        id="sm-1",
        vendor="SCHNEIDER",
        kind=LiveStatefulKind.STATE_MACHINE,
        name="Step",
        instruction="CASE_STATE_MACHINE",
        semantic_state="FULL",
        source_locator="source",
        states=("1", "2"),
        transitions=(transition,),
    )

    result = diagnose_live_stateful_model(model, {"Step": 1})

    assert result.status is LiveStatefulDiagnosisStatus.INDETERMINATE
    assert result.unknown_conditions == ("Ready",)


def test_siemens_existing_state_machine_facts_are_consumed_read_only():
    term = SimpleNamespace(tag="Ready", required=True)
    path = SimpleNamespace(terms=(term,))
    transition = SimpleNamespace(
        source_state="1",
        target_state="2",
        guard_paths=(path,),
        runtime_dependencies=("TON:T1",),
        source_line=44,
    )
    machine = SimpleNamespace(
        id="SIEMENS-SM-1",
        block="FB_Line",
        state_tag="SequenceState",
        semantic_state=SimpleNamespace(value="FULL"),
        case_line=30,
        end_line=80,
        states=("1", "2"),
        transitions=(transition,),
        runtime_dependencies=("TON:T1",),
    )
    project = SimpleNamespace(
        metadata=SimpleNamespace(vendor="Siemens"),
        _siemens_v5_state_machine_facts=SimpleNamespace(machines=(machine,)),
    )

    coverage = build_live_stateful_coverage(project)

    assert coverage.vendor == "SIEMENS"
    assert coverage.state_machines == 1
    assert coverage.models[0].name == "SequenceState"
    assert coverage.models[0].runtime_dependencies == ("TON:T1",)
    assert coverage.models[0].transitions[0].guard_paths == ((('Ready', True),),)


def test_stateful_question_resolves_unique_model():
    models = (
        LiveStatefulModel("1", "ROCKWELL", LiveStatefulKind.TIMER, "T_ConveyorStart", "TON", "RUNTIME_REQUIRED", "r1"),
        LiveStatefulModel("2", "ROCKWELL", LiveStatefulKind.COUNTER, "C_Parts", "CTU", "RUNTIME_REQUIRED", "r2"),
    )

    assert resolve_stateful_model(models, "Why is T_ConveyorStart not done?") == models[0]
    assert resolve_stateful_model(models, "what is wrong?") is None


def test_untrusted_history_samples_never_create_transition():
    now = datetime.now(timezone.utc)
    store = LiveTimelineStore(retention_seconds=60)
    store.append(
        LiveHistoricalSample(now - timedelta(seconds=2), "p", "OUT", "ConveyorRun", "n", True, True, "GOOD", "CURRENT")
    )
    store.append(
        LiveHistoricalSample(now - timedelta(seconds=1), "p", "OUT", "ConveyorRun", "n", False, False, "BAD", "UNTRUSTED")
    )

    assert store.transitions() == ()


def test_trusted_history_identifies_preceding_dependency_change():
    now = datetime.now(timezone.utc)
    store = LiveTimelineStore(retention_seconds=60)
    store.append_many(
        (
            LiveHistoricalSample(now - timedelta(seconds=5), "p", "PE", "JamPhotoeye", "n1", False, True, "GOOD", "CURRENT"),
            LiveHistoricalSample(now - timedelta(seconds=4), "p", "OUT", "ConveyorRun", "n2", True, True, "GOOD", "CURRENT"),
            LiveHistoricalSample(now - timedelta(seconds=3), "p", "PE", "JamPhotoeye", "n1", True, True, "GOOD", "CURRENT"),
            LiveHistoricalSample(now - timedelta(seconds=2), "p", "OUT", "ConveyorRun", "n2", False, True, "GOOD", "CURRENT"),
        )
    )

    result = store.diagnose_recent_transition(
        _context(),
        "ConveyorRun",
        dependency_tag_ids=("PE",),
        lookback_seconds=30,
        now=now,
    )

    assert result.transition is not None
    assert result.transition.old_value is True
    assert result.transition.new_value is False
    assert len(result.preceding_changes) == 1
    assert result.preceding_changes[0].tag_name == "JamPhotoeye"
    assert "not proof" in result.render_text()


def test_history_time_parser_and_question_detection():
    assert requested_history_seconds("what happened 30 seconds ago?") == 30
    assert requested_history_seconds("what happened 5 minutes ago?") == 300
    assert requested_history_seconds("what happened 2 hours ago?") == 7200
    assert is_historical_question("Why did ConveyorRun stop 30 seconds ago?") is True
    assert is_historical_question("Why is ConveyorRun stopped?") is False


def test_history_collector_prioritizes_preferred_diagnostic_tags():
    def mapping(tag_id: str, name: str, node: str):
        return SimpleNamespace(tag_id=tag_id, tag_name=name, selected_node_id=node)

    mappings = (
        mapping("UNRELATED", "AAA_Unrelated", "n1"),
        mapping("OUT", "ConveyorRun", "n2"),
        mapping("PE", "JamPhotoeye", "n3"),
    )
    reconciliation = SimpleNamespace(
        plc_id="p1",
        accepted_mappings=lambda: mappings,
    )

    collector = LiveHistoryCollector(
        manager=SimpleNamespace(),
        reconciliation=reconciliation,
        max_tags=2,
        preferred_tag_ids=("OUT", "PE"),
    )

    assert collector.captured_tag_ids == ("OUT", "PE")
