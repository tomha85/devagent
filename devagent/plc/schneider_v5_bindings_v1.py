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
    from devagent.plc.schneider_tag_merge_v1 import install as _install_tag_merge

    # Granular Control Expert exports can repeat variables across .XST/.XSY.
    # Install deterministic metadata reconciliation before any external caller can
    # invoke the parser so located/address information is never lost to file order.
    _install_tag_merge()

    _v5.run_v4_verification = _production.run_production_verification
    _v5.evidence_index = _evidence.evidence_index
    _v5.detect_risks = _review.detect_risks
    _v5.optimization_candidates = _review.optimization_candidates
    _INSTALLED = True


__all__ = ["install"]
