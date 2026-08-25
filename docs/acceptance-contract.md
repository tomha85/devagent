# Acceptance Contract v1

DevAgent treats acceptance criteria as explicit engineering claims rather than a count of non-empty evidence strings.

Each criterion records:

- `source`: `USER`, `REPOSITORY`, `TASK_POLICY`, or `QUALITY_GATE`;
- `required`: whether it gates `VERIFIED`;
- `status`: `UNPROVEN`, `SATISFIED`, or `CONTRADICTED`;
- criterion-specific evidence;
- a reason describing why the criterion is or is not proven;
- an optional exact repository verification command for repository-derived checks.

`VERIFIED` requires every required criterion to be `SATISFIED` on the final revision. A passing test command is not blanket evidence for a user requirement. User criteria require semantic linkage to changed code/tests or an exact quoted contract, plus final verification when tests are required. Preservation claims additionally require matching regression-test inventory evidence.

Structured requirement files may provide bullet items under `Requirements`, `Required Changes`, or `Acceptance Criteria`; those items are preserved as individual `USER` criteria. DevAgent supplements them with task policies and trusted repository checks rather than replacing them.

## Task policies

`REFACTOR` requires tests and behavior-preservation/regression evidence. `MIGRATION` is high risk, requires tests, and adds compatibility, migration-strategy, and representative-existing-state criteria. Existing high-risk behavior still prevents full verification when no broad/static repository verification capability is available.

The independent reviewer can supplement evidence but cannot override a deterministic contradiction or turn unrelated passing tests into proof of a user criterion.
