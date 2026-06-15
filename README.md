# rf-theia

A **Robot Framework + TPT** testing harness for **Theia / Artheia**: a reusable,
pip-installable library that drives the live supervisor, reads the `Tracer.hh`
feed, regression-tests the artheia generators, and asserts end-to-end signal
flow across Functional Clusters.

This repo is the **harness only** — the reusable library. The `.robot` *scenarios*
that test a specific Theia live in the consuming project (e.g. `pero_theia` at
`testing/scenarios/`) and import this library as `rf_theia.TheiaTestLibrary`.

## Install

Into the consuming workspace's venv:

```sh
pip install -e /path/to/rf-theia[mcp,dev]
# or, from a deb install:  pip install --find-links /opt/theia/wheels rf-theia
```

## Writing a scenario

The harness exposes one Robot library, `rf_theia.TheiaTestLibrary` — a single
entry point with prefixed keyword families (`T Sup` for the supervisor, `T Sig`
for signal/trace flow) plus direct TPT idioms:

```robotframework
*** Settings ***
Library           rf_theia.TheiaTestLibrary

*** Test Cases ***
State machine reaches RUNNING
    Load Rig              central
    Start State Machine   sm
    Emit Event            sm    Ready
    Wait For State        sm    RUNNING    within=5s
    Assert Healthy        sm    within=10s
    Verdict               PASS
    [Teardown]            Tear Down Rig
```

Keywords cover: rig load/teardown, supervisor control (start/stop/restart
children, configure trace/log), state-machine drive (`Emit Event`,
`Wait For State`), and trace/signal assertions over the live `Tracer.hh` stream.

## MCP server (Claude Code)

`run_mcp.sh` launches an MCP server exposing the harness — `run_scenario`,
`list_scenarios`, `list_keywords`, `analyze_trace`, … It resolves the consuming
repo's scenario tree from the environment:

| Var | Meaning | Default |
| --- | --- | --- |
| `RF_THEIA_WORKSPACE` | the consuming workspace root (holds the `.venv`) | the dir holding `rf-theia/` |
| `RF_THEIA_SCENARIOS` | the `.robot` scenario tree | `$RF_THEIA_WORKSPACE/testing/scenarios` |

Point the workspace's `.mcp.json` at `rf-theia/run_mcp.sh`.

## Layout

```
rf_theia/
  TheiaTestLibrary.py   the single Robot library entry point
  adapters/             supervisor gRPC, tracer JSONL decode, the MCP server
  runtime/  testkit/  tpt_engine/  assessment/  space/
pyproject.toml          the rf-theia package (console scripts: rf-theia-mcp)
run_mcp.sh              MCP launcher (resolves the consuming repo's scenarios)
```

Consumed as a git submodule mounted at `rf-theia/` in the umbrella repo.

## License

Apache-2.0 — see [LICENSE](LICENSE).
