"""MCP server: expose rf-theia as MCP tools for Claude Code.

Mirrors the shape of ``up/rf_tpt_ls/adapters/mcp_server.py``, retargeted
at the theia testing surface. Run with::

    python -m rf_theia.adapters.mcp_server

Or via the wrapper ``testing/run_mcp.sh`` which sources the framework's
own venv before exec'ing this module.

Tools exposed:

  - ``run_scenario``        — run one .robot file or directory
  - ``list_scenarios``      — discover scenarios under scenarios/
  - ``list_keywords``       — keyword catalog from TheiaTestLibrary
  - ``create_scenario``     — write a new .robot file
  - ``get_test_results``    — parse the last output.xml
  - ``analyze_trace``       — pandas-style summary over a trace file
  - ``tail_supervisor_log`` — debug helper for failed-run inspection
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any  # noqa: F401

from fastmcp import FastMCP

logger = logging.getLogger("rf_theia.mcp")

mcp = FastMCP(
    "rf-theia",
    instructions=(
        "Theia/Artheia testing harness. Run Robot Framework scenarios "
        "against the live supervisor + Tracer.hh feed, regression-test "
        "artheia generators, and inspect e2e signal flow across FCs."
    ),
)

# rf-theia is a standalone, pip-installable harness (its own repo / submodule);
# the SCENARIOS it runs live in the CONSUMING repo, not in this package. So the
# scenario dir + the workspace venv are resolved from the environment that
# run_mcp.sh exports, with fallbacks for running in-tree.
#   RF_THEIA_SCENARIOS — the .robot scenario tree (consuming repo, e.g.
#                        <repo>/testing/scenarios). Falls back to ./testing/
#                        scenarios under the cwd, then a sibling of this pkg.
#   RF_THEIA_WORKSPACE — the consuming workspace root (holds the .venv robot
#                        runs from). Falls back to cwd.
_PKG_DIR = Path(__file__).resolve().parent.parent          # rf_theia/
_WORKSPACE_DIR = Path(
    os.environ.get("RF_THEIA_WORKSPACE") or os.getcwd()
).resolve()


def _resolve_scenarios() -> Path:
    env = os.environ.get("RF_THEIA_SCENARIOS")
    if env:
        return Path(env).resolve()
    for cand in (_WORKSPACE_DIR / "testing" / "scenarios",
                 _PKG_DIR.parent / "scenarios",      # legacy in-tree layout
                 _PKG_DIR / "scenarios"):
        if cand.is_dir():
            return cand
    return _WORKSPACE_DIR / "testing" / "scenarios"


_SCENARIOS_DIR = _resolve_scenarios()
# The consuming workspace's .venv (rf-theia + robot installed there).
_VENV_BIN = _WORKSPACE_DIR / ".venv" / "bin"
_OUTPUT_DIR = Path("/tmp/rf_theia_output")


@mcp.tool()
def run_scenario(
    scenario_path: str,
    timeout: int = 120,
    tags: str = "",
    dryrun: bool = False,
) -> str:
    """Run a Robot Framework test scenario or directory.

    Args:
        scenario_path: Path relative to testing/rf_theia/scenarios/ (e.g.
            ``supervision/restart_child.robot`` or ``selftest/``).
        timeout:     Max execution time in seconds.
        tags:        Restrict to tests tagged with these (comma-separated).
        dryrun:      Robot's --dryrun mode — parses + resolves keywords
                     without executing them. Useful as a smoke test.
    """
    target = _SCENARIOS_DIR / scenario_path
    if not target.exists():
        available = sorted(
            str(p.relative_to(_SCENARIOS_DIR))
            for p in _SCENARIOS_DIR.rglob("*.robot")
        )
        return (f"Error: {target} not found. Available:\n  "
                + "\n  ".join(available))

    robot = _VENV_BIN / "robot"
    if not robot.exists():
        return (f"Error: {robot} not found. Run "
                "`testing/.venv/bin/pip install -r testing/requirements.txt` first.")

    cmd = [str(robot), "--outputdir", str(_OUTPUT_DIR),
           "--consolecolors", "off"]
    if dryrun:
        cmd.append("--dryrun")
    if tags:
        cmd.extend(["--include", tags])
    cmd.append(str(target))

    env = os.environ.copy()
    extra = str(_TESTING_DIR)
    env["PYTHONPATH"] = (extra + os.pathsep + env.get("PYTHONPATH", "")).rstrip(os.pathsep)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env,
        )
    except subprocess.TimeoutExpired:
        return f"Error: scenario timed out after {timeout}s"

    return (
        f"Return code: {result.returncode}\n\n"
        f"STDOUT:\n{result.stdout}\n\n"
        f"STDERR:\n{result.stderr}"
    )


@mcp.tool()
def list_scenarios() -> str:
    """List available .robot scenarios under testing/rf_theia/scenarios/."""
    files = sorted(_SCENARIOS_DIR.rglob("*.robot"))
    out: list[str] = []
    for f in files:
        rel = f.relative_to(_SCENARIOS_DIR)
        doc = ""
        try:
            with f.open() as fh:
                for line in fh:
                    s = line.strip()
                    if s.startswith("Documentation"):
                        doc = s.replace("Documentation", "", 1).strip()
                        break
                    if s.startswith("*** Test Cases ***"):
                        break
        except OSError:
            pass
        out.append(f"  {rel}: {doc}")
    if not out:
        return "(no scenarios found)"
    return "Available scenarios:\n" + "\n".join(out)


@mcp.tool()
def list_keywords() -> str:
    """Enumerate keywords exposed by TheiaTestLibrary.

    Includes the prefix-grouped families (T Sup, T Sig, plus the
    direct TPT idioms inherited from rf-tpt-ls: Create Partition,
    Add Transition, etc).
    """
    sys.path.insert(0, str(_TESTING_DIR))
    from rf_theia.TheiaTestLibrary import TheiaTestLibrary
    lib = TheiaTestLibrary()
    keywords: list[str] = []
    for name in dir(lib):
        method = getattr(lib, name)
        if not callable(method):
            continue
        robot_name = getattr(method, "robot_name", None)
        if robot_name is None:
            continue
        doc = (method.__doc__ or "").strip().split("\n")[0]
        keywords.append(f"  {robot_name}: {doc}")
    return "rf-theia keywords:\n" + "\n".join(sorted(set(keywords)))


@mcp.tool()
def create_scenario(rel_path: str, content: str) -> str:
    """Write a new .robot scenario file.

    Args:
        rel_path: Path relative to testing/rf_theia/scenarios/, including
                  the .robot extension and any subdirectory (e.g.
                  ``signal_flow/my_repro.robot``).
        content:  Full file content.
    """
    target = _SCENARIOS_DIR / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return f"Wrote {target}"


@mcp.tool()
def get_test_results(output_dir: str = "") -> str:
    """Read pass/fail summary from the last Robot run.

    Args:
        output_dir: Override default ``/tmp/rf_theia_output``.
    """
    out = Path(output_dir or _OUTPUT_DIR) / "output.xml"
    if not out.exists():
        return f"No output.xml at {out}. Run a scenario first."
    import xml.etree.ElementTree as ET
    root = ET.parse(out).getroot()
    stats = root.find(".//statistics/total/stat")
    if stats is None:
        return "(could not parse statistics)"
    parts = [f"{k}={v}" for k, v in stats.attrib.items()]
    return "Last run: " + " ".join(parts)


@mcp.tool()
def analyze_trace(trace_path: str) -> str:
    """Summarize a trace log (text format from Tracer.hh).

    Returns event-kind counts, per-node event totals, and any RPC
    correlation pairs detected — handy for post-mortem of a failed
    scenario.
    """
    from rf_theia.adapters.tracer_jsonl import TraceFeed, TraceRecord
    p = Path(trace_path)
    if not p.exists():
        return f"trace file {p} not found"
    records: list[TraceRecord] = []
    with p.open() as fh:
        for line in fh:
            r = TraceRecord.parse(line)
            if r is not None:
                records.append(r)
    if not records:
        return f"trace file {p} contained no TRC v1 records"
    by_event: dict[str, int] = {}
    by_node: dict[str, int] = {}
    by_corr: dict[int, int] = {}
    for r in records:
        by_event[r.event] = by_event.get(r.event, 0) + 1
        by_node[r.node] = by_node.get(r.node, 0) + 1
        if r.corr_id != 0:
            by_corr[r.corr_id] = by_corr.get(r.corr_id, 0) + 1
    summary = {
        "file": str(p),
        "records": len(records),
        "by_event": dict(sorted(by_event.items())),
        "by_node": dict(sorted(by_node.items())),
        "correlations": len(by_corr),
        "first_ts_ms": records[0].ts_ms,
        "last_ts_ms": records[-1].ts_ms,
    }
    return json.dumps(summary, indent=2)


@mcp.tool()
def tail_supervisor_log(path: str, lines: int = 80) -> str:
    """Return the last N lines of a log file. Generic file tail —
    use it on the supervisor's stderr, FC daemon logs, or anything
    file-backed."""
    p = Path(path)
    if not p.exists():
        return f"{p} not found"
    try:
        with p.open() as fh:
            tail = fh.readlines()[-int(lines):]
        return "".join(tail)
    except OSError as e:
        return f"read error: {e}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
