from __future__ import annotations

from typing import Any

from devagent.providers import ModelProvider

# The PLC agent harness is deliberately small and explicit. It is not part of the
# deterministic PLC proof engine. It only governs AI review candidates before
# they are admitted as AI_CANDIDATE observations.
MAX_REVIEW_ITERATIONS = 2

REVIEW_CRITIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decisions"],
    "properties": {
        "decisions": {
            "type": "array",
            "maxItems": 24,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["finding_id", "decision", "reason", "supported_evidence_ids"],
                "properties": {
                    "finding_id": {"type": "string", "minLength": 1},
                    "decision": {"type": "string", "enum": ["ACCEPT", "REVISE", "REJECT"]},
                    "reason": {"type": "string", "minLength": 1},
                    "supported_evidence_ids": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        }
    },
}

REQUIREMENT_CRITIC_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decisions"],
    "properties": {
        "decisions": {
            "type": "array",
            "maxItems": 50,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["requirement_id", "decision", "reason", "supported_evidence_ids"],
                "properties": {
                    "requirement_id": {"type": "string", "minLength": 1},
                    "decision": {"type": "string", "enum": ["ACCEPT", "REJECT"]},
                    "reason": {"type": "string", "minLength": 1},
                    "supported_evidence_ids": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        }
    },
}


def trace(
    sink: list[dict[str, Any]] | None,
    *,
    graph: str,
    node: str,
    iteration: int,
    outcome: str,
    detail: str,
    counts: dict[str, int] | None = None,
) -> None:
    if sink is None:
        return
    event: dict[str, Any] = {
        "graph": graph,
        "node": node,
        "iteration": iteration,
        "outcome": outcome,
        "detail": detail,
    }
    if counts:
        event["counts"] = dict(sorted(counts.items()))
    sink.append(event)


def critique_review_candidates(
    provider: ModelProvider,
    *,
    candidates: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    iteration: int,
    trace_sink: list[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    if not candidates:
        return {}
    trace(
        trace_sink,
        graph="PLC_ENGINEERING_REVIEW",
        node="CRITIC",
        iteration=iteration,
        outcome="START",
        detail="Evaluate AI review candidates for evidence support, boundedness, and unsupported runtime/safety claims.",
        counts={"candidates": len(candidates)},
    )
    response = provider.request(
        role="plc_engineering_critic",
        payload={
            "instruction": (
                "Act as an independent PLC review critic. Evaluate only the supplied candidate findings and evidence. "
                "ACCEPT only when the finding is directly supported by its cited evidence and does not claim runtime behavior, safety certification, PASS, or release readiness. "
                "Use REVISE when the core concern is supported but wording/claims overreach. REJECT unsupported findings. "
                "supported_evidence_ids must be a subset of evidence actually supporting the candidate."
            ),
            "candidates": candidates,
            "evidence": evidence,
        },
        schema=REVIEW_CRITIC_SCHEMA,
    )
    decisions = {
        str(item["finding_id"]): item
        for item in response.get("decisions", [])
        if isinstance(item, dict) and item.get("finding_id")
    }
    trace(
        trace_sink,
        graph="PLC_ENGINEERING_REVIEW",
        node="CRITIC",
        iteration=iteration,
        outcome="COMPLETE",
        detail="Independent critic completed candidate evaluation.",
        counts={
            "accepted": sum(1 for item in decisions.values() if item.get("decision") == "ACCEPT"),
            "revise": sum(1 for item in decisions.values() if item.get("decision") == "REVISE"),
            "rejected": sum(1 for item in decisions.values() if item.get("decision") == "REJECT"),
        },
    )
    return decisions


def critique_requirement_mappings(
    provider: ModelProvider,
    *,
    mappings: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
    iteration: int,
    trace_sink: list[dict[str, Any]] | None,
) -> dict[str, dict[str, Any]]:
    if not mappings:
        return {}
    trace(
        trace_sink,
        graph="PLC_REQUIREMENT_MAPPING",
        node="CRITIC",
        iteration=iteration,
        outcome="START",
        detail="Evaluate AI requirement trace candidates without permitting verification promotion.",
        counts={"mappings": len(mappings)},
    )
    response = provider.request(
        role="plc_requirement_critic",
        payload={
            "instruction": (
                "Evaluate only whether each proposed requirement-to-evidence trace candidate is plausibly supported by the supplied evidence. "
                "Never declare VERIFIED, PASS, SAFE, compliant, or release-ready. ACCEPT only grounded traceability candidates; otherwise REJECT."
            ),
            "mappings": mappings,
            "evidence": evidence,
        },
        schema=REQUIREMENT_CRITIC_SCHEMA,
    )
    decisions = {
        str(item["requirement_id"]): item
        for item in response.get("decisions", [])
        if isinstance(item, dict) and item.get("requirement_id")
    }
    trace(
        trace_sink,
        graph="PLC_REQUIREMENT_MAPPING",
        node="CRITIC",
        iteration=iteration,
        outcome="COMPLETE",
        detail="Independent requirement critic completed mapping evaluation.",
        counts={
            "accepted": sum(1 for item in decisions.values() if item.get("decision") == "ACCEPT"),
            "rejected": sum(1 for item in decisions.values() if item.get("decision") == "REJECT"),
        },
    )
    return decisions
