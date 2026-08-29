# DevAgent Live Realistic Commissioning Simulator Matrix V1

This matrix is a deterministic, read-only OPC UA test environment for measuring whether DevAgent Live can understand an imported PLC project, reconcile engineering tags to live nodes, identify current blockers/faults/conflicts, and stop when the evidence does not prove a deeper cause.

It is not a substitute for real vendor PLC qualification. It is a known-ground-truth regression and demo environment that should run before onsite testing.

## Demo engineering project

Use:

```bash
examples/live/warehouse_commissioning_demo.L5X
```

The modeled Rockwell rung is intentionally simple and auditable:

```text
RunCmd =
    AutoMode
AND StartRequest
AND SafetyOK
AND NOT SafetyTrip
AND DriveReady
AND NOT DriveFault
AND DownstreamReady
```

The simulator exposes matching OPC UA signals plus `FaultCode`, `MachineState`, speed, sorter readiness, and production values.

## List scenarios

```bash
devagent live sim --list-scenarios
```

## Scenario matrix

| Scenario | Ground truth | Expected agent behavior |
| --- | --- | --- |
| `healthy` | all run conditions satisfied; no fault | `NO_CURRENT_PROVEN_FAULT` |
| `idle` | `StartRequest=FALSE` | explain intentional inactivity; do not call machine faulted |
| `downstream_blocker` | `DownstreamReady=FALSE` | identify exact operational blocker |
| `drive_fault` | `DriveFault=TRUE`, `DriveReady=FALSE`, `FaultCode=101` | report explicit drive fault plus run blocker |
| `safety_trip` | `SafetyOK=FALSE`, `SafetyTrip=TRUE`, `FaultCode=201` | report safety fault and blocked RunCmd |
| `multi_blocker` | `DriveReady=FALSE` and `DownstreamReady=FALSE` | preserve both blockers |
| `logic_conflict` | modeled conditions imply TRUE; `RunCmd=FALSE` | `LOGIC_CONFLICT`; never invent a blocker |
| `stuck_on_conflict` | modeled conditions imply FALSE; `RunCmd=TRUE` | `LOGIC_CONFLICT`; never call normal |
| `normal` | dynamic ready/run transitions | subscription/history qualification |
| `blocker` | legacy downstream blocker | backward-compatible fixed blocker |

## Run one scenario

Terminal 1:

```bash
devagent live sim \
  --endpoint opc.tcp://127.0.0.1:4841/devagent/simulator/ \
  --scenario drive_fault
```

The simulator prints the selected scenario description, expected System Health result, primary ground-truth reason, and the initial known values before waiting for clients.

Terminal 2:

```bash
ENDPOINT="opc.tcp://127.0.0.1:4841/devagent/simulator/"

devagent live assist \
  examples/live/warehouse_commissioning_demo.L5X \
  --endpoint "$ENDPOINT" \
  --max-depth 5 \
  --max-nodes 500 \
  --history-seconds 0
```

Ask both general and specific questions:

```text
Does the system have any faults?
What is wrong with the system?
Why is RunCmd false?
Which permissive is blocking RunCmd?
Why is DriveReady false?
What should I check next?
```

## Qualification rules

A scenario passes only when the answer remains inside the known evidence boundary.

- Correct blocker: the reported blocking signal must match the scenario ground truth.
- Correct fault: explicit fault/alarm/code signals must not be ignored.
- Correct no-fault behavior: `idle` must not be upgraded into a machine fault merely because `RunCmd=FALSE`.
- Correct conflict behavior: contradictory runtime/output state must be reported as `LOGIC_CONFLICT`, not explained with a fabricated permissive.
- Correct follow-up targeting: an explicitly named engineering signal must beat conversational memory.
- Correct root-cause stopping: if a directly observed input has no deterministic writer in the imported project, Live must say the deeper cause is not proven.
- Trusted evidence only: BAD/stale/replayed/unresolved evidence must not support definitive conclusions.
- Read only: no answer may advise or perform PLC write, force, bypass, reset, mode change, download, start/stop, or method control.

## Recommended test order

Run deterministic diagnosis without `--ai` first. This verifies the engineering model, reconciliation, trust layer, and rule engine independently of an LLM.

After deterministic cases pass, repeat with `--ai` to measure explanation quality. AI may improve wording and prioritization but must not change the deterministic ground truth, invent evidence, or raise confidence beyond the bounded diagnosis.

## Boundary

Passing this simulator matrix proves that DevAgent Live behaves correctly against these controlled OPC UA/engineering scenarios. It does not prove compatibility with every PLC, vendor security configuration, network topology, field device, or physical machine failure. Real Siemens, Rockwell, Schneider, secure OPC UA, long-soak, and onsite evidence remain separate qualification gates.
