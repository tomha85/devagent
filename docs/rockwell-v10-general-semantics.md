# Rockwell V10 — General PLC Semantic Coverage

## Product objective

Rockwell examples and public sample projects are qualification specimens only. DevAgent must not contain project-specific tag names, routine names, controller names, or sample-specific proof shortcuts.

The product contract is:

> Import a valid customer PLC project, discover its structure and logic, apply deterministic semantics where supported, identify everything that remains partial or not proven, generate traceable FAT candidates, and never silently promote unsupported behavior to VERIFIED.

## V10 proof levels

V10 separates a local instruction effect from a final retained/scan state.

- `STATICALLY_VERIFIED`: deterministic theorem proves the requirement under the supported model.
- `ACTION_EFFECT_PROVEN`: a bounded retentive action such as OTL/OTU is proven to execute and write its fixed value under the stated Boolean path, but retained/final scan state is still not proven.
- `TRACEABLE_NOT_PROVEN`: implementation evidence is traceable but the bounded theorem cannot prove the behavior.
- `DYNAMICALLY_VERIFIED`: linked tests passed using authenticated evidence from a qualified simulator/HIL/controller backend.
- `CONFLICT`: deterministic or qualified execution evidence conflicts with the requirement.

`ACTION_EFFECT_PROVEN` is intentionally not accepted by release readiness as full requirement verification. It becomes dynamic proof only when linked qualified-backend execution evidence passes.

## Initial semantic capability registry

| Instruction | Family | Bounded semantic capability | Path-only final state |
| --- | --- | --- | --- |
| XIC | Boolean condition | fixed-tag TRUE condition | n/a |
| XIO | Boolean condition | fixed-tag FALSE condition | n/a |
| OTE | Boolean output | continuous Boolean writer | yes, subject to existing writer/identity guards |
| OTL | Retentive output | local SET action | no |
| OTU | Retentive output | local RESET action | no |

The registry is the start of a capability-oriented model. Future additions must extend instruction/language semantics generically rather than special-case a qualification project.

## Required invariants

1. No project-specific controller, routine, rung, or tag names in production semantics.
2. Unsupported/protected/ambiguous behavior remains `PARTIAL` or `NOT_PROVEN`.
3. AI findings never promote deterministic or execution proof status.
4. Local OTL/OTU action proof never implies retained/final state proof.
5. Ambiguous tag scope or unresolved alias identity blocks action-effect promotion.
6. Generated FAT linkage must preserve the exact output, source logic, and Boolean preconditions.
7. Qualified execution may promote a linked action-effect requirement to `DYNAMICALLY_VERIFIED`; FAIL becomes `CONFLICT`.
8. Release readiness does not accept `ACTION_EFFECT_PROVEN` as a completed static requirement gate.

## Qualification strategy

V10 qualification should grow across diverse fixtures and real project specimens:

- discrete machine / simple RLL
- retentive OTL/OTU state
- branch-heavy RLL
- timer/counter-heavy logic
- assignment/MOV/COP/CPS logic
- ST-heavy logic
- AOI-heavy logic
- aliases/UDTs and overlapping storage
- multiple tasks/programs/routine entry paths
- motion/robotics
- intentionally broken projects

The samples `Kinematics_Delta_5_Axis`, `ExampleForCICD`, and similar projects are used only to expose gaps and regressions. Passing any one sample is not a product capability claim.

## Next semantic slices

1. Reuse the capability registry inside RLL normalization to remove duplicated instruction-family declarations.
2. Deterministic scan/writer ordering for final-state proof.
3. Broader branch/output-path semantics.
4. Core Structured Text expression, assignment, IF/ELSIF/ELSE, and bounded state-transition semantics.
5. Timer/counter and assignment families.
6. Motion instruction capability profiles with explicit partial boundaries.
7. Per-project semantic coverage report by language/instruction family.
8. Diverse real-project qualification gate in CI.
