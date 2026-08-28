from __future__ import annotations

_INSTALLED = False


def install() -> None:
    """Install the current Schneider production theorem stack and refresh V5 bindings."""
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
    # theorem. It canonicalizes numeric state identity, range-checks integer
    # states, rejects impossible guards/runtime output bindings, and reconciles
    # CASE-internal writers without weakening the fail-closed runtime boundary.
    _install_state_machine_hardening_v5()

    # Granular Control Expert exports can repeat variables across .XST/.XSY.
    # Install deterministic metadata reconciliation before any external caller can
    # invoke the parser so located/address information is never lost to file order.
    _install_tag_merge()

    # Import V6 only after V5 hardening is active. Its immutable previous-profile
    # binding must capture the hardened V5 capability contract, not the original
    # pre-hardening function. V6 adds every-path interlock/permissive guard proof
    # over FULL V1-V5 Boolean output and state-transition theorems only.
    from devagent.plc.schneider_interlock_permissive_v6 import (
        install as _install_interlock_permissive_v6,
    )

    _install_interlock_permissive_v6()

    # Final V6 uniqueness hardening prevents a FULL output theorem from being
    # treated as the only path when another PARTIAL theorem for the same output
    # also exists in the normalized project inventory.
    from devagent.plc.schneider_interlock_permissive_hardening_v6 import (
        install as _install_interlock_permissive_hardening_v6,
    )

    _install_interlock_permissive_hardening_v6()

    # V7 builds only on the fully installed/hardened V6 transition contracts.
    # It identifies explicit fault-entry topology, requires strong reset/recover/
    # ack/clear intent to dominate every modeled exit from a fault-associated
    # state, and surfaces recovery gaps, bypass paths, stale-command restart
    # hazards, competing recovery targets, and restart/retention FAT boundaries.
    from devagent.plc.schneider_fault_recovery_v7 import install as _install_fault_recovery_v7

    _install_fault_recovery_v7()

    # V8 is identity/ownership infrastructure over the complete V1-V7 theorem
    # stack. It canonicalizes controller variables, DDT/ARRAY members, DFB
    # instance/interface identities, located/topological I/O addresses, and every
    # source read/write binding. Identity ambiguity, physical aliasing, and
    # whole/member ownership overlap fail closed instead of being guessed.
    from devagent.plc.schneider_identity_types_v8 import install as _install_identity_types_v8

    _install_identity_types_v8()

    # Production V5 imports shared production functions by value. Refresh those
    # bindings only after the complete Schneider V1-V8 vendor stack has installed
    # analyzer, evidence, risk, requirement, and report hooks.
    _v5.run_v4_verification = _production.run_production_verification
    _v5.evidence_index = _evidence.evidence_index
    _v5.detect_risks = _review.detect_risks
    _v5.optimization_candidates = _review.optimization_candidates
    _INSTALLED = True


__all__ = ["install"]
