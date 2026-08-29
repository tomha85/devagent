# DevAgent — General Architecture

DevAgent has one evidence-driven core with **three separate product branches**. These branches are siblings in the product architecture; none is a sub-branch of another:

1. **Software Engineering** — repository understanding, bounded code changes, verification, review, and safe branch publication.
2. **DevAgent PLC** — offline/pre-site PLC engineering review and FAT authority.
3. **DevAgent Live** — onsite, read-only commissioning assistance using trusted runtime evidence.

The branches share evidence, provider, trust, reporting, and fail-closed principles, but they have different inputs, execution paths, authority, and release responsibilities.

## General architecture — independent product branches

```text
                                      DevAgent Core
                                           │
              ┌────────────────────────────┼────────────────────────────┐
              │                            │                            │
              ▼                            ▼                            ▼
     SOFTWARE ENGINEERING            DEVAGENT PLC                 DEVAGENT LIVE
      Product Branch #1             Product Branch #2            Product Branch #3
      Repository workflow           Offline / pre-site           Onsite commissioning
                                    engineering + FAT            READ ONLY
              │                            │                            │
              │                            │                            │
      Local / GitHub Repo          PLC engineering export        Engineering context input
              │                            │                     + OPC UA endpoint(s)
              ▼                            ▼                            │
     Understand / Plan          Vendor-dispatched import                │
              │                    │        │        │                  │
              ▼                 Siemens  Rockwell  Schneider             │
        Modify Code                  │        │        │                  │
              │                      └────────┼────────┘                  │
              ▼                               ▼                           │
     Build / Test / Review          Canonical PLC Engineering Model       │
              │                               │                           │
              ▼                               ▼                           │
      Engineering Report          Analyze / Verify / Requirements        │
              │                    Risks / Regression / FAT               │
              ▼                               │                           │
    Commit / Push Safe Branch                 ▼                           │
              │                    FAT Report / Release Readiness          │
              ▼                                                           │
   Developer / Repo Integration                                        ▼
                                                             Connect / Browse / Reconcile
                                                                       │
                                                                       ▼
                                                               Trust / Freshness Gate
                                                                       │
                                                                       ▼
                                                          Engineering ↔ Runtime Join
                                                                       │
                                                                       ▼
                                                       Deterministic Commissioning Diagnosis
                                                                       │
                                                                       ▼
                                                Recursive / Stateful / Historical / Advanced
                                                                       │
                                                                       ▼
                                                         Optional AI Explanation / Q&A
```

The diagram above is intentionally a **product hierarchy**, not a data-dependency diagram. DevAgent Live is a first-class branch directly under DevAgent Core, alongside Software Engineering and DevAgent PLC.

## Read-only integration contract between DevAgent PLC and DevAgent Live

DevAgent PLC and DevAgent Live can exchange engineering context through a bounded read-only contract, but this integration does **not** make Live a child of PLC.

```text
DEVAGENT PLC
    │
    │ produces / exposes stable canonical engineering context
    ▼
┌─────────────────────────────────────────────────────────────┐
│            READ-ONLY ENGINEERING CONTEXT CONTRACT           │
│                                                             │
│ tags / identities / logic / dependencies / source location │
│ stateful facts / semantic coverage / provenance / limits    │
└─────────────────────────────────────────────────────────────┘
    │
    │ consumed read-only
    ▼
DEVAGENT LIVE
```

The authority boundary is:

```text
DEVAGENT PLC  = offline engineering / FAT / release-readiness authority
DEVAGENT LIVE = onsite read-only commissioning authority
```

DevAgent Live must not modify Siemens, Rockwell, Schneider, FAT, regression, theorem, or release-readiness behavior merely to implement an onsite feature. Once engineering information crosses the read-only adapter boundary, commissioning behavior is owned by `devagent/live/**`.

The same separation also means DevAgent PLC does not become responsible for OPC UA session management, live-value trust, history collection, or commissioning Q&A simply because Live consumes its engineering model.

## 1. Software Engineering — Product Branch #1

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

## 2. DevAgent PLC — Product Branch #2: offline engineering authority

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

## 3. DevAgent Live — Product Branch #3: onsite commissioning

DevAgent Live is an independent onsite product branch for commissioning engineers. Its purpose is not to regenerate the offline FAT/report workflow. Its job is to help an engineer understand the running system, inspect trusted live state, diagnose why a machine condition is blocked or abnormal, trace modeled dependencies, inspect bounded history, and identify the next evidence-backed check.

Full commissioning mode uses two inputs owned by the Live workflow:

```text
READ-ONLY ENGINEERING CONTEXT
            +
      OPC UA ENDPOINT(S)
            ↓
       DEVAGENT LIVE
```

The engineering context can be created from the same supported PLC export used by DevAgent PLC, but Live consumes it through its own adapter boundary. This keeps the product architecture separate while avoiding duplicated Siemens/Rockwell/Schneider parsing logic.

Why both inputs matter:

- engineering context provides logic, tags, dependencies, source provenance, interlocks/permissives, states, calls, and modeled semantics;
- OPC UA provides live values, quality, timestamps, namespace identity, and runtime observations;
- endpoint-only operation can observe exposed state, but usually cannot prove hidden PLC source logic or why an output is commanded.

### DevAgent Live internal architecture

```text
                    DEVAGENT LIVE
                         │
          ┌──────────────┴──────────────┐
          │                             │
          ▼                             ▼
Read-only engineering context      OPC UA endpoint(s)
          │                             │
          │                    connect / browse / read
          │                             │
          │                    exact reconciliation
          │                             │
          │                    trust / freshness gate
          │                             │
          └──────────────┬──────────────┘
                         ▼
              Engineering ↔ Runtime Join
                         ▼
            Deterministic Commissioning Engine
                         │
       ┌─────────────────┼─────────────────┐
       │                 │                 │
       ▼                 ▼                 ▼
 Direct blocker     Recursive trace   Stateful / history
       │                 │                 │
       └─────────────────┼─────────────────┘
                         ▼
              Advanced semantic diagnosis
                         │
   numeric / handshake / AOI-FB / fault / sequence /
        motion / PID / UDT-array context
                         │
                         ▼
              Optional bounded AI explanation
                         │
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

## Product branch CLI map

```text
devagent                         → Software Engineering branch
devagent plc ...                 → DevAgent PLC branch
devagent live ...                → DevAgent Live branch
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
What is the current fault code?
Is Speed above the configured limit?
What should I check next?
```

Commercial qualification commands are documented in [`docs/live/commercial-v1-runbook.md`](live/commercial-v1-runbook.md).

## Architecture summary

```text
PRODUCT BRANCH #1 — SOFTWARE
Working Repo → Change → Test → Review → Report → Commit/Push

PRODUCT BRANCH #2 — DEVAGENT PLC
PLC Export → Analyze → Verify → FAT / Evidence → Release Readiness

PRODUCT BRANCH #3 — DEVAGENT LIVE
Engineering Context + OPC UA → Trust → Map → Diagnose → Explain / Next Check
```

The three product branches are siblings under DevAgent Core. They may share stable contracts and evidence primitives, but their execution paths, authority, safety boundaries, and qualification responsibilities intentionally remain separate.
