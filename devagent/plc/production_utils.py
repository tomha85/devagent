from __future__ import annotations

import hashlib
import re
from typing import Any

from devagent.plc.production_models import Severity

STOPWORDS = {
    "shall", "must", "should", "will", "when", "then", "with", "from", "that", "this", "into", "upon",
    "after", "before", "while", "where", "which", "have", "has", "been", "being", "system", "machine",
    "controller", "logic", "true", "false", "active", "inactive", "enable", "enabled", "disable", "disabled",
}
TRUE_WORDS = {"true", "on", "energized", "active"}
FALSE_WORDS = {"false", "off", "deenergized", "inactive"}


def source_locator(source: Any) -> str | None:
    return getattr(source, "locator", None)


def severity(value: str, default: Severity = Severity.MEDIUM) -> Severity:
    try:
        return Severity(str(value).upper())
    except ValueError:
        return default


def tag_occurs(text: str, tag: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(tag)}(?![A-Za-z0-9_])", text, flags=re.IGNORECASE) is not None


def tokens(text: str) -> set[str]:
    return {token.casefold() for token in re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text) if token.casefold() not in STOPWORDS}


def explicit_bool(text: str, tag: str) -> bool | None:
    escaped = re.escape(tag)
    patterns = [
        rf"{escaped}\s*(?:=|==|is|shall\s+be|must\s+be)\s*(TRUE|FALSE|ON|OFF|ACTIVE|INACTIVE)\b",
        rf"\b(TRUE|FALSE|ON|OFF|ACTIVE|INACTIVE)\s*(?:=|==|for)\s*{escaped}\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).casefold()
        if value in TRUE_WORDS:
            return True
        if value in FALSE_WORDS:
            return False
    return None


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha1(":".join(parts).encode("utf-8")).hexdigest()[:10].upper()
    return f"{prefix}-{digest}"
