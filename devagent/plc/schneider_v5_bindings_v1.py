from __future__ import annotations

_INSTALLED = False


def install() -> None:
    """Install the current Schneider production theorem stack and refresh V5 bindings."""
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc.schneider_state_machine_v5 import install as _install_state_machine_v5
    from devagent.plc.schneider_state_machine_hardening_v5 import install as _install_state_machine_hardening_v5
    from devagent.plc import production as _production
    from devagent.plc import production_evidence as _evidence
    from devagent.plc import production_review as _review
    from devagent.plc import production_v5 as _v5
    from devagent.plc.schneider_tag_merge_v1 import install as _install_tag_merge

    _install_state_machine_v5()
    _install_state_machine_hardening_v5()
    _install_tag_merge()

    from devagent.plc.schneider_interlock_permissive_v6 import install as _install_interlock_permissive_v6
    _install_interlock_permissive_v6()

    from devagent.plc.schneider_interlock_permissive_hardening_v6 import install as _install_interlock_permissive_hardening_v6
    _install_interlock_permissive_hardening_v6()

    from devagent.plc.schneider_fault_recovery_v7 import install as _install_fault_recovery_v7
    _install_fault_recovery_v7()

    # V8 owns canonical project-wide symbol/type/I/O identity over the complete
    # V1-V7 theorem stack. Scope hardening prevents DDT/DFB member `<variables>`
    # from leaking into controller-root identity; typed Boolean hardening removes
    # source theorems whose canonical types are not BOOL/EBOOL/BOOLEAN.
    from devagent.plc.schneider_identity_types_v8 import install as _install_identity_types_v8
    from devagent.plc.schneider_identity_hardening_v8 import install as _install_identity_hardening_v8
    from devagent.plc.schneider_v8_compat_hardening import install as _install_v8_compat_hardening

    _install_identity_types_v8()
    _install_identity_hardening_v8()
    # The compatibility closeout rebuilds V8 identity directly over the V7 result
    # so additive identity metadata does not rewrite the V1-V7 theorem provenance.
    _install_v8_compat_hardening()

    # V9 is the commercial/source-support closeout layer. It accounts for every
    # normalized executable statement, discovered source section, protected DFB
    # and DFB call boundary, records deterministic bundle identity, and preserves
    # the external real-export/runtime evidence gates instead of overclaiming.
    from devagent.plc.schneider_closeout_v9 import install as _install_closeout_v9

    _install_closeout_v9()

    # Production V5 imports shared production functions by value. Refresh those
    # bindings only after the complete Schneider V1-V9 vendor stack is installed.
    _v5.run_v4_verification = _production.run_production_verification
    _v5.evidence_index = _evidence.evidence_index
    _v5.detect_risks = _review.detect_risks
    _v5.optimization_candidates = _review.optimization_candidates
    _INSTALLED = True


__all__ = ["install"]
