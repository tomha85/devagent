from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re

from devagent.plc.models import PLCSemanticState
from devagent.plc.schneider_closeout_v9 import analyze_schneider_control_expert_v9


SCHEMA = "devagent-schneider-real-st-gap-analysis-v1"

_ASSIGNMENT = re.compile(r"^\s*(?P<lhs>.+?)\s*:=\s*(?P<rhs>.+?)\s*;?\s*$", re.DOTALL)
_CALL = re.compile(r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_.]*)\s*\(", re.IGNORECASE)
_CONTROL = re.compile(r"^\s*(IF|ELSIF|ELSE|CASE|FOR|WHILE|REPEAT)\b", re.IGNORECASE)
_BOOL_TOKEN = re.compile(
    r"\s*(\(|\)|\bAND\b|\bOR\b|\bXOR\b|\bNOT\b|\bTRUE\b|\bFALSE\b|"
    r"%[A-Za-z]+[A-Za-z0-9_.]*|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)",
    re.IGNORECASE,
)
_COMPARISON = re.compile(r"(?:<>|<=|>=|(?<!:)=|<|>)")
_ARITHMETIC = re.compile(r"(?:\+|-|\*|/|\bMOD\b|\bDIV\b)", re.IGNORECASE)
_FUNCTION_EXPR = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*\s*\(")
_INDEXED = re.compile(r"\[[^\]]+\]")


@dataclass(frozen=True)
class SchneiderSTGapSample:
    statement_id: str
    owner: str
    locator: str
    category: str
    features: tuple[str, ...]
    source_text: str | None


@dataclass(frozen=True)
class SchneiderSTGapCluster:
    category: str
    count: int
    features: tuple[tuple[str, int], ...]
    samples: tuple[SchneiderSTGapSample, ...]


@dataclass(frozen=True)
class SchneiderSTGapAnalysis:
    schema: str
    source_sha256: str
    outcome: str
    support_contract: str
    total_st_statements: int
    partial_st_statements: int
    clusters: tuple[SchneiderSTGapCluster, ...]


def _bool_expression_shape(expr: str) -> bool:
    """Return True only for the same bounded lexical Boolean surface as V1.

    This is diagnostic classification, not a theorem. It deliberately does not
    promote a statement to FULL or infer why a later layer kept it PARTIAL.
    """

    position = 0
    seen = False
    while position < len(expr):
        if expr[position:].strip() == "":
            break
        match = _BOOL_TOKEN.match(expr, position)
        if match is None:
            return False
        seen = True
        position = match.end()
    return seen


def classify_partial_st(text: str) -> tuple[str, tuple[str, ...]]:
    """Classify one already-PARTIAL Control Expert ST statement deterministically.

    Categories describe syntax only. They intentionally do not claim the cause of
    PARTIAL because V5-V9 may withhold a statement for control-flow, type, scope,
    writer-ownership, call-closure, or other fail-closed reasons.
    """

    clean = str(text or "").strip()
    features: set[str] = set()
    upper = clean.upper()

    control = _CONTROL.match(clean)
    if control:
        keyword = control.group(1).upper()
        features.add(keyword)
        if _COMPARISON.search(clean):
            features.add("COMPARISON")
        if _FUNCTION_EXPR.search(clean):
            features.add("FUNCTION_EXPRESSION")
        return "CONTROL_FLOW", tuple(sorted(features))

    call = _CALL.match(clean)
    if call:
        features.add("CALL")
        features.add(f"CALL:{call.group('name').upper()}")
        return "CALL_STATEMENT", tuple(sorted(features))

    assignment = _ASSIGNMENT.match(clean)
    if assignment:
        lhs = assignment.group("lhs").strip()
        rhs = assignment.group("rhs").strip()
        if _INDEXED.search(lhs) or _INDEXED.search(rhs):
            features.add("INDEXED_ACCESS")
        if "." in lhs or re.search(r"\b[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_.]*\b", rhs):
            features.add("STRUCTURED_REF")
        if _FUNCTION_EXPR.search(rhs):
            features.add("FUNCTION_EXPRESSION")
        if _COMPARISON.search(rhs):
            features.add("COMPARISON")
        if _ARITHMETIC.search(rhs):
            features.add("ARITHMETIC")

        lhs_is_simple = bool(
            re.fullmatch(
                r"%[A-Za-z]+[A-Za-z0-9_.]*|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*",
                lhs,
            )
        )
        if lhs_is_simple and _bool_expression_shape(rhs):
            features.add("BOUNDED_BOOLEAN_SHAPE")
            return "BOUNDED_BOOLEAN_SHAPE_PARTIAL", tuple(sorted(features))
        if "INDEXED_ACCESS" in features:
            return "INDEXED_ASSIGNMENT", tuple(sorted(features))
        if "FUNCTION_EXPRESSION" in features:
            return "FUNCTION_EXPRESSION_ASSIGNMENT", tuple(sorted(features))
        if "COMPARISON" in features:
            return "COMPARISON_ASSIGNMENT", tuple(sorted(features))
        if "ARITHMETIC" in features:
            return "ARITHMETIC_ASSIGNMENT", tuple(sorted(features))
        if not lhs_is_simple:
            features.add("COMPLEX_LHS")
            return "COMPLEX_LHS_ASSIGNMENT", tuple(sorted(features))
        return "OTHER_ASSIGNMENT", tuple(sorted(features))

    if "RETURN" in upper:
        features.add("RETURN")
        return "RETURN_OR_EXIT", tuple(sorted(features))
    if _FUNCTION_EXPR.search(clean):
        features.add("FUNCTION_EXPRESSION")
        return "FUNCTION_OR_CALL_EXPRESSION", tuple(sorted(features))
    return "OTHER_STATEMENT", tuple(sorted(features))


def analyze_schneider_real_st_gaps(
    path: str | Path,
    *,
    samples_per_cluster: int = 5,
    include_source_text: bool = False,
) -> SchneiderSTGapAnalysis:
    """Analyze current V9 PARTIAL ST statements without widening any theorem."""

    result = analyze_schneider_control_expert_v9(Path(path))
    facts = getattr(result.project, "_schneider_v9_closeout_facts", None)
    support_contract = facts.support.contract if facts is not None else "NONE"

    st_statements = [item for item in result.project.logic_statements if item.language.upper() == "ST"]
    partial = [item for item in st_statements if item.semantic_state is PLCSemanticState.PARTIAL]

    buckets: dict[str, list[SchneiderSTGapSample]] = defaultdict(list)
    feature_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for statement in partial:
        category, features = classify_partial_st(statement.text)
        sample = SchneiderSTGapSample(
            statement_id=statement.id,
            owner=statement.owner_name,
            locator=statement.locator,
            category=category,
            features=features,
            source_text=statement.text if include_source_text else None,
        )
        buckets[category].append(sample)
        feature_counts[category].update(features)

    clusters = tuple(
        SchneiderSTGapCluster(
            category=category,
            count=len(samples),
            features=tuple(sorted(feature_counts[category].items(), key=lambda item: (-item[1], item[0]))),
            samples=tuple(samples[: max(0, samples_per_cluster)]),
        )
        for category, samples in sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0]))
    )

    return SchneiderSTGapAnalysis(
        schema=SCHEMA,
        source_sha256=result.project.metadata.source_sha256,
        outcome=result.outcome.value,
        support_contract=support_contract,
        total_st_statements=len(st_statements),
        partial_st_statements=len(partial),
        clusters=clusters,
    )


def result_payload(result: SchneiderSTGapAnalysis) -> dict[str, object]:
    return asdict(result)


def render_markdown(result: SchneiderSTGapAnalysis) -> str:
    lines = [
        "# Schneider Real ST Gap Analysis",
        "",
        f"- Schema: `{result.schema}`",
        f"- Source SHA-256: `{result.source_sha256}`",
        f"- Engineering outcome: `{result.outcome}`",
        f"- V9 support contract: `{result.support_contract}`",
        f"- ST statements: **{result.total_st_statements}**",
        f"- PARTIAL ST statements: **{result.partial_st_statements}**",
        "",
        "The clusters below describe source syntax only; they do not change or explain away the fail-closed V9 semantic verdict.",
        "",
        "## Clusters",
        "",
        "| Rank | Category | Count | Dominant features |",
        "| ---: | --- | ---: | --- |",
    ]
    for index, cluster in enumerate(result.clusters, start=1):
        feature_text = ", ".join(f"{name}={count}" for name, count in cluster.features[:5]) or "none"
        lines.append(f"| {index} | `{cluster.category}` | {cluster.count} | {feature_text} |")

    for cluster in result.clusters:
        if not cluster.samples:
            continue
        lines.extend(["", f"### {cluster.category} ({cluster.count})", ""])
        for sample in cluster.samples:
            lines.append(f"- `{sample.owner}` / `{sample.locator}` / `{sample.statement_id}`")
            if sample.features:
                lines.append(f"  - features: {', '.join(sample.features)}")
            if sample.source_text is not None:
                compact = " ".join(sample.source_text.split())
                lines.append(f"  - source: `{compact}`")
    return "\n".join(lines) + "\n"


def write_reports(
    result: SchneiderSTGapAnalysis,
    *,
    json_path: str | Path,
    markdown_path: str | Path,
) -> None:
    json_target = Path(json_path)
    markdown_target = Path(markdown_path)
    json_target.parent.mkdir(parents=True, exist_ok=True)
    markdown_target.parent.mkdir(parents=True, exist_ok=True)
    json_target.write_text(json.dumps(result_payload(result), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown_target.write_text(render_markdown(result), encoding="utf-8")


__all__ = [
    "SCHEMA",
    "SchneiderSTGapAnalysis",
    "SchneiderSTGapCluster",
    "SchneiderSTGapSample",
    "analyze_schneider_real_st_gaps",
    "classify_partial_st",
    "render_markdown",
    "result_payload",
    "write_reports",
]
