from __future__ import annotations

from devagent.live.stateful_context import (
    LiveStatefulDiagnosisStatus,
    LiveStatefulKind,
    LiveStatefulModel,
    LiveStatefulTransition,
    diagnose_live_stateful_model,
)


def test_partial_state_machine_never_proves_transition_ready_or_blocked():
    transition = LiveStatefulTransition(
        source_state="1",
        target_state="2",
        guard_paths=((('Ready', True),),),
        runtime_dependencies=(),
        source_locator="FB:42",
    )
    model = LiveStatefulModel(
        id="partial-sm",
        vendor="SIEMENS",
        kind=LiveStatefulKind.STATE_MACHINE,
        name="SequenceState",
        instruction="CASE_STATE_MACHINE",
        semantic_state="PARTIAL",
        source_locator="FB:30-80",
        states=("1", "2"),
        transitions=(transition,),
    )

    result = diagnose_live_stateful_model(
        model,
        {"SequenceState": 1, "Ready": True},
    )

    assert result.status is LiveStatefulDiagnosisStatus.INDETERMINATE
    assert result.candidate_targets == ()
    assert "not FULL" in result.detail
