from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import xml.etree.ElementTree as ET

from devagent.plc import schneider_closeout_v9 as _v9


_INSTALLED = False
_PREVIOUS_SOURCE_MANIFEST = _v9._source_manifest


def _owner_name(owner) -> str:
    ident = next(
        (item for item in owner.iter() if _v9._v1._local_name(item.tag) == "identProgram"),
        None,
    )
    return (
        (ident.attrib.get("name") if ident is not None else None)
        or owner.attrib.get("name")
        or owner.attrib.get("nameOfFBType")
        or "UNKNOWN"
    )


def _is_nested_fbd_wiring_source(node, parents: dict[object, object], owner) -> bool:
    """Return True only for a linkSource that is actual FBD link wiring metadata.

    Control Expert serializes FBD wire endpoints as ``linkSource`` children of
    ``linkFB`` below an ``FBDSource`` tree. A tag named ``linkSource`` anywhere
    else is not automatically trusted and must remain visible to the V9 source
    audit as unknown executable/export content.
    """

    if _v9._v1._local_name(node.tag).casefold() != "linksource":
        return False

    parent = parents.get(node)
    if parent is None or _v9._v1._local_name(parent.tag).casefold() != "linkfb":
        return False

    cursor = parent
    while cursor is not None and cursor is not owner:
        if _v9._v1._local_name(cursor.tag).casefold() == "fbdsource":
            return True
        cursor = parents.get(cursor)
    return False


def _contextual_unknown_source_tags(path: Path) -> tuple[str, ...]:
    """Recompute unknown *Source nodes while retaining XML ancestry context."""

    _root, files, _total = _v9._v1._preflight_sources(Path(path))
    unknown: set[str] = set()

    for source, relative in files:
        root = ET.parse(source).getroot()
        parents = {
            child: parent
            for parent in root.iter()
            for child in list(parent)
        }

        for owner in root.iter():
            if _v9._v1._local_name(owner.tag) not in _v9._EXECUTABLE_OWNERS:
                continue
            owner_name = _owner_name(owner)
            for node in owner.iter():
                if node is owner:
                    continue
                local = _v9._v1._local_name(node.tag)
                if not local.endswith("Source") or local in _v9._KNOWN_SOURCES:
                    continue
                if _is_nested_fbd_wiring_source(node, parents, owner):
                    continue
                unknown.add(f"{relative}:{owner_name}:{local}")

    return tuple(sorted(unknown, key=str.casefold))


def _source_manifest(path):
    """Filter only proven FBD wiring metadata while preserving V9 input errors.

    The original closeout audit flattened XML nodes into serialized tag names,
    which lost ancestry and made it impossible to distinguish a legitimate FBD
    ``linkSource`` wire endpoint from an unrelated/unknown ``linkSource`` node.
    Recompute only the unknown-source field with parent context and keep every
    other V9 manifest/audit field unchanged.
    """

    try:
        files, audit = _PREVIOUS_SOURCE_MANIFEST(path)
        contextual = _contextual_unknown_source_tags(Path(path))
    except ET.ParseError as exc:
        raise _v9._v1.SchneiderInputError(
            f"Invalid Control Expert XML exchange artifact: {exc}"
        ) from exc

    if contextual == audit.unknown_executable_source_tags:
        return files, audit
    return files, replace(audit, unknown_executable_source_tags=contextual)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _v9._source_manifest = _source_manifest
    _INSTALLED = True


__all__ = ["install"]
