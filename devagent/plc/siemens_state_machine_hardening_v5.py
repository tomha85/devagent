from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from devagent.plc import siemens_state_machine_v5 as _v5


_INSTALLED = False
_PREVIOUS_ANALYZER = _v5.analyze_siemens_tia_v5


def _hardened_analyzer(path: Path):
    result = _PREVIOUS_ANALYZER(path)
    facts = getattr(result.project, "_siemens_v5_state_machine_facts", None)
    if facts is None:
        return result

    runtime_dependent = any(
        machine.runtime_dependencies
        for machine in facts.machines
    )
    if not runtime_dependent or result.outcome is not _v5.PLCOutcome.STATICALLY_VERIFIED:
        return result

    # A deterministic transition relation is not equivalent to proving the
    # timer/counter state that enables that transition. Keep the local V5 CASE
    # theorem and its source evidence, but fail the whole-project outcome closed
    # until engineer-executed runtime evidence exists.
    return replace(
        result,
        outcome=_v5.PLCOutcome.PARTIALLY_VERIFIED,
        limitations=list(dict.fromkeys([
            *result.limitations,
            (
                "At least one bounded Siemens V5 state transition depends on TON/TOF/TP/CTU/CTD runtime state. "
                "The transition relation is traceable, but whole-project static closure is withheld until engineer runtime evidence verifies timing/counting evolution."
            ),
        ])),
    )


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import siemens_tia_v1 as _v1

    _v5.analyze_siemens_tia_v5 = _hardened_analyzer
    _v1.analyze_siemens_tia = _hardened_analyzer
    _dispatch.analyze_siemens_tia = _hardened_analyzer
    _INSTALLED = True


__all__ = ["install"]
