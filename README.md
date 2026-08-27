# DevAgent

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Status: Beta](https://img.shields.io/badge/status-beta-blue.svg)](#project-status)
[![Production CI](https://github.com/tomha85/devagent/actions/workflows/ci.yml/badge.svg)](https://github.com/tomha85/devagent/actions/workflows/ci.yml)
[![Sponsor DevAgent](https://img.shields.io/badge/Sponsor-DevAgent-EA4AAA?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/tomha85)

**A local, evidence-driven software engineering agent that turns a requirement into a tested, independently reviewed patch, prints a developer-grade engineering report, and only publishes a branch when the result is `VERIFIED`.**

> **From requirement to evidence-backed verified branch.**

> ❤️ **DevAgent is free during public beta.** If DevAgent saves you engineering time, consider [sponsoring continued development](https://github.com/sponsors/tomha85).

DevAgent runs against a developer's local repository. It discovers the application, gathers source evidence, compiles explicit acceptance criteria, plans a bounded change, creates backups, implements the minimum necessary patch, runs repository-supported verification, independently reviews the final diff, and decides `VERIFIED`, `PARTIALLY_VERIFIED`, or `BLOCKED` from evidence rather than model confidence.

For a normal verified run the deterministic harness prints the complete engineering report first, then commits reviewed paths and fast-forward pushes the developer's current non-protected branch. If the developer starts on `main`, `master`, or `trunk`, DevAgent creates a new safe branch instead. Runtime DevAgent never creates a pull request, merges, rebases, force-pushes, or deploys.

## Why DevAgent?

Many coding agents optimize first for code generation. DevAgent is built around a different question:

**Can this change be supported by repository evidence and verified on the current revision?**

Core principles:

- **Evidence before modification** — insufficient source evidence blocks implementation.
- **Explicit acceptance contracts** — required criteria are `SATISFIED`, `UNPROVEN`, or `CONTRADICTED`; passing tests do not blanket-prove unrelated requirements.
- **False-`VERIFIED` resistance** — unsupported required criteria, missing final verification, known regressions, failed review, or source-state violations prevent a trustworthy success result.
- **Minimal-change discipline** — prefer the smallest correct diff over broad speculative refactors.
- **Backups before edits** — existing files are backed up before first modification.
- **Local-first isolation** — clean repositories use retained external detached worktrees by default; dirty developer work is protected.
- **Repository-native verification** — manifests, package scripts, test layouts, build files, and bounded CI evidence determine verification capabilities.
- **Independent review** — the final diff is reviewed separately from implementation.
- **Developer-grade reporting** — implementation logic, symbols, tests, acceptance evidence, verification, failures, gaps, and source-control status are recorded before publication.
- **Bring your own model** — OpenAI, Anthropic/Claude, xAI/Grok, Google Gemini, and OpenAI-compatible endpoints use one engineering workflow.
- **Deterministic branch publication** — only a `VERIFIED` isolated run can be committed and normally fast-forward pushed; `--no-publish` keeps the result local.

## Install

From PyPI:

```bash
python -m pip install devagent-ai
```

For an isolated CLI installation:

```bash
pipx install devagent-ai
```

For development from source:

```bash
git clone https://github.com/tomha85/devagent.git
cd devagent
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
pytest -q
```

The PyPI distribution is `devagent-ai`; the Python package and CLI command are both `devagent`.

## Configure a provider

OpenAI:

```bash
devagent setup --provider openai --model YOUR_MODEL
export OPENAI_API_KEY=...
devagent doctor
devagent doctor --live
```

Anthropic / Claude:

```bash
devagent setup --provider anthropic --model YOUR_MODEL
export ANTHROPIC_API_KEY=...
devagent doctor
```

xAI / Grok:

```bash
devagent setup --provider xai --model YOUR_MODEL
export XAI_API_KEY=...
devagent doctor
```

Google Gemini:

```bash
devagent setup --provider gemini --model gemini-3.7-flash
export GEMINI_API_KEY=...
devagent doctor
```

Gemini uses Google's documented OpenAI-compatible Gemini endpoint so DevAgent can reuse the same bounded provider interface and deterministic local schema validation. `--provider google` is an alias for the same integration.

OpenAI-compatible local or private endpoint:

```bash
devagent setup \
  --provider compatible \
  --model local-model \
  --base-url http://127.0.0.1:11434/v1
```

DevAgent stores the provider configuration and the **name** of the API-key environment variable. It does not store the API key itself.

### Model routing by engineering role

DevAgent can use one default model or route different models to stable roles:

```text
investigator → understand repository/problem
planner      → produce bounded implementation/verification plan
implementer  → implement, diagnose, repair/replan
reviewer     → independently review final diff
```

The deterministic harness remains responsible for safety, tool execution, verification validity, acceptance adjudication, final status, reporting, and source-control publication regardless of which model handles a role.

#### Example: use multiple AI models in one DevAgent run

You do not have to use one AI model for every reasoning step. If you believe different models are better suited to different engineering roles, configure a default model plus any role-specific overrides. Roles that are not explicitly configured fall back to the default model.

```bash
# Keep credentials in environment variables; DevAgent does not store the keys.
export OPENAI_API_KEY=...
export GEMINI_API_KEY=...
export ANTHROPIC_API_KEY=...
export XAI_API_KEY=...

# Default/fallback model.
devagent setup --provider openai --model YOUR_OPENAI_MODEL

# Optional per-role models.
devagent setup --role investigator --provider gemini --model YOUR_GEMINI_MODEL
devagent setup --role planner --provider anthropic --model YOUR_CLAUDE_MODEL
devagent setup --role implementer --provider openai --model YOUR_OPENAI_MODEL
devagent setup --role reviewer --provider xai --model YOUR_GROK_MODEL

# Inspect routing and optionally probe every configured cloud model.
devagent models
devagent doctor --live

# Run normally; saved role routing is applied automatically.
cd my-repo
devagent "Fix the checkout race condition and add regression coverage."
```

For example, a user may choose a fast or lower-cost model for repository investigation, a different model for planning, a preferred coding model for implementation, and another provider for independent review. This can be useful for cost, latency, provider diversity, or model-strength preferences, but it does not guarantee a better result. DevAgent still requires the same repository evidence, acceptance gates, deterministic verification, and publication rules.

When using saved role routing, run the task without run-level `--provider`, `--model`, or `--base-url` overrides. Supplying those flags explicitly selects one provider/model for that run instead of the saved per-role routing.

## Run

From the application repository:

```bash
devagent "Fix websocket reconnect bug and add regression tests"
```

For a normal `VERIFIED` result:

```text
DISCOVER / UNDERSTAND
        ↓
COMPILE ACCEPTANCE CONTRACT
        ↓
BASELINE / PLAN / GATHER CONTEXT
        ↓
IMPLEMENT MINIMAL PATCH
        ↓
TARGETED + BROAD VERIFICATION
        ↓
DIAGNOSE / REPLAN if needed
        ↓
INDEPENDENT REVIEW
        ↓
FINAL CURRENT-REVISION VERIFICATION
        ↓
FULL ENGINEERING REPORT
        ↓
VERIFIED ONLY: COMMIT + FAST-FORWARD PUSH BRANCH
        ↓
STOP — NO PR, NO MERGE
```

Repeated prompts on a normal local development branch continue that same branch when local/remote history is safely compatible. A protected starting branch (`main`, `master`, `trunk`) causes a new branch such as:

```text
devagent/20260825T020000Z-ab12cd
```

Explicitly start a new branch:

```bash
devagent \
  --publish-branch feature/devagent-csv-export \
  "Add CSV export to reports"
```

Run without commit/push:

```bash
devagent --no-publish "Add CSV export to reports"
```

Long requirements can be read from any bounded UTF-8 text file path; the filename extension is unrestricted:

```bash
devagent --input requirements/customer-feature.md
devagent --input ./task
devagent --input ../specs/release.requirement
```

Binary data, invalid UTF-8, secret-like paths, and files above the input-size bound are refused.

## PLC engineer quick start

DevAgent also provides an **offline PLC engineering review, requirement-verification, regression-analysis, risk-review, and FAT-planning workflow** through `devagent plc`.

The PLC workflow is intended for controls/PLC engineers who want to review exported engineering artifacts before FAT, site travel, commissioning, or release. DevAgent does **not** connect to, upload to, download to, force, start, stop, or control a PLC, TIA Portal, Studio 5000, PLCSIM, Logix Echo, HIL bench, or production machine. Runtime FAT and commissioning execution remain owned by the PLC engineer.

### 1. Export the PLC project

**Rockwell Studio 5000:** export the full controller project as `.L5X`.

```text
Line1_Controller.L5X
```

**Siemens TIA Portal:** provide a TIA Openness/XML/generated-source file or export directory. Supported engineering inputs include `.scl`, `.db`, `.udt`, `.xml`, `.stl`, and `.awl`. Proprietary `.ap*` / `.zap*` project archives must be exported from TIA Portal first.

For Siemens, include the related OB/FB/FC logic, DB/UDT/type evidence, generated sources, and Openness XML used by the controller whenever available. A complete export gives DevAgent a stronger project-wide call/data/identity/support-boundary view than isolated snippets.

### 2. Add customer or machine requirements

`--requirements` is repeatable and accepts `.txt`, `.md`, `.csv`, `.json`, `.docx`, and `.pdf` when PDF support is installed.

Example:

```markdown
- Conveyor shall not run unless the main guard circuit is healthy.
- A safety fault shall prevent the motor run output.
- Reset shall not automatically restart the conveyor.
```

### 3. Run the engineering review

Rockwell:

```bash
devagent plc ./exports/Line1_Controller.L5X \
  --requirements ./requirements/line1.md \
  --ai \
  --output-dir ./plc-review/line1-current
```

Siemens:

```bash
devagent plc ./exports/TIA_Line1/ \
  --requirements ./requirements/line1.md \
  --ai \
  --output-dir ./plc-review/line1-current
```

`--ai` enables the evidence-constrained AI engineering-review layer. It is optional; the deterministic PLC analysis and FAT-planning path still runs without it.

### 4. Compare a PLC revision

Use `--baseline` with a previous export from the same vendor to identify changed logic, affected requirements, changed risks, and FAT tests that should be repeated.

```bash
devagent plc ./exports/new/TIA_Line1/ \
  --baseline ./exports/old/TIA_Line1/ \
  --requirements ./requirements/line1.md \
  --ai \
  --output-dir ./plc-review/line1-revision
```

The same pattern works for Rockwell using current and previous `.L5X` files.

### 5. Review the evidence package

A written run includes `fat_report.md` plus machine-readable evidence such as canonical IR, dependency graph, static verification, requirement verification, FAT tests/plans, risks, optimizations, regression impact, recommendations, evidence manifest, release readiness, and an exact run manifest.

The engineer should pay special attention to four kinds of results:

- **statically proven behavior** — supported semantics with the required identity, writer ownership, reachability, call binding, and source evidence;
- **`PARTIAL` / `OPAQUE` / `PROTECTED` support regions** — behavior that DevAgent intentionally refuses to overclaim from available exported source;
- **`FAT_REQUIRED` behavior** — timing, counting, hardware, external I/O/device, system-service, scheduling, unsupported-logic, or other runtime evidence that must be exercised by an engineer;
- **revision impact** — requirements, risks, logic areas, and FAT tests whose previous evidence may no longer be valid after a PLC change.

A passing static check does **not** replace FAT when the report requires runtime evidence.

Recommended workflow:

```text
EXPORT PLC PROJECT
        ↓
DEVAGENT PLC ENGINEERING REVIEW
        ↓
REQUIREMENT + LOGIC + RISK REVIEW
        ↓
GENERATED FAT PROCEDURES
        ↓
ENGINEER EXECUTES REQUIRED FAT EXTERNALLY
        ↓
CAPTURE / IMPORT TRUSTED EVIDENCE WHEN APPLICABLE
        ↓
ENGINEERING APPROVAL
        ↓
SITE COMMISSIONING / RELEASE PROCESS
```

For signed runtime-result, policy, trust-store, and approval workflows, and a complete explanation of Rockwell/Siemens evidence boundaries, see **[PLC Engineer Guide](docs/plc-engineer-guide.md)**.

## Practical examples

DevAgent is intended for real repository work, not only one-line code generation. Run it from the repository you want to change and describe the engineering outcome you need.

### 1. Fix a bug and prove the regression is covered

```bash
cd my-service
devagent "Fix the websocket reconnect bug that duplicates subscriptions after a network drop. Add a regression test and keep the public API unchanged."
```

**Benefit:** DevAgent first discovers the relevant implementation and tests, turns the request into explicit acceptance criteria, makes a bounded patch, runs repository-supported verification, independently reviews the final diff, and only reports `VERIFIED` when the required evidence supports it.

### 2. Add a feature on a dedicated branch

```bash
cd my-app
devagent \
  --publish-branch feature/csv-export \
  "Add CSV export for filtered reports. Preserve the existing JSON export behavior and add tests."
```

**Benefit:** a verified change can be committed and pushed to the requested feature branch while DevAgent stops before PR creation or merge, leaving integration control with the developer or repository owner.

### 3. Give DevAgent a longer product or customer requirement

```bash
cd my-repo
devagent --input requirements/customer-billing-retry.md
```

The input can be any bounded UTF-8 text file; it does not need a special extension or DevAgent-specific format.

**Benefit:** long requirements stay in a reviewable file instead of being compressed into a short prompt, while DevAgent still derives bounded implementation and verification work from repository evidence.

### 4. Perform a refactor that includes rename/move/delete operations

```bash
cd my-repo
devagent "Rename LegacyOrderService to OrderService, move it into the services package, update all references, remove the obsolete module, and preserve behavior."
```

**Benefit:** structural changes go through backup-first workspace operations, path/scope checks, repository verification, and final-diff review instead of uncontrolled file manipulation.

### 5. Change a database schema with forward/rollback verification

```bash
cd my-python-service
devagent "Add a nullable status column to the SQLite orders table, provide a forward and rollback migration, update the data-access layer, and verify both migration directions."
```

**Benefit:** migration work can be treated as high-risk engineering work with explicit acceptance evidence instead of assuming that a generated migration is correct because it looks plausible. The current qualified production fixture covers SQLite forward/rollback migration behavior; broader PostgreSQL/MySQL coverage remains an external-validation area.

### 6. Work in Java or .NET repositories

```bash
cd my-java-service
devagent "Add validation for duplicate customer IDs in this Maven service and add the appropriate JUnit regression test."
```

```bash
cd my-dotnet-service
devagent "Fix the null-handling bug in the order import path and verify the .NET project still builds successfully."
```

**Benefit:** DevAgent discovers repository-native Maven/Gradle and .NET project evidence instead of forcing every repository through a Python-centric workflow.

### 7. Keep all changes local for inspection

```bash
cd my-repo
devagent --no-publish "Refactor retry handling to remove duplicate logic and keep behavior unchanged."
```

**Benefit:** you still get implementation, verification, independent review, and the engineering report, but DevAgent does not commit or push the result.

### 8. Use the model/provider you prefer

```bash
# Configure once
devagent setup --provider anthropic --model YOUR_MODEL
export ANTHROPIC_API_KEY=...

# Then use the same DevAgent engineering workflow
devagent "Fix the failing checkout integration test without weakening the assertion."
```

You can similarly configure OpenAI, Gemini, Grok/xAI, or an OpenAI-compatible endpoint.

**Benefit:** the model supplies reasoning, while DevAgent keeps the same deterministic acceptance, safety, verification, reporting, and publication rules around it.

## What DevAgent adds around an AI coding model

| Common engineering risk | DevAgent behavior |
| --- | --- |
| The model says “done” without enough proof | Required acceptance criteria remain `UNPROVEN` or the run becomes `PARTIALLY_VERIFIED` / `BLOCKED` instead of falsely claiming success. |
| A patch touches unrelated code | Evidence gathering, explicit scope, minimal-change planning, and independent diff review constrain the change. |
| Existing developer work is damaged | Clean repositories use isolated worktrees by default; dirty tracked/untracked developer work is protected; files are backed up before first modification. |
| Tests passed before a later edit | Verification is revision-aware, so older successful evidence does not prove a newer tree. |
| A generated change breaks the build or tests | DevAgent runs repository-supported targeted/broad checks and can diagnose/replan before final verification. |
| An agent pushes directly to a protected primary branch | Starting from `main`, `master`, or `trunk` causes DevAgent to work on a safe branch; runtime DevAgent does not merge or deploy. |
| You are locked to one model vendor | OpenAI, Claude, Gemini, Grok/xAI, and compatible endpoints can use the same engineering harness. |
| It is hard to audit what the agent actually did | DevAgent emits an engineering report with decisions, changed symbols, tests, acceptance evidence, verification, failures, gaps, and source-control status. |

The goal is not to replace developer judgment. The goal is to make autonomous engineering work **bounded, reviewable, reproducible, and harder to falsely declare complete**.

Useful commands:

```bash
devagent --help
devagent --version
devagent setup --help
devagent doctor
devagent models
devagent status
devagent plc --help
devagent benchmark --help
```

### Pinned real-world benchmark

DevAgent includes an opt-in benchmark runner for pinned GitHub repositories. A benchmark case injects a deterministic defect into an exact commit and uses an **external oracle** before and after DevAgent. This avoids treating DevAgent's own report as the benchmark oracle.

```bash
devagent benchmark \
  --catalog /path/to/realworld-cases.json \
  --report .devagent/realworld-benchmark.json
```

A `VERIFIED` result with a failing external oracle is explicitly counted as a **false VERIFIED**. See [docs/realworld-benchmark.md](docs/realworld-benchmark.md) for the catalog contract and safety boundary.

## Outcome contract

### `VERIFIED`

Used only when required acceptance criteria are satisfied by admissible evidence, applicable final verification passes on the current revision, no known new regression remains, scope is acceptable, and independent review approves the final diff.

Only `VERIFIED` is eligible for automatic commit/push.

### `PARTIALLY_VERIFIED`

Used when meaningful implementation evidence exists but complete proof cannot be obtained, for example because of unavailable hardware, VPNs, credentials, external services, or environment limitations.

It is never auto-published.

### `BLOCKED`

Used when DevAgent cannot safely understand, implement, or verify the request. It is never auto-published.

A truthful conservative result is preferable to a false `VERIFIED`.

## Safety boundary

DevAgent uses defense-in-depth controls around repository modification, command execution, and publishing:

- external isolated worktrees for clean repositories by default;
- dirty tracked and real untracked developer files are protected;
- backups before first file modification;
- workspace confinement and symlink-escape checks;
- secret-like path exclusions;
- engineering commands executed as argv without a general shell;
- credential environment scrubbing for verification where appropriate;
- model-facing command policy blocks Git write operations;
- publication is a separate deterministic post-report step and disables repository-controlled Git hooks for commit/push;
- only reviewed changed paths are staged;
- remote branch state is captured and rechecked to block publication races;
- protected targets are refused and force push is never used;
- no runtime PR, merge, rebase, force-push, or deployment automation.

On Linux, DevAgent can execute engineering commands inside a bubblewrap-based operating-system sandbox. Production qualification exercises required sandbox mode with network access denied. Required mode fails closed when isolation cannot be established rather than silently falling back. Review the report and pushed branch before integrating customer or production code: sandboxing reduces execution risk, but it does not make arbitrary generated changes universally safe.

## Repository intelligence and verification

Discovery understands common evidence from:

- Python: `pyproject.toml`, requirements, pytest/unittest conventions;
- JavaScript/TypeScript: `package.json`, scripts, TypeScript configuration, React/Vite/Next-style repositories;
- Go: `go.mod`;
- Rust: `Cargo.toml`;
- Java: Maven/Gradle;
- C/C++: CMake/Make/Meson;
- .NET: solution/project files;
- CI: GitHub Actions, Jenkins, GitLab CI, Azure Pipelines;
- multi-component repositories and repository documentation.

Verification can include baseline tests, targeted tests, component/broad checks, integration/e2e commands, builds, lint/type checks, and `git diff --check`. Every verification result records phase and revision so an edit invalidates success from an older tree.

## Production qualification

DevAgent 0.8.0 uses cumulative production qualification rather than replacing older evidence with a smaller new suite.

- **v4 — 70 required cases** covering end-to-end engineering behavior, acceptance truthfulness, task/risk scope, provider contracts and parity, model routing, worktree and Git publication safety, CLI input, review/repair loops, report/evaluation integrity, release integrity, large-repository behavior, structural refactors, Java/.NET discovery and execution, SQLite migration forward/rollback, and real repository-native stacks.
- **v5 — 9 required autonomy cases** covering bounded parallel coordination, dirty-source refusal, real isolated parallel DevAgent runs, bounded/relevant skills and provider injection, automation overlap claim/recovery, and provider-benchmark deduplication, live structured-contract behavior, and secret redaction.

The v0.8 merge commit on `main` passed both catalogs in required Linux sandbox mode:

```text
v4: 70/70 passed
v5:  9/9 passed
combined: 79/79 passed
```

The qualification environment exercises real local toolchains for:

```text
Python / pytest
Node + TypeScript repository discovery
Go
Rust / Cargo
C++ / Make
Java / Maven
.NET build
SQLite migration forward + rollback
```

Run the same release qualification catalogs locally on a machine with the required toolchains:

```bash
DEVAGENT_SANDBOX=required DEVAGENT_NETWORK=deny \
python -m devagent.qualification \
  --catalog evaluation/benchmark_v4.json \
  --report .devagent/production-qualification-v4.json

DEVAGENT_SANDBOX=required DEVAGENT_NETWORK=deny \
python -m devagent.qualification \
  --catalog evaluation/benchmark_v5.json \
  --report .devagent/production-qualification-v5.json
```

Production CI also runs Python 3.10/3.11/3.12, a clean wheel build/install, real bubblewrap sandbox smoke, and both qualification catalogs. Qualification JSON is retained as CI evidence.

**100% qualified means 100% of these explicit catalogs passed on that revision and environment.** It does not mean mathematical correctness for every unseen repository, environment, model response, language, provider, or engineering task, and it is not a claim that DevAgent is universally superior to every hosted coding platform.

See [docs/production-readiness.md](docs/production-readiness.md) for the project's earlier readiness assessment and its explicit limitations.

## Provider architecture

Currently supported:

| Provider | Configuration |
| --- | --- |
| OpenAI | `--provider openai` |
| Anthropic / Claude | `--provider anthropic` or `--provider claude` |
| xAI / Grok | `--provider xai` or `--provider grok` |
| Google Gemini | `--provider gemini` or `--provider google` |
| Local / OpenAI-compatible | `--provider compatible --base-url ...` |

Provider choice affects reasoning quality, cost, latency, and privacy characteristics. It does not change DevAgent's deterministic acceptance, safety, verification, reporting, and publication rules. `devagent models` labels deterministic adapter status as `CONTRACT-QUALIFIED` or `SUPPORTED`; `devagent doctor --live` is the explicit real API/model structured-output readiness probe and consumes provider usage.

## Engineering report

The report is emitted **before** source-control publication and includes:

- implementation logic summary first;
- requirement, task type, and risk;
- root cause or design gap;
- implementation decisions;
- deterministically extractable changed symbols and test cases;
- acceptance criteria with status, evidence, and reasons;
- verification matrix with command, phase, revision, exit code, duration, and test counts;
- failed-check output and failure classification;
- independent-review result;
- completeness and known gaps;
- recommendations;
- source-control plan/status and developer review checklist.

After a successful verified publication, a separate receipt records remote, branch, exact commit SHA, committed/pushed status, and confirms no PR or merge was performed.

## Local run data

Run artifacts are stored under the target repository's `.devagent/` state, including metadata, per-run backups, observations, verification records, `report.json`, retained worktrees, and evidence-backed repository/strategy memory.

Repository facts carry source fingerprints so stale evidence can be invalidated when source files change.

## Development

```bash
python -m compileall -q devagent
pytest -q
git diff --check
python -m devagent.qualification
```

Automated provider tests normally use deterministic or mocked clients and do not consume cloud-model credits. Source-control tests use disposable local Git repositories. Production qualification deliberately executes its real local toolchain fixtures.

## Project status

DevAgent 0.8.6 is **beta software with a verified core release baseline**. The exact v0.8 merge revision on `main` passed Production CI across Python 3.10/3.11/3.12, clean wheel installation, real Linux bubblewrap sandbox execution, production qualification v4 (**70/70**), and autonomy qualification v5 (**9/9**), for **79/79 cumulative required qualification cases**.

The current core includes evidence-backed `VERIFIED` / `PARTIALLY_VERIFIED` / `BLOCKED` outcomes, backup-first editing, isolated worktrees, bounded structural file operations, repository-native verification, independent review, safe branch publication, provider/model choice, Java and .NET engineering discovery/execution, SQLite migration forward/rollback verification, large-monorepo deep-manifest discovery, bounded parallel agents, repository-local skills, foreground automations, Linux OS sandboxing, bounded browser/local-UI verification, and real-provider structured-contract benchmarking.

Current `main` also includes vendor-dispatched PLC engineering review for **Rockwell Studio 5000 full-project `.L5X`** and **Siemens TIA Portal exported engineering artifacts**, with separate production qualification workflows and fail-closed evidence boundaries. The Siemens qualification is cumulative through V9 and covers the bounded supported SCL/LAD/FBD, call/interface, state/interlock/recovery, canonical identity/type, support-accounting, malformed-input, revision, and large-project fixture surfaces. Unsupported, protected, ambiguous, or runtime-dependent behavior is withheld from static proof and routed to explicit limitations and engineer-executed FAT rather than silently reported as verified.

These results are **bounded engineering claims**, not universal-correctness or market-superiority claims. They are tied to explicit qualification cases, pinned revisions, deterministic fixtures/external oracles where applicable, and the environments actually exercised by CI.

Remaining work is primarily **breadth and external validation**, not missing core architecture: a larger public corpus of pinned upstream repositories and tasks; broader browser/UI coverage across dynamic applications and multiple browser environments; a wider Java/Gradle, .NET test-framework, and PostgreSQL/MySQL migration matrix beyond the current qualified fixtures; larger and more diverse monorepo stress cases beyond the current >12,000-file deep-manifest case; more real-world multi-agent workload studies; continuous paid real-provider benchmarking across a broader set of model/provider combinations; and additional real license-safe Siemens/Rockwell customer export qualification beyond deterministic repository fixtures. GitHub branch protection/rulesets are external repository settings and must be configured separately; DevAgent does not claim to configure them itself.

The project intentionally prioritizes trustworthy outcomes, reproducible evidence, and safe engineering behavior over feature count or unsupported "best agent" claims.

## ❤️ Support DevAgent

DevAgent is currently free to use during public beta.

If DevAgent saves you engineering time or helps you deliver safer, better-verified software or PLC engineering work, you can support continued development through [GitHub Sponsors](https://github.com/sponsors/tomha85).

Your sponsorship helps fund:

- new engineering and PLC capabilities;
- Siemens and Rockwell verification;
- additional AI provider support;
- regression and production qualification;
- documentation and examples;
- continued free public releases.

[![Sponsor DevAgent](https://img.shields.io/badge/Sponsor-DevAgent-EA4AAA?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/tomha85)

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Changes to safety, verification, reporting, acceptance semantics, providers, or bounded publication behavior should include regression evidence and must not give model-generated actions unrestricted Git publishing authority.

## Security

Please do not publish sensitive vulnerability details in a public issue. See [SECURITY.md](SECURITY.md) for the reporting process and security scope.

## License and attribution

DevAgent is open source under the [MIT License](LICENSE).

```text
Copyright (c) 2026 Tom Ha
```

DevAgent was created by **Tom Ha**. Original repository: **https://github.com/tomha85/devagent**. See [NOTICE](NOTICE) and [COPYRIGHT](COPYRIGHT) for project attribution.
