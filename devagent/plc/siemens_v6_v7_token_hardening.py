from __future__ import annotations

from dataclasses import replace
import re

from devagent.plc import siemens_interlock_permissive_v6 as _v6
from devagent.plc import siemens_recovery_v7 as _v7


_INSTALLED = False
_PREVIOUS_RISKS = _v7._risks

_PERMISSIVE_TOKENS = {
    "permissive",
    "permit",
    "ready",
    "healthy",
    "okay",
    "available",
    "enable",
    "enabled",
}
_INTERLOCK_TOKENS = {
    "interlock",
    "estop",
    "emergency",
    "guard",
    "door",
    "trip",
    "tripped",
    "fault",
    "faulted",
    "inhibit",
    "safe",
    "safety",
}
_RECOVERY_TOKENS = {
    "reset",
    "recover",
    "recovery",
    "ack",
    "acknowledge",
    "clear",
    "cleared",
    "restart",
}
_FAULT_STATE_TOKENS = {
    "fault",
    "faulted",
    "error",
    "trip",
    "tripped",
    "alarm",
    "abort",
    "aborted",
    "failed",
    "failure",
}
_NONFAULT_STATE_TOKENS = {
    "no",
    "normal",
    "healthy",
    "ok",
    "okay",
    "clear",
    "cleared",
    "reset",
    "ready",
}


def _semantic_tokens(*values: object) -> set[str]:
    """Tokenize PLC identifiers/descriptions without substring semantics.

    A lower/digit-to-uppercase boundary is treated as a CamelCase separator, so
    ``MotorReady`` becomes ``motor``/``ready`` while ``AlreadyDone`` becomes
    ``already``/``done`` and cannot accidentally match ``ready``. Acronym-style
    identifiers such as ``EStopHealthy`` retain the useful ``estop`` token.
    """

    tokens: set[str] = set()
    for value in values:
        text = str(value or "")
        text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
        tokens.update(
            token.casefold()
            for token in re.findall(r"[A-Za-z0-9]+", text)
            if token
        )
    return tokens


def _role_for_ref(ref: str, description: str | None) -> str:
    base = ref.split(".", 1)[0]
    tokens = _semantic_tokens(base, description)
    if tokens & _RECOVERY_TOKENS:
        return "RECOVERY"
    if tokens & _INTERLOCK_TOKENS:
        return "INTERLOCK"
    if tokens & _PERMISSIVE_TOKENS:
        return "PERMISSIVE"
    return "GUARD"


def _is_fault_state(value: str) -> bool:
    if re.fullmatch(r"[-+]?\d+", value):
        return False
    tokens = _semantic_tokens(value)
    if tokens & _NONFAULT_STATE_TOKENS:
        return False
    return bool(tokens & _FAULT_STATE_TOKENS)


def _risks(previous, engineering, verifications, executions, engineering_findings):
    result = list(
        _PREVIOUS_RISKS(
            previous,
            engineering,
            verifications,
            executions,
            engineering_findings,
        )
    )
    hardened = []
    for risk in result:
        if (
            risk.category == "FAULT_RECOVERY"
            and "overlapping recovery and non-recovery transitions" in risk.title
        ):
            risk = replace(
                risk,
                title=risk.title.replace(
                    "overlapping recovery and non-recovery transitions",
                    "overlapping recovery and competing transitions",
                ),
            )
        hardened.append(risk)
    return hardened


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    # These are classification/report hardenings only. They do not alter the V5
    # source transition theorem or promote any runtime-dependent behavior to
    # static proof. Patch the globals consumed by V6/V7 before analysis runs.
    _v6._role_for_ref = _role_for_ref
    _v7._is_fault_state = _is_fault_state
    _v7._risks = _risks
    _INSTALLED = True

    # V8 runs after the fully-qualified V7 analyzer. It adds project-wide,
    # scope-aware Siemens symbol/type identity and preflight input bounds without
    # widening any V1-V7 source theorem.
    from devagent.plc.siemens_identity_types_v8 import install as _install_siemens_identity_types_v8

    _install_siemens_identity_types_v8()

    # V9 is the commercial closeout layer. It accounts for every imported
    # executable/protected/call region as FULL/PARTIAL/OPAQUE/PROTECTED and
    # keeps every non-FULL boundary visible in evidence, risks, and reporting.
    from devagent.plc.siemens_closeout_v9 import install as _install_siemens_closeout_v9

    _install_siemens_closeout_v9()

    # Compatibility hardening deliberately runs last. It preserves the proven
    # V1-V7 schema provenance and outcome semantics while retaining all V8/V9
    # identity/support evidence and release risks.
    from devagent.plc.siemens_v8_v9_compat_hardening import install as _install_siemens_v8_v9_compat_hardening

    _install_siemens_v8_v9_compat_hardening()


__all__ = ["install"]
