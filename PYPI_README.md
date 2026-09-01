# DevAgent

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Status: Beta](https://img.shields.io/badge/status-beta-blue.svg)](https://github.com/tomha85/devagent#project-status)
[![Sponsor DevAgent](https://img.shields.io/badge/Sponsor-DevAgent-EA4AAA?logo=githubsponsors&logoColor=white)](https://github.com/sponsors/tomha85)

**An evidence-driven engineering agent for software engineering, offline PLC engineering/FAT review, and read-only onsite commissioning.**

> **From requirement to evidence-backed engineering decisions.**

DevAgent is free during public beta. If it saves you engineering time, consider [supporting continued development](https://github.com/sponsors/tomha85).

- **Project:** https://github.com/tomha85/devagent
- **Documentation:** https://github.com/tomha85/devagent#readme
- **Issues:** https://github.com/tomha85/devagent/issues

## General Architecture

DevAgent has **three sibling product branches** under one evidence-driven core. Each branch owns a distinct engineering responsibility, input model, safety boundary, and qualification path.

```text
                         DevAgent Core
              Evidence-Driven Engineering Platform
                              |
          +-------------------+-------------------+
          |                   |                   |
          v                   v                   v
 Software Engineering     DevAgent PLC        DevAgent Live
 Product Branch #1        Product Branch #2   Product Branch #3
          |                   |                   |
 Local / GitHub repo      PLC export           Engineering context
          |               Siemens              + OPC UA endpoint(s)
          |               Rockwell                  |
          |               Schneider                 v
          |                   |              Reconcile / Trust
          v                   v              Freshness / History
 Understand / Plan       Canonical PLC               |
 Modify / Test / Review  Engineering Model           v
          |                   |              Deterministic
          v                   v              Commissioning Diagnosis
 Engineering Report      Analyze / Verify             |
 + Safe Branch           FAT / Release Readiness      v
                                                Evidence / Explanation
                                                + Next Safe Check
```

| Product branch | Primary input | Authority |
| --- | --- | --- |
| **Software Engineering** | Local / GitHub repository | Understand, modify, verify, review, report, publish a safe branch |
| **DevAgent PLC** | Exported PLC engineering artifacts | Offline engineering review, requirements, FAT, evidence, release readiness |
| **DevAgent Live** | Read-only engineering context + OPC UA runtime | Onsite commissioning diagnosis, runtime evidence, history, Q&A - **no PLC control** |

### Read-only PLC to Live integration contract

DevAgent Live is **not a child of DevAgent PLC**. Stable PLC engineering context can cross into Live through a bounded read-only adapter contract. FAT and release-readiness authority stays with DevAgent PLC; OPC UA sessions, runtime trust/history, diagnosis, and commissioning Q&A stay with DevAgent Live.

```text
Canonical PLC Engineering Model
              |
              | READ-ONLY ENGINEERING CONTEXT
              v
       DevAgent Live Adapter <----- OPC UA Runtime Evidence
              |
              v
      Engineering <-> Runtime Join
              |
              v
      Commissioning Diagnosis
```

The read-only engineering-context path does **not** transfer FAT authority, release-readiness authority, or PLC control authority to DevAgent Live.

For the expanded architecture, see:
https://github.com/tomha85/devagent/blob/main/docs/general-architecture.md

## Why DevAgent?

Many AI engineering tools optimize first for generation. DevAgent is built around a different question:

**Can this engineering claim be supported by evidence on the exact analyzed revision, PLC export, or trusted runtime state?**

Core principles include:

- evidence before modification;
- explicit acceptance contracts;
- false-`VERIFIED` resistance;
- minimal-change discipline;
- backups before edits;
- repository-native verification;
- independent review;
- bring-your-own-model support;
- deterministic publication rules;
- a strict read-only commissioning boundary for DevAgent Live.

## Install

Standard package:

```bash
python -m pip install devagent-ai
```

With OPC UA runtime support for DevAgent Live:

```bash
python -m pip install "devagent-ai[live]"
```

Or with pipx:

```bash
pipx install devagent-ai
```

The PyPI distribution is `devagent-ai`; the Python package and CLI command are both `devagent`.

## Software Engineering

Run DevAgent from a repository and describe the engineering outcome:

```bash
cd my-repo
devagent "Fix the websocket reconnect bug and add regression tests"
```

A normal software-engineering path is:

```text
DISCOVER / UNDERSTAND
        |
        v
COMPILE ACCEPTANCE CONTRACT
        |
        v
BASELINE / PLAN / GATHER CONTEXT
        |
        v
IMPLEMENT MINIMAL PATCH
        |
        v
TARGETED + BROAD VERIFICATION
        |
        v
INDEPENDENT REVIEW
        |
        v
FINAL CURRENT-REVISION VERIFICATION
        |
        v
FULL ENGINEERING REPORT
        |
        v
VERIFIED ONLY: COMMIT + FAST-FORWARD PUSH SAFE BRANCH
```

DevAgent runtime does not create pull requests, merge, rebase, force-push, or deploy.

## DevAgent PLC

DevAgent PLC performs offline/pre-site engineering review for supported exports from:

- **Rockwell Studio 5000 / Logix Designer** - full-project `.L5X`;
- **Siemens TIA Portal** - supported exported source/XML engineering artifacts;
- **Schneider Electric EcoStruxure Control Expert / Unity Pro** - supported XML exchange exports, with `.XEF` preferred.

Example:

```bash
devagent plc ./exports/Line1_Controller.L5X \
  --requirements ./requirements/line1.md \
  --ai \
  --output-dir ./plc-review/line1-current
```

The PLC workflow can provide engineering review, requirements verification, risk detection, FAT planning, regression analysis, recommendations, evidence, and release-readiness reporting. Static analysis does not pretend to replace simulator, HIL, field wiring, process physics, or real-controller execution when those are required.

Full PLC guide:
https://github.com/tomha85/devagent/blob/main/docs/plc-engineer-guide.md

## DevAgent Live

DevAgent Live is the separate **read-only onsite commissioning** branch. It can combine supported engineering context with trusted OPC UA runtime evidence to diagnose blockers and explain the next evidence-backed check.

```text
Supported PLC engineering context
              +
      OPC UA runtime endpoint
              |
              v
         DevAgent Live
              |
              v
  reconcile engineering <-> runtime
              |
              v
 deterministic diagnosis + evidence
```

Example:

```bash
devagent live assist \
  --project-folder /path/to/customer-line1 \
  --primary-project plc/Line1.L5X \
  --endpoint opc.tcp://192.168.10.20:4840/ \
  --history-seconds 900 \
  --ai \
  --provider openai \
  --model YOUR_OPENAI_MODEL
```

### READ ONLY safety boundary

```text
READ ONLY
no write
no force
no reset
no bypass
no upload / download
no mode change
no start / stop control
```

The LLM may interpret or explain evidence. It cannot turn model confidence into PLC truth or authorize machine control.

Live documentation:
https://github.com/tomha85/devagent/tree/main/docs/live

## Bring Your Own Model

DevAgent supports OpenAI, Anthropic/Claude, xAI/Grok, Google Gemini, and OpenAI-compatible endpoints behind the same deterministic engineering harness.

Example:

```bash
devagent setup --provider openai --model YOUR_MODEL
export OPENAI_API_KEY=...
devagent doctor --live
```

Provider choice can affect reasoning quality, latency, cost, and privacy characteristics. It does not change DevAgent's deterministic acceptance, verification, safety, reporting, or publication rules.

## Outcome Contract

DevAgent reports bounded engineering outcomes:

- **`VERIFIED`** - required criteria are supported by admissible evidence and final verification/review passes on the current revision;
- **`PARTIALLY_VERIFIED`** - meaningful evidence exists but complete proof is unavailable;
- **`BLOCKED`** - DevAgent cannot safely understand, implement, or verify the request.

A conservative result is preferred over a false `VERIFIED`.

## Project Status

DevAgent is beta software. Qualification claims are tied to explicit test catalogs, pinned revisions, fixtures/external oracles where applicable, and the environments actually exercised. They are not universal-correctness or market-superiority claims.

For the current implementation status, qualification details, limitations, examples, and complete documentation, see the main repository README:
https://github.com/tomha85/devagent#readme

## Support

DevAgent is currently free during public beta.

If DevAgent saves you engineering time, you can support continued development through GitHub Sponsors:
https://github.com/sponsors/tomha85

## License

DevAgent is open source under the MIT License.

Copyright (c) 2026 Tom Ha

Original repository: https://github.com/tomha85/devagent
