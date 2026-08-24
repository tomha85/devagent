# DevAgent Production Evaluation v1

DevAgent's production evaluation is designed to measure **truthfulness and engineering correctness**, not just whether a model produced a patch.

The benchmark contract treats a false `VERIFIED` as the highest-severity evaluation failure.

## Core metrics

Every evaluated engineering run can capture:

- expected vs. actual outcome (`VERIFIED`, `PARTIALLY_VERIFIED`, `BLOCKED`)
- acceptance-criteria evidence coverage
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

`devagent.evaluation` provides:

- `evaluate(...)` — run DevAgent and capture deterministic metrics
- `EvaluationExpectation` — declare the expected outcome and safety/scope budgets
- `score_evaluation(...)` — compare evidence against the expectation
- `evaluate_case(...)` — run and score one disposable engineering case
- `aggregate_results(...)` — aggregate a benchmark suite
- `write_suite_report(...)` — emit versioned JSON suitable for CI artifacts

## Primary release invariant

```text
false_verified == 0
```

A benchmark suite may contain legitimate `BLOCKED` or `PARTIALLY_VERIFIED` cases. Those are not failures when the scenario expectation requires a conservative outcome.

## Seed benchmark matrix

`benchmark_v1.json` maps the first production invariants to executable pytest coverage already in the repository. The normal Production CI workflow runs these tests on Python 3.10, 3.11, and 3.12.

Run locally:

```bash
python -m compileall -q devagent
pytest -q
git diff --check
```

Run only the evaluation contract tests:

```bash
pytest -q tests/test_evaluation_harness.py tests/test_e2e_fake_provider.py
```

## Real-provider evaluation

The deterministic test suite uses `ScriptedFakeProvider` and consumes no paid API credits. Real-provider smoke evaluations should be run deliberately against disposable repositories and should not be required on every pull request.

Example:

```python
from pathlib import Path

from devagent.evaluation import EvaluationExpectation, evaluate_case
from devagent.models import Outcome

result, scored = evaluate_case(
    "fastapi-bug-001",
    "bug_fix",
    Path("/tmp/disposable-repo"),
    "Fix the failing endpoint and add a regression test.",
    provider,
    expectation=EvaluationExpectation(
        expected_outcomes=(Outcome.VERIFIED,),
        max_files_changed=4,
        max_lines_changed=160,
    ),
)

assert scored.passed
assert not scored.false_verified
```

## Expansion target

The seed matrix is intentionally focused on high-value invariants already supported by DevAgent. The next benchmark expansion should add disposable real engineering fixtures across Python/FastAPI, Node/TypeScript, React, Go, Rust, C/C++, Java, and monorepos, including environment failures, pre-existing failures, ambiguous requirements, and adversarial repositories.
