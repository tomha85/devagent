from __future__ import annotations

import asyncio
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .agent_integration import LiveDataTrustLayer
from .engineering_context import LiveEngineeringContext
from .manager import MultiPlcConnectionManager
from .tag_reconciliation import LiveTagReconciliation


@dataclass(frozen=True)
class LiveHistoricalSample:
    timestamp: datetime
    plc_id: str
    tag_id: str
    tag_name: str
    node_id: str
    value: Any
    definitive_current: bool
    quality: str
    trust: str


@dataclass(frozen=True)
class LiveSignalTransition:
    timestamp: datetime
    plc_id: str
    tag_id: str
    tag_name: str
    node_id: str
    old_value: Any
    new_value: Any


@dataclass(frozen=True)
class LiveHistoricalDiagnosis:
    target_output: str
    transition: LiveSignalTransition | None
    preceding_changes: tuple[LiveSignalTransition, ...]
    lookback_seconds: float
    limitations: tuple[str, ...]

    def render_text(self) -> str:
        if self.transition is None:
            return (
                f"Historical diagnosis: no matching trusted transition for {self.target_output} "
                f"was captured in the last {self.lookback_seconds:g} seconds."
            )
        lines = [
            "Historical diagnosis (read-only, trusted timeline):",
            (
                f"- {self.transition.tag_name} changed "
                f"{self.transition.old_value!r} -> {self.transition.new_value!r} "
                f"at {self.transition.timestamp.isoformat()}"
            ),
        ]
        if self.preceding_changes:
            lines.append("- Relevant preceding dependency changes:")
            for item in self.preceding_changes:
                delta = (self.transition.timestamp - item.timestamp).total_seconds()
                lines.append(
                    f"  - {item.tag_name}: {item.old_value!r} -> {item.new_value!r} "
                    f"{delta:.3f}s before target transition"
                )
        else:
            lines.append(
                "- No trusted dependency transition was captured before the target transition in this window."
            )
        if self.limitations:
            lines.append("- Limitations:")
            lines.extend(f"  - {item}" for item in self.limitations)
        lines.append(
            "Temporal ordering is commissioning evidence, not proof of physical/process causation."
        )
        return "\n".join(lines)


_TIME_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h)\b",
    re.IGNORECASE,
)


def requested_history_seconds(question: str, *, default: float = 60.0) -> float:
    """Return the requested historical age, bounded to one day."""
    match = _TIME_RE.search(str(question or ""))
    if match is None:
        return default
    value = float(match.group("value"))
    unit = match.group("unit").casefold()
    if unit.startswith("m"):
        value *= 60.0
    elif unit.startswith("h"):
        value *= 3600.0
    return max(1.0, min(value, 86400.0))


def requested_transition_direction(question: str) -> str | None:
    """Return STOP/START intent when the question explicitly asks for one."""
    text = " " + re.sub(r"[_-]+", " ", str(question or "").casefold()) + " "
    stop_terms = (
        " stop ",
        " stopped ",
        " turn off ",
        " turned off ",
        " went false ",
        " became false ",
        " dropped ",
        " deenergized ",
        " de energized ",
    )
    start_terms = (
        " start ",
        " started ",
        " turn on ",
        " turned on ",
        " went true ",
        " became true ",
        " energized ",
        " resumed ",
        " restart ",
        " restarted ",
    )
    if any(term in text for term in stop_terms):
        return "STOP"
    if any(term in text for term in start_terms):
        return "START"
    return None


def is_historical_question(question: str) -> bool:
    text = " " + str(question or "").casefold() + " "
    phrases = (
        " ago ",
        " before ",
        " earlier ",
        " what happened ",
        " why did ",
        " when did ",
        " fault occurred ",
        " fault happen ",
        " last time ",
    )
    return any(item in text for item in phrases)


def _truthy_state(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in {"true", "on", "run", "running", "active", "energized", "1"}:
            return True
        if text in {
            "false",
            "off",
            "stop",
            "stopped",
            "inactive",
            "deenergized",
            "0",
        }:
            return False
    return None


def _matches_direction(item: LiveSignalTransition, direction: str | None) -> bool:
    if direction is None:
        return True
    old = _truthy_state(item.old_value)
    new = _truthy_state(item.new_value)
    if old is None or new is None:
        return False
    if direction == "STOP":
        return old and not new
    if direction == "START":
        return (not old) and new
    return True


class LiveTimelineStore:
    def __init__(
        self,
        *,
        retention_seconds: float = 900.0,
        max_samples: int = 20000,
        max_transitions: int = 10000,
        continuity_seconds: float = 5.0,
    ) -> None:
        if retention_seconds <= 0:
            raise ValueError("retention_seconds must be > 0")
        if max_samples < 1 or max_transitions < 1:
            raise ValueError("history bounds must be >= 1")
        if continuity_seconds <= 0:
            raise ValueError("continuity_seconds must be > 0")
        self.retention_seconds = float(retention_seconds)
        self.continuity_seconds = float(continuity_seconds)
        self._samples: deque[LiveHistoricalSample] = deque(maxlen=max_samples)
        self._transitions: deque[LiveSignalTransition] = deque(maxlen=max_transitions)
        self._latest_by_tag: dict[str, LiveHistoricalSample] = {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _trim(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.retention_seconds)
        while self._samples and self._samples[0].timestamp < cutoff:
            self._samples.popleft()
        while self._transitions and self._transitions[0].timestamp < cutoff:
            self._transitions.popleft()
        stale_ids = [
            tag_id
            for tag_id, sample in self._latest_by_tag.items()
            if sample.timestamp < cutoff
        ]
        for tag_id in stale_ids:
            self._latest_by_tag.pop(tag_id, None)

    def append(self, sample: LiveHistoricalSample) -> None:
        self._trim(sample.timestamp)
        previous = self._latest_by_tag.get(sample.tag_id)
        self._samples.append(sample)
        self._latest_by_tag[sample.tag_id] = sample
        if previous is None:
            return
        gap = (sample.timestamp - previous.timestamp).total_seconds()
        continuous = 0.0 <= gap <= self.continuity_seconds
        if (
            continuous
            and previous.definitive_current
            and sample.definitive_current
            and previous.value != sample.value
        ):
            self._transitions.append(
                LiveSignalTransition(
                    timestamp=sample.timestamp,
                    plc_id=sample.plc_id,
                    tag_id=sample.tag_id,
                    tag_name=sample.tag_name,
                    node_id=sample.node_id,
                    old_value=previous.value,
                    new_value=sample.value,
                )
            )

    def append_many(self, samples: Iterable[LiveHistoricalSample]) -> None:
        for sample in samples:
            self.append(sample)

    def transitions(self) -> tuple[LiveSignalTransition, ...]:
        return tuple(self._transitions)

    def latest_samples(self) -> tuple[LiveHistoricalSample, ...]:
        return tuple(self._latest_by_tag.values())

    def diagnose_recent_transition(
        self,
        context: LiveEngineeringContext,
        target_output: str,
        *,
        dependency_tag_ids: Iterable[str] = (),
        lookback_seconds: float = 60.0,
        now: datetime | None = None,
        preceding_seconds: float = 10.0,
        max_preceding: int = 12,
        target_age_seconds: float | None = None,
        direction: str | None = None,
    ) -> LiveHistoricalDiagnosis:
        if lookback_seconds <= 0 or preceding_seconds <= 0:
            raise ValueError("history windows must be > 0")
        if target_age_seconds is not None and target_age_seconds < 0:
            raise ValueError("target_age_seconds must be >= 0")
        if direction not in {None, "STOP", "START"}:
            raise ValueError("direction must be STOP, START, or None")
        tag = context.unique_tag_for_reference(target_output)
        if tag is None:
            return LiveHistoricalDiagnosis(
                target_output=target_output,
                transition=None,
                preceding_changes=(),
                lookback_seconds=lookback_seconds,
                limitations=(
                    "Target does not resolve to exactly one canonical engineering tag.",
                ),
            )
        current = now or self._now()
        cutoff = current - timedelta(seconds=lookback_seconds)
        candidates = [
            item
            for item in self._transitions
            if item.tag_id == tag.id
            and cutoff <= item.timestamp <= current
            and _matches_direction(item, direction)
        ]
        if not candidates:
            direction_text = f" {direction.lower()}" if direction else ""
            return LiveHistoricalDiagnosis(
                target_output=target_output,
                transition=None,
                preceding_changes=(),
                lookback_seconds=lookback_seconds,
                limitations=(
                    f"No trusted{direction_text} transition matching the requested event was captured.",
                    "The timeline only contains data observed after this Live session started; earlier controller history is not reconstructed.",
                ),
            )
        if target_age_seconds is None:
            target_transition = max(candidates, key=lambda item: item.timestamp)
        else:
            target_transition = min(
                candidates,
                key=lambda item: abs(
                    (current - item.timestamp).total_seconds() - target_age_seconds
                ),
            )

        dependency_ids = {str(item) for item in dependency_tag_ids}
        dependency_ids.discard(tag.id)
        before = target_transition.timestamp
        after = before - timedelta(seconds=preceding_seconds)
        preceding = [
            item
            for item in self._transitions
            if item.tag_id in dependency_ids and after <= item.timestamp <= before
        ]
        preceding.sort(
            key=lambda item: (
                (before - item.timestamp).total_seconds(),
                item.tag_name.casefold(),
            )
        )
        limitations = [
            "Only trusted CURRENT transitions with continuous observations are considered.",
            "A preceding change is a temporal candidate; deterministic PLC logic and engineer evidence are still required to prove causation.",
        ]
        if target_age_seconds is not None:
            actual_age = (current - target_transition.timestamp).total_seconds()
            limitations.append(
                f"Selected the matching transition nearest requested age {target_age_seconds:g}s; observed age={actual_age:.3f}s."
            )
        return LiveHistoricalDiagnosis(
            target_output=target_output,
            transition=target_transition,
            preceding_changes=tuple(preceding[:max_preceding]),
            lookback_seconds=lookback_seconds,
            limitations=tuple(limitations),
        )


class LiveHistoryCollector:
    """Bounded read-only polling collector used by onsite historical diagnosis."""

    def __init__(
        self,
        manager: MultiPlcConnectionManager,
        reconciliation: LiveTagReconciliation,
        *,
        retention_seconds: float = 300.0,
        poll_interval_seconds: float = 1.0,
        max_tags: int = 64,
        preferred_tag_ids: Iterable[str] = (),
        store: LiveTimelineStore | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be > 0")
        if max_tags < 1 or max_tags > 256:
            raise ValueError("max_tags must be between 1 and 256")
        self.manager = manager
        self.reconciliation = reconciliation
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.max_tags = max_tags
        self.store = store or LiveTimelineStore(
            retention_seconds=retention_seconds,
            continuity_seconds=max(2.0, poll_interval_seconds * 3.0),
        )
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.last_error: str | None = None
        self.cycles = 0

        priorities = [
            str(item).strip()
            for item in preferred_tag_ids
            if str(item).strip()
        ]
        order = {tag_id: index for index, tag_id in enumerate(priorities)}
        accepted = list(reconciliation.accepted_mappings())
        accepted.sort(
            key=lambda mapping: (
                0 if mapping.tag_id in order else 1,
                order.get(mapping.tag_id, 10**9),
                mapping.tag_name.casefold(),
                mapping.tag_id,
            )
        )
        self._mappings = tuple(accepted[:max_tags])
        self._node_to_mapping = {
            mapping.selected_node_id: mapping
            for mapping in self._mappings
            if mapping.selected_node_id is not None
        }

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def captured_tag_ids(self) -> tuple[str, ...]:
        return tuple(mapping.tag_id for mapping in self._mappings)

    async def start(self) -> None:
        if self.active or not self._mappings:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="devagent-live-history")

    async def stop(self) -> None:
        self._stop.set()
        task, self._task = self._task, None
        if task is None:
            return
        try:
            await asyncio.wait_for(
                task, timeout=max(1.0, self.poll_interval_seconds * 2.0)
            )
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        plc_id = self.reconciliation.plc_id
        trust_layer = LiveDataTrustLayer()
        node_ids = tuple(self._node_to_mapping)
        while not self._stop.is_set():
            try:
                results = await self.manager.read_many({plc_id: node_ids})
                batch = results[plc_id]
                if batch.error:
                    self.last_error = str(batch.error)
                status = self.manager.status(plc_id)
                samples: list[LiveHistoricalSample] = []
                for value in batch.values:
                    mapping = self._node_to_mapping.get(value.node_id)
                    if mapping is None:
                        continue
                    record = trust_layer.record(
                        plc_id=plc_id,
                        plc_name=status.plc_name,
                        value=value,
                    )
                    stamp = (
                        value.source_timestamp
                        or value.server_timestamp
                        or value.received_at
                    )
                    samples.append(
                        LiveHistoricalSample(
                            timestamp=stamp,
                            plc_id=plc_id,
                            tag_id=mapping.tag_id,
                            tag_name=mapping.tag_name,
                            node_id=value.node_id,
                            value=value.value,
                            definitive_current=record.definitive_current,
                            quality=record.quality,
                            trust=record.trust,
                        )
                    )
                self.store.append_many(samples)
                self.cycles += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass


__all__ = [
    "LiveHistoricalSample",
    "LiveSignalTransition",
    "LiveHistoricalDiagnosis",
    "LiveTimelineStore",
    "LiveHistoryCollector",
    "requested_history_seconds",
    "requested_transition_direction",
    "is_historical_question",
]
