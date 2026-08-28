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
    _install_state_machine_hardening_v5()
    _install_tag_merge()

    from devagent.plc.schneider_interlock_permissive_v6 import (
        install as _install_interlock_permissive_v6,
    )
    _install_interlock_permissive_v6()

    from devagent.plc.schneider_interlock_permissive_hardening_v6 import (
        install as _install_interlock_permissive_hardening_v6,
    )
    _install_interlock_permissive_hardening_v6()

    from devagent.plc.schneider_fault_recovery_v7 import install as _install_fault_recovery_v7
    _install_fault_recovery_v7()

    # V8 owns canonical project-wide symbol/type/I/O identity over the complete
    # V1-V7 theorem stack. Its hardening pass removes flat pseudo-global symbols
    # that originate only from DDT/DFB interface `<variables>` containers.
    from devagent.plc.schneider_identity_types_v8 import install as _install_identity_types_v8
    from devagent.plc.schneider_identity_hardening_v8 import (
        install as _install_identity_hardening_v8,
    )
    _install_identity_types_v8()
    _install_identity_hardening_v8()

    # Production V5 imports shared production functions by value. Refresh those
    # bindings only after the complete Schneider V1-V8 vendor stack is installed.
    _v5.run_v4_verification = _production.run_production_verification
    _v5.evidence_index = _evidence.evidence_index
    _v5.detect_risks = _review.detect_risks
    _v5.optimization_candidates = _review.optimization_candidates
    _INSTALLED = True


__all__ = ["install"]
