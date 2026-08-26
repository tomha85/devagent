# Rockwell Production Qualification

This document records the production qualification boundary for DevAgent PLC's Rockwell Automation support.

## Trust boundary

DevAgent separates deterministic static proof from runtime verification:

- `STATICALLY_VERIFIED` means the exported L5X behavior is proven only within the explicitly modeled semantics.
- `PARTIALLY_VERIFIED` / `NOT_PROVEN` is required for unsupported, protected, ambiguous, unresolved-alias, multi-writer, or otherwise incomplete semantics.
- Simulator/HIL/controller `PASS` requires authenticated execution evidence bound to the exact project, generated FAT plan, qualified backend registry, release policy, and verification context.
- Human engineering approval is still required by production release policy.
- DevAgent does not infer physical wiring, process physics, safety integrity level, or vendor-runtime behavior that is absent from evidence.

## Production pipeline

1. Project validation
2. Canonical PLC IR
3. Logic semantics
4. Dependency graph
5. AI engineering review
6. Requirement ingestion
7. Requirement verification
8. Test generation
9. Test execution
10. Risk detection
11. Optimization review
12. Regression analysis
13. Recommendations
14. Evidence + FAT report
15. Release readiness

## Rockwell deterministic coverage baseline

The current production baseline includes:

- full-project Studio 5000 `.L5X` validation and provenance hashing
- controller/program tags, aliases, UDT members, tasks, scheduled programs, routines, main/fault routines
- RLL Boolean branch-path semantics with fail-closed complex-path handling
- bounded Structured Text normalization with explicit partial states for unsupported control structures
- AOI inventory, internal RLL/ST normalization, call/interface binding, and protected/unresolved fail-closed behavior
- JSR routine call graph and task/program execution structure
- typed Rockwell compare semantics for supported EQ/NE/LT/LE/GT/GE aliases and SINT/INT/DINT/LINT/REAL/LREAL domains
- canonical scope/case/`AliasFor` writer identity, including whole-tag/member storage overlap
- canonical multi-writer and OTL/OTU risk analysis
- deterministic requirement proof only when writer ownership and semantics are unambiguous
- generated FAT candidates that remain `NOT_RUN` until a qualified execution backend supplies evidence
- FactoryTalk Logix Echo execution-runner contract with signed runtime-project binding and qualified-backend enforcement
- signed release policy, backend registry, execution evidence, verification context, and human approval gates

FBD/SFC, protected logic, unsupported Structured Text control structures, unqualified motion behavior, and instructions outside the deterministic registry remain explicitly partial/not-proven rather than guessed.

## Pinned public Rockwell acceptance artifact

The public sample below is used as an external real-project acceptance target:

- Repository: `RockwellAutomation/ra-logix-cicd`
- Repository revision: `de14d4ac87d5295b2380cce441ed581d1930947f`
- Artifact: `1-production-files/L5Xs/ExampleForCICD_L85E.L5X`
- Git blob: `ea3814f7d3657de569539228042903dc9ea8a908`
- Upstream license: MIT, Copyright (c) 2024 Rockwell Automation

The external artifact is not vendored into this repository by this document. Qualification must record the exact downloaded bytes/project SHA-256 before comparing results.

## Merge qualification

Every Rockwell production change must preserve these gates:

- Python 3.10, 3.11, and 3.12 full test suite
- coverage gate
- wheel build/install
- CLI smoke tests
- dependency validation
- production sandbox qualification
- no unresolved material P1/P2 false-verification findings
- no claim of dynamic PASS without qualified execution evidence
