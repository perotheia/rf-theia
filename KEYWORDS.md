# rf-theia — Keyword Cheat Sheet

```
Library    rf_theia.TheiaTestLibrary
```

Keyword families are grouped by prefix. All live on a single library —
one import gets you everything.


## T Sup — supervisor control

| Keyword                       | Args                                  | Notes |
|-------------------------------|---------------------------------------|-------|
| `T Sup Connect`               | `endpoint=localhost:5051`             | Open gRPC channel; raises if unreachable. |
| `T Sup Disconnect`            |                                       | Close channel. |
| `T Sup Start Child`           | `name`                                | Re-spawn by name; supervisor must have a spec. |
| `T Sup Restart Child`         | `name`                                | RestartChild RPC. |
| `T Sup Terminate Child`       | `name`                                | TerminateChild RPC. |
| `T Sup Expect Child State`    | `name`  `state`  `within=5s`          | Poll snapshot; states: STARTING/RUNNING/STOPPING/STOPPED/CRASHED. |
| `T Sup Expect Restart Count`  | `name`  `count`  `within=10s`         | Asserts restart_count >= count. |
| `T Sup Get Topology`          |                                       | Returns dict: supervisors[], children[]. |


## T Sig — signal-flow tracing

| Keyword                       | Args                                  | Notes |
|-------------------------------|---------------------------------------|-------|
| `T Sig Open Trace`            | `source`                              | File path, `file://...`, or `tcp://...` (future). |
| `T Sig Close Trace`           |                                       | Stop tail thread. |
| `T Sig Expect Trace`          | `event`  `node=`  `within=2s`         | One matching record. |
| `T Sig Expect Order`          | `*events`  `same_correlation=True`    | Sequence; pass events as positional args. |
| `T Sig Expect Latency`        | `from_event`  `to_event`  `lt=50ms`   | Pair must share correlation_id. |
| `T Sig Filter Records`        | `**where`                             | All matching records as list. |

Event names match Tracer.hh's `TraceEvent` (lowercase): `send`,
`send_reply`, `recv`, `dispatch`, `dispatch_done`, `info`, `terminate`,
`call_result`, `call_timeout`, `call_error`, `call_wait`, `call_resume`,
`state_transition`, `state_timeout`.


## TPT engine (vendored from rf-tpt-ls)

| Keyword            | Args                                          | Description |
|--------------------|-----------------------------------------------|-------------|
| `Create Partition` | `name`                                        | Add time partition. |
| `Add Transition`   | `source`  `target`  `condition`               | Conditions: `after Xs`, `event:NAME`, `Signal > value`. |
| `Set Signal`       | `name`  `value`                               | Numeric set. |
| `Apply Ramp`       | `signal_name`  `start`  `end`  `duration`     | Linear stimulus. |
| `Run Time Engine`  | `initial=`  `timeout=60s`                     | Execute the partition graph. |


## Utility

| Keyword    | Args         | Notes |
|------------|--------------|-------|
| `T Wait`   | `duration`   | Accepts `5s`, `500ms`, plain number (seconds). |


## Duration grammar

Most `within=`, `lt=`, `timeout=`, `duration=` args accept:

- `5s`     — 5 seconds
- `500ms`  — 500 milliseconds
- `2.5`    — 2.5 seconds (bare number)


## Coming next

- **T Art** (phase 2) — artheia generator regression. `T Art Parse`,
  `T Art Gen Proto`, `T Art Diff Against Golden`, etc.
- **T Prov** (phase 3) — provisioning, Puppet dry-run, manifest audit.
- **T Orch** (phase 3) — two-phase orchestration: stage 1 provision +
  stage 2 supervisor start, end-to-end.
