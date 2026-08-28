from __future__ import annotations

from dataclasses import replace
import xml.etree.ElementTree as ET

from devagent.plc import schneider_closeout_v9 as _v9


_INSTALLED = False
_PREVIOUS_SOURCE_MANIFEST = _v9._source_manifest


def _is_nested_wiring_source(entry: str) -> bool:
    """Return True for Control Expert connection metadata misread as source roots.

    Real Control Expert FBD exports place ``linkSource`` nodes below ``linkFB``
    connection elements. They describe a wire endpoint, not an executable source
    language container such as STSource/LDSource/FBDSource/SFCSource/ILSource.
    V9 must not report those nested wiring nodes as unknown executable sources.
    """

    return str(entry).rsplit(":", 1)[-1].casefold() == "linksource"


def _source_manifest(path):
    """Filter nested FBD wiring metadata while preserving V9 input errors.

    This hardening can be imported before or after the original V9 malformed-XML
    wrapper depending on module import order. Normalize ElementTree ParseError at
    this boundary as well so the public SchneiderInputError contract is stable.
    """

    try:
        files, audit = _PREVIOUS_SOURCE_MANIFEST(path)
    except ET.ParseError as exc:
        raise _v9._v1.SchneiderInputError(
            f"Invalid Control Expert XML exchange artifact: {exc}"
        ) from exc

    filtered = tuple(
        item
        for item in audit.unknown_executable_source_tags
        if not _is_nested_wiring_source(item)
    )
    if filtered == audit.unknown_executable_source_tags:
        return files, audit
    return files, replace(audit, unknown_executable_source_tags=filtered)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _v9._source_manifest = _source_manifest
    _INSTALLED = True


__all__ = ["install"]
