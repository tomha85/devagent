from __future__ import annotations

import hashlib
import json
from typing import Any

from devagent.controls.models import ControlSystemSpec


def normalized_spec_payload(spec: ControlSystemSpec) -> dict[str, Any]:
    controllers = [
        {"id": item.id, "vendor": item.vendor, "platform": item.platform}
        for item in sorted(spec.controllers, key=lambda value: value.id)
    ]

    equipment: list[dict[str, Any]] = []
    for item in sorted(spec.equipment, key=lambda value: value.id):
        equipment.append(
            {
                "id": item.id,
                "type": item.type,
                "standard": item.standard,
                "controller": item.controller,
                "signals": sorted(item.signals),
                "commands": {
                    name: enabled for name, enabled in sorted(item.commands, key=lambda pair: pair[0])
                },
                "status": sorted(item.status),
                "permissives": sorted(item.permissives),
                "interlocks": sorted(item.interlocks),
                "alarms": [
                    {
                        "id": alarm.id,
                        "priority": alarm.priority,
                        "operator_response": alarm.operator_response,
                    }
                    for alarm in sorted(item.alarms, key=lambda value: value.id)
                ],
                "hmi": {
                    "faceplate": item.hmi.faceplate,
                    "historian": item.hmi.historian,
                },
                "requirements": [
                    {
                        "id": requirement.id,
                        "text": requirement.text,
                        "criticality": requirement.criticality,
                    }
                    for requirement in sorted(item.requirements, key=lambda value: value.id)
                ],
            }
        )

    return {
        "schema": spec.schema,
        "project_id": spec.project_id,
        "controllers": controllers,
        "equipment": equipment,
    }


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def spec_sha256(spec: ControlSystemSpec) -> str:
    return hashlib.sha256(canonical_json_bytes(normalized_spec_payload(spec))).hexdigest()
