# DevAgent Live Commercial V1 Qualification Runbook

DevAgent Live is an onsite, **read-only** commissioning assistant. DevAgent PLC remains the offline engineering/FAT authority. Live may consume the existing canonical PLC engineering model, but this runbook does not require or permit PLC writes, forces, resets, mode changes, downloads, or method-call control.

## Required evidence

Commercial V1 readiness requires five gates:

1. Real OPC UA runtime qualification: exact `LQ-001..LQ-014`, `PASS=14`, `FAIL=0`, `BLOCKED=0`.
2. Real Rockwell + Siemens + Schneider qualification using each vendor engineering export and a real/vendor-supported OPC UA endpoint.
3. Deterministic timer/counter/state-machine diagnosis contract.
4. Trusted historical commissioning timeline contract.
5. Production doctor PASS plus a real read-only soak of at least 8 hours.

A local simulator is useful for runtime validation but **does not certify a Siemens, Rockwell, or Schneider endpoint**.

## 1. Install and doctor

Use a clean Python 3.10+ environment and install the Live runtime extra:

```bash
python -m pip install "devagent-ai[live]"
```

For each real onsite/vendor qualification target:

```bash
devagent live doctor \
  --project /path/to/engineering-export \
  --endpoint opc.tcp://10.0.0.20:4840/ \
  --output-parent ./evidence \
  --output-dir ./evidence/doctor-rockwell
```

For authenticated endpoints, supply secrets by environment variable and secure-channel options. Never place a literal password on argv.

The final commercial doctor artifact must show `DR-001..DR-008` all PASS.

## 2. Real OPC UA 14-case qualification

```bash
devagent live qualify --output-dir ./evidence/live-runtime-qualification
```

Required result:

```text
PASS=14
FAIL=0
BLOCKED=0
Overall: PASS
```

Do not proceed to a commercial-ready claim when the runtime matrix is BLOCKED or FAIL.

## 3. Real vendor qualification

Create one existing `devagent-live-commission-v1` config containing at least one real qualification PLC for each family:

- Rockwell Studio 5000 engineering export + real/vendor-supported OPC UA endpoint
- Siemens TIA engineering export + real/vendor-supported OPC UA endpoint
- Schneider Control Expert engineering export + real/vendor-supported OPC UA endpoint

Validate first:

```bash
devagent live commission ./real-vendor-commission.json --validate-only
```

Then run:

```bash
devagent live vendor-qualify ./real-vendor-commission.json \
  --output-dir ./evidence/vendor-qualification
```

Each vendor passes only when project parsing, OPC UA connection, exact engineering-tag reconciliation, and trusted CURRENT capture complete successfully. A missing vendor is BLOCKED. A real connect/mapping/capture defect is FAIL.

## 4. Onsite stateful + historical diagnosis

Start the commissioning assistant with bounded history enabled:

```bash
devagent live assist /path/to/project-export \
  --endpoint opc.tcp://10.0.0.20:4840/ \
  --history-seconds 900 \
  --history-poll-seconds 1 \
  --history-max-tags 128
```

Examples:

```text
Why is Conveyor7_Run not active?
Why is that permissive false?
Why is SequenceState not advancing?
Why is Timer1 not done?
Why did Conveyor7_Run stop 30 seconds ago?
```

Live may prove a current modeled Boolean blocker or a FULL bounded state-transition guard. Timer/counter elapsed time, edge history, retentivity, scan order, and physical/process causation remain runtime evidence and are never inferred from static code alone.

Historical ordering identifies trusted temporal candidates only; “changed before” is not automatically “caused”.

## 5. Long-running production soak

Use the same validated commissioning config against the intended production-like endpoints:

```bash
devagent live soak ./real-vendor-commission.json \
  --duration-hours 8 \
  --interval-seconds 1 \
  --min-current-ratio 0.95 \
  --max-consecutive-error-cycles 5 \
  --max-memory-growth-mb 256 \
  --output-dir ./evidence/live-soak
```

For a higher-confidence site release, repeat with a 24-hour soak.

The soak tracks per-PLC trusted CURRENT ratio, read-error cycles, consecutive outage cycles, final recovery state, and process RSS high-water growth. PASS requires each PLC to finish CONNECTED and stay within configured quality/recovery/resource thresholds.

## 6. Final Commercial V1 gate

```bash
devagent live commercial-readiness \
  --runtime-qualification ./evidence/live-runtime-qualification/live_release_qualification.json \
  --vendor-qualification ./evidence/vendor-qualification/live_vendor_qualification.json \
  --doctor-report ./evidence/doctor-rockwell/live_doctor.json \
  --soak-report ./evidence/live-soak/live_soak_report.json \
  --min-soak-hours 8 \
  --output-dir ./evidence/commercial-v1-readiness
```

Required final result:

```text
CV1-001 PASS
CV1-002 PASS
CV1-003 PASS
CV1-004 PASS
CV1-005 PASS
Overall: PASS
Commercial V1 ready: YES
```

If any required real artifact is missing, the relevant gate is BLOCKED. If an artifact is present but inconsistent or below the contract, the relevant gate is FAIL. DevAgent must never convert missing real-world evidence into a PASS claim.
