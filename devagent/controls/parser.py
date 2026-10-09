from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from devagent.controls.models import ControlSystemSpec
from devagent.controls.schema import ControlSpecError, parse_control_system_payload

_MAX_SPEC_BYTES = 2 * 1024 * 1024


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ControlSpecError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def load_control_system_spec(path: Path | str) -> ControlSystemSpec:
    target = Path(path).expanduser().resolve(strict=True)
    if not target.is_file():
        raise ControlSpecError(f"controls specification is not a file: {target}")
    size = target.stat().st_size
    if size > _MAX_SPEC_BYTES:
        raise ControlSpecError(
            f"controls specification exceeds {_MAX_SPEC_BYTES} byte production limit"
        )
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ControlSpecError("controls specification must be UTF-8 JSON") from exc
    try:
        payload = json.loads(text, object_pairs_hook=_unique_object)
    except json.JSONDecodeError as exc:
        raise ControlSpecError(
            f"invalid controls specification JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    return parse_control_system_payload(payload)
