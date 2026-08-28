from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from devagent.plc.models import PLCSemanticState
from devagent.plc import schneider_interlock_permissive_v6 as _v6


_INSTALLED = False
_PREVIOUS_OUTPUT_CONTRACTS = _v6._output_contracts


def _hardened_output_contracts(project):
    """Withhold all-path output proof when any duplicate output theorem exists."""
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


def _rewrite_v6(value: str) -> str:
    text = str(value)
    for old in (
        "Schneider Control Expert V1",
        "Schneider Control Expert V2",
        "Schneider Control Expert V3",
        "Schneider Control Expert V4",
        "Schneider Control Expert V5",
        "Schneider V1",
        "Schneider V2",
        "Schneider V3",
        "Schneider V4",
        "Schneider V5",
    ):
        text = text.replace(old, "Schneider V6")
    for version in ("V1", "V2", "V3", "V4", "V5"):
        text = text.replace(f"under the {version} contract", "under the V6 contract")
    return text


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import schneider_integration_v1 as _integration

    _v6._output_contracts = _hardened_output_contracts

    previous_evidence = _integration._evidence_index
    previous_findings = _integration._findings

    def evidence_index(engineering):
        items = list(previous_evidence(engineering))
        if _v6._facts(engineering.project) is None:
            return items
        return [
            replace(item, summary=_rewrite_v6(item.summary))
            if item.kind == "SCHNEIDER_CONTROL_EXPERT_CAPABILITY_PROFILE"
            else item
            for item in items
        ]

    def findings(engineering, valid_evidence_ids):
        items = list(previous_findings(engineering, valid_evidence_ids))
        if _v6._facts(engineering.project) is None:
            return items
        return [
            replace(
                item,
                title=_rewrite_v6(item.title),
                summary=_rewrite_v6(item.summary),
                recommendation=_rewrite_v6(item.recommendation),
            )
            for item in items
        ]

    _integration._evidence_index = evidence_index
    _integration._findings = findings
    _INSTALLED = True


__all__ = ["install"]
