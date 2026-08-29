from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping

from .engineering_context import (
    LiveEngineeringContext,
    LiveLogicStatement,
    normalize_engineering_identifier,
)


class LiveAdvancedKind(str, Enum):
    NUMERIC_COMPARISON = "NUMERIC_COMPARISON"
    ONE_SHOT = "ONE_SHOT"
    LATCH = "LATCH"
    HANDSHAKE = "HANDSHAKE"
    AOI_FB = "AOI_FB"
    FAULT_CODE = "FAULT_CODE"
    SEQUENCER = "SEQUENCER"
    MOTION = "MOTION"
    PID = "PID"
    UDT = "UDT"
    ARRAY = "ARRAY"


@dataclass(frozen=True)
class LiveNumericOperand:
    reference: str | None = None
    literal: float | int | None = None

    @property
    def display(self) -> str:
        return self.reference if self.reference is not None else repr(self.literal)


@dataclass(frozen=True)
class LiveNumericComparison:
    id: str
    result_tag: str | None
    left: LiveNumericOperand
    operator: str
    right: LiveNumericOperand
    source_locator: str
    semantic_state: str
    origin: str

    @property
    def references(self) -> tuple[str, ...]:
        result: list[str] = []
        for item in (self.left.reference, self.right.reference, self.result_tag):
            if item and item not in result:
                result.append(item)
        return tuple(result)


@dataclass(frozen=True)
class LiveAdvancedModel:
    id: str
    kind: LiveAdvancedKind
    name: str
    instruction: str
    references: tuple[str, ...]
    source_locators: tuple[str, ...]
    semantic_state: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LiveAdvancedCoverage:
    numeric_comparisons: tuple[LiveNumericComparison, ...]
    models: tuple[LiveAdvancedModel, ...]
    limitations: tuple[str, ...]

    def count(self, kind: LiveAdvancedKind) -> int:
        if kind is LiveAdvancedKind.NUMERIC_COMPARISON:
            return len(self.numeric_comparisons)
        return sum(item.kind is kind for item in self.models)


_SIMPLE_REF = r'[A-Za-z_#%][A-Za-z0-9_#%\.\[\]:]*'
_SIMPLE_NUM = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)'
_OPERAND = rf'(?:{_SIMPLE_REF}|{_SIMPLE_NUM})'
_ASSIGN_COMPARE_EXACT_RE = re.compile(
    rf'^\s*(?P<result>{_SIMPLE_REF})\s*:=\s*(?P<left>{_OPERAND})\s*'
    rf'(?P<op>>=|<=|<>|!=|==|=|>|<)\s*(?P<right>{_OPERAND})\s*;?\s*$',
    re.IGNORECASE,
)
_BARE_COMPARE_RE = re.compile(
    rf'(?P<left>{_OPERAND})\s*(?P<op>>=|<=|<>|!=|==|=|>|<)\s*(?P<right>{_OPERAND})',
    re.IGNORECASE,
)
_RLL_COMPARE_RE = re.compile(
    rf'\b(?P<op>GRT|GEQ|LES|LEQ|EQU|NEQ)\s*\(\s*'
    rf'(?P<left>{_OPERAND})\s*,\s*(?P<right>{_OPERAND})\s*\)',
    re.IGNORECASE,
)

_OPERATOR_MAP = {
    ">": ">",
    "GRT": ">",
    ">=": ">=",
    "GEQ": ">=",
    "<": "<",
    "LES": "<",
    "<=": "<=",
    "LEQ": "<=",
    "=": "==",
    "==": "==",
    "EQU": "==",
    "<>": "!=",
    "!=": "!=",
    "NEQ": "!=",
}

_ONE_SHOT_NAMES = {
    "ONS", "OSR", "OSF", "R_TRIG", "F_TRIG", "R_EDGE", "F_EDGE", "P_TRIG", "N_TRIG",
}
_LATCH_NAMES = {"OTL", "OTU", "SET", "RESET", "SR", "RS", "LATCH", "UNLATCH"}
_SEQUENCER_NAMES = {"SQO", "SQC", "SQL", "SEQ", "SEQUENCER"}
_PID_NAMES = {"PID", "PIDE", "PID_COMPACT", "CONT_C", "FB41", "PIDFF", "PID_AT"}
_ROCKWELL_MOTION_NAMES = {
    "MAJ", "MAS", "MASD", "MAM", "MAH", "MSO", "MSF", "MAFR", "MAG", "MDAC", "MDO", "MDS",
}

_HANDSHAKE_SUFFIXES = (
    ("REQUEST", ("request", "req")),
    ("ACK", ("acknowledge", "ack")),
    ("BUSY", ("busy",)),
    ("DONE", ("complete", "completed", "done")),
    ("ERROR", ("error", "fault")),
    ("TIMEOUT", ("timeout", "timedout")),
)
_FAULT_CODE_SUFFIXES = ("faultcode", "errorcode", "alarmcode", "diagcode", "diagnosticcode")


def _enum_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip()


def _source_locator(value: Any) -> str:
    return str(getattr(value, "locator", value) or "").strip()


def _operand(token: str) -> LiveNumericOperand | None:
    raw = str(token or "").strip()
    if not raw:
        return None
    try:
        number = float(raw)
    except ValueError:
        return LiveNumericOperand(reference=raw)
    if number.is_integer():
        return LiveNumericOperand(literal=int(number))
    return LiveNumericOperand(literal=number)


def _comparison(
    *,
    identifier: str,
    result_tag: str | None,
    left: str,
    operator: str,
    right: str,
    source_locator: str,
    semantic_state: str,
    origin: str,
) -> LiveNumericComparison | None:
    lhs = _operand(left)
    rhs = _operand(right)
    op = _OPERATOR_MAP.get(str(operator).upper(), _OPERATOR_MAP.get(str(operator)))
    if lhs is None or rhs is None or op is None:
        return None
    if lhs.reference is None and rhs.reference is None:
        return None
    return LiveNumericComparison(
        id=identifier,
        result_tag=str(result_tag).strip() if result_tag else None,
        left=lhs,
        operator=op,
        right=rhs,
        source_locator=source_locator,
        semantic_state=semantic_state,
        origin=origin,
    )


def _statement_numeric(statement: LiveLogicStatement) -> tuple[LiveNumericComparison, ...]:
    if str(statement.semantic_state or "").upper() != "FULL":
        return ()
    text = statement.text or ""
    exact = _ASSIGN_COMPARE_EXACT_RE.fullmatch(text)
    if exact is not None:
        item = _comparison(
            identifier=f"NUM:{statement.id}:ASSIGN:1",
            result_tag=exact.group("result"),
            left=exact.group("left"),
            operator=exact.group("op"),
            right=exact.group("right"),
            source_locator=statement.source_locator or statement.locator,
            semantic_state=statement.semantic_state,
            origin="STATEMENT_ASSIGNMENT",
        )
        return (item,) if item is not None else ()

    # Compound/conditional expressions are useful context, but are not equivalent to
    # any result tag unless the complete RHS is the supported comparison above.
    result: list[LiveNumericComparison] = []
    for index, match in enumerate(_BARE_COMPARE_RE.finditer(text), start=1):
        item = _comparison(
            identifier=f"NUM:{statement.id}:CONTEXT:{index}",
            result_tag=None,
            left=match.group("left"),
            operator=match.group("op"),
            right=match.group("right"),
            source_locator=statement.source_locator or statement.locator,
            semantic_state=statement.semantic_state,
            origin="STATEMENT_COMPARISON_CONTEXT",
        )
        if item is not None:
            result.append(item)
    return tuple(result)


def _rung_numeric(project: Any) -> tuple[LiveNumericComparison, ...]:
    result: list[LiveNumericComparison] = []
    for rung in tuple(getattr(project, "rungs", ()) or ()):
        text = str(getattr(rung, "text", "") or "")
        for index, match in enumerate(_RLL_COMPARE_RE.finditer(text), start=1):
            item = _comparison(
                identifier=f"NUM:RUNG:{getattr(rung, 'id', '')}:{index}",
                result_tag=None,
                left=match.group("left"),
                operator=match.group("op"),
                right=match.group("right"),
                source_locator=_source_locator(getattr(rung, "source", None)),
                semantic_state="FULL",
                origin="RUNG_COMPARISON",
            )
            if item is not None:
                result.append(item)
    return tuple(result)


def extract_numeric_comparisons(project: Any, context: LiveEngineeringContext) -> tuple[LiveNumericComparison, ...]:
    candidates: list[LiveNumericComparison] = []
    for statement in context.statements:
        candidates.extend(_statement_numeric(statement))
    candidates.extend(_rung_numeric(project))
    seen: set[tuple[Any, ...]] = set()
    result: list[LiveNumericComparison] = []
    for item in candidates:
        key = (
            normalize_engineering_identifier(item.result_tag),
            normalize_engineering_identifier(item.left.reference) if item.left.reference else item.left.literal,
            item.operator,
            normalize_engineering_identifier(item.right.reference) if item.right.reference else item.right.literal,
            item.source_locator,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return tuple(result)


def _classify_instruction(name: str) -> LiveAdvancedKind | None:
    upper = str(name or "").strip().upper().replace("-", "_")
    if upper in _ONE_SHOT_NAMES:
        return LiveAdvancedKind.ONE_SHOT
    if upper in _LATCH_NAMES:
        return LiveAdvancedKind.LATCH
    if upper in _SEQUENCER_NAMES or upper.startswith("SQO") or upper.startswith("SQC"):
        return LiveAdvancedKind.SEQUENCER
    if upper in _PID_NAMES or upper.startswith("PID_"):
        return LiveAdvancedKind.PID
    if upper in _ROCKWELL_MOTION_NAMES or upper.startswith("MC_"):
        return LiveAdvancedKind.MOTION
    return None


def _instruction_models(project: Any) -> tuple[LiveAdvancedModel, ...]:
    result: list[LiveAdvancedModel] = []
    for rung in tuple(getattr(project, "rungs", ()) or ()):
        source = _source_locator(getattr(rung, "source", None))
        for index, instruction in enumerate(tuple(getattr(rung, "instructions", ()) or ()), start=1):
            name = str(getattr(instruction, "name", "") or "").strip()
            kind = _classify_instruction(name)
            if kind is None:
                continue
            args = tuple(str(item).strip() for item in tuple(getattr(instruction, "arguments", ()) or ()) if str(item).strip())
            result.append(
                LiveAdvancedModel(
                    id=f"ADV:RUNG:{getattr(rung, 'id', '')}:{index}",
                    kind=kind,
                    name=args[-1] if args and kind is LiveAdvancedKind.LATCH else (args[0] if args else name),
                    instruction=name,
                    references=args,
                    source_locators=(source,) if source else (),
                    semantic_state="RUNTIME_REQUIRED" if kind in {LiveAdvancedKind.ONE_SHOT, LiveAdvancedKind.LATCH, LiveAdvancedKind.SEQUENCER, LiveAdvancedKind.MOTION, LiveAdvancedKind.PID} else "FULL",
                    metadata={"rung_id": str(getattr(rung, "id", "") or "")},
                )
            )
    for statement in tuple(getattr(project, "logic_statements", ()) or ()):
        source = _source_locator(getattr(statement, "source", None))
        for index, call in enumerate(tuple(getattr(statement, "calls", ()) or ()), start=1):
            name = str(call or "").strip()
            kind = _classify_instruction(name)
            if kind is None:
                continue
            reads = tuple(str(item) for item in tuple(getattr(statement, "reads", ()) or ()) if str(item))
            writes = tuple(str(item) for item in tuple(getattr(statement, "writes", ()) or ()) if str(item))
            references = tuple(dict.fromkeys((*reads, *writes)))
            result.append(
                LiveAdvancedModel(
                    id=f"ADV:STMT:{getattr(statement, 'id', '')}:{index}",
                    kind=kind,
                    name=writes[0] if len(writes) == 1 else name,
                    instruction=name,
                    references=references,
                    source_locators=(source,) if source else (),
                    semantic_state="RUNTIME_REQUIRED",
                    metadata={"statement_id": str(getattr(statement, "id", "") or "")},
                )
            )
    return tuple(result)


def _aoi_fb_models(project: Any) -> tuple[LiveAdvancedModel, ...]:
    result: list[LiveAdvancedModel] = []
    definitions: dict[str, Any] = {}
    for aoi in tuple(getattr(project, "aois", ()) or ()):
        name = str(getattr(aoi, "name", "") or "").strip()
        if not name:
            continue
        definitions[name.casefold()] = aoi
        params = tuple(str(getattr(item, "name", "") or "").strip() for item in tuple(getattr(aoi, "parameters", ()) or ()) if str(getattr(item, "name", "") or "").strip())
        result.append(
            LiveAdvancedModel(
                id=f"ADV:AOI:DEF:{getattr(aoi, 'id', name)}",
                kind=LiveAdvancedKind.AOI_FB,
                name=name,
                instruction="AOI_DEFINITION",
                references=(),
                source_locators=(),
                semantic_state="FULL" if bool(getattr(aoi, "internal_body_modeled", False)) and not bool(getattr(aoi, "source_protected", False)) else "PARTIAL",
                metadata={
                    "definition": True,
                    "parameters": params,
                    "source_protected": bool(getattr(aoi, "source_protected", False)),
                    "internal_body_modeled": bool(getattr(aoi, "internal_body_modeled", False)),
                },
            )
        )

    fb_names = {
        str(getattr(stmt, "owner_name", "") or "").strip().casefold()
        for stmt in tuple(getattr(project, "logic_statements", ()) or ())
        if "function_block" in str(getattr(stmt, "owner_type", "") or "").casefold()
        and str(getattr(stmt, "owner_name", "") or "").strip()
    }
    for rung in tuple(getattr(project, "rungs", ()) or ()):
        source = _source_locator(getattr(rung, "source", None))
        for index, instruction in enumerate(tuple(getattr(rung, "instructions", ()) or ()), start=1):
            name = str(getattr(instruction, "name", "") or "").strip()
            if name.casefold() not in definitions:
                continue
            args = tuple(str(item).strip() for item in tuple(getattr(instruction, "arguments", ()) or ()) if str(item).strip())
            definition = definitions[name.casefold()]
            result.append(
                LiveAdvancedModel(
                    id=f"ADV:AOI:CALL:{getattr(rung, 'id', '')}:{index}",
                    kind=LiveAdvancedKind.AOI_FB,
                    name=f"{name}@{getattr(rung, 'id', index)}",
                    instruction=name,
                    references=args,
                    source_locators=(source,) if source else (),
                    semantic_state="FULL" if bool(getattr(definition, "internal_body_modeled", False)) and not bool(getattr(definition, "source_protected", False)) else "PARTIAL",
                    metadata={"definition": False, "instance_kind": "AOI"},
                )
            )
    for statement in tuple(getattr(project, "logic_statements", ()) or ()):
        source = _source_locator(getattr(statement, "source", None))
        for index, call in enumerate(tuple(getattr(statement, "calls", ()) or ()), start=1):
            name = str(call or "").strip()
            key = name.casefold()
            if key not in definitions and key not in fb_names:
                continue
            refs = tuple(dict.fromkeys(
                str(item) for item in (*tuple(getattr(statement, "reads", ()) or ()), *tuple(getattr(statement, "writes", ()) or ())) if str(item)
            ))
            result.append(
                LiveAdvancedModel(
                    id=f"ADV:FB:CALL:{getattr(statement, 'id', '')}:{index}",
                    kind=LiveAdvancedKind.AOI_FB,
                    name=f"{name}@{getattr(statement, 'id', index)}",
                    instruction=name,
                    references=refs,
                    source_locators=(source,) if source else (),
                    semantic_state=_enum_text(getattr(statement, "semantic_state", "PARTIAL")),
                    metadata={"definition": False, "instance_kind": "AOI" if key in definitions else "FUNCTION_BLOCK"},
                )
            )
    return tuple(result)


def _handshake_role(name: str) -> tuple[str, str] | None:
    raw = str(name or "").strip()
    lowered = raw.casefold()
    for role, suffixes in _HANDSHAKE_SUFFIXES:
        for suffix in suffixes:
            match = re.search(rf'(?i)(?:[._:\-]|(?<=[a-z0-9])){re.escape(suffix)}$', raw)
            if match:
                stem = raw[: match.start()].rstrip("._:-")
                if stem:
                    return stem, role
            if lowered == suffix:
                return None
    return None


def _handshake_models(context: LiveEngineeringContext) -> tuple[LiveAdvancedModel, ...]:
    grouped: dict[str, dict[str, str]] = {}
    display_stem: dict[str, str] = {}
    for tag in context.tags:
        resolved = _handshake_role(tag.name)
        if resolved is None:
            continue
        stem, role = resolved
        key = normalize_engineering_identifier(stem)
        if not key:
            continue
        grouped.setdefault(key, {})[role] = tag.name
        display_stem.setdefault(key, stem)
    result: list[LiveAdvancedModel] = []
    for key, roles in grouped.items():
        if "REQUEST" not in roles or not ({"ACK", "BUSY", "DONE"} & set(roles)):
            continue
        refs = tuple(roles[role] for role in ("REQUEST", "ACK", "BUSY", "DONE", "ERROR", "TIMEOUT") if role in roles)
        result.append(
            LiveAdvancedModel(
                id=f"ADV:HS:{key}",
                kind=LiveAdvancedKind.HANDSHAKE,
                name=display_stem[key],
                instruction="NAMED_HANDSHAKE",
                references=refs,
                source_locators=(),
                semantic_state="INFERRED",
                metadata={"roles": dict(roles)},
            )
        )
    return tuple(result)


def _fault_code_models(context: LiveEngineeringContext) -> tuple[LiveAdvancedModel, ...]:
    result: list[LiveAdvancedModel] = []
    for tag in context.tags:
        key = normalize_engineering_identifier(tag.name)
        if not any(key.endswith(suffix) for suffix in _FAULT_CODE_SUFFIXES):
            continue
        result.append(
            LiveAdvancedModel(
                id=f"ADV:FAULT:{tag.id}",
                kind=LiveAdvancedKind.FAULT_CODE,
                name=tag.name,
                instruction="FAULT_CODE",
                references=(tag.name,),
                source_locators=(),
                semantic_state="RUNTIME_REQUIRED",
                metadata={"data_type": tag.data_type, "description": tag.description},
            )
        )
    return tuple(result)


def _udt_array_models(project: Any, context: LiveEngineeringContext) -> tuple[LiveAdvancedModel, ...]:
    result: list[LiveAdvancedModel] = []
    data_types = {
        str(getattr(item, "name", "") or "").casefold(): item
        for item in tuple(getattr(project, "data_types", ()) or ())
        if str(getattr(item, "name", "") or "").strip()
    }
    for tag in context.tags:
        dtype = str(tag.data_type or "").strip()
        dtype_key = dtype.casefold()
        if dtype_key in data_types:
            raw = data_types[dtype_key]
            members = tuple(str(getattr(item, "name", "") or "") for item in tuple(getattr(raw, "members", ()) or ()) if str(getattr(item, "name", "") or ""))
            result.append(
                LiveAdvancedModel(
                    id=f"ADV:UDT:{tag.id}",
                    kind=LiveAdvancedKind.UDT,
                    name=tag.name,
                    instruction="STRUCTURED_TYPE",
                    references=(tag.name,),
                    source_locators=(),
                    semantic_state="CONTEXT_ONLY",
                    metadata={"data_type": dtype, "members": members},
                )
            )
        if "array" in dtype_key or "[" in dtype or "]" in dtype:
            result.append(
                LiveAdvancedModel(
                    id=f"ADV:ARRAY:{tag.id}",
                    kind=LiveAdvancedKind.ARRAY,
                    name=tag.name,
                    instruction="ARRAY",
                    references=(tag.name,),
                    source_locators=(),
                    semantic_state="CONTEXT_ONLY",
                    metadata={"data_type": dtype},
                )
            )
    return tuple(result)


def build_live_advanced_coverage(project: Any, context: LiveEngineeringContext) -> LiveAdvancedCoverage:
    models: list[LiveAdvancedModel] = []
    models.extend(_instruction_models(project))
    models.extend(_aoi_fb_models(project))
    models.extend(_handshake_models(context))
    models.extend(_fault_code_models(context))
    models.extend(_udt_array_models(project, context))

    deduped: list[LiveAdvancedModel] = []
    seen: set[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = set()
    for item in models:
        key = (item.kind.value, normalize_engineering_identifier(item.name), item.references, item.source_locators)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return LiveAdvancedCoverage(
        numeric_comparisons=extract_numeric_comparisons(project, context),
        models=tuple(deduped),
        limitations=(
            "Advanced Live semantics are read-only and evidence bounded.",
            "Name-derived handshakes are INFERRED context until confirmed by explicit PLC logic or engineer evidence.",
            "One-shot, latch, sequencer, motion, and PID instructions require runtime/history evidence; DevAgent does not simulate hidden controller state.",
            "AOI/FB internals are not claimed when source-protected, partial, or absent from the canonical project.",
            "RLL comparators are modeled as conditions only; Live does not bind them to rung writes without proven topology equivalence.",
            "UDT/array models provide structure context only unless individual members/elements are exposed and reconciled through OPC UA.",
        ),
    )


__all__ = [
    "LiveAdvancedKind",
    "LiveNumericOperand",
    "LiveNumericComparison",
    "LiveAdvancedModel",
    "LiveAdvancedCoverage",
    "extract_numeric_comparisons",
    "build_live_advanced_coverage",
]
