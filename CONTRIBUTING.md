# Contributing to DevAgent

Thanks for your interest in contributing to DevAgent.

DevAgent is an evidence-driven engineering agent. Contributions should preserve its core principle: **do not claim a software change is verified unless the repository evidence and executed checks support that conclusion.**

## Before you start

For non-trivial changes, consider opening an issue first so the problem, scope, and expected behavior are clear before implementation.

Please keep pull requests focused. Avoid unrelated refactors, formatting churn, dependency upgrades, or broad architectural changes unless they are part of the stated goal.

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
6. Keep commit/push publication post-report, post-`VERIFIED`, deterministic, isolated-worktree-only, limited to a new non-protected branch, and outside model-facing command execution. Preserve `--no-publish` as an explicit review-only escape hatch. Never add automatic PR, merge, rebase, force-push, or deploy behavior.
7. Do not fabricate command execution or verification results.
8. Keep provider-specific behavior behind the provider abstraction when possible.
9. Treat `BLOCKED` or `PARTIALLY_VERIFIED` as valid outcomes when evidence is insufficient.
10. Keep automated tests deterministic and free of paid cloud-model calls.

## Tests

Bug fixes should include a regression test that fails before the fix when practical.

Features should include tests for the new behavior and relevant edge cases.

Changes to orchestration, safety, verification, review, provider contracts, worktrees, repository discovery, or branch publishing should include focused regression coverage because these areas directly affect the trustworthiness of final outcomes.

Branch-publishing tests should use local bare Git repositories where possible and must prove that protected/non-VERIFIED publication attempts are rejected. CLI tests should also prove that the engineering report is emitted before any commit/push action.

## Pull requests

A good pull request includes:

- a concise problem statement,
- the root cause or design rationale,
- the minimum implementation needed,
- tests added or updated,
- verification commands and results,
- known limitations,
- confirmation that no unrelated changes were included.

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

## License

By contributing to DevAgent, you agree that your contribution will be licensed under the project's [MIT License](LICENSE).
