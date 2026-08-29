from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any


class Quality(str, Enum):
    GOOD = "GOOD"
    UNCERTAIN = "UNCERTAIN"
    BAD = "BAD"


class TrustState(str, Enum):
    CURRENT = "CURRENT"
    UNCERTAIN = "UNCERTAIN"
    UNTRUSTED = "UNTRUSTED"
    STALE = "STALE"


@dataclass(frozen=True)
class EndpointSummary:
    endpoint_url: str
    security_mode: str
    security_policy_uri: str
    user_token_types: tuple[str, ...]
    server_application_name: str


@dataclass(frozen=True)
class BrowseNode:
    path: str
    node_id: str
    browse_name: str
    display_name: str
    node_class: str
    data_type: str | None
    user_access: tuple[str, ...]
    readable: bool
    writable: bool


@dataclass(frozen=True)
class RuntimeValue:
    node_id: str
    value: Any
    variant_type: str | None
    status_code: str
    quality: Quality
    source_timestamp: datetime | None
    server_timestamp: datetime | None
    received_at: datetime
    age_seconds: float | None
    stale: bool
    replayed: bool = False

    @property
    def trust(self) -> TrustState:
        if self.quality is Quality.BAD:
            return TrustState.UNTRUSTED
        if self.stale:
            return TrustState.STALE
        if self.quality is Quality.UNCERTAIN:
            return TrustState.UNCERTAIN
        return TrustState.CURRENT

    @property
    def loaded_successfully(self) -> bool:
        return self.quality is not Quality.BAD
