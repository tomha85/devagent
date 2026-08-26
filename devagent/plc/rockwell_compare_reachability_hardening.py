from __future__ import annotations

from devagent.plc import rockwell_compare as _compare
from devagent.plc import rockwell_compare_hardening as _hardening
from devagent.plc.rockwell_entrypoint_hardening import rung_has_execution_entry

_ORIGINAL_COMPARE_MODELS = _hardening.compare_models
_INSTALLED = False


def compare_models(project):
    """Expose typed-compare models only for rungs in the executable routine closure."""

    rung_by_id = {rung.id: rung for rung in project.rungs}
    result = []
    for model in _ORIGINAL_COMPARE_MODELS(project):
        rung = rung_by_id.get(model.rung_id)
        if rung is None or not rung_has_execution_entry(project, rung):
            continue
        result.append(model)
    return result


def install() -> None:
    """Install the reachability gate into both public and hardened compare globals."""

    global _INSTALLED
    if _INSTALLED:
        return
    # The V8 hardening functions resolve ``compare_models`` from their module
    # globals at runtime. Rebinding that symbol makes FAT generation, static
    # checks, and typed requirement verification share the exact same execution
    # reachability model rather than independently rescanning raw rungs.
    _hardening.compare_models = compare_models
    _compare.compare_models = compare_models
    _INSTALLED = True


__all__ = ["compare_models", "install"]
