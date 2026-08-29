from __future__ import annotations

from datetime import datetime, timezone

from devagent.live.models import Quality, RuntimeValue, TrustState


def _value(*, quality: Quality, stale: bool) -> RuntimeValue:
    now = datetime.now(timezone.utc)
    return RuntimeValue(
        node_id="ns=2;s=Test",
        value=True,
        variant_type="Boolean",
        status_code="Good",
        quality=quality,
        source_timestamp=now,
        server_timestamp=now,
        received_at=now,
        age_seconds=0.0,
        stale=stale,
    )


def test_runtime_value_trust_is_fail_closed() -> None:
    assert _value(quality=Quality.GOOD, stale=False).trust is TrustState.CURRENT
    assert _value(quality=Quality.UNCERTAIN, stale=False).trust is TrustState.UNCERTAIN
    assert _value(quality=Quality.BAD, stale=False).trust is TrustState.UNTRUSTED
    assert _value(quality=Quality.GOOD, stale=True).trust is TrustState.STALE
    assert _value(quality=Quality.BAD, stale=True).trust is TrustState.UNTRUSTED


def test_bad_quality_is_not_a_successful_load() -> None:
    assert _value(quality=Quality.GOOD, stale=False).loaded_successfully is True
    assert _value(quality=Quality.BAD, stale=False).loaded_successfully is False
