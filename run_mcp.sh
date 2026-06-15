#!/usr/bin/env bash
# rf-theia MCP launcher. rf-theia is a STANDALONE harness (its own repo, mounted
# as the `rf-theia/` submodule); the .robot SCENARIOS it runs live in the
# CONSUMING repo at <repo>/testing/scenarios. This script bridges the two: it
# locates the consuming workspace (the dir the submodule sits in), exports where
# the scenarios + venv are, and execs the MCP server from the workspace .venv.
#
# Pointed at by the workspace-root .mcp.json so Claude Code discovers it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"   # rf-theia/ (submodule)
# The consuming workspace = the dir holding the rf-theia/ submodule.
WORKSPACE="${RF_THEIA_WORKSPACE:-$(cd "${SCRIPT_DIR}/.." && pwd)}"

VENV_PY="${WORKSPACE}/.venv/bin/python"
if [[ ! -x "${VENV_PY}" ]]; then
    echo "rf-theia: ${VENV_PY} missing — create the workspace venv and install" >&2
    echo "  the harness:  python3 -m venv .venv &&" >&2
    echo "    ./.venv/bin/pip install -e 'rf-theia/[mcp,dev]'" >&2
    exit 1
fi

# Scenarios live in the consuming repo (default <workspace>/testing/scenarios);
# override with RF_THEIA_SCENARIOS for a non-standard layout.
export RF_THEIA_WORKSPACE="${WORKSPACE}"
export RF_THEIA_SCENARIOS="${RF_THEIA_SCENARIOS:-${WORKSPACE}/testing/scenarios}"
# adapters also import tools.supdbg from the workspace root.
export PYTHONPATH="${WORKSPACE}:${PYTHONPATH:-}"

exec "${VENV_PY}" -m rf_theia.adapters.mcp_server "$@"
