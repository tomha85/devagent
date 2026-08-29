from __future__ import annotations

import re


_CONTROL_VERBS = r"force|write|bypass|reset|download|upload|start|stop|jog|set|override|command"
_DIRECT_CONTROL = re.compile(
    rf"^\s*(?:please\s+)?(?:{_CONTROL_VERBS})\b",
    flags=re.IGNORECASE,
)
_REQUEST_CONTROL = re.compile(
    rf"\b(?:can\s+you|could\s+you|would\s+you|please|how\s+do\s+i|how\s+can\s+i)\s+"
    rf"(?:{_CONTROL_VERBS})\b",
    flags=re.IGNORECASE,
)
_TURN_CONTROL = re.compile(
    r"^\s*(?:please\s+)?turn\s+(?:on|off)\b",
    flags=re.IGNORECASE,
)


def is_plc_control_request(text: str) -> bool:
    """Return True only for explicit PLC/machine control intent.

    Diagnostic wording such as "why did the motor stop?" is intentionally not
    blocked. Imperative/request wording such as "stop the motor" or "how do I
    force this tag" is blocked before any provider call.
    """

    value = str(text or "").strip()
    if not value:
        return False
    return bool(
        _DIRECT_CONTROL.search(value)
        or _REQUEST_CONTROL.search(value)
        or _TURN_CONTROL.search(value)
    )


def read_only_control_refusal() -> str:
    return (
        "DevAgent Live is read-only and will not execute or instruct PLC control changes "
        "such as write, force, bypass, reset, start/stop, jog, download, upload, override, "
        "or mode/output commands. Ask a diagnostic question instead, for example: "
        "'Why is Conveyor7_Run not active?' or 'Which permissive is blocking Conveyor7_Run?'"
    )


__all__ = ["is_plc_control_request", "read_only_control_refusal"]
