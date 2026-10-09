from __future__ import annotations

import hashlib
import json
from typing import Any

from devagent.controls.models import (
    ControlSystemSpec,
    EquipmentSpec,
    RequirementAssertion,
    RequirementSpec,
)


def _canonical_assertion(
    assertion: RequirementAssertion | None,
) -> RequirementAssertion | None:
    if assertion is None:
        return None
    return RequirementAssertion(
        conditions=tuple(sorted(assertion.conditions, key=lambda pair: pair[0])),
        expected_ref=assertion.expected_ref,
        expected_value=assertion.expected_value,
    )


def _canonical_requirement(item: RequirementSpec) -> RequirementSpec:
    return RequirementSpec(
        id=item.id,
        text=item.text,
        criticality=item.criticality,
        assertion=_canonical_assertion(item.assertion),
    )


def _canonical_equipment(item: EquipmentSpec) -> EquipmentSpec:
    """Return a deeply immutable equipment record in canonical semantic order."""

    return EquipmentSpec(
        id=item.id,
        type=item.type,
        standard=item.standard,
        controller=item.controller,
        signals=tuple(sorted(item.signals)),
        commands=tuple(sorted(item.commands, key=lambda pair: pair[0])),
        status=tuple(sorted(item.status)),
        permissives=tuple(sorted(item.permissives)),
        interlocks=tuple(sorted(item.interlocks)),
        faults=tuple(sorted(item.faults)),
        alarms=tuple(sorted(item.alarms, key=lambda value: value.id)),
        hmi=item.hmi,
        requirements=tuple(
            _canonical_requirement(value)
            for value in sorted(item.requirements, key=lambda value: value.id)
        ),
        area=item.area,
        safety_zone=item.safety_zone,
        io=tuple(sorted(item.io, key=lambda value: (value.member, value.address))),
    )


def canonical_spec(spec: ControlSystemSpec) -> ControlSystemSpec:
    """Canonicalize semantically unordered authoring collections.

    The returned graph consists only of frozen dataclasses, tuples, strings, and
    booleans. This prevents a post-hash mutable dict/list from changing authoring
    intent without changing its recorded identity.
    """

    return ControlSystemSpec(
        schema=spec.schema,
        project_id=spec.project_id,
        controllers=tuple(sorted(spec.controllers, key=lambda value: value.id)),
        equipment=tuple(
            _canonical_equipment(item)
            for item in sorted(spec.equipment, key=lambda value: value.id)
        ),
    )


def normalized_spec_payload(spec: ControlSystemSpec) -> dict[str, Any]:
    canonical = canonical_spec(spec)
    controllers = [
        {
            "id": item.id,
            "vendor": item.vendor,
            "platform": item.platform,
            "network": item.network,
        }
        for item in canonical.controllers
    ]

    equipment: list[dict[str, Any]] = []
    for item in canonical.equipment:
        equipment.append(
            {
                "id": item.id,
                "type": item.type,
                "standard": item.standard,
                "controller": item.controller,
                "area": item.area,
                "safety_zone": item.safety_zone,
                "signals": list(item.signals),
                "commands": {name: enabled for name, enabled in item.commands},
                "status": list(item.status),
                "permissives": list(item.permissives),
                "interlocks": list(item.interlocks),
                "faults": list(item.faults),
                "alarms": [
                    {
                        "id": alarm.id,
                        "priority": alarm.priority,
                        "operator_response": alarm.operator_response,
                        "source_signal": alarm.source_signal,
                    }
                    for alarm in item.alarms
                ],
                "hmi": {
                    "faceplate": item.hmi.faceplate,
                    "historian": item.hmi.historian,
                },
                "io": [
                    {
                        "member": mapping.member,
                        "direction": mapping.direction,
                        "address": mapping.address,
                    }
                    for mapping in item.io
                ],
                "requirements": [
                    {
                        "id": requirement.id,
                        "text": requirement.text,
                        "criticality": requirement.criticality,
                        "assertion": (
                            None
                            if requirement.assertion is None
                            else {
                                "conditions": {
                                    ref: value
                                    for ref, value in requirement.assertion.conditions
                                },
                                "expect": {
                                    requirement.assertion.expected_ref:
                                        requirement.assertion.expected_value
                                },
                            }
                        ),
                    }
                    for requirement in item.requirements
                ],
            }
        )

    return {
        "schema": canonical.schema,
        "project_id": canonical.project_id,
        "controllers": controllers,
        "equipment": equipment,
    }


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def spec_sha256(spec: ControlSystemSpec) -> str:
    return hashlib.sha256(canonical_json_bytes(normalized_spec_payload(spec))).hexdigest()
