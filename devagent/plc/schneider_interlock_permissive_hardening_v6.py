from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from devagent.plc.models import PLCSemanticState
from devagent.plc import schneider_interlock_permissive_v6 as _v6


_INSTALLED = False
_PREVIOUS_OUTPUT_CONTRACTS = _v6._output_contracts


def _hardened_output_contracts(project):
    """Withhold all-path output proof when any duplicate output theorem exists.

    V6 must never treat one FULL output theorem as unique while a second
    normalized theorem for the same output is also present but PARTIAL. The
    upstream V1-V5 writer layers remain authoritative; this is a final V6
    fail-closed uniqueness guard over their exported output-logic inventory.
    """

    contracts = list(_PREVIOUS_OUTPUT_CONTRACTS(project))
    counts: dict[str, int] = defaultdict(int)
    for logic in project.output_logic:
        counts[logic.output_tag.casefold()] += 1

    result = []
    for contract in contracts:
        if counts[contract.output_tag.casefold()] <= 1:
            result.append(contract)
            continue
        result.append(
            replace(
                contract,
                all_path_terms=(),
                semantic_state=PLCSemanticState.PARTIAL,
                reason="ambiguous_multiple_output_theorems",
            )
        )
    return tuple(result)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _v6._output_contracts = _hardened_output_contracts
    _INSTALLED = True


__all__ = ["install"]
