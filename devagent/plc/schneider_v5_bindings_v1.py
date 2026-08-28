from __future__ import annotations

_INSTALLED = False


def install() -> None:
    """Refresh Production V5 by-value bindings after Schneider integration."""
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import production as _production
    from devagent.plc import production_evidence as _evidence
    from devagent.plc import production_review as _review
    from devagent.plc import production_v5 as _v5

    _v5.run_v4_verification = _production.run_production_verification
    _v5.evidence_index = _evidence.evidence_index
    _v5.detect_risks = _review.detect_risks
    _v5.optimization_candidates = _review.optimization_candidates
    _INSTALLED = True


__all__ = ["install"]
