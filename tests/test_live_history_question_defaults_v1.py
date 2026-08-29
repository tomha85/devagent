from __future__ import annotations

from devagent.live.history import requested_history_seconds


def test_historical_question_without_explicit_age_uses_lookback_only():
    window = requested_history_seconds("Why did ConveyorRun stop?")

    assert float(window) == 60.0
    assert getattr(window, "direction", None) == "STOP"
    assert getattr(window, "age_seconds", "missing") is None


def test_historical_question_with_explicit_age_preserves_age_and_direction():
    window = requested_history_seconds("Why did ConveyorRun stop 30 seconds ago?")

    assert float(window) == 30.0
    assert getattr(window, "direction", None) == "STOP"
    assert getattr(window, "age_seconds", None) == 30.0
