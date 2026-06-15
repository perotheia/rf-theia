"""Built-in component library.

Probes are :class:`rf_theia.runtime.components.Component` subclasses.
They're concrete instances Robot can name via the ``Run Component``
keyword. Same shape as ``runtime/flows/`` — code, not data.
"""

from .loop_pair import Echo, Sink
from .sm_prober import SmProber
from .sm_stub import SmStub

__all__ = ["Echo", "Sink", "SmProber", "SmStub"]
