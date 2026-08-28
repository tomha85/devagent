from __future__ import annotations

_INSTALLED = False


def install() -> None:
    """Install Schneider V5 sequencing/hardening, then refresh Production V5 bindings."""
    global _INSTALLED
    if _INSTALLED:
        return

    # Import V5 only here. __init__ invokes this function after the V4 graphical
    # writer-hardening install, so Schneider V5 captures the exact hardened V4
    # analyzer/capability chain rather than an earlier vendor layer.
    from devagent.plc.schneider_state_machine_v5 import install as _install_state_machine_v5
    from devagent.plc.schneider_state_machine_hardening_v5 import (
        install as _install_state_machine_hardening_v5,
    )
    from devagent.plc import production as _production
    from devagent.plc import production_evidence as _evidence
    from devagent.plc import production_review as _review
    from devagent.plc import production_v5 as _v5
    from devagent.plc.schneider_tag_merge_v1 import install as _install_tag_merge

    _install_state_machine_v5()
    # Production hardening is deliberately installed immediately after the V5
    # theorem and before shared production functions are rebound. It canonicalizes
    # numeric state identity, range-checks integer states, rejects impossible guards
    # and timer/counter output bindings, and reconciles CASE-internal writers without
    # weakening the fail-closed runtime boundary.
    _install_state_machine_hardening_v5()

    # Granular Control Expert exports can repeat variables across .XST/.XSY.
    # Install deterministic metadata reconciliation before any external caller can
    # invoke the parser so located/address information is never lost to file order.
    _install_tag_merge()

    # Production V5 imports shared production functions by value. Refresh those
    # bindings only after Schneider V5 and its hardening layer have installed their
    # vendor-specific analyzer, evidence, risk, requirement, and report hooks.
    _v5.run_v4_verification = _production.run_production_verification
    _v5.evidence_index = _evidence.evidence_index
    _v5.detect_risks = _review.detect_risks
    _v5.optimization_candidates = _review.optimization_candidates
    _INSTALLED = True


__all__ = ["install"]
