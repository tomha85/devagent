# DevAgent Live Realtime Evidence Integrity V2

DevAgent Live is a **READ ONLY** commissioning and verification client. This document defines the commercial integrity contract for realtime OPC UA evidence used by current-state and historical Q&A.

## Authority boundary

DevAgent does not write, force, reset, bypass, download, start/stop, change controller mode, or call PLC control methods.

Current-state engineering conclusions are bounded by:

1. the canonical imported PLC engineering model;
2. accepted deterministic engineering-to-OPC-UA reconciliation;
3. trusted OPC UA values;
4. connection/session continuity;
5. realtime snapshot freshness/coherency;
6. deterministic diagnosis before optional AI explanation.

The LLM may interpret language and explain deterministic evidence. It does not create PLC truth.

## Commercial realtime flow

```text
OPC UA Server
    |
    +-- MonitoredItem subscription (continuous)
    |       sampling default: 100 ms
    |       publishing default: 250 ms
    |       server queue: bounded
    |       client iterator queue: bounded + disconnect-on-overflow
    |
    +-- Multi-node Read (question-time coherent fallback)
            |
            v
Realtime evidence cache
    |
    +-- source/server freshness gate
    +-- GOOD quality gate
    +-- replay rejection for current truth
    +-- receipt/skew coherency gate
    +-- connection-epoch fence
            |
            v
Deterministic PLC diagnosis
            |
            v
Recursive root-cause trace
            |
            v
Optional AI explanation
            |
            v
Final current-state revalidation
```

## Evidence gap contract

Historical evidence is **not** allowed to silently remain "complete" after a known telemetry gap.

DevAgent records an evidence gap when it detects any of the following:

- OPC UA MonitoredItem `Overflow` InfoBit: the server queue reached its limit and purged detected changes;
- DevAgent bounded realtime event buffer eviction;
- OPC UA connection/session continuity loss;
- subscription recreation where complete notification continuity cannot be proven;
- OPC UA monitored-item creation rejection.

For OPC UA DataValues, the Overflow InfoBit is treated according to OPC UA Part 4: it means not every detected change was returned because the MonitoredItem queue reached its limit.

When a gap overlaps a requested historical window, DevAgent reports:

```text
Evidence integrity: INCOMPLETE
```

If no transition was captured in an incomplete window, DevAgent must not convert that absence into proof that no transition occurred.

## Client queue overflow behavior

The supported asyncua 2.x iterator API provides a bounded event queue and `OverflowPolicy.DISCONNECT`. Production Live uses disconnect-on-overflow instead of `DROP_OLDEST`/`DROP_NEWEST`, because silent client-side notification loss is unacceptable for commissioning evidence.

A forced reconnect invalidates current realtime cache/epoch continuity. Current-state Q&A therefore requires fresh trusted evidence before it can become definitive again.

## Exact monitored-set reconciliation

Production Live treats the current accepted mapping set as the desired monitored set.

On mapping refresh:

```text
old desired set
      |
      v
new reconciliation
      |
      v
exact desired monitored set
      |
      +-- removed nodes stop consuming monitor capacity
      +-- new nodes are added deterministically
      +-- configured max is enforced
      +-- omitted count is visible in :status
```

This prevents stale mappings from accumulating until the monitored-node limit is exhausted.

## Late/out-of-order events

Network and server scheduling can deliver source-timestamped notifications out of arrival order.

Production history does not allow a late sample to rewind the current cache, but it does preserve the sample in the bounded historical timeline and recomputes transitions by timestamp. Equal-timestamp conflicting values do not invent a temporal transition because their order is not defensible.

## Reconnect and Republish

asyncua may recover a subscription and Republish retained NotificationMessages. DevAgent tracks replayed events for observability. Replayed values may contribute to bounded historical evidence but are not accepted as current cache truth.

If the subscription must be recreated and continuity cannot be proven, DevAgent records an evidence gap.

## Operator observability

`:status` for the production assist path exposes:

- evidence integrity COMPLETE / INCOMPLETE;
- total evidence gaps detected;
- server monitored-item overflow count;
- local event-buffer drop count;
- replay/Republish event count;
- subscription recreation count;
- desired / active / omitted monitored-node counts;
- last OPC UA notification sequence number when available;
- last evidence-gap timestamp and reason.

## Historical report semantics

`Evidence integrity: COMPLETE` means DevAgent detected no known evidence gap in the requested retained window.

It does **not** mean DevAgent proves that the physical process, field wiring, network, PLC runtime, or OPC UA server is infallible. It means the bounded DevAgent acquisition path has no detected gap that invalidates the requested timeline.

`Evidence integrity: INCOMPLETE` means the timeline is useful as partial engineering evidence but must not be presented as exhaustive proof of all changes in that window.

## Qualification gates before calling a release Commercial Production

A release should demonstrate all of the following on the exact release commit:

1. focused integrity/race regression suite;
2. full Python test suite;
3. package wheel build/install;
4. real asyncua secure OPC UA server integration;
5. server MonitoredItem overflow injection and detection;
6. client iterator queue overload / disconnect-on-overflow behavior;
7. reconnect + Republish + subscription recreation behavior;
8. late/out-of-order historical event preservation;
9. exact monitored-set refresh/add/remove behavior;
10. 256-node subscription/load qualification;
11. multi-PLC qualification;
12. slow-AI final-state revalidation qualification;
13. multi-hour soak with bounded memory/event queues;
14. zero PLC write/control surface regression.

Until those gates are executed successfully on a release candidate, the implementation should be described as a **production candidate**, not field-proven exhaustive evidence.
