# DevAgent Controls Platform V1

DevAgent Controls is the deterministic controls-authoring, staging, verification,
and evidence-ingestion branch above the existing DevAgent PLC verification
engine. The product release is V1; the hardened authoring contracts are
`devagent-controls-spec-v2` and `devagent-controls-ir-v2`.

## Authority boundary

Controls can:

- validate strict, versioned company controls specifications;
- normalize them into an immutable Controls IR with stable SHA-256 identities;
- enforce qualified Motor/VFD/Conveyor/Valve equipment contracts;
- generate deterministic Rockwell full-project `.L5X` staging artifacts;
- re-import generated Rockwell artifacts through the existing DevAgent PLC analyzer;
- compare the re-imported `CanonicalPLCProject` with a formal expected PLC projection;
- generate deterministic Ignition staging configuration from the same equipment IDs;
- generate requirement-traceable FAT plans and a bounded deterministic model simulation;
- ingest and cryptographically verify qualified external vendor/runtime evidence;
- bind external FAT and human approvals to the exact build/evidence revision;
- provide CLI and local self-service portal workflows, including revision diff.

Controls does **not** connect to a PLC, write/force tags, change controller mode,
download PLC projects, deploy an Ignition Gateway project, or execute a physical
FAT. External evidence is supplied by qualified engineering/vendor environments
and is verified rather than invented.

A staging build can reach only:

```text
READY_FOR_ENGINEERING_REVIEW
```

An external qualification can later reach:

```text
READY_FOR_ENGINEERING_APPROVAL
APPROVED_FOR_RELEASE_HANDOFF
```

only when the required signed evidence and approvals are present. Even then,
DevAgent Controls performs no production deployment.

## Architecture

```text
ControlSystemSpec v2
  -> strict fail-closed validation
  -> deterministic normalization
  -> immutable ControlsIR v2
  -> company equipment standards
       |
       +-> Rockwell generator -> .L5X
       |                         |
       |                         +-> existing DevAgent PLC analyzer
       |                              -> CanonicalPLCProject
       |                              -> formal IR/PLC semantic projection proof
       |
       +-> Ignition staging generator
       |      -> UDT-equivalent definitions
       |      -> equipment instances
       |      -> alarm bindings
       |      -> history policy
       |      -> faceplate/navigation bindings
       |
       +-> FAT generator
              -> standard cases
              -> structured requirement cases
              -> PLC/HMI alarm cases
              -> deterministic standard-model simulation

all outputs
  -> SHA-256 generation manifest
  -> build self-verification
  -> deterministic revision diff / engineering review
  -> signed Studio 5000 import + re-export evidence
  -> signed Ignition import + normalized semantic export evidence
  -> qualified execution-backend evidence
  -> signed integrated Controls FAT results
  -> per-controller approval
  -> build-wide signed engineering approval
  -> release handoff only
```

## Company standards

The V1 product release qualifies these exact standard IDs:

- `motor-v1`
- `vfd-v1`
- `conveyor-v1`
- `valve-v1`

Each standard pins the required command/status contract, required physical
feedback signals, status-to-feedback mapping, command semantics, generated output
surface, minimum permissive/interlock/fault/alarm coverage, fault-status contract,
alarm-source policy, historian policy, and qualified faceplate. Interlocks and
faults are modeled separately: every fault is also an interlock so an active
fault cannot bypass inhibit logic, but a non-fault interlock such as a guard
condition does not automatically assert FAULTED. A version-shaped
but unknown standard such as `conveyor-v999` fails closed; `latest` is never
accepted as an engineering standard.

Company rules include explicit checks such as:

- `CTRL-E310` required permissive coverage;
- `CTRL-E417` required reset behavior where applicable;
- `CTRL-E325` explicit fault-source coverage;
- `CTRL-E331` every fault has an operator-visible alarm binding;
- `CTRL-E500` explicit alarm source binding;
- `CTRL-W500` operator response guidance;
- `CTRL-E600` structured assertion requirement for HIGH/CRITICAL requirements;
- `CTRL-E700` unique logical I/O mapping;
- `CTRL-E120` required physical feedback coverage;
- `CTRL-E405` explicit command semantics;
- `CTRL-W710` staged I/O mapping completeness before release qualification.

The authoring model also supports controller network metadata, equipment
`area`, `safety_zone`, and explicit staged I/O mappings. Physical I/O
addresses are metadata only; generated standard logic does not write physical
addresses directly. Staging may proceed with incomplete I/O mapping so engineers
can review logic early, but external release qualification fails closed until
every required feedback signal and every generated output has an explicit I/O
mapping.

## Structured requirements

HIGH/CRITICAL behavior can be expressed as deterministic Boolean assertions:

```json
{
  "id": "REQ_CONV101_GUARD",
  "text": "CONV_101 must not run while GUARD_OPEN is active.",
  "criticality": "HIGH",
  "assertion": {
    "conditions": {
      "COMMAND.START": true,
      "SIGNAL.GUARD_OPEN": true
    },
    "expect": {
      "OUTPUT.RUN": false
    }
  }
}
```

Supported condition references are `COMMAND.*` and `SIGNAL.*`. Supported
expected references are `OUTPUT.*`, `STATUS.*`, and `ALARM.*`. References
must resolve to declared authoring members or validation fails.

PLC-verifiable assertions are converted into per-controller DevAgent PLC
requirement handoffs. HMI/cross-domain assertions remain on the Controls
FAT/HMI evidence surface and are not misrepresented as PLC-only proof.

## Determinism

The following are stable for the same semantic input:

- `spec_sha256`;
- `controls_ir_sha256`;
- normalized authoring bytes;
- generated Rockwell bytes;
- Ignition staging JSON;
- FAT plan;
- build artifact SHA-256 values.

Semantically unordered collections are canonicalized before hashing. Company
rule evidence is emitted in canonical order, so merely reordering author input
does not change deterministic build evidence.

## Rockwell generation and round-trip proof

The qualified generator target is currently **Rockwell ControlLogix only**.
It uses a pinned Studio-5000-exported golden template and deterministic XML
transforms. CompactLogix fails closed until a pinned CompactLogix golden
template has its own qualification; DevAgent does not synthesize an
unqualified project shell.

The generator emits bounded RLL using the supported deterministic instruction
surface and does not create `.ACD` files. It never asks an LLM to invent ladder
logic.

For Motor/VFD/Conveyor standards, the primary run request uses one-writer
STOP-dominant seal-in logic. Loss of a permissive or activation of an interlock
drops the request; clearing the condition does not restore a dropped request
without a new primary command. `RUNNING` is not inferred from the command
output: it is driven by the required `RUN_FB` feedback signal. Valve `OPEN`
and `CLOSED` status similarly use required `OPEN_FB`/`CLOSED_FB` feedback
rather than assuming commanded position equals physical position.

Generated projects are re-imported through
`devagent.plc.safe_analysis.analyze_rockwell_l5x`. Controls independently
projects the authoring IR into the expected canonical PLC semantic surface and
compares it with the re-imported project. Verification covers controller
identity, controller tag inventory/contracts, generated program/rung
read/write semantics, one-writer output ownership, unknown/partial
instructions, direct physical output writes, and the existing DevAgent PLC
verification outcome.

A mismatch blocks build finalization.

## Ignition staging and semantic qualification

Ignition staging uses `devagent-ignition-staging-v2` and generates:

- `udts.json`;
- `equipment.json`;
- `alarms.json`;
- `history.json`;
- `views.json`;
- `navigation.json`.

PLC, HMI, historian, alarm, FAT, and requirement traceability retain the same
stable equipment ID, for example `CONV_101`.

A real Gateway qualification remains external. The qualification adapter must
return signed import evidence plus a normalized Gateway export projection.
DevAgent canonicalizes semantically unordered exported collections and requires
the actual Gateway projection to match the deterministic staging projection.
This avoids treating a simple "import succeeded" checkbox as semantic proof.

## FAT and runtime evidence

The generated FAT plan always starts as:

```text
execution_status = NOT_RUN
execution_owner  = CONTROLS_ENGINEER
```

Generated cases cover standard positive paths, seal-in hold/drop behavior,
permissive loss, interlock/fault activation, stop/opposite-command inhibition,
physical feedback-to-status behavior, reset where applicable, PLC/HMI alarm
bindings, and explicit structured requirement assertions.

The built-in simulation is a **model-only** consistency check. It is not Logix
Echo, HIL, a real controller, process physics, or machine-safety validation.

External qualification reuses DevAgent PLC's qualified execution-backend
registry, signed execution results, verification-context binding, release
policy, trust store, and per-controller human approval. In addition, Controls
requires a signed `controls-fat-results.json` whose exact test set matches the
generated FAT plan and whose runtime bindings match the qualified controller
execution contexts.

## Build-wide approval

Per-controller PLC approvals do not by themselves approve the HMI or integrated
Controls evidence. DevAgent therefore computes a build-wide approval-context
hash that binds:

- the generation manifest;
- trust store;
- Studio 5000 import/re-export semantic evidence;
- Ignition import/export semantic evidence;
- integrated FAT evidence;
- controller runtime verification contexts.

A separate signed `controls-engineering-approval.json` must approve that exact
context before the result can become `APPROVED_FOR_RELEASE_HANDOFF`. A later
change to any bound evidence changes the approval-context hash and invalidates
reuse of the old approval.

## CLI

Validate and inspect:

```bash
devagent controls validate examples/controls/conveyor_v1.json
devagent controls inspect examples/controls/conveyor_v1.json
```

Build and verify:

```bash
devagent controls build \
  examples/controls/conveyor_v1.json \
  --output-dir /tmp/controls-build

devagent controls verify /tmp/controls-build
```

Compare a candidate spec with a verified baseline build:

```bash
devagent controls diff \
  /tmp/controls-build \
  candidate-controls.json
```

Create a hash-bound review request:

```bash
devagent controls request-review /tmp/controls-build \
  --requested-by "Lead Controls Engineer" \
  --output /tmp/controls-review-request.json
```

Verify external signed qualification evidence:

```bash
devagent controls qualify /tmp/controls-build \
  --evidence-dir /path/to/controls-evidence \
  --output /tmp/controls-external-qualification.json
```

Run the local portal:

```bash
devagent controls portal \
  --workspace .devagent/controls-portal \
  --host 127.0.0.1 \
  --port 8765
```

Remote portal binding is refused unless the operator explicitly supplies
`--allow-remote`; that option is intended only behind an operator-managed
secure boundary.

## Build package

A successful staging build contains:

```text
build/
  input/spec.json
  controls-ir.json
  company-standards.json
  io-map.json
  generation-manifest.json
  release-readiness.json
  engineering-handoff.json

  rockwell/
    <controller>.L5X
    symbol-map.json

  ignition/
    udts.json
    equipment.json
    alarms.json
    history.json
    views.json
    navigation.json

  requirements/
    controls-requirements.json
    hmi-requirements.json
    by-controller/
      <controller>.json

  tests/
    fat-plan.json
    model-simulation.json

  verification/
    rockwell-roundtrip.json
    ignition-binding.json
```

Every build artifact except the generation manifest itself is SHA-256 bound by
`generation-manifest.json`. `devagent controls verify` fails closed on
missing, added, modified, stale, or non-deterministic artifacts.

## External evidence package

External qualification expects an operator-controlled evidence directory:

```text
evidence/
  trust-store.json
  release-policy.json                  # optional; default policy otherwise
  ignition-gateway-import.json         # signed
  ignition-gateway-export.json         # signed normalized semantic export
  controls-fat-results.json            # signed integrated FAT
  controls-engineering-approval.json   # signed; added only after review

  <controller>/
    studio5000-import.json             # signed
    studio5000-export.L5X
    backend-registry.json              # signed
    execution-results.json             # signed
    approval.json                      # signed PLC/controller approval
```

The first qualification run can intentionally return
`READY_FOR_ENGINEERING_APPROVAL` and the exact
`approval_context_sha256`. After the engineer signs an approval bound to that
hash, re-running qualification can return `APPROVED_FOR_RELEASE_HANDOFF`.

## Portal

The local portal is a thin facade over the same deterministic library used by
the CLI. It supports a guided equipment form for controller/equipment identity,
area, safety zone, signals, permissives, interlocks, faults, alarms, I/O mapping,
historian, and structured requirement assertions. It can validate, build,
compare against a prior verified Controls-IR hash, and create a review request.
It does not contain an independent controls rules/generation implementation.

## CI

`.github/workflows/controls-qualification.yml` runs every
`tests/test_controls*.py` test and then performs:

```text
validate
 -> company standards
 -> deterministic build
 -> PLC semantic round-trip
 -> Ignition coherence
 -> FAT generation/model check
 -> build self-verification
 -> independent second build
 -> reproducibility comparison
 -> evidence artifact upload
```

External Studio 5000/Gateway/runtime qualification cannot be honestly
fabricated in Linux CI. The software contracts and signature-verification path
are exercised with deterministic fixtures; real vendor qualification requires
real tool/runtime evidence.

## Acceptance contract

The internal staging gate is:

```text
SPEC_VALIDATION=PASS
COMPANY_STANDARD_RULES=PASS
DETERMINISTIC_GENERATION=PASS
ROCKWELL_REIMPORT=PASS
CONTROLS_IR_ROUNDTRIP=PASS
IGNITION_BINDING_COHERENCE=PASS
FAT_GENERATION=PASS
MODEL_SIMULATION=PASS
RELEASE_IO_MAPPING=PASS_OR_REVIEW_REQUIRED
UNQUALIFIED_DYNAMIC_PASS=0
PRODUCTION_DEPLOYMENT=NOT_PERFORMED
```

The external release-handoff gate additionally requires:

```text
STUDIO5000_IMPORT_EXPORT_SEMANTICS=PASS
IGNITION_IMPORT_EXPORT_SEMANTICS=PASS
RELEASE_IO_MAPPING=PASS
QUALIFIED_RUNTIME_BACKEND=PASS
SIGNED_RUNTIME_RESULTS=PASS
SIGNED_INTEGRATED_CONTROLS_FAT=PASS
PER_CONTROLLER_ENGINEERING_APPROVAL=PASS
BUILD_WIDE_ENGINEERING_APPROVAL=PASS
PRODUCTION_DEPLOYMENT=NOT_PERFORMED
```
