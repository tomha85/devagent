from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


ProjectLoader = Callable[[Path], Any]


def normalize_engineering_identifier(value: str | None) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip()


def _source_locator(source: Any) -> str:
    locator = getattr(source, "locator", None)
    if locator:
        return str(locator)
    return str(source or "").strip()


def _program_from_scope(scope: str) -> str | None:
    raw = str(scope or "").strip()
    if ":" not in raw:
        return None
    prefix, value = raw.split(":", 1)
    if prefix.strip().casefold() != "program" or not value.strip():
        return None
    return value.strip()


@dataclass(frozen=True)
class LiveEngineeringTag:
    id: str
    name: str
    scope: str
    data_type: str
    description: str | None
    external_access: str | None
    alias_for: str | None

    def identity_forms(self) -> tuple[str, ...]:
        forms = {normalize_engineering_identifier(self.name)}
        program = _program_from_scope(self.scope)
        if program:
            forms.add(normalize_engineering_identifier(f"{program}.{self.name}"))
            forms.add(normalize_engineering_identifier(f"Program:{program}.{self.name}"))
        return tuple(sorted(item for item in forms if item))


@dataclass(frozen=True)
class LiveLogicTerm:
    tag_reference: str
    required: bool


@dataclass(frozen=True)
class LiveLogicPath:
    terms: tuple[LiveLogicTerm, ...]


@dataclass(frozen=True)
class LiveLogicRule:
    id: str
    output_tag: str
    instruction: str
    paths: tuple[LiveLogicPath, ...]
    source_locator: str
    language: str
    origin: str
    semantic_state: str
    evidence_id: str


@dataclass(frozen=True)
class LiveLogicStatement:
    id: str
    language: str
    owner_type: str
    owner_name: str
    routine: str
    locator: str
    text: str
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    calls: tuple[str, ...]
    semantic_state: str
    source_locator: str


@dataclass(frozen=True)
class LiveEngineeringContext:
    vendor: str
    engineering_tool: str
    controller_name: str
    source_path: str
    source_sha256: str
    full_project: bool
    tags: tuple[LiveEngineeringTag, ...]
    rules: tuple[LiveLogicRule, ...]
    statements: tuple[LiveLogicStatement, ...]
    limitations: tuple[str, ...]

    def tag_by_id(self) -> dict[str, LiveEngineeringTag]:
        return {tag.id: tag for tag in self.tags}

    def tags_for_reference(self, reference: str) -> tuple[LiveEngineeringTag, ...]:
        target = normalize_engineering_identifier(reference)
        if not target:
            return ()
        return tuple(
            tag
            for tag in self.tags
            if target in tag.identity_forms()
        )

    def unique_tag_for_reference(self, reference: str) -> LiveEngineeringTag | None:
        matches = self.tags_for_reference(reference)
        return matches[0] if len(matches) == 1 else None

    def rules_for_output(self, output_reference: str) -> tuple[LiveLogicRule, ...]:
        target = normalize_engineering_identifier(output_reference)
        if not target:
            return ()
        return tuple(
            rule
            for rule in self.rules
            if normalize_engineering_identifier(rule.output_tag) == target
        )

    def output_names(self) -> tuple[str, ...]:
        seen: set[str] = set()
        ordered: list[str] = []
        for rule in self.rules:
            key = normalize_engineering_identifier(rule.output_tag)
            if key and key not in seen:
                seen.add(key)
                ordered.append(rule.output_tag)
        return tuple(ordered)


@dataclass(frozen=True)
class LiveLoadedEngineering:
    source_path: Path
    engineering: Any
    project: Any
    context: LiveEngineeringContext


def _extract_project(engineering: Any) -> Any:
    project = getattr(engineering, "project", None)
    if project is not None:
        return project
    nested = getattr(engineering, "engineering", None)
    project = getattr(nested, "project", None) if nested is not None else None
    if project is not None:
        return project
    raise ValueError(
        "PLC engineering analysis did not expose a canonical project for DevAgent Live"
    )


def _rule_evidence_id(source_sha256: str, rule: Any) -> str:
    payload = {
        "project_sha256": source_sha256,
        "id": str(getattr(rule, "id", "") or ""),
        "output_tag": str(getattr(rule, "output_tag", "") or ""),
        "instruction": str(getattr(rule, "instruction", "") or ""),
        "source": _source_locator(getattr(rule, "source", None)),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"LIVE-ENG-LOGIC:{digest}"


def _build_tags(raw_tags: Iterable[Any]) -> tuple[LiveEngineeringTag, ...]:
    result: list[LiveEngineeringTag] = []
    seen: set[str] = set()
    for raw in raw_tags:
        tag_id = str(getattr(raw, "id", "") or "").strip()
        name = str(getattr(raw, "name", "") or "").strip()
        if not tag_id or not name:
            continue
        if tag_id in seen:
            raise ValueError(f"Duplicate canonical engineering tag id: {tag_id}")
        seen.add(tag_id)
        result.append(
            LiveEngineeringTag(
                id=tag_id,
                name=name,
                scope=str(getattr(raw, "scope", "") or "").strip(),
                data_type=str(getattr(raw, "data_type", "") or "").strip(),
                description=(
                    str(getattr(raw, "description", "")).strip()
                    if getattr(raw, "description", None)
                    else None
                ),
                external_access=(
                    str(getattr(raw, "external_access", "")).strip()
                    if getattr(raw, "external_access", None) is not None
                    else None
                ),
                alias_for=(
                    str(getattr(raw, "alias_for", "")).strip()
                    if getattr(raw, "alias_for", None)
                    else None
                ),
            )
        )
    return tuple(result)


def _build_rules(
    raw_rules: Iterable[Any],
    *,
    source_sha256: str,
) -> tuple[LiveLogicRule, ...]:
    rules: list[LiveLogicRule] = []
    for raw in raw_rules:
        output_tag = str(getattr(raw, "output_tag", "") or "").strip()
        if not output_tag:
            continue
        paths: list[LiveLogicPath] = []
        for raw_path in tuple(getattr(raw, "paths", ()) or ()):
            terms = tuple(
                LiveLogicTerm(
                    tag_reference=str(getattr(term, "tag", "") or "").strip(),
                    required=bool(getattr(term, "required", False)),
                )
                for term in tuple(getattr(raw_path, "terms", ()) or ())
                if str(getattr(term, "tag", "") or "").strip()
            )
            paths.append(LiveLogicPath(terms=terms))
        rules.append(
            LiveLogicRule(
                id=str(getattr(raw, "id", "") or "").strip() or f"rule-{len(rules)+1}",
                output_tag=output_tag,
                instruction=str(getattr(raw, "instruction", "") or "").strip(),
                paths=tuple(paths),
                source_locator=_source_locator(getattr(raw, "source", None)),
                language=str(getattr(raw, "language", "") or "").strip(),
                origin=str(getattr(raw, "origin", "") or "").strip(),
                semantic_state=_enum_value(getattr(raw, "semantic_state", "")),
                evidence_id=_rule_evidence_id(source_sha256, raw),
            )
        )
    return tuple(rules)


def _build_statements(raw_statements: Iterable[Any]) -> tuple[LiveLogicStatement, ...]:
    statements: list[LiveLogicStatement] = []
    for raw in raw_statements:
        writes = tuple(
            str(item).strip()
            for item in tuple(getattr(raw, "writes", ()) or ())
            if str(item).strip()
        )
        reads = tuple(
            str(item).strip()
            for item in tuple(getattr(raw, "reads", ()) or ())
            if str(item).strip()
        )
        statements.append(
            LiveLogicStatement(
                id=str(getattr(raw, "id", "") or "").strip() or f"stmt-{len(statements)+1}",
                language=str(getattr(raw, "language", "") or "").strip(),
                owner_type=str(getattr(raw, "owner_type", "") or "").strip(),
                owner_name=str(getattr(raw, "owner_name", "") or "").strip(),
                routine=str(getattr(raw, "routine", "") or "").strip(),
                locator=str(getattr(raw, "locator", "") or "").strip(),
                text=str(getattr(raw, "text", "") or "").strip(),
                reads=reads,
                writes=writes,
                calls=tuple(
                    str(item).strip()
                    for item in tuple(getattr(raw, "calls", ()) or ())
                    if str(item).strip()
                ),
                semantic_state=_enum_value(getattr(raw, "semantic_state", "")),
                source_locator=_source_locator(getattr(raw, "source", None)),
            )
        )
    return tuple(statements)


def build_live_engineering_context(engineering: Any) -> LiveEngineeringContext:
    project = _extract_project(engineering)
    metadata = getattr(project, "metadata", None)
    if metadata is None:
        raise ValueError("Canonical PLC project does not expose metadata")

    source_sha256 = str(getattr(metadata, "source_sha256", "") or "").strip()
    tags = _build_tags(tuple(getattr(project, "tags", ()) or ()))
    rules = _build_rules(
        tuple(getattr(project, "output_logic", ()) or ()),
        source_sha256=source_sha256,
    )
    statements = _build_statements(
        tuple(getattr(project, "logic_statements", ()) or ())
    )

    limitations: list[str] = []
    for warning in tuple(getattr(project, "warnings", ()) or ()):
        text = str(warning).strip()
        if text and text not in limitations:
            limitations.append(text)
    if not rules:
        limitations.append(
            "Canonical engineering model exposes no deterministically evaluable output logic; "
            "Live may show source context but cannot prove blockers from PLC logic."
        )
    partial_rules = [
        rule.id
        for rule in rules
        if rule.semantic_state and rule.semantic_state.upper() != "FULL"
    ]
    if partial_rules:
        limitations.append(
            f"{len(partial_rules)} output logic rule(s) are not FULL semantic coverage; "
            "DevAgent Live must not treat those rules as complete controller behavior."
        )

    return LiveEngineeringContext(
        vendor=str(getattr(metadata, "vendor", "") or "").strip(),
        engineering_tool=str(getattr(metadata, "engineering_tool", "") or "").strip(),
        controller_name=str(getattr(metadata, "controller_name", "") or "").strip(),
        source_path=str(getattr(metadata, "source_path", "") or "").strip(),
        source_sha256=source_sha256,
        full_project=bool(getattr(metadata, "full_project", False)),
        tags=tags,
        rules=rules,
        statements=statements,
        limitations=tuple(limitations),
    )


def load_live_engineering_context(
    path: Path,
    *,
    project_loader: ProjectLoader | None = None,
) -> LiveLoadedEngineering:
    source_path = Path(path).expanduser().resolve(strict=False)
    if project_loader is None:
        from devagent.plc.plc_dispatch import analyze_plc_project

        project_loader = analyze_plc_project
    engineering = project_loader(source_path)
    project = _extract_project(engineering)
    return LiveLoadedEngineering(
        source_path=source_path,
        engineering=engineering,
        project=project,
        context=build_live_engineering_context(engineering),
    )


__all__ = [
    "LiveEngineeringContext",
    "LiveEngineeringTag",
    "LiveLoadedEngineering",
    "LiveLogicPath",
    "LiveLogicRule",
    "LiveLogicStatement",
    "LiveLogicTerm",
    "build_live_engineering_context",
    "load_live_engineering_context",
    "normalize_engineering_identifier",
]
