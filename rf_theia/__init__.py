"""rf-theia — Robot Framework testing harness for the theia/artheia stack.

Five DSL surfaces — `T Sup`, `T Sig`, `T Art`, `T Prov`, `T Orch` — share a
single ``TheiaTestLibrary``. The TPT engine, space (SPT) primitives, and
assessment helpers are vendored verbatim from ``up/rf_tpt_ls/`` and stay
domain-agnostic.

See ``docs/tasks/BACKLOG/testing_framework.md`` for the design rationale.
"""

__version__ = "0.1.0"
