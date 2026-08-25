# Changelog

All notable changes to DevAgent are documented here.

## 0.4.0 — 2026-08-25

### Production hardening and qualification

- Promoted the package metadata from Alpha to Beta after expanding the release qualification surface and validating the release candidate on supported CI toolchains.
- Added the production qualification v3 catalog with 50 required cases. The catalog retains the `false_verified == 0` invariant and now covers provider parity, actual multi-language toolchain execution, and release integrity in addition to the existing end-to-end, acceptance, safety, review, repair, reporting, and evaluation contracts.
- Added real executable qualification fixtures for Python/pytest, Node with TypeScript repository discovery, Go, Rust/Cargo, and C++/Make. Production qualification fails when a required toolchain is unavailable or its discovered repository-native command fails.
- Kept expensive real-toolchain fixtures out of the normal unit-test path while making the dedicated production qualification runner explicitly enable and execute them.
- Added Google Gemini / Google provider aliases through Google's OpenAI-compatible Gemini endpoint, reusing DevAgent's existing bounded structured-response handling and deterministic local schema validation.
- Added release-integrity regression tests proving package/version consistency, exact green-main release targeting, exact-tag package builds, `twine` validation, and PyPI Trusted Publishing requirements.
- Production CI now runs Python 3.10/3.11/3.12 suites, clean wheel installation, and the production qualification catalog; the qualification JSON report is retained as a CI artifact.
- Updated the default qualification catalog to `evaluation/benchmark_v3.json`.
- Versioned the Python distribution and CLI as `0.4.0`.

### Accuracy wording

- A 100% production-qualification result means every explicitly cataloged case passed. It is not a mathematical guarantee for every unseen repository, environment, model response, language, or engineering request.
- DevAgent continues to prefer a truthful `PARTIALLY_VERIFIED` or `BLOCKED` result over an unsupported `VERIFIED` result.

## 0.3.2 — 2026-08-24

### Developer review reporting and verified branch publishing

- Added automatic post-report branch publication for `VERIFIED` runs: DevAgent prints the full engineering review report first, then deterministic harness code commits and pushes the verified change. The current local non-protected development branch is continued by default; `main`, `master`, and `trunk` still cause creation of a new safe branch.
- Added `--no-publish` for developers who want a local review-only run; `--publish-branch` explicitly starts a new target branch instead of continuing the current development branch.
- Publishing is performed by deterministic harness code, not by the model or engineering command runner.
- Publishing requires the default isolated worktree, refuses direct publication to `main`, `master`, and `trunk`, supports safe continuation of the current local development branch, stages only reviewed changed paths, creates one commit, re-checks the expected remote HEAD, and uses normal fast-forward push semantics.
- DevAgent still never creates pull requests or performs merges, rebases, deployments, force pushes, or destructive Git operations.
- Expanded final reports with an implementation-logic summary first, exact Python function/class/test-symbol inventory, acceptance-criteria evidence, per-command verification phase/revision/exit code/duration/test counts, failure classifications, bounded stdout/stderr for failed checks, independent-review details, completeness assessment, and deterministic recommendations.
- Added a post-report source-control publication receipt with remote, branch, commit SHA, commit status, push status, and any publication error.
- Added machine-readable source-control results to `report.json` after publication completes.
- Added local bare-remote regression tests proving successful branch push and rejection of protected/non-VERIFIED publication attempts.
- Added CLI regression coverage proving the engineering report is emitted before automatic publication and that `--no-publish` disables commit/push.

## 0.3.1 — 2026-08-24

### Packaging and distribution

- Published the Python distribution as `devagent-ai` so users can install with `pip install devagent-ai` or `pipx install devagent-ai`, while the CLI command remains `devagent`.
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
- No merge, rebase, force-push, PR creation, or deploy automation.

## 0.2.1 — 2026-08-24

- Hardened real-provider structured output handling and bounded repair.
- Improved deterministic context retrieval and verification discovery.
- Hardened worktree isolation and generated-state handling.
- Added regression coverage for production safety and real-provider workflow gaps.

## 0.2.0

- Initial public evidence-driven DevAgent workflow.
