# Contributing to DevAgent

Thanks for your interest in contributing to DevAgent.

DevAgent is an evidence-driven engineering agent. Contributions should preserve its core principle: **do not claim a software or industrial engineering result is verified unless the available evidence and executed checks support that conclusion.**

## Before you start

For non-trivial changes, consider opening an issue first so the problem, scope, and expected behavior are clear before implementation.

Please keep pull requests focused. Avoid unrelated refactors, formatting churn, dependency upgrades, or broad architectural changes unless they are part of the stated goal.

Read [OPEN_SOURCE.md](OPEN_SOURCE.md) before contributing PLC fixtures, vendor examples, qualification cases, commissioning evidence, or other industrial data.

## Development setup

```bash
git clone https://github.com/tomha85/devagent.git
cd devagent
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

Useful checks:

```bash
python -m compileall devagent
pytest -q
git diff --check
```

## Engineering expectations

Changes should follow these rules:

1. Understand the existing behavior before editing it.
2. Prefer the smallest correct change.
3. Add or update meaningful tests for behavior changes.
4. Preserve developer dirty work and backup-before-edit behavior.
5. Do not weaken workspace confinement or secret-path protections.
6. Keep commit/push publication post-report, post-`VERIFIED`, deterministic, isolated-worktree-only, and outside model-facing command execution. Current local non-protected development branches may continue only with remote-head/concurrency checks and normal fast-forward push semantics; protected branches must create a new safe branch. Preserve `--no-publish` as an explicit review-only escape hatch. Never add automatic PR, merge, rebase, force-push, or deploy behavior.
7. Do not fabricate command execution or verification results.
8. Keep provider-specific behavior behind the provider abstraction when possible.
9. Treat `BLOCKED` or `PARTIALLY_VERIFIED` as valid outcomes when evidence is insufficient.
10. Keep automated tests deterministic and free of paid cloud-model calls unless a test is explicitly defined as an opt-in live-provider qualification.
11. Preserve the DevAgent Live read-only boundary. Do not add PLC write, force, reset, bypass, download, mode-change, start/stop, or equivalent machine-control authority to model-facing Live workflows.
12. Keep static/simulator evidence distinct from real-vendor or field certification claims.

## Public/private asset boundary

This repository is the open-source core. Do **not** contribute confidential or non-redistributable assets, including:

- customer PLC projects, exports, requirements, reports, or screenshots;
- customer OPC UA endpoints, credentials, certificates, mappings, namespaces, runtime captures, or site topology;
- customer incident history, evidence history, or proprietary operating data;
- private field-failure corpora or production compatibility intelligence;
- vendor artifacts whose licenses do not permit redistribution;
- customer-specific semantic/rule packs or private qualification corpora;
- employer or third-party confidential material.

Use synthetic, independently authored, public-domain, permissively licensed, or otherwise redistribution-safe fixtures.

If you are unsure whether you have the right to publish a PLC export, sample, log, screenshot, document, or vendor artifact, do not include it in a pull request until redistribution rights are confirmed.

## Tests

Bug fixes should include a regression test that fails before the fix when practical.

Features should include tests for the new behavior and relevant edge cases.

Changes to orchestration, safety, verification, review, provider contracts, worktrees, repository discovery, branch publishing, PLC proof semantics, or Live trust/evidence handling should include focused regression coverage because these areas directly affect the trustworthiness of final outcomes.

Branch-publishing tests should use local bare Git repositories where possible and must prove that protected/non-`VERIFIED` publication attempts are rejected, current development branches continue by fast-forward only, and remote movement/divergence is blocked. CLI tests should also prove that the engineering report is emitted before any commit/push action.

## Pull requests

A good pull request includes:

- a concise problem statement,
- the root cause or design rationale,
- the minimum implementation needed,
- tests added or updated,
- verification commands and results,
- known limitations,
- confirmation that no unrelated changes were included,
- confirmation that no customer, confidential, or non-redistributable vendor material was added.

Please ensure before opening the PR:

```bash
python -m compileall devagent
pytest -q
git diff --check
```

## Commit and branch guidance

Use descriptive branches and commits, for example:

```text
fix/structured-provider-contract
feat/worktree-lifecycle
chore/docs-cleanup
```

Prefer clear commit messages that explain the engineering change rather than the editing process.

## Security changes

If your contribution fixes a vulnerability or exposes a sensitive security issue, follow [SECURITY.md](SECURITY.md) instead of opening a public issue with exploit details.

## License and contribution rights

By contributing to DevAgent, you agree that your accepted contribution will be licensed under the project's [MIT License](LICENSE).

You must have the right to submit the contribution under that license. Do not submit code, documentation, datasets, PLC exports, screenshots, logs, vendor material, customer material, or other content that you are not authorized to redistribute under compatible terms.

The MIT License for repository content does not grant rights to use DevAgent branding in a way that implies official endorsement. See [TRADEMARKS.md](TRADEMARKS.md).
