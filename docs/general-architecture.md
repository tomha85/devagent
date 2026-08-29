# DevAgent — General Architecture

DevAgent has one evidence-driven core with **three intentionally different product workflows**:

1. **Software Engineering** — repository understanding, bounded code changes, verification, review, and safe branch publication.
2. **DevAgent PLC** — offline/pre-site PLC engineering review and FAT authority.
3. **DevAgent Live** — onsite, read-only commissioning assistance that consumes engineering context and trusted OPC UA runtime evidence.

The three workflows share evidence, trust, reporting, provider, and fail-closed principles, but they do **not** perform the same work and they do not share control authority.

## General architecture

```text
                                      DevAgent Core
                                           │
                 ┌─────────────────────────┼─────────────────────────┐
                 │                         │                         │
        Software Engineering         DevAgent PLC              DevAgent Live
          Repository workflow       Offline / pre-site        Onsite commissioning
                 │                  engineering authority      READ ONLY consumer
                 │                         │                         │
        Local / GitHub Repo      PLC engineering export             │
                 │                         │                         │
        Understand / Plan       ┌──────────┼──────────┐              │
                 │              │          │          │              │
          Modify Code        Siemens    Rockwell   Schneider         │
                 │           TIA        Studio     Control           │
        Build / Test /        exports    5000/L5X   Expert/XEF       │
            Review              │          │          │              │
                 │              └──────────┼──────────┘              │
        Engineering Report               │                          │
                 │               Canonical PLC model ───────────────┤
        Commit / Push Branch             │                          │
                 │               Analyze / Verify                   │
      Developer / Repo Integration       │                          │
                                  Requirements / Risks               │
                                  Regression / Evidence              │
                                  FAT Plan / FAT Report              │
                                  Release Readiness                  │
                                                                     │
                                                        OPC UA endpoint(s)
                                                                     │
                                                     Connect / Browse / Map
                                                                     │
                                                   Trust / Freshness Gate
                                                                     │
                                              Engineering ↔ Runtime Join
                                                                     │
                                          Deterministic Commissioning Diagnosis
                                                                     │
                                   Recursive / Stateful / Historical / Advanced
                                                                     │
                                                Optional AI Explanation / Q&A
```

The important boundary is:

```text
DEVAGENT PLC  = engineering authority
DEVAGENT LIVE = commissioning consumer
```

DevAgent Live may reuse the stable PLC import/canonical engineering model **read-only**. Live must not modify Siemens, Rockwell, Schneider, FAT, regression, theorem, or release-readiness behavior simply to add an onsite feature. Once engineering information crosses the Live adapter boundary, onsite diagnosis is owned by `devagent/live/**`.

## 1. Software Engineering

DevAgent works directly with a software working repository. It can understand the repository, implement a bounded code change, run repository-native tests/builds, independently review the result, produce an engineering report, and publish a verified commit to a safe branch.

```text
Working Repo
    ↓
Discover / Understand
    ↓
Acceptance Contract
    ↓
Plan / Implement
    ↓
Build / Test / Review
    ↓
Engineering Report
    ↓
VERIFIED only: Commit / Push Safe Branch
    ↓
Developer / Repository Integration
```

If a software run starts on `main`, `master`, or `trunk`, DevAgent creates a safe working branch. Runtime DevAgent does not create or merge pull requests, rebase, force-push, or deploy.

## 2. DevAgent PLC — offline engineering authority

DevAgent PLC works from exported PLC engineering artifacts rather than editing or controlling the live PLC. Siemens, Rockwell, and Schneider each have a vendor-specific import path, but all feed the canonical PLC engineering/review workflow.

Primary responsibilities include:

- AI Engineering Review;
- Requirement Verification;
- Test Generation and FAT planning;
- Risk Detection;
- Optimization recommendations;
- Regression Analysis;
- Evidence and provenance;
- FAT Report;
- Release Readiness.

Simplified flow:

```text
PLC Engineering Export
        ↓
Vendor Import
        ↓
Canonical PLC Engineering Model
        ↓
Logic / Dependency Analysis
        ↓
Requirement / Risk / Regression Review
        ↓
FAT Procedures + Evidence
        ↓
Engineering Report + Release Readiness
```

DevAgent PLC is a **pre-site/offline engineering workflow**. It does not claim simulator, HIL, field wiring, process physics, or real-controller execution unless corresponding runtime evidence is supplied.

## 3. DevAgent Live — onsite commissioning branch

DevAgent Live is a separate onsite tool for commissioning engineers. Its purpose is not to regenerate the offline FAT/report workflow. Its job is to help an engineer understand the system, inspect trusted live state, diagnose why a machine condition is blocked or abnormal, trace modeled dependencies, and identify the next evidence-backed check.

Full commissioning mode uses both:

```text
PLC engineering project/export
            +
      OPC UA endpoint
```

Why both are needed:

- the engineering export provides logic, tags, dependencies, source provenance, interlocks/permissives, states, calls, and other modeled engineering context;
- OPC UA provides live values, quality, timestamps, namespace identity, and runtime observations;
- endpoint-only mode can observe what is exposed, but it usually cannot prove hidden PLC source logic or why an output is commanded.

### Live data flow

```text
PLC Engineering Export
        ↓
Existing stable PLC parser / canonical model
        ↓
LiveEngineeringContext
        │
        ├── tags / identities
        ├── output logic / dependencies
        ├── source locations
        ├── stateful / sequence facts when modeled
        ├── numeric / analog comparison context
        ├── AOI / FB context
        ├── fault / motion / PID / UDT / array context
        └── provenance / limitations
        │
        ├──────────────────────────────────────────┐
        │                                          │
        ▼                                          ▼
Engineering model                           OPC UA endpoint(s)
                                                   │
                                      connect / browse / read
                                                   │
                                      exact reconciliation
                                                   │
                                      trust / freshness gate
                                                   │
                         GOOD + CURRENT + non-stale + non-replayed only
                                                   │
        └──────────────────────┬────────────────────┘
                               ▼
                    Engineering ↔ Runtime Join
                               ▼
                 Deterministic Commissioning Engine
                               │
                 ┌─────────────┼─────────────┐
                 │             │             │
          Direct blocker   Recursive trace   Stateful/history
                 │             │             │
                 └─────────────┼─────────────┘
                               │
                 Advanced semantic diagnosis
                               │
       numeric / handshake / AOI-FB / fault / sequence /
             motion / PID / UDT-array context
                               │
                               ▼
                    Optional bounded AI explanation
                               ▼
                  Engineer answer + evidence + next check
```

### Live evidence rules

Live remains fail-closed:

- only trusted `CURRENT` runtime evidence may support definitive current-state conclusions;
- BAD, stale, replayed, uncertain, missing, or ambiguous evidence cannot be silently promoted;
- multiple writers, partial/opaque logic, stateful history gaps, unsupported semantics, and ambiguous target identity remain limitations rather than guessed root causes;
- historical ordering may identify temporal candidates, but “changed before” does not automatically mean “caused”;
- AI may explain bounded evidence, but it cannot upgrade evidence class or invent hidden PLC logic.

### Read-only control boundary

DevAgent Live has no PLC write/control authority. The product boundary excludes:

```text
write / force / reset / bypass / download / mode change /
start / stop / PLC control method calls
```

Natural-language requests for those actions are refused before diagnosis/AI execution.

## CLI workflow map

```text
devagent                         → Software Engineering
devagent plc ...                 → Offline PLC engineering / FAT authority
devagent live ...                → Read-only onsite commissioning
```

Typical Live start:

```bash
python -m pip install "devagent-ai[live]"

devagent live assist /path/to/project-export \
  --endpoint opc.tcp://10.0.0.20:4840/ \
  --history-seconds 900
```

Example onsite questions:

```text
Why is Conveyor7_Run not active?
Which permissive is blocking it?
Why is that permissive false?
Why is SequenceState not advancing?
Why is Timer1 not done?
Why did Conveyor7_Run stop 30 seconds ago?
What should I check next?
```

Commercial qualification commands are documented in [`docs/live/commercial-v1-runbook.md`](live/commercial-v1-runbook.md).

## Architecture summary

```text
Software:   Working Repo → Change → Test → Review → Report → Commit/Push
PLC:        PLC Export   → Analyze → Verify → FAT / Evidence → Release Readiness
Live:       PLC Context + OPC UA → Trust → Map → Diagnose → Explain / Next Check
```

The three branches share the DevAgent evidence-driven core, but their execution, authority, safety boundaries, and release responsibilities intentionally remain separate.
