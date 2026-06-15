# rf-theia — testing harness for theia/artheia

Robot Framework + TPT engine retargeted at the theia stack. Five DSL
surfaces share one library: `T Sup ...`, `T Sig ...`, `T Art ...`,
`T Prov ...`, `T Orch ...`.

Plan and design rationale: `docs/tasks/BACKLOG/testing_framework.md`.
Keyword cheat-sheet: `KEYWORDS.md`.


## Setup

rf-theia shares the single **workspace `.venv`** (no separate testing venv).
From the repo root:

```bash
python3 -m venv .venv          # if it doesn't already exist
./.venv/bin/pip install -e 'testing/[mcp,dev]'
```

This editable-installs the `rf_theia` package (so `robot`, `rf-theia-mcp`,
and the deps land in `.venv/bin`). Invoke the binaries directly
(`.venv/bin/robot`, `.venv/bin/python`) or `source .venv/bin/activate`.


## Running scenarios

```bash
cd testing

# Hermetic — rf-theia self-tests (proves the framework works).
PYTHONPATH=. ../.venv/bin/robot \
  --outputdir /tmp/rf_theia_output \
  rf_theia/scenarios/_selftest/

# Real SUT regression — platform layer.
PYTHONPATH=. ../.venv/bin/robot \
  --outputdir /tmp/rf_theia_output \
  rf_theia/scenarios/platform/

# Dryrun (parse + resolve keywords, don't execute) — catches typos in
# any scenario whether or not the live stack is up.
PYTHONPATH=. ../.venv/bin/robot --dryrun \
  --outputdir /tmp/rf_theia_dry \
  rf_theia/scenarios/
```

Filter by tag:

```bash
../.venv/bin/robot --include hermetic rf_theia/scenarios/
../.venv/bin/robot --exclude live    rf_theia/scenarios/
```

Tag conventions:

- `hermetic`  — no live processes required (most _selftest cases).
- `live`      — needs the SUT running (supervisor, trace stream, ...).
- Category tags by DSL family: `supervision`, `signal-flow`,
  `hybrid-automata`, `temporal-logic`, `components`, `topology`,
  `platform-executor`, ...


## MCP integration

`.mcp.json` at the workspace root points Claude Code at
`testing/run_mcp.sh`. The launcher activates the rf-theia venv before
exec'ing `rf_theia.adapters.mcp_server`.

Tools exposed: `run_scenario`, `list_scenarios`, `list_keywords`,
`create_scenario`, `get_test_results`, `analyze_trace`,
`tail_supervisor_log`.

Verify after install:

```bash
claude mcp list           # should show rf-theia ✓ Connected
```


## Bug investigation workflow

1. Describe the bug to Claude Code in plain English.
2. Claude calls `create_scenario` with a targeted `.robot` using the
   keyword families below.
3. Claude calls `run_scenario`, reads pass/fail.
4. Iterate: refine timing, add assertions, narrow the root cause.
5. If a passing reproducer is interesting, leave it under
   `scenarios/<surface>/` as a regression.


## Layout

```
testing/
├── pyproject.toml
├── requirements.txt
├── run_mcp.sh                  # MCP launcher (workspace-relative)
├── TESTING.md                  # this file
├── KEYWORDS.md                 # keyword cheat sheet
└── rf_theia/
    ├── TheiaTestLibrary.py     # the one library
    ├── adapters/
    │   ├── supervisor_grpc.py  # T Sup → tools/supdbg/_gen reuse
    │   ├── tracer_jsonl.py     # T Sig → Tracer.hh text-format parser
    │   └── mcp_server.py       # FastMCP shell
    ├── tpt_engine/             # vendored from up/rf_tpt_ls
    ├── space/                  # vendored, parked (SPT primitives)
    ├── assessment/             # vendored, pandas-style analysis
    └── scenarios/
        ├── _selftest/          # rf-theia's OWN tests (hidden by underscore)
        │   ├── keywords_load/
        │   ├── hybrid_automata/
        │   ├── temporal_logic/
        │   ├── supervision/
        │   ├── components/
        │   └── topology/
        ├── platform/           # SUT regression: supervisor, runtime, gateway
        │   ├── executor/
        │   ├── runtime/
        │   └── gateway/
        ├── services/           # per-FC functional tests (sm, com, exec, …)
        ├── applications/       # vehicle apps (demo, vendor)
        ├── integration/        # cross-machine, end-to-end
        └── fixtures/           # captured artheia outputs for hermetic tests
```


## What this is NOT

- Not a replacement for component-local unit tests. `platform/runtime/test/`,
  `artheia/tests/`, etc. still own per-component correctness.
- Not auto-included in `bazel test`. rf-theia runs via the Robot CLI
  (or MCP). A Bazel `py_test` wrapper may come later.
- Not a textual TPT DSL. Time partitions live in Python via the engine;
  textual TPT design parked at `docs/tasks/BACKLOG/TPT-DSL.md`.
