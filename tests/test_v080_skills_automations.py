from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from devagent.automations import Automation, AutomationStore, run_due
from devagent.autonomy import AgentTaskResult
from devagent.skills import SkillAwareProvider, SkillRegistry


class _CaptureProvider:
    def __init__(self) -> None:
        self.payload = None

    def request(self, *, role, payload, schema):
        self.payload = payload
        return {"ok": True}


def test_skill_registry_is_bounded_safe_and_requirement_relevant(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".devagent" / "skills" / "sqlite-migration"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# SQLite migration safety\nPreserve existing rows and prove rollback behavior.\n",
        encoding="utf-8",
    )
    unrelated = tmp_path / ".devagent" / "skills" / "css"
    unrelated.mkdir()
    (unrelated / "SKILL.md").write_text("# CSS layout\nUse grid alignment.\n", encoding="utf-8")
    oversized = tmp_path / ".devagent" / "skills" / "oversized"
    oversized.mkdir()
    (oversized / "SKILL.md").write_bytes(b"x" * ((64 * 1024) + 1))

    registry = SkillRegistry.discover(tmp_path)
    matched = registry.match("Implement a safe SQLite migration with rollback")

    assert [item.name for item in matched] == ["sqlite-migration"]
    assert "oversized" not in {item.name for item in registry.skills}


def test_skill_aware_provider_injects_only_matched_repo_skill(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".devagent" / "skills" / "pytest-regression"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Pytest regression testing\nAlways add a focused regression test.\n",
        encoding="utf-8",
    )
    provider = _CaptureProvider()
    wrapped = SkillAwareProvider(provider, SkillRegistry.discover(tmp_path))

    response = wrapped.request(
        role="planner",
        payload={"requirement": "Add pytest regression coverage"},
        schema={"type": "object"},
    )

    assert response == {"ok": True}
    assert provider.payload is not None
    assert [item["name"] for item in provider.payload["repository_skills"]] == ["pytest-regression"]


def test_automation_claim_prevents_overlap_and_recovers_after_lease(tmp_path: Path) -> None:
    store = AutomationStore(tmp_path)
    store.upsert(Automation("nightly", "Run regression verification", 600, 100))

    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = tuple(executor.map(lambda _index: store.claim_due(100), range(2)))
    claimed = [item for batch in attempts for item in batch]

    assert len(claimed) == 1
    first = claimed[0]
    assert first.claim_id
    assert first.claim_until_epoch == 3700
    assert store.claim_due(100) == ()

    recovered = store.claim_due(first.claim_until_epoch)
    assert len(recovered) == 1
    second = recovered[0]
    assert second.claim_id and second.claim_id != first.claim_id

    store.record_outcomes(
        {"nightly": "STALE"},
        claims={"nightly": first.claim_id},
        now_epoch=second.claim_until_epoch,
    )
    still_claimed = store.load()[0]
    assert still_claimed.claim_id == second.claim_id
    assert still_claimed.last_outcome is None

    store.record_outcomes(
        {"nightly": "VERIFIED"},
        claims={"nightly": second.claim_id},
        now_epoch=second.claim_until_epoch,
    )
    saved = store.load()[0]
    assert saved.claim_id is None
    assert saved.claim_until_epoch is None
    assert saved.last_outcome == "VERIFIED"
    assert saved.next_run_epoch == second.claim_until_epoch + 600


def test_automation_store_schedules_due_work_and_records_outcomes(tmp_path: Path, monkeypatch) -> None:
    store = AutomationStore(tmp_path)
    store.upsert(Automation("nightly", "Run regression verification", 600, 100))
    assert [item.id for item in store.due(100)] == ["nightly"]

    class _Coordinator:
        def __init__(self, repository_root, *, max_parallel=2):
            assert Path(repository_root).resolve() == tmp_path.resolve()
            assert max_parallel == 2

        def run(self, tasks):
            tasks = list(tasks)
            assert [(item.id, item.requirement) for item in tasks] == [
                ("nightly", "Run regression verification")
            ]
            return (AgentTaskResult("nightly", "VERIFIED", "run-1", "/tmp/worktree"),)

    monkeypatch.setattr("devagent.automations.ParallelAgentCoordinator", _Coordinator)
    outcomes = run_due(tmp_path, max_parallel=2, now_epoch=100)

    assert outcomes == (("nightly", "VERIFIED"),)
    saved = store.load()[0]
    assert saved.last_outcome == "VERIFIED"
    assert saved.next_run_epoch == 700
    assert saved.claim_id is None
    assert saved.claim_until_epoch is None
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
