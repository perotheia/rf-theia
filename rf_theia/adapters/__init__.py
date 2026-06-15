"""Adapters: lazy-loaded modules that bridge the keyword library to live
theia subsystems (supervisor gRPC, Tracer.hh JSONL, artheia CLI, Puppet,
MCP server). Each adapter is independently importable so the keyword
library can fail-late for any subsystem the test host can't reach."""
