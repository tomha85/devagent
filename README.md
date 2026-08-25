# DevAgent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#project-status)
[![Production CI](https://github.com/tomha85/devagent/actions/workflows/ci.yml/badge.svg)](https://github.com/tomha85/devagent/actions/workflows/ci.yml)

**A local, evidence-driven software engineering agent that turns a requirement into a tested, independently reviewed patch, prints a developer-grade engineering report, then automatically commits and fast-forward pushes a `VERIFIED` result to the developer's current local branch — creating a new safe branch only when starting from `main`, `master`, or `trunk` — without creating a PR or merging code.**

DevAgent runs against a local repository. It discovers the application, gathers source evidence, compiles acceptance criteria, plans a bounded change, creates backups, implements the minimum necessary patch, runs repository-supported verification, reviews the final diff, prints a detailed engineering review report, and only then performs bounded source-control publication when the result is `VERIFIED`.

> **From requirement to evidence-backed verified branch.**
>
> Normal flow: **implement → verify → report → commit → push branch → stop**. DevAgent never creates a pull request or merges code. Use `--no-publish` for a local review-only run.

## Why DevAgent?

Many coding agents optimize for generating code quickly. DevAgent is designed around a different question:

**Can the change be supported by repository evidence and verified locally?**

Core principles:

- **Local-first** — works against the developer's local repository and environment.
- **Evidence before modification** — implementation is blocked when source evidence is insufficient.
- **Minimal-change discipline** — prefer the smallest correct diff over broad refactors.
- **Backups before edits** — existing files are backed up before first modification.
- **Repository-native verification** — use commands supported by manifests, package scripts, tests, and CI evidence.
- **Independent review** — the final diff is reviewed separately from implementation.
- **Developer-grade reporting** — the report begins with implementation logic, then lists exact changed symbols, tests, acceptance evidence, failures, gaps, and completeness.
- **Provider choice** — OpenAI, Anthropic/Claude, xAI/Grok, and OpenAI-compatible local endpoints.
- **Bounded automatic publishing** — after the report, only a `VERIFIED` result may be committed and fast-forward pushed from the isolated worktree; normal development branches continue in place, while protected branches cause DevAgent to create a new safe branch.
- **Review-only escape hatch** — `--no-publish` disables commit/push when the developer wants to inspect locally first.
- **No PR or merge automation** — DevAgent never creates PRs, merges, rebases, force-pushes, or deploys.
- **Evidence-backed outcomes** — final status is `VERIFIED`, `PARTIALLY_VERIFIED`, or `BLOCKED`.

## Quick start

### Install

From PyPI:

```bash
python -m pip install devagent-ai
```

For an isolated CLI installation, `pipx` is recommended:

```bash
pipx install devagent-ai
```

Or install and develop directly from a source checkout:

```bash
git clone https://github.com/tomha85/devagent.git
cd devagent
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest -q
```

Both installation paths expose the same `devagent` CLI. The PyPI distribution is named `devagent-ai`; a source checkout remains the best option for contributors.

### Configure an AI provider

OpenAI example:

```bash
devagent setup --provider openai --model YOUR_MODEL
export OPENAI_API_KEY=...
devagent doctor
```

Anthropic example:

```bash
devagent setup --provider anthropic --model YOUR_MODEL
export ANTHROPIC_API_KEY=...
devagent doctor
```

OpenAI-compatible local endpoint:

```bash
devagent setup \
  --provider compatible \
  --model local-model \
  --base-url http://127.0.0.1:11434/v1
```

DevAgent stores provider configuration and the **name** of the API-key environment variable. It does not store the API key itself.

### Run

From the application repository you want DevAgent to work on:

```bash
devagent "Fix websocket reconnect bug and add regression tests"
```

For a normal `VERIFIED` run, DevAgent will:

```text
implement and verify
        ↓
print the full engineering review report
        ↓
continue the current local development branch
(or create a new DevAgent branch from main/master/trunk)
        ↓
commit only reviewed changed paths
        ↓
fast-forward push that branch to origin
        ↓
print a source-control publication receipt
        ↓
STOP — no PR, no merge
```

By default, DevAgent treats the developer's current local Git branch as the working branch. Repeated prompts continue that same non-protected branch. If the developer is on `main`, `master`, or `trunk`, DevAgent creates a unique safe branch such as:

```text
devagent/20260825T020000Z-ab12cd
```

To explicitly start a new branch instead of continuing the current development branch:

```bash
devagent \
  --publish-branch feature/devagent-csv-export \
  "Add CSV export to reports"
```

To run DevAgent without any commit/push:

```bash
devagent --no-publish \
  "Add CSV export to reports"
```

`--publish` remains accepted as an explicit expression of the default publishing behavior, but is not required.

Automatic publication is intentionally narrow:

- the engineering report is emitted before any commit or push,
- the run must finish `VERIFIED`,
- the default isolated worktree must be active,
- `main`, `master`, and `trunk` are never publication targets; DevAgent creates a new safe branch when started there,
- an existing branch may be continued only when it is the developer's current local non-protected branch and its remote history is compatible,
- remote branch state is captured before model execution and checked again before publication,
- only reviewed changed paths are staged,
- one commit is created and pushed with normal fast-forward Git semantics; no force push is used,
- no pull request is created,
- no merge, rebase, force push, or deployment is performed.

Other inputs:

```bash
devagent "Add CSV export to reports"
devagent --input error.log
pytest 2>&1 | devagent
devagent
```

Useful commands:

```bash
devagent --help
devagent --version
devagent setup --help
devagent doctor
devagent status
```

Normal mode stays quiet apart from state updates, the engineering report, and the post-report publication receipt. `--verbose` exposes operational diagnostics, not hidden chain-of-thought.

## What DevAgent does

A normal engineering run follows a deterministic lifecycle:

```text
PREFLIGHT
  ↓
DISCOVER
  ↓
UNDERSTAND
  ↓
TASK_SPEC / ACCEPTANCE CRITERIA
  ↓
BASELINE
  ↓
PLAN
  ↓
GATHER_CONTEXT
  ↓
REPRODUCE
  ↓
IMPLEMENT MINIMAL PATCH
  ↓
VERIFY_TARGETED
  ↓
DIAGNOSE / REPLAN when needed
  ↓
VERIFY_BROAD
  ↓
INDEPENDENT REVIEW
  ↓
QUALITY_CHECK
  ↓
FINAL_VERIFY
  ↓
LEARN
  ↓
REPORT
  ↓
IF VERIFIED: COMMIT + FAST-FORWARD PUSH WORKING BRANCH
  ↓
STOP
```

Python owns deterministic state transitions, safety gates, filesystem operations, verification validity, retry bounds, final status, report generation, and verified-branch publication. The model reasons inside bounded roles and never receives a general-purpose Git publishing tool.

## Evidence gate

DevAgent does not treat model confidence as evidence.

Before implementation, it expects enough repository evidence to support:

- the engineering problem,
- expected behavior,
- affected paths,
- likely root cause or design location,
- a minimal proposed solution,
- source evidence tying those claims to the repository.

If the evidence is insufficient, `BLOCKED` is the correct result.

## Verification model

Verification is evidence-driven and revision-aware.

Depending on the repository, DevAgent can run:

- baseline tests,
- targeted tests,
- broader unit/component tests,
- functional or integration checks,
- build commands,
- lint/type checks,
- `git diff --check`,
- final current-revision verification.

Each verification result records the command, exit code, duration, output, failure classification, phase, and workspace revision.

The final engineering report includes:

- an **implementation logic summary first**,
- requirement, task type, and risk,
- root cause / design gap,
- implementation decisions,
- exact changed Python functions/classes/methods when deterministically extractable,
- exact changed Python test case names when deterministically extractable,
- acceptance criteria and concrete evidence,
- verification matrix with phase/revision/test counts,
- failed-check stdout/stderr and failure classification,
- independent-review result,
- completeness assessment,
- known gaps / not-run checks,
- recommendations,
- source-control plan/status,
- developer review checklist.

A code modification invalidates prior successful verification for the old revision.

## Outcome contract

### `VERIFIED`

Used only when the current implementation has sufficient acceptance evidence, applicable verification passes, no known new regression remains, scope is acceptable, and independent review approves the final diff.

After the `VERIFIED` engineering report is emitted, DevAgent automatically attempts bounded commit/push to a new branch unless `--no-publish` was supplied.

### `PARTIALLY_VERIFIED`

Used when implementation evidence exists but meaningful verification cannot be completed, commonly because of environmental, hardware, VPN, credential, or external-service limitations.

`PARTIALLY_VERIFIED` is never committed/pushed by the automatic publisher.

### `BLOCKED`

Used when DevAgent cannot safely understand, implement, or verify the task.

`BLOCKED` is never committed/pushed by the automatic publisher.

DevAgent is intentionally conservative: a truthful `BLOCKED` is better than a false `VERIFIED`.

## Safety boundary

DevAgent uses defense-in-depth controls around repository modification, command execution, and branch publishing.

- Clean Git repositories use a retained detached worktree by default.
- Pre-existing dirty developer files are protected.
- Existing files are backed up before their first modification.
- Workspace paths are confined and checked against symlink escape.
- Secret-like paths such as `.env*`, private keys, SSH/AWS credentials, and generated dependency trees are excluded from automatic reads.
- Engineering commands run as argv without a shell.
- Credential environment variables are scrubbed from verification subprocesses where appropriate.
- The model-facing command policy continues to block Git write operations.
- Automatic publication is a separate deterministic post-report step and requires a `VERIFIED` result.
- Publication requires the isolated worktree, refuses protected/existing target branches, and stages only reviewed changed paths.
- `--no-publish` disables the publication step.
- DevAgent never creates PRs, merges, rebases, force-pushes, or deploys.

DevAgent is **not** an operating-system sandbox. Always review the engineering report and pushed branch before integrating changes.

## Repository intelligence

DevAgent discovers repository structure from source files and repository-native evidence such as:

- `README` / contribution documentation,
- `pyproject.toml`, `requirements.txt`, `pytest.ini`,
- `package.json`, lockfiles, TypeScript configuration,
- `Cargo.toml`, `go.mod`,
- Maven and Gradle files,
- CMake / Make / Meson,
- `.sln` / `.csproj`,
- Docker and Compose files,
- GitHub Actions, Jenkins, GitLab CI, and Azure Pipelines.

Current discovery supports Python, JavaScript/TypeScript, React-style projects, Go, Rust, Java, C/C++, .NET, Make-based projects, and multi-component repositories.

CI is treated as executable documentation when it provides safe, bounded command evidence.

## Provider architecture

The engineering workflow is model-independent. Providers implement a common request contract.

Currently supported:

| Provider | Configuration |
| --- | --- |
| OpenAI | `--provider openai` |
| Anthropic / Claude | `--provider anthropic` |
| xAI / Grok | `--provider xai` |
| Local / compatible | `--provider compatible --base-url ...` |

The goal is to let developers choose the model that fits their accuracy, privacy, latency, and cost requirements without changing the core engineering workflow.

## Example engineering report and publication receipt

```text
DEVAGENT ENGINEERING REVIEW REPORT

STATUS
VERIFIED

IMPLEMENTATION LOGIC SUMMARY
Requirement: Add multiplication support while preserving divide behavior.
Problem / design gap: The calculator has no multiplication API.
Chosen implementation logic:
- Add multiply(a, b) as a separate function.
- Preserve divide(a, b) behavior.
- Add positive, negative, and zero multiplication tests.
Code-level effect:
- ADDED function multiply at calculator.py:6
Test / verification logic:
- ADDED test test_multiply_positive at test_calculator.py:10
- ADDED test test_multiply_negative at test_calculator.py:14
- ADDED test test_multiply_zero at test_calculator.py:18
- Final/current revision checks: python -m pytest -q; git diff --check
Preserved behavior / scope constraints:
- Existing divide behavior remains covered.
Why this result is considered sufficient / insufficient:
- Required acceptance evidence: 4/4
- Final/current verification: 2 passed, 0 failed
- Independent review: approved
- Outcome decision: VERIFIED

FUNCTIONS / CLASSES / SYMBOLS CHANGED
- ADDED | function | calculator.py:6 | multiply

TEST CASES / UNIT TESTS
- UNCHANGED | function | test_calculator.py:4 | test_divide
- ADDED | function | test_calculator.py:10 | test_multiply_positive
- ADDED | function | test_calculator.py:14 | test_multiply_negative
- ADDED | function | test_calculator.py:18 | test_multiply_zero

ACCEPTANCE CRITERIA + EVIDENCE
✓ AC-1 [REQUIRED] multiply(a, b) returns the product
✓ AC-2 [REQUIRED] negative multiplication works
✓ AC-3 [REQUIRED] multiplication by zero works
✓ AC-4 [REQUIRED] existing divide behavior remains covered

VERIFICATION MATRIX
✓ python -m pytest -q | phase=final | revision=1 | exit=0 | tests=5/5
✓ git diff --check | phase=final | revision=1 | exit=0

INDEPENDENT REVIEW
APPROVED

COMPLETENESS ASSESSMENT
Outcome: VERIFIED
Required acceptance criteria evidenced: 4/4
Independent review: APPROVED
COMPLETE FOR DEVELOPER REVIEW

SOURCE CONTROL
Remote: origin
Branch: devagent/<run-id>
Commit: NOT CREATED
Committed: NO
Pushed: NO
Pull request: NOT CREATED
Merge: NOT PERFORMED

Engineering report complete. Starting deterministic branch publication...
SOURCE CONTROL PUBLICATION RECEIPT
Status: PUSHED
Remote: origin
Branch: devagent/<run-id>
Commit: <sha>
Committed: YES
Pushed: YES
Pull request: NOT CREATED
Merge: NOT PERFORMED
```

The report appears before the commit/push. The receipt proves exactly what happened afterward.

## Local run data

DevAgent keeps run artifacts under the target repository's `.devagent/` state:

```text
.devagent/
├── runs/<run-id>/
│   ├── metadata.json
│   ├── backups/
│   ├── observations.jsonl
│   ├── verification.json
│   └── report.json
├── worktrees/<run-id>/
└── memory/
    ├── repository.json
    └── strategies.json
```

After publication completes, the machine-readable `report.json` records the requested remote/branch, exact commit SHA, commit status, push status, and any publication error.

Repository facts are tied to evidence fingerprints and can be invalidated when their source changes.

## Development

Run the test suite:

```bash
python -m compileall devagent
pytest -q
git diff --check
```

Install the current checkout in editable mode:

```bash
pip install -e ".[dev]"
```

Automated tests use a deterministic fake provider and do not consume cloud-model credits. Source-control publication tests use a local bare Git repository rather than a network remote.

## Project status

DevAgent is currently **alpha software**. The core evidence-driven workflow is functional, but real-provider behavior and repository coverage are still being hardened through disposable end-to-end engineering fixtures.

Near-term focus:

- real-provider contract hardening,
- stronger repository context retrieval,
- safer verification-capability discovery,
- worktree lifecycle improvements,
- more realistic multi-language evaluation fixtures,
- packaging and release quality.

The project intentionally prioritizes trustworthy outcomes over feature count.

## Contributing

Contributions are welcome.

Please read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. In particular, changes to DevAgent's safety, verification, reporting, or bounded publication behavior should include regression tests and must not give model-generated actions unrestricted Git publishing authority.

For bugs and feature requests, use the [GitHub issue tracker](https://github.com/tomha85/devagent/issues).

## Security

Please do not publish sensitive vulnerability details in a public issue.

See [SECURITY.md](SECURITY.md) for the current reporting process and security scope.

## License

DevAgent is open source under the [MIT License](LICENSE).

```text
Copyright (c) 2026 Tom Ha
```

The MIT license permits use, copying, modification, merging, publishing, distribution, sublicensing, and sale of copies, provided the copyright and permission notice are retained as required by the license.

## Author and original project

DevAgent was created by **Tom Ha**.

Original repository: **https://github.com/tomha85/devagent**

See [NOTICE](NOTICE) and [COPYRIGHT](COPYRIGHT) for project attribution.
