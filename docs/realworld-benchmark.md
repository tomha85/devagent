# Real-world benchmark

DevAgent 0.5 adds a pinned repository benchmark harness intended to measure autonomous engineering behavior against an oracle that is independent from the model and from DevAgent's own acceptance adjudication.

## Benchmark contract

A case must specify:

- a credential-free `https://github.com/OWNER/REPO` repository URL;
- an exact 40-character source commit SHA;
- one or more deterministic exact-text mutations that inject the benchmark defect;
- the engineering requirement given to DevAgent;
- an argv-form external oracle command;
- optional maximum changed-file and changed-line limits.

The runner clones only the pinned revision, verifies the resolved SHA, applies the mutation with exact occurrence checks, commits that mutation as the benchmark baseline, and runs the oracle **before** DevAgent. The mutated baseline is required to fail the oracle. DevAgent then runs in its normal isolated workflow. Finally, the same oracle is executed against DevAgent's retained working result.

A case passes only when all of the following are true:

```text
mutated baseline oracle fails
AND DevAgent evaluation contract passes
AND final external oracle passes
AND false_verified == false
```

If DevAgent returns `VERIFIED` but the external oracle fails, the benchmark records a **false VERIFIED** regardless of model confidence, report prose, or internal acceptance evidence.

## Catalog format

```json
{
  "schema_version": 1,
  "primary_invariant": "false_verified == 0",
  "cases": [
    {
      "id": "project-specific-bug",
      "category": "bug_fix",
      "repository_url": "https://github.com/OWNER/REPO",
      "revision": "0123456789abcdef0123456789abcdef01234567",
      "requirement": "Fix the injected bug and preserve existing behavior.",
      "mutations": [
        {
          "path": "src/module.py",
          "old": "exact original text",
          "new": "deterministic injected defect",
          "count": 1
        }
      ],
      "oracle_command": ["python", "-m", "pytest", "-q", "tests/test_module.py"],
      "max_files_changed": 3,
      "max_lines_changed": 100
    }
  ]
}
```

Do not use a moving branch name such as `main` as `revision`. Mutation drift is a hard setup failure rather than being silently adapted.

## Run

Configure the provider normally, then run:

```bash
devagent benchmark \
  --catalog /path/to/realworld-cases.json \
  --report .devagent/realworld-benchmark.json
```

Run only selected cases with repeated `--case CASE_ID` arguments.

The benchmark runner intentionally does not commit, push, create pull requests, merge, rebase, force-push, or deploy changes to benchmark source repositories.

## Credential boundary

Repository clone URLs cannot contain credentials. Oracle subprocesses receive a minimal environment with a sandboxed `HOME`; cloud API keys, package registry tokens, and `CARGO_HOME` are not inherited. Rustup toolchain location may be exposed separately so Rust compilers installed through rustup remain usable.

## What this does not claim

Passing a catalog proves only the cases and pinned revisions that were actually executed. It is not evidence that DevAgent can solve every issue in those upstream projects or every unseen repository. Catalog results should always report case count, pass rate, false-VERIFIED count, model/provider, and pinned revisions.
