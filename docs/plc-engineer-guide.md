# DevAgent PLC Engineer Guide

DevAgent's PLC workflow is an **offline engineering review and verification workflow** for exported PLC engineering artifacts. It is designed to help a PLC/control engineer understand logic, verify requirements against supported static semantics, identify risks, generate engineer-ready FAT procedures, compare revisions, and produce an evidence-backed customer report **before onsite commissioning**.

DevAgent does **not** connect to, download to, upload to, force, start, stop, or control a PLC, TIA Portal, Studio 5000, PLCSIM, Logix Echo, HIL bench, or production machine. The PLC engineer remains the execution owner for runtime FAT and commissioning evidence.

## 1. Install and check DevAgent

```bash
python -m pip install devagent-ai

devagent --version
devagent plc --help
```

AI review is optional. The deterministic PLC analysis, evidence, risk, requirement, regression, and FAT-planning layers do not depend on an AI model being allowed to invent proof.

To enable the evidence-constrained AI engineering-review layer, configure a provider normally, for example:

```bash
devagent setup --provider openai --model YOUR_MODEL
export OPENAI_API_KEY=...
devagent doctor --live
```

The same PLC workflow can use another supported provider such as Claude/Anthropic, Gemini, Grok/xAI, or a compatible private endpoint.

## 2. Export the PLC project

### Rockwell Studio 5000

Export the **full controller project** as an `.L5X` file.

Recommended input:

```text
Line1_Controller.L5X
```

A full-project L5X gives DevAgent the best available offline evidence for controllers, programs, routines, tags, AOIs, tasks, calls, logic, data identity, writers, and revision analysis. Avoid giving DevAgent only a screenshot or isolated rung when the goal is whole-project verification.

### Siemens TIA Portal

Export engineering artifacts from TIA Portal as an Openness/XML/generated-source file or an export directory. Supported input forms include:

```text
.scl
.db
.udt
.xml
.stl
.awl
```

A directory may contain the related exported artifacts so DevAgent can build project-wide identity, block/call, data, requirement, risk, and support-boundary evidence.

Proprietary TIA project/archive formats such as `.ap*` and `.zap*` are **not** treated as transparent engineering source. Export them from TIA Portal first.

For best results include the engineering artifacts that describe the controller logic and data used by the machine, such as OB/FB/FC logic, DBs, UDT/type definitions, generated SCL/STL/AWL sources, and Openness XML for LAD/FBD networks when available.

## 3. Prepare requirements

Requirements are optional but strongly recommended when the goal is requirement verification or a customer FAT report.

`--requirements` is repeatable and accepts engineering requirement artifacts such as `.txt`, `.md`, `.csv`, `.json`, `.docx`, and `.pdf` when PDF support is installed.

Example `requirements.md`:

```markdown
# Conveyor safety requirements

- The conveyor shall not run unless the main guard circuit is healthy.
- A safety fault shall prevent the run command from energizing the motor output.
- Reset shall not automatically restart the conveyor.
- Loss of the permissive shall remove the run request.
- The HMI running indication shall match the commanded running state.
```

Write requirements in normal engineering language. Do not rewrite them only to match PLC tag names; DevAgent should preserve the distinction between the customer requirement and the evidence found in the controller project.

## 4. Run a first engineering review

### Rockwell example

```bash
devagent plc ./exports/Line1_Controller.L5X \
  --requirements ./requirements/line1.md \
  --ai \
  --output-dir ./plc-review/line1-current
```

### Siemens example

```bash
devagent plc ./exports/TIA_Line1/ \
  --requirements ./requirements/line1.md \
  --ai \
  --output-dir ./plc-review/line1-current
```

Without `--ai`, DevAgent still performs the deterministic PLC analysis and FAT-planning workflow:

```bash
devagent plc ./exports/TIA_Line1/ \
  --requirements ./requirements/line1.md \
  --output-dir ./plc-review/line1-current
```

Use `--no-write` when you only want the report printed and do not want a run package written to disk.

## 5. What DevAgent produces

A written PLC run contains an engineering evidence package such as:

```text
canonical_ir.json
dependency_graph.json
static_verification.json
engineering_review.json
requirements.json
requirement_verification.json
fat_tests.json
fat_plan.json
execution_plan.json
test_execution.json
risks.json
optimizations.json
regression.json
recommendations.json
evidence_manifest.json
release_readiness.json
pipeline_stages.json
fat_report.md
run_manifest.json
```

The main artifact for a PLC engineer or customer review is usually:

```text
fat_report.md
```

The JSON artifacts are the machine-readable evidence behind that report. The run manifest binds the analysis to exact source and planning hashes so evidence from an older project revision is not silently treated as proof for a newer revision.

## 6. How to read the result

Do **not** interpret every parsed network or every passing static check as proof of machine behavior.

DevAgent deliberately separates what can be proven from exported engineering evidence from what still needs runtime evidence.

### Static proof

A static result can support a requirement only when the required identity, writer ownership, execution reachability, call binding, and supported logic semantics are proven for that exact revision.

### `PARTIAL`, `OPAQUE`, or `PROTECTED`

These classifications are intentional safety boundaries, not parser success disguised as proof.

They mean some behavior is not safely provable from the available exported source. Examples can include unsupported graphical topology, protected code, ambiguous/dynamic identity, unsupported instructions, runtime scheduling effects, or vendor/runtime behavior outside the bounded theorem.

### `FAT_REQUIRED`

A generated FAT requirement means the missing evidence must be obtained by an engineer in the appropriate runtime environment. Typical examples include timing/count behavior, hardware-dependent behavior, external I/O/device behavior, system services, simulator/controller behavior, and logic outside the supported static model.

**A green static result does not replace FAT when the report says runtime evidence is required.**

## 7. Compare a PLC revision before going onsite

Use `--baseline` with a previous export from the **same vendor**.

### Rockwell revision comparison

```bash
devagent plc ./exports/new/Line1_Controller.L5X \
  --baseline ./exports/old/Line1_Controller.L5X \
  --requirements ./requirements/line1.md \
  --ai \
  --output-dir ./plc-review/line1-revision
```

### Siemens revision comparison

```bash
devagent plc ./exports/new/TIA_Line1/ \
  --baseline ./exports/old/TIA_Line1/ \
  --requirements ./requirements/line1.md \
  --ai \
  --output-dir ./plc-review/line1-revision
```

The regression analysis is intended to answer practical commissioning questions such as:

- What PLC logic changed?
- Which requirements are affected?
- Which risks changed?
- Which FAT tests must be repeated?
- Did a writer, call path, interlock, permissive, state transition, recovery/reset path, or data identity change?
- Is old evidence still valid for this exact revision?

## 8. Recommended PLC engineer workflow

```text
EXPORT PLC PROJECT
        ↓
RUN DEVAGENT PLC REVIEW
        ↓
REVIEW STATIC PROOF + SUPPORT BOUNDARY
        ↓
REVIEW REQUIREMENT TRACEABILITY
        ↓
REVIEW RISKS + OPTIMIZATION RECOMMENDATIONS
        ↓
REVIEW GENERATED FAT PROCEDURES
        ↓
EXECUTE REQUIRED FAT IN EXTERNAL SIMULATOR/HIL/PLC ENVIRONMENT
        ↓
CAPTURE ENGINEER-OWNED RESULTS/EVIDENCE
        ↓
RE-RUN / IMPORT TRUSTED EXECUTION EVIDENCE WHEN APPLICABLE
        ↓
ENGINEERING APPROVAL
        ↓
SITE COMMISSIONING / RELEASE PROCESS
```

DevAgent is most valuable **before travel**: use it to find unproven requirements, hidden dependencies, regression scope, unreachable/contradictory logic, writer conflicts, unsupported behavior, and the exact runtime checks an engineer should perform.

## 9. Advanced: import runtime FAT evidence

DevAgent can consume execution results after the PLC engineer executes the FAT externally. Imported execution results require a backend/provenance registry; signed policy/evidence flows can also use a trust store and approval artifact.

The CLI contract includes:

```text
--execution-results FILE
--execution-backend-registry FILE
--release-policy FILE
--trust-store FILE
--approval FILE
```

`--execution-results` requires `--execution-backend-registry`. External release policies require `--trust-store` because they must be cryptographically trusted rather than accepted as arbitrary input.

This is deliberately stricter than importing an unsigned spreadsheet and calling the system verified.

## 10. What to give DevAgent for the strongest result

For a customer or pre-FAT review, provide as much of the following as is legitimately available:

- complete Rockwell `.L5X`, or complete Siemens exported engineering bundle;
- customer/URS/functional requirements;
- previous PLC export when reviewing a change;
- alarm/interlock/permissive expectations;
- sequence/state-machine expectations;
- reset/recovery/restart requirements;
- commissioning constraints and known hardware/runtime dependencies;
- trusted runtime FAT evidence when available.

Do not provide credentials, production secrets, or proprietary files you are not authorized to process.

## 11. Vendor-specific evidence boundaries

### Rockwell

DevAgent's Rockwell path is built around Studio 5000 `.L5X` engineering evidence. Supported deterministic semantics can be used for static proof; unsupported or runtime-dependent behavior is withheld from static verification and routed to risk/FAT evidence instead.

### Siemens

The Siemens path supports exported TIA engineering artifacts and classifies imported executable/support regions with explicit support accounting. Supported SCL and bounded LAD/FBD/call/data/state/interlock/recovery semantics may be analyzed deterministically. Unknown topology/instructions, protected behavior, ambiguous/dynamic identity, and runtime-dependent behavior remain `PARTIAL`, `OPAQUE`, `PROTECTED`, or FAT-required rather than being silently treated as verified.

Current repository qualification uses deterministic fixtures and explicit external-evidence boundaries. Synthetic qualification fixtures are not represented as real customer TIA exports. Real customer/license-safe export validation remains an external evidence gate.

## 12. Important safety rule

DevAgent is an engineering **analysis, verification, regression, and FAT-planning assistant**. It is not a safety PLC certifier, functional-safety authority, or substitute for the machine builder's engineering process.

For safety-related machinery, the responsible engineer must still follow the applicable company procedures, validation plan, machine risk assessment, safety lifecycle, vendor requirements, and regulatory/functional-safety obligations.
