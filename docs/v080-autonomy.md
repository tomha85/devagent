# v0.8 Autonomy

DevAgent v0.8 adds bounded autonomy without weakening the evidence-first verification model.

## Parallel agents

`devagent-autonomy --tasks tasks.json --max-parallel 2`

The task file is a JSON array:

```json
[
  {"id": "api", "requirement": "Fix the API regression and verify tests."},
  {"id": "docs", "requirement": "Update the matching documentation and verify references."}
]
```

Parallel batches require a clean Git worktree with a valid HEAD. Generated `.devagent/` state is ignored for cleanliness, but real source changes are not. Each real DevAgent run must obtain a distinct detached worktree. In-process Git worktree creation is serialized only while mutating Git worktree metadata; engineering runs execute concurrently after isolation succeeds.

The public concurrency limit is four agents per batch and sixteen tasks per invocation. Results are returned in input order. Parallel runs never auto-merge their isolated changes.

## Worktree pool

`WorktreePool` is a bounded semaphore-backed lease pool used by the parallel coordinator. It prevents unbounded fan-out and records peak in-process concurrency. Duplicate active lease IDs are rejected.

## Repository skills

Skills live under:

`.devagent/skills/<skill-name>/SKILL.md`

`devagent-skills --match "safe SQLite migration"`

Only simple skill directory names are accepted. Symlinks, binary files, empty files, files larger than 64 KiB, and entries beyond the 64-skill discovery cap are ignored. `SkillAwareProvider` injects at most three deterministic token-overlap matches into provider payloads. Skills are guidance, not command authority; deterministic harness safety and verification remain authoritative.

## Automations

`devagent-automation add --id nightly --every-seconds 86400 --requirement "Run regression verification."`

`devagent-automation run-due --max-parallel 2`

Automations are explicit foreground schedules stored at `.devagent/automations.json`. The minimum interval is five minutes and the store is capped at 64 entries. `run-due` executes only currently due enabled entries and advances their next-run timestamp after recording the observed outcome.

DevAgent does not install a daemon or silently create OS schedulers. Users may invoke `run-due` from their own cron, systemd timer, CI scheduler, or other trusted scheduler.

## Continuous real-provider benchmark

`devagent-provider-benchmark`

`devagent-provider-benchmark --interval-seconds 3600`

The benchmark runs one strict structured-output contract against each unique configured provider/model/base-URL target. Role configurations pointing to the same target are deduplicated. Reports contain provider/model identity, pass/fail, latency, and a bounded error string; API key/token/secret values present in the environment are redacted from recorded errors.

A nonzero interval repeats in the foreground until interrupted. The minimum recurring interval is five minutes. This is a live connectivity/structured-contract benchmark, not a claim that a model is universally correct for arbitrary software-engineering tasks.

## Qualification

v0.8 keeps the complete v4 Production Qualification catalog and adds a v5 autonomy catalog. Both catalogs must pass in Production CI. The invariant remains:

`false_verified == 0`
