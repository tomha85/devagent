# Changelog

All notable changes to DevAgent are documented here.

## 0.3.1 — 2026-08-24

### Packaging and distribution

- Renamed the Python distribution from `devagent-local` to `devagent` so users can install with `pip install devagent` or `pipx install devagent`.
- Kept source-checkout and editable installation fully supported with `pip install -e ".[dev]"`.
- Added a PyPI Trusted Publishing workflow triggered by published GitHub releases.
- Added release-tag/package-version validation before PyPI publication.
- Added packaging metadata regression tests for the distribution name, version, CLI entry point, and development extras.

## 0.3.0 — 2026-08-24

### Production quality and evaluation

- Added GitHub Actions production CI on Python 3.10, 3.11, and 3.12.
- Added a coverage gate, compile check, `git diff --check`, CLI smoke tests, dependency validation, and clean wheel-install smoke test.
- Added a production evaluation contract with expected outcomes and bounded scope budgets.
- Added explicit false-`VERIFIED` and unexpected-`BLOCKED` metrics.
- Added acceptance-evidence, review-approval, final-verification, new-regression, and source-repository immutability checks.
- Added provider-independent model-call accounting for evaluation runs.
- Added versioned machine-readable evaluation reports.
- Added a seed production benchmark matrix covering end-to-end verification, evidence blocking, bounded repair/replan, provider contracts, worktree safety, command safety, and evaluation integrity.

### v0.2.1 foundations retained

- Strict structured provider contracts for OpenAI, Anthropic/Claude, xAI/Grok, and OpenAI-compatible endpoints.
- Deterministic repository retrieval with normalized matching and source/test relationships.
- Safe verification-capability probing.
- External retained worktree isolation for clean repositories.
- Generated-state filtering while preserving protection for real developer changes.
- Exact built-in `git diff --check` verification.
- Evidence-first outcome contract: `VERIFIED`, `PARTIALLY_VERIFIED`, or `BLOCKED`.
- No automatic commit, push, merge, rebase, or deploy.

## 0.2.1 — 2026-08-24

- Hardened real-provider structured output handling and bounded repair.
- Improved deterministic context retrieval and verification discovery.
- Hardened worktree isolation and generated-state handling.
- Added regression coverage for production safety and real-provider workflow gaps.

## 0.2.0

- Initial public evidence-driven DevAgent workflow.
