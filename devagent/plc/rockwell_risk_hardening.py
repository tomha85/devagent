from __future__ import annotations

from collections import defaultdict

from devagent.plc import production_review as _review
from devagent.plc.production_models import RiskFinding, Severity
from devagent.plc.production_utils import stable_id
from devagent.plc.rockwell_alias_hardening import canonical_tag_identity, canonical_writer_sources

_ORIGINAL_DETECT_RISKS = _review.detect_risks


def _external_writes(project):
    for rung in project.rungs:
        for write in rung.writes:
            yield write, rung.program
    for statement in project.logic_statements:
        if statement.owner_type == "aoi":
            continue
        program = statement.source.program or (
            statement.owner_name if statement.owner_type == "program" else None
        )
        for write in statement.writes:
            yield write, program


def _canonical_multi_writer_risks(project) -> list[RiskFinding]:
    representatives: dict[tuple[str, str], tuple[str, str | None]] = {}
    for ref, program in _external_writes(project):
        identity = canonical_tag_identity(project, ref, program)
        representatives.setdefault(identity, (ref, program))

    result: list[RiskFinding] = []
    for identity, (ref, program) in sorted(representatives.items(), key=lambda item: item[0]):
        sources = canonical_writer_sources(project, ref, program)
        if len(sources) <= 1:
            continue
        subject = f"{identity[0]}::{identity[1]}"
        result.append(
            RiskFinding(
                stable_id("RISK", "MULTI_WRITER_CANONICAL", subject),
                "MULTIPLE_WRITERS",
                f"Multiple canonical writers for {subject}",
                Severity.MEDIUM,
                f"{subject} is written by {len(sources)} distinct executable sources after Rockwell scope/case/AliasFor normalization.",
                "Final value can depend on task/scan order, retentive behavior, or a later cross-language/alias write.",
                "Review writer precedence and consolidate or explicitly document intentional ownership before release.",
                sources,
            )
        )
    return result


def _canonical_retention_risks(project) -> list[RiskFinding]:
    latched: dict[tuple[str, str], list[str]] = defaultdict(list)
    unlatched: set[tuple[str, str]] = set()
    labels: dict[tuple[str, str], str] = {}

    for rung in project.rungs:
        for instruction in rung.instructions:
            if not instruction.arguments:
                continue
            name = instruction.name.upper()
            if name not in {"OTL", "OTU"}:
                continue
            operand = instruction.arguments[0].strip()
            if not operand:
                continue
            identity = canonical_tag_identity(project, operand, rung.program)
            labels.setdefault(identity, operand)
            if name == "OTL":
                latched[identity].append(rung.id)
            else:
                unlatched.add(identity)

    result: list[RiskFinding] = []
    for identity, evidence_ids in sorted(latched.items(), key=lambda item: item[0]):
        if identity in unlatched:
            continue
        subject = f"{identity[0]}::{identity[1]}"
        result.append(
            RiskFinding(
                stable_id("RISK", "LATCH_CANONICAL", subject),
                "RETENTIVE_LOGIC",
                f"Latched output {labels.get(identity, subject)} has no modeled canonical OTU writer",
                Severity.MEDIUM,
                "An OTL writer was found without a corresponding OTU after Rockwell scope/case/AliasFor normalization.",
                "A retained command/state may remain set longer than intended if reset logic is external, protected, unsupported, or absent.",
                "Confirm the reset/unlatch path and attach external/AOI/hardware evidence when the reset is outside normalized RLL.",
                tuple(sorted(set(evidence_ids))),
            )
        )
    return result


def detect_risks(engineering, verifications, executions, engineering_findings):
    """Replace raw-name Rockwell writer/latch risks with canonical equivalents."""
    risks = [
        risk
        for risk in _ORIGINAL_DETECT_RISKS(
            engineering,
            verifications,
            executions,
            engineering_findings,
        )
        if risk.category not in {"MULTIPLE_WRITERS", "RETENTIVE_LOGIC"}
    ]
    risks.extend(_canonical_multi_writer_risks(engineering.project))
    risks.extend(_canonical_retention_risks(engineering.project))
    return risks


def install() -> None:
    _review.detect_risks = detect_risks


__all__ = ["detect_risks", "install"]
