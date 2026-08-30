from types import SimpleNamespace

from devagent.live.stateful_context import build_live_stateful_coverage
from devagent.plc.rockwell_stateful_runtime import stateful_models


def test_rockwell_stateful_models_missing_rungs_is_safe_empty() -> None:
    project = SimpleNamespace(metadata=SimpleNamespace(vendor="ROCKWELL"))

    assert stateful_models(project) == []

    coverage = build_live_stateful_coverage(project)
    assert coverage.vendor == "ROCKWELL"
    assert coverage.models == ()
    assert coverage.timers == 0
    assert coverage.counters == 0
