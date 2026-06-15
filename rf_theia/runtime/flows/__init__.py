"""Built-in flow library.

Flows are Python classes; the engine instantiates them on demand.
This is intentional: we keep flows as code so they get type-checking,
IDE jump-to-definition, and step-debuggability. YAML-defined flows are
a future addition only if we ever ship enough of them to justify it.
"""

from .restart_child import RestartChild

__all__ = ["RestartChild"]
