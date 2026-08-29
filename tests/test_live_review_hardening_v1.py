from __future__ import annotations

from datetime import datetime, timedelta, timezone

from devagent.live.engineering_context import LiveEngineeringContext, LiveEngineeringTag
from devagent.live.history import (
    LiveHistoricalSample,
    LiveTimelineStore,
    requested_history_seconds,
)


def _context() -> LiveEngineeringContext:
    return LiveEngineeringContext(
        vendor="SELF",
        engineering_tool="SELF",
        controller_name="SELF",
        source_path="self",
        source_sha256="self",
        full_project=True,
        tags=(
            LiveEngineeringTag(
                id="OUT",
                name="ConveyorRun",
                scope="Controller",
                data_type="BOOL",
                description=None,
                external_access="Read Only",
                alias_for=None,
            ),
        ),
        rules=(),
        statements=(),
        limitations=(),
    )


def _sample(at, value, *, current=True):
    return LiveHistoricalSample(
        timestamp=at,
        plc_id="p1",
        tag_id="OUT",
        tag_name="ConveyorRun",
        node_id="ns=2;s=ConveyorRun",
        value=value,
        definitive_current=current,
        quality="GOOD" if current else "BAD",
        trust="CURRENT" if current else "EXCLUDED",
    )


def test_outage_gap_does_not_create_false_recovery_transition():
    now = datetime.now(timezone.utc)
    store = LiveTimelineStore(
        retention_seconds=120.0,
        continuity_seconds=3.0,
    )

    store.append(_sample(now - timedelta(seconds=30), True))
    store.append(_sample(now, False))

    assert store.transitions() == ()


def test_historical_stop_question_selects_stop_not_recent_restart():
    now = datetime.now(timezone.utc)
    store = LiveTimelineStore(
        retention_seconds=120.0,
        continuity_seconds=3.0,
    )

    # Stop around 30 seconds ago.
    store.append(_sample(now - timedelta(seconds=31), True))
    store.append(_sample(now - timedelta(seconds=30), False))
    # Continuity was interrupted, then a separate restart occurred 5 seconds ago.
    store.append(_sample(now - timedelta(seconds=6), False))
    store.append(_sample(now - timedelta(seconds=5), True))

    window = min(
        requested_history_seconds("Why did ConveyorRun stop 30 seconds ago?"),
        120.0,
    )
    assert getattr(window, "direction", None) == "STOP"
    assert getattr(window, "age_seconds", None) == 30.0

    diagnosis = store.diagnose_recent_transition(
        _context(),
        "ConveyorRun",
        lookback_seconds=window,
        now=now,
    )

    assert diagnosis.transition is not None
    assert diagnosis.transition.old_value is True
    assert diagnosis.transition.new_value is False
    assert diagnosis.transition.timestamp == now - timedelta(seconds=30)
