from __future__ import annotations

from typing import Any, Callable

from devagent.plc.agent_harness_v15 import begin_run_trace, end_run_trace
from devagent.plc import production_v5

_ORIGINAL: Callable[..., Any] = production_v5.run_production_verification_v5
_INSTALLED = False


def _run_with_harness_trace(*args: Any, **kwargs: Any):
    trace, token = begin_run_trace()
    try:
        result = _ORIGINAL(*args, **kwargs)
        result.ai_harness_trace = list(trace)
        return result
    finally:
        end_run_trace(token)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    production_v5.run_production_verification_v5 = _run_with_harness_trace
    _INSTALLED = True
