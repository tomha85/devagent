from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

from .agent_integration import LiveAgentEvidenceItem
from .errors import LiveConfigurationError
from .models import BrowseNode


class LiveTypeCompatibility(str, Enum):
    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"
    UNKNOWN = "UNKNOWN"


class LiveTagMatchKind(str, Enum):
    EXPLICIT = "EXPLICIT"
    EXACT_QUALIFIED = "EXACT_QUALIFIED"
    EXACT_NAME = "EXACT_NAME"


class LiveTagMappingStatus(str, Enum):
    AUTO_BOUND = "AUTO_BOUND"
    EXPLICIT_BOUND = "EXPLICIT_BOUND"
    MANUAL_REQUIRED = "MANUAL_REQUIRED"
    AMBIGUOUS = "AMBIGUOUS"
    NODE_COLLISION = "NODE_COLLISION"
    TYPE_CONFLICT = "TYPE_CONFLICT"
    NON_READABLE = "NON_READABLE"
    EXTERNAL_ACCESS_BLOCKED = "EXTERNAL_ACCESS_BLOCKED"
    UNMATCHED = "UNMATCHED"


_ACCEPTED_STATUSES = {
    LiveTagMappingStatus.AUTO_BOUND,
    LiveTagMappingStatus.EXPLICIT_BOUND,
}


@dataclass(frozen=True)
class LiveTagCandidate:
    node_id: str
    path: str
    browse_name: str
    display_name: str
    data_type: str | None
    readable: bool
    user_access: tuple[str, ...]
    match_kind: LiveTagMatchKind
    type_compatibility: LiveTypeCompatibility


@dataclass(frozen=True)
class LiveTagMapping:
    tag_id: str
    tag_name: str
    tag_scope: str
    tag_data_type: str
    status: LiveTagMappingStatus
    reason: str
    candidates: tuple[LiveTagCandidate, ...]
    selected_node_id: str | None = None
    selected_path: str | None = None
    evidence_id: str = ""

    @property
    def accepted(self) -> bool:
        return self.status in _ACCEPTED_STATUSES and self.selected_node_id is not None


@dataclass(frozen=True)
class LiveTagReconciliation:
    plc_id: str
    mappings: tuple[LiveTagMapping, ...]

    def mapping_by_tag_id(self) -> dict[str, LiveTagMapping]:
        return {mapping.tag_id: mapping for mapping in self.mappings}

    def accepted_mappings(self) -> tuple[LiveTagMapping, ...]:
        return tuple(mapping for mapping in self.mappings if mapping.accepted)

    def unresolved_mappings(self) -> tuple[LiveTagMapping, ...]:
        return tuple(mapping for mapping in self.mappings if not mapping.accepted)

    def node_request_map(
        self,
        *,
        required_tag_ids: Iterable[str] | None = None,
        require_all: bool = True,
    ) -> dict[str, tuple[str, ...]]:
        by_id = self.mapping_by_tag_id()
        if required_tag_ids is None:
            selected = list(self.mappings)
        else:
            requested = []
            seen: set[str] = set()
            for tag_id in required_tag_ids:
                text = str(tag_id).strip()
                if not text:
                    raise LiveConfigurationError("Required engineering tag id cannot be blank")
                if text in seen:
                    continue
                seen.add(text)
                if text not in by_id:
                    raise LiveConfigurationError(f"Unknown engineering tag id: {text}")
                requested.append(by_id[text])
            selected = requested

        unresolved = [mapping for mapping in selected if not mapping.accepted]
        if unresolved and require_all:
            details = ", ".join(
                f"{mapping.tag_id}={mapping.status.value}"
                for mapping in unresolved
            )
            raise LiveConfigurationError(
                f"Live tag reconciliation is incomplete for PLC {self.plc_id}: {details}"
            )

        node_ids: list[str] = []
        seen_nodes: set[str] = set()
        for mapping in selected:
            if not mapping.accepted or mapping.selected_node_id is None:
                continue
            if mapping.selected_node_id not in seen_nodes:
                seen_nodes.add(mapping.selected_node_id)
                node_ids.append(mapping.selected_node_id)
        return {self.plc_id: tuple(node_ids)}

    def evidence_items(self) -> tuple[LiveAgentEvidenceItem, ...]:
        items: list[LiveAgentEvidenceItem] = []
        for mapping in self.mappings:
            kind = "LIVE_TAG_MAPPING" if mapping.accepted else "LIVE_TAG_MAPPING_LIMITATION"
            selected = mapping.selected_node_id or "none"
            items.append(
                LiveAgentEvidenceItem(
                    id=mapping.evidence_id,
                    kind=kind,
                    summary=(
                        f"Engineering-to-live tag reconciliation for {mapping.tag_name} "
                        f"({mapping.tag_id}) on PLC {self.plc_id}: status={mapping.status.value}, "
                        f"selected_node={selected}. {mapping.reason}"
                    ),
                    source_locator=f"opcua-map:{self.plc_id}:{mapping.tag_id}",
                    payload={
                        "plc_id": self.plc_id,
                        "tag_id": mapping.tag_id,
                        "tag_name": mapping.tag_name,
                        "tag_scope": mapping.tag_scope,
                        "tag_data_type": mapping.tag_data_type,
                        "status": mapping.status.value,
                        "accepted": mapping.accepted,
                        "selected_node_id": mapping.selected_node_id,
                        "selected_path": mapping.selected_path,
                        "candidates": [
                            {
                                "node_id": candidate.node_id,
                                "path": candidate.path,
                                "browse_name": candidate.browse_name,
                                "display_name": candidate.display_name,
                                "data_type": candidate.data_type,
                                "readable": candidate.readable,
                                "match_kind": candidate.match_kind.value,
                                "type_compatibility": candidate.type_compatibility.value,
                            }
                            for candidate in mapping.candidates
                        ],
                    },
                )
            )
        return tuple(items)


_GENERIC_SCOPE_TERMS = {
    "controller",
    "global",
    "local",
    "program",
    "programs",
    "plc",
    "tag",
    "tags",
}

_ENGINEERING_TYPE_FAMILIES = {
    "bool": "bool",
    "boolean": "bool",
    "sint": "int8",
    "usint": "uint8",
    "byte": "uint8",
    "int": "int16",
    "integer": "int16",
    "uint": "uint16",
    "word": "uint16",
    "dint": "int32",
    "udint": "uint32",
    "dword": "uint32",
    "lint": "int64",
    "ulint": "uint64",
    "lword": "uint64",
    "real": "float32",
    "float": "float32",
    "lreal": "float64",
    "double": "float64",
    "string": "string",
    "wstring": "string",
}

_LIVE_TYPE_FAMILIES = {
    "boolean": "bool",
    "sbyte": "int8",
    "byte": "uint8",
    "int16": "int16",
    "uint16": "uint16",
    "int32": "int32",
    "uint32": "uint32",
    "int64": "int64",
    "uint64": "uint64",
    "float": "float32",
    "double": "float64",
    "string": "string",
}


def _normalize_identifier(value: str | None) -> str:
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def _path_components(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(
        component
        for component in (
            _normalize_identifier(part)
            for part in re.split(r"[./\\:]+", str(value))
        )
        if component
    )


def _scope_terms(scope: str | None) -> tuple[str, ...]:
    return tuple(
        component
        for component in _path_components(scope)
        if component not in _GENERIC_SCOPE_TERMS
    )


def _engineering_type_family(data_type: str | None) -> str | None:
    if not data_type:
        return None
    base = re.sub(r"\[.*?\]", "", str(data_type)).strip()
    normalized = _normalize_identifier(base)
    return _ENGINEERING_TYPE_FAMILIES.get(normalized)


def _live_type_family(data_type: str | None) -> str | None:
    return _LIVE_TYPE_FAMILIES.get(_normalize_identifier(data_type))


def _type_compatibility(
    engineering_data_type: str | None,
    live_data_type: str | None,
) -> LiveTypeCompatibility:
    engineering = _engineering_type_family(engineering_data_type)
    live = _live_type_family(live_data_type)
    if engineering is None or live is None:
        return LiveTypeCompatibility.UNKNOWN
    if engineering == live:
        return LiveTypeCompatibility.COMPATIBLE
    return LiveTypeCompatibility.INCOMPATIBLE


def _external_access_blocked(tag: Any) -> bool:
    value = getattr(tag, "external_access", None)
    normalized = _normalize_identifier(value)
    return normalized in {"none", "noaccess", "disabled", "false"}


def _tag_identity(tag: Any) -> tuple[str, str, str, str]:
    tag_id = str(getattr(tag, "id", "")).strip()
    tag_name = str(getattr(tag, "name", "")).strip()
    scope = str(getattr(tag, "scope", "") or "").strip()
    data_type = str(getattr(tag, "data_type", "") or "").strip()
    if not tag_id:
        raise LiveConfigurationError("Engineering tag id cannot be blank")
    if not tag_name:
        raise LiveConfigurationError(f"Engineering tag {tag_id} has a blank name")
    return tag_id, tag_name, scope, data_type


def _is_variable(node: BrowseNode) -> bool:
    return str(node.node_class).strip().casefold() == "variable"


def _name_matches(tag_name: str, node: BrowseNode) -> bool:
    target = _normalize_identifier(tag_name)
    identities = {
        _normalize_identifier(node.browse_name),
        _normalize_identifier(node.display_name),
    }
    components = _path_components(node.path)
    if components:
        identities.add(components[-1])
    identities.discard("")
    return target in identities


def _scope_matches(scope: str, node: BrowseNode) -> bool:
    terms = _scope_terms(scope)
    if not terms:
        return False
    components = set(_path_components(node.path)[:-1])
    return all(term in components for term in terms)


def _candidate(tag_name: str, scope: str, data_type: str, node: BrowseNode) -> LiveTagCandidate:
    qualified = _scope_matches(scope, node)
    return LiveTagCandidate(
        node_id=node.node_id,
        path=node.path,
        browse_name=node.browse_name,
        display_name=node.display_name,
        data_type=node.data_type,
        readable=bool(node.readable),
        user_access=tuple(node.user_access),
        match_kind=(
            LiveTagMatchKind.EXACT_QUALIFIED
            if qualified
            else LiveTagMatchKind.EXACT_NAME
        ),
        type_compatibility=_type_compatibility(data_type, node.data_type),
    )


def _mapping_evidence_id(
    plc_id: str,
    tag_id: str,
    status: LiveTagMappingStatus,
    selected_node_id: str | None,
    candidates: Sequence[LiveTagCandidate],
) -> str:
    payload = {
        "plc_id": plc_id,
        "tag_id": tag_id,
        "status": status.value,
        "selected_node_id": selected_node_id,
        "candidates": [
            {
                "node_id": candidate.node_id,
                "path": candidate.path,
                "match_kind": candidate.match_kind.value,
                "type_compatibility": candidate.type_compatibility.value,
                "readable": candidate.readable,
            }
            for candidate in candidates
        ],
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return f"LIVE-MAP:{plc_id}:{digest}"


def _build_mapping(
    *,
    plc_id: str,
    tag_id: str,
    tag_name: str,
    tag_scope: str,
    tag_data_type: str,
    status: LiveTagMappingStatus,
    reason: str,
    candidates: Sequence[LiveTagCandidate],
    selected: LiveTagCandidate | None = None,
) -> LiveTagMapping:
    ordered = tuple(sorted(candidates, key=lambda item: (item.path, item.node_id)))
    selected_node_id = selected.node_id if selected is not None else None
    selected_path = selected.path if selected is not None else None
    return LiveTagMapping(
        tag_id=tag_id,
        tag_name=tag_name,
        tag_scope=tag_scope,
        tag_data_type=tag_data_type,
        status=status,
        reason=reason,
        candidates=ordered,
        selected_node_id=selected_node_id,
        selected_path=selected_path,
        evidence_id=_mapping_evidence_id(
            plc_id,
            tag_id,
            status,
            selected_node_id,
            ordered,
        ),
    )


def _explicit_target(
    tag_id: str,
    tag_name: str,
    explicit_node_map: Mapping[str, str] | None,
) -> str | None:
    if not explicit_node_map:
        return None
    by_id = explicit_node_map.get(tag_id)
    by_name = explicit_node_map.get(tag_name)
    if by_id is not None and by_name is not None and str(by_id) != str(by_name):
        raise LiveConfigurationError(
            f"Conflicting explicit live-node mappings for engineering tag {tag_id}"
        )
    value = by_id if by_id is not None else by_name
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        raise LiveConfigurationError(
            f"Explicit live-node mapping for engineering tag {tag_id} cannot be blank"
        )
    return text


def _reconcile_one(
    *,
    plc_id: str,
    tag: Any,
    nodes: Sequence[BrowseNode],
    explicit_node_map: Mapping[str, str] | None,
) -> LiveTagMapping:
    tag_id, tag_name, scope, data_type = _tag_identity(tag)
    exact_nodes = [
        node
        for node in nodes
        if _is_variable(node) and _name_matches(tag_name, node)
    ]
    candidates = [_candidate(tag_name, scope, data_type, node) for node in exact_nodes]

    if _external_access_blocked(tag):
        return _build_mapping(
            plc_id=plc_id,
            tag_id=tag_id,
            tag_name=tag_name,
            tag_scope=scope,
            tag_data_type=data_type,
            status=LiveTagMappingStatus.EXTERNAL_ACCESS_BLOCKED,
            reason="Engineering metadata explicitly blocks external access for this tag.",
            candidates=candidates,
        )

    explicit = _explicit_target(tag_id, tag_name, explicit_node_map)
    if explicit is not None:
        target_node = next((node for node in nodes if node.node_id == explicit), None)
        if target_node is None or not _is_variable(target_node):
            return _build_mapping(
                plc_id=plc_id,
                tag_id=tag_id,
                tag_name=tag_name,
                tag_scope=scope,
                tag_data_type=data_type,
                status=LiveTagMappingStatus.UNMATCHED,
                reason=f"Explicit mapping target {explicit} was not found as a Variable node.",
                candidates=candidates,
            )
        target = _candidate(tag_name, scope, data_type, target_node)
        explicit_candidate = replace(target, match_kind=LiveTagMatchKind.EXPLICIT)
        explicit_candidates = tuple(candidates) + (
            () if any(item.node_id == explicit_candidate.node_id for item in candidates) else (explicit_candidate,)
        )
        if not target_node.readable:
            return _build_mapping(
                plc_id=plc_id,
                tag_id=tag_id,
                tag_name=tag_name,
                tag_scope=scope,
                tag_data_type=data_type,
                status=LiveTagMappingStatus.NON_READABLE,
                reason="Explicit mapping target is not readable for the current OPC UA user.",
                candidates=explicit_candidates,
            )
        if target.type_compatibility is LiveTypeCompatibility.INCOMPATIBLE:
            return _build_mapping(
                plc_id=plc_id,
                tag_id=tag_id,
                tag_name=tag_name,
                tag_scope=scope,
                tag_data_type=data_type,
                status=LiveTagMappingStatus.TYPE_CONFLICT,
                reason="Explicit mapping target has a deterministically incompatible data type.",
                candidates=explicit_candidates,
            )
        return _build_mapping(
            plc_id=plc_id,
            tag_id=tag_id,
            tag_name=tag_name,
            tag_scope=scope,
            tag_data_type=data_type,
            status=LiveTagMappingStatus.EXPLICIT_BOUND,
            reason=(
                "Operator-supplied explicit NodeId accepted after Variable/read-access checks; "
                f"type compatibility={target.type_compatibility.value}."
            ),
            candidates=explicit_candidates,
            selected=explicit_candidate,
        )

    if not candidates:
        return _build_mapping(
            plc_id=plc_id,
            tag_id=tag_id,
            tag_name=tag_name,
            tag_scope=scope,
            tag_data_type=data_type,
            status=LiveTagMappingStatus.UNMATCHED,
            reason="No exact normalized browse/display/path-leaf name match was found; fuzzy matching is not auto-accepted.",
            candidates=(),
        )

    readable = [candidate for candidate in candidates if candidate.readable]
    if not readable:
        return _build_mapping(
            plc_id=plc_id,
            tag_id=tag_id,
            tag_name=tag_name,
            tag_scope=scope,
            tag_data_type=data_type,
            status=LiveTagMappingStatus.NON_READABLE,
            reason="Exact live node candidate(s) exist but none are readable for the current OPC UA user.",
            candidates=candidates,
        )

    qualified = [
        candidate
        for candidate in readable
        if candidate.match_kind is LiveTagMatchKind.EXACT_QUALIFIED
    ]
    working = qualified if qualified else readable
    if len(working) > 1:
        return _build_mapping(
            plc_id=plc_id,
            tag_id=tag_id,
            tag_name=tag_name,
            tag_scope=scope,
            tag_data_type=data_type,
            status=LiveTagMappingStatus.AMBIGUOUS,
            reason="Multiple exact readable candidates remain after scope qualification; explicit mapping is required.",
            candidates=candidates,
        )

    selected = working[0]
    if selected.type_compatibility is LiveTypeCompatibility.INCOMPATIBLE:
        return _build_mapping(
            plc_id=plc_id,
            tag_id=tag_id,
            tag_name=tag_name,
            tag_scope=scope,
            tag_data_type=data_type,
            status=LiveTagMappingStatus.TYPE_CONFLICT,
            reason="Exact node name matched but engineering and live data types are deterministically incompatible.",
            candidates=candidates,
        )
    if selected.type_compatibility is LiveTypeCompatibility.UNKNOWN:
        return _build_mapping(
            plc_id=plc_id,
            tag_id=tag_id,
            tag_name=tag_name,
            tag_scope=scope,
            tag_data_type=data_type,
            status=LiveTagMappingStatus.MANUAL_REQUIRED,
            reason="Exact node name matched, but data-type compatibility cannot be proven; explicit mapping is required.",
            candidates=candidates,
        )
    if getattr(tag, "alias_for", None):
        return _build_mapping(
            plc_id=plc_id,
            tag_id=tag_id,
            tag_name=tag_name,
            tag_scope=scope,
            tag_data_type=data_type,
            status=LiveTagMappingStatus.MANUAL_REQUIRED,
            reason="Engineering tag is an alias; automatic runtime binding is withheld until an explicit NodeId is supplied.",
            candidates=candidates,
        )

    return _build_mapping(
        plc_id=plc_id,
        tag_id=tag_id,
        tag_name=tag_name,
        tag_scope=scope,
        tag_data_type=data_type,
        status=LiveTagMappingStatus.AUTO_BOUND,
        reason=(
            "Unique exact normalized tag identity, readable Variable access, and compatible data type were proven"
            + (" with engineering scope qualification." if qualified else ".")
        ),
        candidates=candidates,
        selected=selected,
    )


def reconcile_engineering_tags(
    plc_id: str,
    engineering_tags: Iterable[Any],
    browse_nodes: Iterable[BrowseNode],
    *,
    explicit_node_map: Mapping[str, str] | None = None,
) -> LiveTagReconciliation:
    plc_id = str(plc_id).strip()
    if not plc_id:
        raise LiveConfigurationError("plc_id cannot be blank for tag reconciliation")
    tags = tuple(engineering_tags)
    nodes = tuple(browse_nodes)
    seen_tag_ids: set[str] = set()
    mappings: list[LiveTagMapping] = []
    for tag in tags:
        tag_id, _name, _scope, _data_type = _tag_identity(tag)
        if tag_id in seen_tag_ids:
            raise LiveConfigurationError(f"Duplicate engineering tag id: {tag_id}")
        seen_tag_ids.add(tag_id)
        mappings.append(
            _reconcile_one(
                plc_id=plc_id,
                tag=tag,
                nodes=nodes,
                explicit_node_map=explicit_node_map,
            )
        )

    selected_by_node: dict[str, list[int]] = {}
    for index, mapping in enumerate(mappings):
        if mapping.accepted and mapping.selected_node_id is not None:
            selected_by_node.setdefault(mapping.selected_node_id, []).append(index)
    for node_id, indexes in selected_by_node.items():
        if len(indexes) <= 1:
            continue
        tag_ids = ", ".join(mappings[index].tag_id for index in indexes)
        for index in indexes:
            mapping = mappings[index]
            mappings[index] = replace(
                mapping,
                status=LiveTagMappingStatus.NODE_COLLISION,
                reason=(
                    f"Live node {node_id} would bind multiple engineering tags ({tag_ids}); "
                    "one-to-one reconciliation is required."
                ),
                selected_node_id=None,
                selected_path=None,
                evidence_id=_mapping_evidence_id(
                    plc_id,
                    mapping.tag_id,
                    LiveTagMappingStatus.NODE_COLLISION,
                    None,
                    mapping.candidates,
                ),
            )

    return LiveTagReconciliation(plc_id=plc_id, mappings=tuple(mappings))


async def reconcile_connected_plc_tags(
    manager: Any,
    plc_id: str,
    engineering_tags: Iterable[Any],
    *,
    explicit_node_map: Mapping[str, str] | None = None,
    max_depth: int = 4,
    max_nodes: int = 500,
) -> LiveTagReconciliation:
    nodes = await manager.browse(
        plc_id,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )
    return reconcile_engineering_tags(
        plc_id,
        engineering_tags,
        nodes,
        explicit_node_map=explicit_node_map,
    )


async def reconcile_connected_project_tags(
    manager: Any,
    plc_id: str,
    project: Any,
    *,
    explicit_node_map: Mapping[str, str] | None = None,
    max_depth: int = 4,
    max_nodes: int = 500,
) -> LiveTagReconciliation:
    tags = getattr(project, "tags", None)
    if tags is None:
        raise LiveConfigurationError("Canonical engineering project does not expose tags")
    return await reconcile_connected_plc_tags(
        manager,
        plc_id,
        tags,
        explicit_node_map=explicit_node_map,
        max_depth=max_depth,
        max_nodes=max_nodes,
    )
