# DevAgent Production Evaluation

DevAgent's production evaluation measures **truthfulness and engineering correctness**, not merely whether a model produced a patch.

The benchmark contract treats a false `VERIFIED` as the highest-severity evaluation failure.

## Core metrics

Every evaluated engineering run can capture:

- expected vs. actual outcome (`VERIFIED`, `PARTIALLY_VERIFIED`, `BLOCKED`)
- required acceptance-criteria satisfaction coverage (explicit status, not non-empty evidence)
- false `VERIFIED`
- unexpected `BLOCKED`
- known new regressions
- changed-file and changed-line scope
- independent-review approval
- final-verification completion
- source-repository immutability
- model calls
- deterministic tool calls
- runtime

`devagent.evaluation` provides `evaluate(...)`, `EvaluationExpectation`, `score_evaluation(...)`, `evaluate_case(...)`, `aggregate_results(...)`, and `write_suite_report(...)` for deterministic evaluation and machine-readable reports.

## Primary release invariant

```text
false_verified == 0
```

A suite may contain legitimate `BLOCKED` or `PARTIALLY_VERIFIED` cases. Those are correct when the evidence contract does not support `VERIFIED`.

## Qualification catalogs

`benchmark_v1.json` preserves the original seed benchmark. `benchmark_v2.json` is the 40-case deterministic functional qualification catalog introduced for the v0.4 development line. `benchmark_v3.json` is the production qualification catalog used for the 0.4.0 release gate.

Production qualification v3 contains 52 required cases covering:

- end-to-end engineering behavior
- false-`VERIFIED` truthfulness and acceptance contracts
- task classification and scope
- provider contracts and provider parity
- model-role routing
- worktree and source-control safety
- CLI requirement input
- review and repair/replan loops
- report and evaluation integrity
- actual Python/pytest, Node/TypeScript, Go, Rust/Cargo, and C++/Make toolchain execution
- package version, release targeting, exact-tag builds, and PyPI Trusted Publishing integrity

The normal suite remains fast: real-toolchain fixtures skip unless run through the production qualification runner. `devagent.qualification` explicitly enables those fixtures. Missing required toolchains or failing discovered commands therefore fail production qualification rather than being counted as proof.

Run the normal repository checks:

```bash
python -m compileall -q devagent
pytest -q
git diff --check
```

Run production qualification:

```bash
python -m devagent.qualification \
  --catalog evaluation/benchmark_v3.json \
  --report .devagent/production-qualification-v3.json
```

`benchmark_v3.json` is also the default catalog for `python -m devagent.qualification` in DevAgent 0.4.0.

A green qualification report means **100% of the explicitly cataloged production functionality passed**. It is not a claim that every possible repository, model response, environment, language, browser workflow, or unseen engineering task is universally correct.

## CI release gate

Production CI runs the complete Python suite on Python 3.10, 3.11, and 3.12, builds and installs the wheel in a clean environment, and executes production qualification v3 on a runner that must provide the supported qualification toolchains. The machine-readable qualification JSON is uploaded as CI evidence.

GitHub release automation runs only after successful Production CI on `main`, checks out the exact CI-tested SHA, validates package/version consistency, and creates the version tag/release at that exact revision. PyPI publication builds the exact release tag and uses Trusted Publishing.

## Real-provider evaluation

Most deterministic provider contract tests mock network clients so CI consumes no paid API credits. They validate request shape, provider routing, local schema validation, repair bounds, and credential handling. Real paid-provider smoke tests remain deliberate release/engineering exercises because provider availability, quotas, and model behavior are external variables.

Google Gemini support uses Google's documented OpenAI-compatible endpoint so DevAgent can preserve one provider interface while retaining its deterministic local schema validation. Provider-specific advanced features outside DevAgent's current engineering contract remain outside this qualification claim.

## Expansion target

Production v3 substantially broadens executable coverage, but it is not the end state. Further qualification should add larger disposable FastAPI and React applications, database migrations, Java/.NET builds, monorepos, browser/UI runtime verification, adversarial repositories, external-service failure cases, and broader paid real-provider matrices.
