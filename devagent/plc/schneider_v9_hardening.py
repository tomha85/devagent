from __future__ import annotations

import xml.etree.ElementTree as ET

from devagent.plc import schneider_closeout_v9 as _v9


_INSTALLED = False
_PREVIOUS_SOURCE_MANIFEST = _v9._source_manifest
_PREVIOUS_CAPABILITY = _v9.schneider_capability_profile_v9
_REQUIRED_EXTERNAL_CORPUS = (
    "M340",
    "M580",
    "legacy Unity Pro",
    "mixed ST+LD+FBD",
    "DFB+DDT",
    "CASE/state-machine",
    "interlock/fault/recovery",
    "large industrial project",
)


def _hardened_source_manifest(path):
    try:
        return _PREVIOUS_SOURCE_MANIFEST(path)
    except ET.ParseError as exc:
        raise _v9._v1.SchneiderInputError(
            f"Invalid Control Expert XML exchange artifact: {exc}"
        ) from exc


def _hardened_capability(project):
    profile = dict(_PREVIOUS_CAPABILITY(project))
    facts = _v9._facts(project)
    profile["required_external_corpus"] = list(_REQUIRED_EXTERNAL_CORPUS)
    if facts is None:
        return profile

    support = facts.support
    audit = facts.source_audit
    profile["commercial_closeout_status"] = (
        "IMPLEMENTATION_QUALIFIED_PENDING_EXTERNAL_EVIDENCE"
        if support.contract == "FULL"
        and audit.metadata_consistent
        and not audit.unknown_executable_source_tags
        and not audit.missing_source_sections
        else "PARTIAL_FAIL_CLOSED"
    )
    return profile


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from devagent.plc import plc_dispatch as _dispatch
    from devagent.plc import schneider_control_expert_v1 as _root
    from devagent.plc import schneider_integration_v1 as _integration

    _v9._source_manifest = _hardened_source_manifest
    _v9.schneider_capability_profile_v9 = _hardened_capability
    _root.schneider_capability_profile = _hardened_capability
    _dispatch.analyze_schneider_control_expert = _v9.analyze_schneider_control_expert_v9
    _integration.schneider_capability_profile = _hardened_capability
    _INSTALLED = True


__all__ = ["install"]
