# rf-theia

Robot Framework + TPT testing harness for **Theia / Artheia** — a reusable,
pip-installable library that drives the live supervisor + the `Tracer.hh` feed,
regression-tests the artheia generators, and inspects end-to-end signal flow
across Functional Clusters.

This repo is the **harness only**. The `.robot` test *scenarios* live in the
consuming project (e.g. `pero_theia` at `testing/scenarios/`) and import this
library as `rf_theia.TheiaTestLibrary`. rf-theia is the reusable tool; the
scenarios are project tests.

## Layout

```
rf_theia/
  TheiaTestLibrary.py   the single Robot Library entry point (T Sup / T Sig …)
  adapters/             supervisor gRPC, tracer JSONL, the MCP server
  runtime/  testkit/  tpt_engine/  assessment/  space/
pyproject.toml          the `rf-theia` package (console scripts: rf-theia-mcp)
run_mcp.sh              MCP launcher (resolves the consuming repo's scenarios)
```

## Install

```sh
# into the consuming workspace's venv:
pip install -e /path/to/rf-theia[mcp,dev]
```

## MCP (Claude Code)

`run_mcp.sh` launches the MCP server. It resolves the consuming repo's scenario
tree from the environment:

- `RF_THEIA_WORKSPACE` — the consuming workspace root (holds the `.venv`).
- `RF_THEIA_SCENARIOS` — the `.robot` scenario tree (default
  `$RF_THEIA_WORKSPACE/testing/scenarios`).

Point the workspace's `.mcp.json` at `rf-theia/run_mcp.sh`.

## As a submodule

rf-theia is consumed as a git submodule (mounted at `rf-theia/`):

```sh
git submodule add ssh://git@cicd.skyway.porsche.com/PG50/rf-theia.git rf-theia
```
