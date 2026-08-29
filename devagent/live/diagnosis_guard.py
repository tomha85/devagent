from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from .diagnosis import (
    LiveCommissioningDiagnosis,
    LiveDiagnosisStatus,
    LiveObservedTag,
    diagnose_output as _diagnose_output,
)
from .engineering_context import LiveEngineeringContext


_STATEFUL_INSTRUCTIONS = {
    "OTL",
    "OTU",
    "SET",
    "RESET",
    "LATCH",
    "UNLATCH",
    "SR",
    "RS",
}


def _stateful_instruction(instruction: str) -> bool:
    normalized = str(instruction or "").strip().upper().replace("-", "_")
    tokens = {token for token in normalized.replace("/", "_").split("_") if token}
    return normalized in _STATEFUL_INSTRUCTIONS or bool(tokens & _STATEFUL_INSTRUCTIONS)


def _guarded_limitation(
    diagnosis: LiveCommissioningDiagnosis,
    detail: str,
) -> LiveCommissioningDiagnosis:
    return replace(
        diagnosis,
        status=LiveDiagnosisStatus.INDETERMINATE,
        expected_output=None,
        blockers=(),
        limitations=tuple(dict.fromkeys((*diagnosis.limitations, detail))),
        summary=(
            f"DevAgent Live cannot prove the current cause for {diagnosis.target_output}: {detail}"
        ),
        next_checks=(
            "Inspect the authoritative PLC source and current trusted runtime evidence; "
            "do not treat this stateful or incompletely modeled rule as combinational proof.",
        ),
    )


def diagnose_output(
    context: LiveEngineeringContext,
    output_reference: str,
    observations: Iterable[LiveObservedTag],
) -> LiveCommissioningDiagnosis:
    """Safety-hardened public diagnosis surface.

    Canonical PLC output logic may be consumed by Live, but Live refuses to turn
    partial semantics or stateful set/reset/latch instructions into a definitive
    current blocker claim. This keeps PLC engineering authority upstream and
    prevents onsite Q&A from over-interpreting the canonical IR.
    """

    rules = context.rules_for_output(output_reference)
    diagnosis = _diagnose_output(context, output_reference, observations)

    if len(rules) == 1:
        rule = rules[0]
        if str(rule.semantic_state or "").strip().upper() != "FULL":
            return _guarded_limitation(
                diagnosis,
                f"canonical rule {rule.id} has semantic_state={rule.semantic_state or 'UNKNOWN'}, not FULL",
            )
        if _stateful_instruction(rule.instruction):
            return _guarded_limitation(
                diagnosis,
                f"canonical rule {rule.id} uses stateful instruction {rule.instruction}; current output state depends on history",
            )

    # Alternative OR paths may contain conditions that are false even while one
    # path is satisfied. Those are not blockers for an output whose modeled
    # result is TRUE, so do not expose them as active blocking causes.
    if diagnosis.expected_output is True and diagnosis.blockers:
        diagnosis = replace(diagnosis, blockers=())

    return diagnosis


__all__ = ["diagnose_output"]
